from sam2_functions import (
    get_auto_mask_predictor,
    generate_image_masks,
)
from data import HCIOldDataset, UrbanLFSynDataset
import warnings
from utils import (
    visualize_segmentation_mask,
    masks_iou,
    masks_to_segments,
    predict_mask_subview_position,
)
from time import time
import torch
import yaml
import os
import matplotlib.pyplot as plt
import numpy as np
from utils import get_LF_disparities

warnings.filterwarnings("ignore")

with open("ours.yaml") as f:
    CONFIG = yaml.load(f, Loader=yaml.FullLoader)


def get_mask_disparities(masks_central, disparities):
    """
    Get mean disparity of each mask
    masks_central: torch.tensor [n, u, v] (torch.bool)
    disparities: np.array [u, v] (np.float32)
    returns: torch.tensor [n] (torch.float32)
    """
    mask_disparities = torch.zeros((masks_central.shape[0],)).cuda()
    for i, mask_i in enumerate(masks_central):
        disparities_i = disparities[mask_i]
        disparities_i = disparities_i[~torch.any(disparities_i.isnan())]
        mask_disparities[i] = torch.median(disparities[mask_i]).item()
    return mask_disparities


def get_coarse_matching(LF, masks_central, mask_disparities, disparities):
    """
    Predict subview masks using disparities
    LF: np.array [s, t, u, v, 3] (np.uint8)
    masks_central: torch.tensor [u, v] (torch.bool)
    mask_disparities: torch.tensor [n] (torch.float32)
    returns: torch.tensor [n, s, t, u, v] (torch.bool)
    """
    s_size, t_size, u_size, v_size = LF.shape[:4]
    result = torch.zeros(
        (masks_central.shape[0], s_size, t_size, u_size, v_size), dtype=torch.bool
    ).cuda()
    for s in range(s_size):
        for t in range(t_size):
            for i, (mask, disparity) in enumerate(zip(masks_central, mask_disparities)):
                result[i][s][t] = predict_mask_subview_position(
                    mask, disparities, s - s_size // 2, t - t_size // 2
                )
            result_st = torch.cumsum(result[:, s, t], dim=0)
            result[:, s, t] = torch.where(
                result_st == 1, result[:, s, t], torch.zeros_like(result[:, s, t])
            )  # deal with occlusion
    return result


def get_prompts_for_masks(coarse_masks):
    """
    Calculate prompts from coarse masks
    coarse_masks: torch.tensor [n, s, t, u, v] (torch.bool)
    returns: torch.tensor [n, s, t, 2] (torch.float),
             torch.tensor [n, s, t, 4] (torch.float)
    """
    n, s_size, t_size = coarse_masks.shape[:3]
    point_prompts = torch.zeros((n, s_size, t_size, 2), dtype=torch.float).cuda()
    box_prompts = torch.zeros((n, s_size, t_size, 4), dtype=torch.float).cuda()
    for s in range(s_size):
        for t in range(t_size):
            if s == s_size // 2 and t == t_size // 2:
                continue
            for mask_i, mask in enumerate(coarse_masks[:, s, t]):
                point_prompts_i = torch.nonzero(mask).flip(1)
                if point_prompts_i.shape[0] == 0:
                    continue
                box_pormpts_i = torch.tensor(
                    [
                        point_prompts_i[:, 0].min(),
                        point_prompts_i[:, 1].min(),
                        point_prompts_i[:, 0].max(),
                        point_prompts_i[:, 1].max(),
                    ]
                ).cuda()
                point_prompts_i_centroid = point_prompts_i.float().mean(axis=0)
                distances = torch.norm(
                    point_prompts_i - point_prompts_i_centroid, dim=1
                )
                point_prompts_i = point_prompts_i[torch.argmin(distances), :][None]
                point_prompts[mask_i, s, t] = point_prompts_i
                box_prompts[mask_i, s, t] = box_pormpts_i
    return point_prompts, box_prompts


