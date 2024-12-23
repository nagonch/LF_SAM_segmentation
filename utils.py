import numpy as np
import imgviz
from PIL import Image
import yaml
import torch
from scipy.io import savemat
from plenpy.lightfields import LightField
import logging
from scipy import ndimage
from skimage.segmentation import mark_boundaries
import os
from plenpy.lightfields import LightField

logging.getLogger("plenpy").setLevel(logging.WARNING)


def masks_iou(predicted_masks, target_mask):
    target_mask = target_mask[None]
    intersection = (predicted_masks & target_mask).sum(dim=(1, 2))
    union = (predicted_masks | target_mask).sum(dim=(1, 2))
    ious = intersection / (union + 1e-9)
    return ious


def predict_mask_subview_position(mask, disparities, s, t):
    """
    Use mask's disparity to predict its position in (s, t)
    mask: torch.tensor [u, v] (torch.bool)
    disparity: float
    s, t: float
    returns: torch.tensor [u, v] (torch.bool)
    """
    st = torch.tensor([s, t]).float().cuda()
    uv_0 = torch.nonzero(mask)
    disparities_uv = disparities[mask].reshape(-1)
    uv = (uv_0 - disparities_uv.mean() * st).long()
    u = uv[:, 0]
    v = uv[:, 1]
    uv = uv[(u >= 0) & (v >= 0) & (u < mask.shape[0]) & (v < mask.shape[1])]
    mask_result = torch.zeros_like(mask)
    mask_result[uv[:, 0], uv[:, 1]] = 1
    return mask_result


def masks_to_segments(masks):
    """
    Convert [n, s, t, u, v] masks to [s, t, u v] segments
    The bigger the segment, the smaller the ID
    TODO: move to utils
    masks: torch.tensor [n, s, t, u, v] (torch.bool)
    returns: torch.tensor [s, t, u, v] (torch.long)
    """
    s, t, u, v = masks.shape[1:]
    areas = masks[:, s // 2, t // 2].cpu().sum(dim=(1, 2))
    masks_result = torch.zeros((s, t, u, v), dtype=torch.long).cuda()
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
    disp, _ = LF.get_disparity(vmin=-10, vmax=10)
    return disp


def visualize_segmentation_mask(
    segments,
    LF=None,
    just_return=False,
    filename=None,
    only_boundaries=False,
):
    s, t, u, v = segments.shape
    segments = np.transpose(segments, (0, 2, 1, 3)).reshape(s * u, t * v)
    if LF is not None:
        LF = np.transpose(LF, (0, 2, 1, 3, 4)).reshape(s * u, t * v, 3)
    if only_boundaries and LF is not None:
        boundaries = mark_boundaries(LF, segments)
        vis = np.transpose(boundaries.reshape(s, u, t, v, 3), (0, 2, 1, 3, 4))
    else:
        vis = np.transpose(
            imgviz.label2rgb(
                label=segments,
                image=LF,
                colormap=imgviz.label_colormap(segments.max() + 1),
            ).reshape(s, u, t, v, 3),
            (0, 2, 1, 3, 4),
        )
    if not just_return:
        if filename:
            savemat(
                filename,
                {"LF": vis},
            )
        segments = LightField(vis)
        segments.show()
    return vis


def get_mask_vis(mask):
    s, t, u, v = mask.shape
    mask = mask.permute(0, 2, 1, 3)
    mask = mask.reshape(s * u, t * v)
    return mask


def visualize_segments(segments, filename):
    s, t, u, v = segments.shape
    segments = np.transpose(segments, (0, 2, 1, 3)).reshape(s * u, t * v)
    vis = imgviz.label2rgb(
        label=segments,
        colormap=imgviz.label_colormap(segments.max() + 1),
    )
    im = Image.fromarray(vis)
    im.save(filename)


def remap_labels(labels):
    max_label = 0
    labels_remapped = torch.zeros(labels.shape).to(torch.int32).cuda()
    structure_4d = ndimage.generate_binary_structure(4, 4)
    for label in torch.unique(labels):
        img = (labels == label).to(torch.int32)
        img = torch.tensor(ndimage.label(img.cpu().numpy(), structure_4d)[0]).cuda()
        for unique_label in torch.unique(img)[1:]:
            if (img == unique_label).sum(axis=(2, 3)).float().mean() >= MERGER_CONFIG[
                "min-avg-labels-gt-merger"
            ]:
                labels_remapped[img == unique_label] = max_label + unique_label
        max_label = labels_remapped.max()
    return labels_remapped


def LF_lawnmower(LF):
    result_LF = []
    rows, cols, u, v, _ = LF.shape
    for i in range(rows):
        if i % 2 == 0:
            for j in range(cols):
                result_LF.append(LF[i, j])
        else:
            for j in range(cols - 1, -1, -1):
                result_LF.append(LF[i, j])
    result_LF = np.stack(result_LF)
    return result_LF


def lawnmower_indices(s, t, reverse=False):
    indices = []
    for i in range(s):
        if i % 2 == 0:
            for j in range(t):
                indices.append([i, j])
        else:
            for j in range(t - 1, -1, -1):
                indices.append([i, j])
    if reverse:
        indices = list(reversed(indices))
    return indices


def save_LF_lawnmower(LF, folder, prev_frame_last_subview=None, reverse=False):
    os.makedirs(folder, exist_ok=True)
    shape = LF.shape
    s, t = shape[:2]
    indices = lawnmower_indices(s, t, reverse)
    frame_n = 0
    if prev_frame_last_subview is not None:
        Image.fromarray(prev_frame_last_subview).save(
            f"{folder}/{str(frame_n).zfill(4)}.jpeg"
        )
        frame_n += 1
    for i, j in indices:
        Image.fromarray(LF[i, j]).save(f"{folder}/{str(frame_n).zfill(4)}.jpeg")
        frame_n += 1


if __name__ == "__main__":
    pass
