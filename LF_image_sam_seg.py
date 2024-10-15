from sam2_functions import get_auto_mask_predictor, generate_image_masks
from data import HCIOldDataset
import warnings
from utils import visualize_segmentation_mask
import torch
from torchvision.transforms.functional import resize
import numpy as np
import yaml
import os
import torch.nn.functional as F
import matplotlib.pyplot as plt
from plenpy.lightfields import LightField

warnings.filterwarnings("ignore")

with open("LF_sam_image_seg.yaml") as f:
    CONFIG = yaml.load(f, Loader=yaml.FullLoader)


def masks_to_segments(masks):
    """
    Convert [N, S, T, U, V] masks to [S, T, U, V] segments
    The bigger the segment, the smaller the ID
    TODO: move to utils
    """
    areas = masks.cpu().sum(dim=(3, 4)).float().mean(dim=(1, 2)).cuda()
    s, t, u, v = masks.shape[1:]
    masks_result = torch.zeros((s, t, u, v)).long().cuda()
    for i, mask_i in enumerate(torch.argsort(areas, descending=True)):
        masks_result[masks[mask_i]] = i  # smaller segments on top of bigger ones
    return masks_result


def get_LF_disparities(LF):
    """
    Get disparities for subview [s//2, t//2]
    LF: np.array [s, t, u, v, 3] (np.uint8)
    returns: np.array [u, v] (np.float32)
    """
    LF = LightField(LF)
    disp, _ = LF.get_disparity()
    return np.nan_to_num(disp)


def get_mask_disparities(masks_central, disparities):
    """
    Get mean disparity of each mask
    masks_central: torch.tensor [n, u, v] (torch.bool)
    disparities: np.array [u, v] (np.float32)
    returns: torch.tensor [n] (torch.float32)
    """
    mask_disparities = torch.zeros((masks_central.shape[0],)).cuda()
    for i, mask_i in enumerate(masks_central):
        mask_disparities[i] = disparities[mask_i].mean().item()
    return mask_disparities


def predict_mask_subview_position(mask, disparity, s, t):
    """
    Use mask's disparity to predict its position in (s, t)
    mask: torch.tensor [u, v] (torch.bool)
    disparity: float
    s, t: float
    returns: torch.tensor [u, v] (torch.bool)
    """
    st = F.normalize(torch.tensor([s, t]).cuda()[None].float())[0]
    uv_0 = torch.nonzero(mask)
    uv = (uv_0 - disparity * st).long()
    u = uv[:, 0]
    v = uv[:, 1]
    uv = uv[(u >= 0) & (v >= 0) & (u <= mask.shape[0]) & (v <= mask.shape[1])]
    mask_result = torch.zeros_like(mask)
    mask_result[uv[:, 0], uv[:, 1]] = 1
    return mask_result


def get_coarse_matching(LF, masks_central, mask_disparities):
    """
    Predict subview masks using disparities
    LF: np.array [s, t, u, v, 3] (np.uint8)
    masks_central: torch.tensor [u, v] (torch.bool)
    mask_disparities: torch.tensor [n] (torch.float32)
    returns: torch.tensor [n, s, t, u, v] (torch.bool)
    """
    s_size, t_size, u_size, v_size = LF.shape[:4]
    result = (
        torch.zeros((masks_central.shape[0], s_size, t_size, u_size, v_size))
        .cuda()
        .bool()
    )
    for i, (mask, disparity) in enumerate(zip(masks_central, mask_disparities)):
        for s in range(s_size):
            for t in range(t_size):
                result[i][s][t] = predict_mask_subview_position(
                    mask, disparity, s - s_size // 2, t - t_size // 2
                )
    return result


def get_fine_matching(LF, image_predictor, coarse_segments):
    """
    Predict subview masks using disparities
    LF: np.array [s, t, u, v, 3] (np.uint8)
    image_predictor: SAM2ImagePredictor
    coarse_segments: torch.tensor [n, s, t, u, v] (torch.bool)
    returns: torch.tensor [n, s, t, u, v] (torch.bool)
    """
    fine_masks = torch.zeros_like(coarse_segments)
    s_size, t_size = LF.shape[:2]
    for s in range(s_size):
        for t in range(t_size):
            if s == s_size // 2 and t == t_size // 2:
                fine_masks[:, s, t] = coarse_segments[:, s, t]
                continue
            coarse_segments_st = coarse_segments[:, s, t]
            image_predictor.set_image(LF[s, t])
            fine_segments_st = []
            for segment in coarse_segments_st:
                points = (
                    torch.nonzero(segment).float().mean(dim=0).flip(0)[None]
                )  # TODO: consider more points
                labels = np.ones((points.shape[0]))
                fine_segment_result, iou_preds, _ = image_predictor.predict(
                    point_coords=points,
                    point_labels=labels,
                    multimask_output=True,
                )
                fine_segment_result = torch.tensor(fine_segment_result).cuda()
                result_segment = fine_segment_result[np.argmax(iou_preds)]
                fine_segments_st.append(result_segment)
            fine_masks[:, s, t] = torch.stack(fine_segments_st, dim=0)
    return fine_masks


def LF_image_sam_seg(mask_predictor, LF):
    s_central, t_central = LF.shape[0] // 2, LF.shape[1] // 2
    masks_central = generate_image_masks(mask_predictor, LF[s_central, t_central])
    disparities = torch.tensor(get_LF_disparities(LF)).cuda()
    mask_disparities = get_mask_disparities(masks_central, disparities)
    coarse_matched_masks = get_coarse_matching(LF, masks_central, mask_disparities)
    image_predictor = mask_predictor.predictor
    fine_matched_masks = get_fine_matching(LF, image_predictor, coarse_matched_masks)
    result_segments = masks_to_segments(fine_matched_masks)
    visualize_segmentation_mask(result_segments.cpu().numpy(), LF)
    return result_segments


if __name__ == "__main__":
    os.makedirs(CONFIG["files-folder"], exist_ok=True)
    mask_predictor = get_auto_mask_predictor()
    image_predictor = mask_predictor.predictor
    dataset = HCIOldDataset()
    for i, (LF, _, _) in enumerate(dataset):
        segments = LF_image_sam_seg(
            mask_predictor,
            LF,
        )
        torch.save(segments, f"segments/{str(i).zfill(4)}.pt")
        del segments