def get_refined_matching(LF, image_predictor, coarse_masks, point_prompts, box_prompts):
    """
    Predict subview masks using disparities
    LF: np.array [s, t, u, v, 3] (np.uint8)
    image_predictor: SAM2ImagePredictor
    coarse_masks: torch.tensor [n, s, t, u, v] (torch.bool)
    returns: torch.tensor [n, s, t, u, v] (torch.bool)
    """
    s_size, t_size = LF.shape[:2]
    n = coarse_masks.shape[0]
    mask_ious = torch.zeros((n, s_size, t_size), dtype=torch.float)
    for s in range(s_size):
        for t in range(t_size):
            if s == s_size // 2 and t == t_size // 2:
                continue
            coarse_masks_st = torch.clone(coarse_masks[:, s, t, :, :])
            image_predictor.set_image(LF[s, t])
            point_prompts_st = point_prompts[:, s, t]
            box_prompts_st = box_prompts[:, s, t]
            for segment_i, (point_prompts_i, box_prompts_i) in enumerate(
                zip(point_prompts_st, box_prompts_st)
            ):
                point_prompts_i = point_prompts_i[None]
                if point_prompts_i.sum() <= 1e-6:
                    continue
                labels = torch.ones(point_prompts_i.shape[0])
                fine_segment_result, _, _ = image_predictor.predict(
                    point_coords=point_prompts_i,
                    point_labels=labels,
                    box=box_prompts_i,
                    multimask_output=True,
                )
                fine_segment_result = torch.tensor(
                    fine_segment_result, dtype=torch.bool
                ).cuda()
                ious = masks_iou(fine_segment_result, coarse_masks_st[segment_i])
                if ious.max() > 0.5:
                    match_idx = torch.argmax(ious)
                    mask_ious[segment_i, s, t] = ious[match_idx]
                    coarse_masks[segment_i, s, t] = fine_segment_result[
                        match_idx
                    ]  # replacing coarse masks with fine ones
    return coarse_masks, mask_ious


def refine_image_sam(
    LF, image_predictor, coarse_matched_masks, point_prompts, box_prompts
):
    print("get_fine_matching...", end="")
    refined_matched_masks, mask_ious = get_refined_matching(
        LF, image_predictor, coarse_matched_masks, point_prompts, box_prompts
    )
    return refined_matched_masks, mask_ious


def sam_fast_LF_segmentation(mask_predictor, LF, visualize=False):
    s_central, t_central = LF.shape[0] // 2, LF.shape[1] // 2

    print("generate_image_masks...", end="")
    masks_central = generate_image_masks(mask_predictor, LF[s_central, t_central])
    print(f"done, shape: {masks_central.shape}")

    print("get_LF_disparities...", end="")
    disparities = torch.tensor(get_LF_disparities(LF)).cuda()
    print(f"done, shape: {disparities.shape}")

    print("get_mask_disparities...", end="")
    mask_disparities = get_mask_disparities(masks_central, disparities)

    mask_depth_order = torch.argsort(mask_disparities)
    masks_central = masks_central[mask_depth_order]
    mask_disparities = mask_disparities[mask_depth_order]
    del mask_depth_order
    print(f"done, shape: {mask_disparities.shape}")
    print("get_coarse_matching...", end="")
    coarse_matched_masks = get_coarse_matching(
        LF, masks_central, mask_disparities, disparities
    )
    del disparities
    if visualize:
        print("visualizing coarse segments...")
        coarse_matched_segments = masks_to_segments(coarse_matched_masks)
        visualize_segmentation_mask(coarse_matched_segments.cpu().numpy(), LF)
    print(f"done, shape: {coarse_matched_masks.shape}")
    del mask_disparities
    del masks_central
    point_prompts, box_prompts = get_prompts_for_masks(coarse_matched_masks)
    refined_matched_masks, mask_ious = refine_image_sam(
        LF,
        mask_predictor.predictor,
        coarse_matched_masks,
        point_prompts,
        box_prompts,
    )
    del mask_predictor
    del coarse_matched_masks
    print(f"done, shape: {refined_matched_masks.shape}, mean_iou: {mask_ious.mean()}")
    if visualize:
        print("visualizing segments...")
        refined_segments = masks_to_segments(refined_matched_masks)
        visualize_segmentation_mask(refined_segments.cpu().numpy(), LF)
    return refined_matched_masks


def sam_fast_LF_segmentation_dataset(
    dataset,
    save_folder,
    continue_progress=False,
    visualize=False,
):
    mask_predictor = get_auto_mask_predictor()
    time_path = f"{save_folder}/computation_times.pt"
    computation_times = []
    if continue_progress and os.path.exists(time_path):
        computation_times = torch.load(time_path).tolist()
    for i, (LF, _, _) in enumerate(dataset):
        masks_path = f"{save_folder}/{str(i).zfill(4)}_masks.pt"
        segments_path = f"{save_folder}/{str(i).zfill(4)}_segments.pt"
        if (
            all([os.path.exists(path) for path in [masks_path, segments_path]])
            and continue_progress
        ):
            continue
        print(f"segmenting lf {i}")
        start_time = time()
        result_masks = sam_fast_LF_segmentation(
            mask_predictor,
            LF,
            visualize=visualize,
        )
        end_time = time()
        computation_times.append(end_time - start_time)
        result_segments = masks_to_segments(result_masks)
        torch.save(result_masks, masks_path)
        torch.save(result_segments, segments_path)
        del result_masks
        del result_segments
        torch.save(
            torch.tensor(computation_times),
            time_path,
        )


if __name__ == "__main__":
    dataset = UrbanLFSynDataset(
        "/home/nagonch/repos/LF_object_tracking/UrbanLF_Syn/val"
    )
    sam_fast_LF_segmentation_dataset(dataset, "test_result", visualize=True)
