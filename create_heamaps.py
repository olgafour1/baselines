from __future__ import print_function
import numpy as np
from clam.utils.file_utils import save_hdf5
import sys
import argparse
import shutil
import torch
import torch.nn as nn
import pdb
import os
import pandas as pd
from utils.utils import *
import yaml
from clam.wsi_core.batch_process_utils import initialize_df
from clam.models.resnet_custom import resnet50_baseline
from clam.vis_utils.heatmap_utils import initialize_wsi, drawHeatmap, compute_from_patches
from models.TransMIL import TransMIL
import h5py

parser = argparse.ArgumentParser(description='Heatmap inference script')
parser.add_argument('--save_exp_code', type=str, default=None,
                    help='experiment code')
parser.add_argument('--overlap', type=float, default=None)
parser.add_argument('--config_file', type=str,
                    default="config_template_transmil.yaml")
args = parser.parse_args()



def infer_single_slide_dtfd(data, model):
    results_dict = model(data)


    return results_dict


# def infer_single_slide(model, features, label, reverse_label_dict, k=1):
# 	features = features.to(device)
# 	with torch.no_grad():
# 		if isinstance(model, (CLAM_SB, CLAM_MB)):
# 			model_results_dict = model(features)
# 			logits, Y_prob, Y_hat, A, _ = model(features)
# 			Y_hat = Y_hat.item()
#
# 			if isinstance(model, (CLAM_MB,)):
# 				A = A[Y_hat]
#
# 			A = A.view(-1, 1).cpu().numpy()
#
# 		else:
# 			raise NotImplementedError
#
# 		print('Y_hat: {}, Y: {}, Y_prob: {}'.format(reverse_label_dict[Y_hat], label, ["{:.4f}".format(p) for p in Y_prob.cpu().flatten()]))
#
# 		probs, ids = torch.topk(Y_prob, k)
# 		probs = probs[-1].cpu().numpy()
# 		ids = ids[-1].cpu().numpy()
# 		preds_str = np.array([reverse_label_dict[idx] for idx in ids])
#
# 	return ids, preds_str, probs, A


def load_params(df_entry, params):
    for key in params.keys():
        if key in df_entry.index:
            dtype = type(params[key])
            val = df_entry[key]
            val = dtype(val)
            if isinstance(val, str):
                if len(val) > 0:
                    params[key] = val
            elif not np.isnan(val):
                params[key] = val
            else:
                pdb.set_trace()

    return params


def parse_config_dict(args, config_dict):
    if args.save_exp_code is not None:
        config_dict['exp_arguments']['save_exp_code'] = args.save_exp_code
    if args.overlap is not None:
        config_dict['patching_arguments']['overlap'] = args.overlap
    return config_dict


if __name__ == '__main__':
    config_path = os.path.join(args.config_file)
    config_dict = yaml.safe_load(open(config_path, 'r'))
    config_dict = parse_config_dict(args, config_dict)

    for key, value in config_dict.items():
        if isinstance(value, dict):
            print('\n' + key)
            for value_key, value_value in value.items():
                print(value_key + " : " + str(value_value))
        else:
            print('\n' + key + " : " + str(value))

    decision = input('Continue? Y/N ')
    if decision in ['Y', 'y', 'Yes', 'yes']:
        pass
    elif decision in ['N', 'n', 'No', 'NO']:
        exit()
    else:
        raise NotImplementedError

    args = config_dict
    patch_args = argparse.Namespace(**args['patching_arguments'])
    data_args = argparse.Namespace(**args['data_arguments'])
    model_args = args['model_arguments']
    model_args.update({'n_classes': args['exp_arguments']['n_classes']})
    model_args = argparse.Namespace(**model_args)
    exp_args = argparse.Namespace(**args['exp_arguments'])
    heatmap_args = argparse.Namespace(**args['heatmap_arguments'])
    sample_args = argparse.Namespace(**args['sample_arguments'])

    patch_size = tuple([patch_args.patch_size for i in range(2)])
    step_size = tuple((np.array(patch_size) * (1 - patch_args.overlap)).astype(int))
    print('patch_size: {} x {}, with {:.2f} overlap, step size is {} x {}'.format(patch_size[0], patch_size[1],
                                                                                  patch_args.overlap, step_size[0],
                                                                                  step_size[1]))

    preset = data_args.preset
    def_seg_params = {'seg_level': -1, 'sthresh': 15, 'mthresh': 11, 'close': 2, 'use_otsu': False,
                      'keep_ids': 'none', 'exclude_ids': 'none'}
    def_filter_params = {'a_t': 50.0, 'a_h': 8.0, 'max_n_holes': 10}
    def_vis_params = {'vis_level': -1, 'line_thickness': 250}
    def_patch_params = {'use_padding': True, 'contour_fn': 'four_pt'}

    if preset is not None:
        preset_df = pd.read_csv(preset)
        for key in def_seg_params.keys():
            def_seg_params[key] = preset_df.loc[0, key]

        for key in def_filter_params.keys():
            def_filter_params[key] = preset_df.loc[0, key]

        for key in def_vis_params.keys():
            def_vis_params[key] = preset_df.loc[0, key]

        for key in def_patch_params.keys():
            def_patch_params[key] = preset_df.loc[0, key]

    if data_args.process_list is None:
        if isinstance(data_args.data_dir, list):
            slides = []
            for data_dir in data_args.data_dir:
                slides.extend(os.listdir(data_dir))
        else:
            slides = sorted(os.listdir(data_args.data_dir))
        slides = [slide for slide in slides if data_args.slide_ext in slide]
        df = initialize_df(slides, def_seg_params, def_filter_params, def_vis_params, def_patch_params,
                           use_heatmap_args=False)

    else:
        df = pd.read_csv(os.path.join('heatmaps_dtfd/process_lists', data_args.process_list))
        df = initialize_df(df, def_seg_params, def_filter_params, def_vis_params, def_patch_params,
                           use_heatmap_args=False)

    mask = df['process'] == 1
    process_stack = df[mask].reset_index(drop=True)
    total = len(process_stack)
    print('\nlist of slides to process: ')
    print(process_stack.head(len(process_stack)))

    print('\ninitializing model from checkpoint')
    ckpt_path = model_args.ckpt_path
    print('\nckpt path: {}'.format(ckpt_path))

    model=TransMIL(n_classes=2).cuda()

    feature_extractor = resnet50_baseline(pretrained=True)
    feature_extractor.eval()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print('Done!')

    label_dict = data_args.label_dict
    class_labels = list(label_dict.keys())
    class_encodings = list(label_dict.values())
    reverse_label_dict = {class_encodings[i]: class_labels[i] for i in range(len(class_labels))}

    if torch.cuda.device_count() > 1:
        device_ids = list(range(torch.cuda.device_count()))
        feature_extractor = nn.DataParallel(feature_extractor, device_ids=device_ids).to('cuda:0')
    else:
        feature_extractor = feature_extractor.to(device)

    os.makedirs(exp_args.production_save_dir, exist_ok=True)
    os.makedirs(exp_args.raw_save_dir, exist_ok=True)
    blocky_wsi_kwargs = {'top_left': None, 'bot_right': None, 'patch_size': patch_size, 'step_size': patch_size,
                         'custom_downsample': patch_args.custom_downsample, 'level': patch_args.patch_level,
                         'use_center_shift': heatmap_args.use_center_shift}

    for i in range(len(process_stack)):
        slide_name = process_stack.loc[i, 'slide_id']
        if data_args.slide_ext not in slide_name:
            slide_name += data_args.slide_ext
        print('\nprocessing: ', slide_name)

        try:
            label = process_stack.loc[i, 'label']
        except KeyError:
            label = 'Unspecified'

        slide_id = slide_name.replace(data_args.slide_ext, '')

        if not isinstance(label, str):
            grouping = reverse_label_dict[label]
        else:
            grouping = label

        p_slide_save_dir = os.path.join(exp_args.production_save_dir, exp_args.save_exp_code, str(grouping))
        os.makedirs(p_slide_save_dir, exist_ok=True)

        r_slide_save_dir = os.path.join(exp_args.raw_save_dir, exp_args.save_exp_code, str(grouping), slide_id)
        os.makedirs(r_slide_save_dir, exist_ok=True)

        if heatmap_args.use_roi:
            x1, x2 = process_stack.loc[i, 'x1'], process_stack.loc[i, 'x2']
            y1, y2 = process_stack.loc[i, 'y1'], process_stack.loc[i, 'y2']
            top_left = (int(x1), int(y1))
            bot_right = (int(x2), int(y2))
        else:
            top_left = None
            bot_right = None

        print('slide id: ', slide_id)
        print('top left: ', top_left, ' bot right: ', bot_right)

        if isinstance(data_args.data_dir, str):
            slide_path = os.path.join(data_args.data_dir, slide_name)
        elif isinstance(data_args.data_dir, dict):
            data_dir_key = process_stack.loc[i, data_args.data_dir_key]
            slide_path = os.path.join(data_args.data_dir[data_dir_key], slide_name)
        else:
            raise NotImplementedError

        mask_file = os.path.join(r_slide_save_dir, slide_id + '_mask.pkl')

        # Load segmentation and filter parameters
        seg_params = def_seg_params.copy()
        filter_params = def_filter_params.copy()
        vis_params = def_vis_params.copy()

        seg_params = load_params(process_stack.loc[i], seg_params)
        filter_params = load_params(process_stack.loc[i], filter_params)
        vis_params = load_params(process_stack.loc[i], vis_params)

        keep_ids = str(seg_params['keep_ids'])
        if len(keep_ids) > 0 and keep_ids != 'none':
            seg_params['keep_ids'] = np.array(keep_ids.split(',')).astype(int)
        else:
            seg_params['keep_ids'] = []

        exclude_ids = str(seg_params['exclude_ids'])
        if len(exclude_ids) > 0 and exclude_ids != 'none':
            seg_params['exclude_ids'] = np.array(exclude_ids.split(',')).astype(int)
        else:
            seg_params['exclude_ids'] = []

        for key, val in seg_params.items():
            print('{}: {}'.format(key, val))

        for key, val in filter_params.items():
            print('{}: {}'.format(key, val))

        for key, val in vis_params.items():
            print('{}: {}'.format(key, val))

        print('Initializing WSI object')
        wsi_object = initialize_wsi(slide_path, seg_mask_path=mask_file, seg_params=seg_params,
                                    filter_params=filter_params)
        print('Done!')

        wsi_ref_downsample = wsi_object.level_downsamples[patch_args.patch_level]

        # the actual patch size for heatmap visualization should be the patch size * downsample factor * custom downsample factor
        vis_patch_size = tuple(
            (np.array(patch_size) * np.array(wsi_ref_downsample) * patch_args.custom_downsample).astype(int))

        block_map_save_path = os.path.join(r_slide_save_dir, '{}_blockmap.h5'.format(slide_id))
        mask_path = os.path.join(r_slide_save_dir, '{}_mask.jpg'.format(slide_id))
        if vis_params['vis_level'] < 0:
            best_level = wsi_object.wsi.get_best_level_for_downsample(32)
            vis_params['vis_level'] = best_level
        mask = wsi_object.visWSI(**vis_params, number_contours=True)
        mask.save(mask_path)

        features_path = os.path.join(r_slide_save_dir, slide_id + '.pt')
        h5_path = os.path.join(r_slide_save_dir, slide_id + '.h5')
        pt_origin_path="/run/user/1001/gvfs/smb-share:server=rds.icr.ac.uk,share=data/DBI/DUDBI/DYNCESYS/OlgaF/multi_magnification_project/camelyon_data/feats_256/pt_files/"
        h5_origin_path = "/run/user/1001/gvfs/smb-share:server=rds.icr.ac.uk,share=data/DBI/DUDBI/DYNCESYS/OlgaF/multi_magnification_project/camelyon_data/feats_256/h5_files/"
        origin_pt_dir = os.path.join(pt_origin_path, slide_id + '.pt')
        origin_h5_dir = os.path.join(h5_origin_path, slide_id + '.h5')

        ##### check if h5_features_file exists ######
        if not os.path.isfile(h5_path):
            _, _, wsi_object = compute_from_patches(wsi_object=wsi_object,
                                                    feature_extractor=feature_extractor,
                                                    batch_size=exp_args.batch_size, **blocky_wsi_kwargs,
                                                    attn_save_path=None, feat_save_path=h5_path,
                                                    ref_scores=None)

        ##### check if pt_features_file exists ######
        if not os.path.isfile(features_path):
            file = h5py.File(h5_path, "r")
            features = torch.tensor(file['features'][:])
            torch.save(features, features_path)
            file.close()

        # if not os.path.isfile(features_path):
        #     try:
        #         shutil.copy(origin_h5_dir, h5_path)
        #         shutil.copy(origin_pt_dir, features_path)
        #         print("File copied successfully.")
        #     except shutil.SameFileError:
        #         print("Source and destination represents the same file.")

        # load features
        features = torch.load(features_path)
        process_stack.loc[i, 'bag_size'] = len(features)

        mDataList = features.unsqueeze(0)


        wsi_object.saveSegmentation(mask_file)
        model.eval()
        results_dict = model(data=mDataList.cuda())

        #Y_hats, Y_hats_str, Y_probs, A = infer_single_slide_dtfd(data=mDataList, model=model)


        del features

        if not os.path.isfile(block_map_save_path):
            file = h5py.File(h5_path, "r")
            coords = file['coords'][:]
            file.close()
            asset_dict = {'attention_scores': results_dict['A'].cpu().detach().numpy(), 'coords': coords}
            block_map_save_path = save_hdf5(block_map_save_path, asset_dict, mode='w')


        file = h5py.File(block_map_save_path, 'r')
        dset = file['attention_scores']
        coord_dset = file['coords']
        scores = dset[:]
        coords = coord_dset[:]
        file.close()


        wsi_kwargs = {'top_left': top_left, 'bot_right': bot_right, 'patch_size': patch_size, 'step_size': step_size,
                      'custom_downsample': patch_args.custom_downsample, 'level': patch_args.patch_level,
                      'use_center_shift': heatmap_args.use_center_shift}

        heatmap_save_name = '{}_blockmap.tiff'.format(slide_id)
        if os.path.isfile(os.path.join(r_slide_save_dir, heatmap_save_name)):
            pass
        else:
            print(scores.shape)
            heatmap = drawHeatmap(scores,
                                  coords,
                                  slide_path,
                                  wsi_object=wsi_object,
                                  cmap=heatmap_args.cmap,
                                  alpha=heatmap_args.alpha,
                                  use_holes=True,
                                  binarize=False,
                                  vis_level=-1,
                                  blur=True,
                                  blank_canvas=False,
                                  thresh=-1,
                                  patch_size=vis_patch_size,
                                  convert_to_percentiles=True)

            heatmap.save(os.path.join(r_slide_save_dir, '{}_blockmap.png'.format(slide_id)))
            del heatmap

        save_path = os.path.join(r_slide_save_dir,
                                 '{}_{}_roi_{}.h5'.format(slide_id, patch_args.overlap, heatmap_args.use_roi))

