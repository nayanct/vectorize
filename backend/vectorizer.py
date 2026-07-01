"""
vectorizer.py - raster-to-SVG vectorization engine.

This is the same color-quantization + contour-tracing pipeline used by the
desktop app, refactored to:
  - operate on in-memory image bytes instead of files on disk
  - report progress through a callback instead of a Tk queue
  - support cooperative cancellation via a threading.Event
  - return the finished SVG as a string instead of writing it to disk

No GUI toolkit is imported here. This module is safe to run in a plain
Python process (e.g. inside a FastAPI worker thread).
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps


# --------------------------------------------------------------------------
# Settings / errors
# --------------------------------------------------------------------------

@dataclass
class VectorizeSettings:
    detail: int = 7
    colors: int = 72
    seam_fix: bool = True
    preserve_transparency: bool = True
    background: Tuple[int, int, int] = (255, 255, 255)


class VectorizeCancelled(Exception):
    """Raised internally when a job's cancel event is set mid-run."""


ProgressCallback = Callable[[float, str], None]
CancelToken = "threading.Event"  # typing hint only, avoids importing threading here


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise VectorizeCancelled()


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_svg_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------
# Image loading / prep
# --------------------------------------------------------------------------

def _load_image_rgba_from_bytes(data: bytes):
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGBA")
    w, h = img.size
    arr = np.array(img, dtype=np.uint8)
    return arr, w, h


def _resize_for_work(arr_rgba, detail: int):
    h, w = arr_rgba.shape[:2]
    max_side = int(360 + detail * 135)
    scale = min(1.0, max_side / max(w, h))

    if scale >= 0.999:
        return arr_rgba.copy()

    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(arr_rgba, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _composite_rgb(arr_rgba, bg: Tuple[int, int, int]):
    rgb = arr_rgba[:, :, :3].astype(np.float32)
    alpha = arr_rgba[:, :, 3:4].astype(np.float32) / 255.0
    bg_arr = np.array(bg, dtype=np.float32).reshape(1, 1, 3)

    out = rgb * alpha + bg_arr * (1.0 - alpha)
    return np.clip(out, 0, 255).astype(np.uint8)


def _edge_preserve_smooth(rgb, detail: int):
    if detail >= 9:
        diameter, sigma_color, sigma_space = 3, 20, 20
    elif detail >= 6:
        diameter, sigma_color, sigma_space = 5, 30, 30
    else:
        diameter, sigma_color, sigma_space = 7, 45, 45

    return cv2.bilateralFilter(rgb, diameter, sigma_color, sigma_space)


# --------------------------------------------------------------------------
# Color quantization
# --------------------------------------------------------------------------

def _kmeans_lab_quantize(rgb_smooth, original_rgb, k: int, detail: int, cancel_event=None):
    h, w = rgb_smooth.shape[:2]
    k = int(clamp(k, 2, min(256, h * w)))

    lab = cv2.cvtColor(rgb_smooth, cv2.COLOR_RGB2LAB)
    flat = lab.reshape(-1, 3).astype(np.float32)

    rng = np.random.default_rng(1234)
    max_samples = int(45000 + detail * 12000)

    if flat.shape[0] > max_samples:
        sample_idx = rng.choice(flat.shape[0], size=max_samples, replace=False)
        sample = flat[sample_idx]
    else:
        sample = flat

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 24, 0.35)
    attempts = 2 if detail <= 8 else 3

    _check_cancel(cancel_event)
    _, _, centers = cv2.kmeans(sample, k, None, criteria, attempts, cv2.KMEANS_PP_CENTERS)

    labels = np.empty(flat.shape[0], dtype=np.int32)
    centers_f = centers.astype(np.float32)
    chunk = 250000

    for start in range(0, flat.shape[0], chunk):
        _check_cancel(cancel_event)
        end = min(start + chunk, flat.shape[0])
        block = flat[start:end]
        diff = block[:, None, :] - centers_f[None, :, :]
        dist = np.sum(diff * diff, axis=2)
        labels[start:end] = np.argmin(dist, axis=1)

    label_map = labels.reshape(h, w).astype(np.int32)

    colors = np.zeros((k, 3), dtype=np.uint8)
    flat_original = original_rgb.reshape(-1, 3)
    flat_labels = label_map.reshape(-1)

    for i in range(k):
        if i % 16 == 0:
            _check_cancel(cancel_event)

        members = flat_original[flat_labels == i]

        if members.size == 0:
            center_lab = np.uint8([[centers[i]]])
            rgb_center = cv2.cvtColor(center_lab, cv2.COLOR_LAB2RGB)[0, 0]
            colors[i] = rgb_center
        else:
            if len(members) > 50000:
                idx = rng.choice(len(members), size=50000, replace=False)
                members = members[idx]
            colors[i] = np.median(members, axis=0).astype(np.uint8)

    return label_map, colors


def _remove_tiny_components(label_map, colors, detail: int, progress: Optional[ProgressCallback] = None, cancel_event=None):
    h, w = label_map.shape
    total = h * w
    min_area = max(3, int(total * (0.00012 / max(1, detail))))

    if detail >= 9:
        min_area = max(2, min_area // 2)

    labels_unique = np.unique(label_map)
    cleaned = label_map.copy()
    kernel = np.ones((3, 3), dtype=np.uint8)

    for idx, lab in enumerate(labels_unique):
        if idx % 8 == 0:
            _check_cancel(cancel_event)
            if progress:
                progress(idx / max(1, len(labels_unique)), "cleaning small regions")

        mask = (cleaned == lab).astype(np.uint8)
        count, cc, stats, _ = cv2.connectedComponentsWithStats(mask, 8)

        for component_id in range(1, count):
            area = int(stats[component_id, cv2.CC_STAT_AREA])

            if area >= min_area:
                continue

            component_mask = cc == component_id
            dilated = cv2.dilate(component_mask.astype(np.uint8), kernel, iterations=1).astype(bool)
            border = dilated & ~component_mask

            neighbor_labels = cleaned[border]
            neighbor_labels = neighbor_labels[neighbor_labels != lab]

            if neighbor_labels.size:
                replacement = int(np.bincount(neighbor_labels.astype(np.int32)).argmax())
                cleaned[component_mask] = replacement

    return cleaned


def _path_from_contour(contour) -> str:
    pts = contour.reshape(-1, 2)

    if len(pts) < 3:
        return ""

    commands = [f"M {int(pts[0][0])} {int(pts[0][1])}"]
    commands.extend(f"L {int(x)} {int(y)}" for x, y in pts[1:])
    commands.append("Z")

    return " ".join(commands)


def _trace_svg_paths(
    label_map,
    colors,
    detail: int,
    seam_fix: bool,
    progress: Optional[ProgressCallback] = None,
    valid_mask=None,
    cancel_event=None,
):
    unique_labels, counts = np.unique(label_map, return_counts=True)
    label_counts = dict(zip(unique_labels.tolist(), counts.tolist()))
    ordered_labels = sorted(unique_labels.tolist(), key=lambda x: label_counts.get(int(x), 0), reverse=True)

    kernel = np.ones((3, 3), dtype=np.uint8)
    eps_ratio = float(np.interp(detail, [1, 10], [0.010, 0.0012]))
    min_contour_area = max(1.0, (11 - detail) * 0.55)
    dilation_iters = 1 if seam_fix else 0

    paths = []
    total_labels = max(1, len(ordered_labels))

    for idx, lab in enumerate(ordered_labels):
        if idx % 4 == 0:
            _check_cancel(cancel_event)
            if progress:
                progress(idx / total_labels, "tracing vector paths")

        mask_bool = label_map == lab

        if valid_mask is not None:
            mask_bool = mask_bool & valid_mask

        mask = mask_bool.astype(np.uint8) * 255

        if dilation_iters:
            mask = cv2.dilate(mask, kernel, iterations=dilation_iters)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        fill = "#{:02x}{:02x}{:02x}".format(int(colors[lab][0]), int(colors[lab][1]), int(colors[lab][2]))

        for contour in contours:
            area = cv2.contourArea(contour)

            if area < min_contour_area:
                continue

            arc = cv2.arcLength(contour, True)
            epsilon = max(0.35, eps_ratio * arc)
            approx = cv2.approxPolyDP(contour, epsilon, True)

            d = _path_from_contour(approx)

            if d:
                paths.append((int(area), int(lab), fill, d))

    paths.sort(key=lambda row: row[0], reverse=True)
    return paths


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def vectorize_to_svg_string(
    image_bytes: bytes,
    settings: VectorizeSettings,
    progress: ProgressCallback,
    cancel_event=None,
    source_name: str = "image",
) -> str:
    """Run the full pipeline and return the finished SVG as a string.

    `progress(fraction, stage)` is called throughout with fraction in [0, 1].
    `cancel_event`, if given, is checked periodically; VectorizeCancelled is
    raised as soon as it is observed set.
    """

    t0 = time.perf_counter()

    _check_cancel(cancel_event)
    progress(0.01, "loading image")
    arr_rgba, original_w, original_h = _load_image_rgba_from_bytes(image_bytes)

    _check_cancel(cancel_event)
    progress(0.06, "resizing")
    work_rgba = _resize_for_work(arr_rgba, settings.detail)
    work_h, work_w = work_rgba.shape[:2]

    _check_cancel(cancel_event)
    progress(0.12, "preparing color data")
    work_rgb = _composite_rgb(work_rgba, settings.background)

    valid_mask = None
    if settings.preserve_transparency and work_rgba.shape[2] == 4:
        valid_mask = work_rgba[:, :, 3] >= 8

    smooth_rgb = _edge_preserve_smooth(work_rgb, settings.detail)

    computed_colors = int(np.interp(settings.detail, [1, 10], [16, 160]))
    color_count = int(clamp(round((settings.colors * 0.65) + (computed_colors * 0.35)), 4, 256))

    _check_cancel(cancel_event)
    progress(0.20, f"clustering colors ({color_count})")
    label_map, colors = _kmeans_lab_quantize(
        smooth_rgb, work_rgb, color_count, settings.detail, cancel_event=cancel_event
    )

    progress(0.55, "removing speckles")
    label_map = _remove_tiny_components(
        label_map,
        colors,
        settings.detail,
        progress=lambda p, s: progress(0.55 + 0.10 * p, s),
        cancel_event=cancel_event,
    )

    progress(0.67, "building paths")
    paths = _trace_svg_paths(
        label_map,
        colors,
        settings.detail,
        settings.seam_fix,
        progress=lambda p, s: progress(0.67 + 0.25 * p, s),
        valid_mask=valid_mask,
        cancel_event=cancel_event,
    )

    _check_cancel(cancel_event)
    progress(0.94, "writing svg")

    stroke_width = "0.65" if settings.seam_fix else "0"
    shape_rendering = "geometricPrecision"

    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{original_w}" height="{original_h}" '
        f'viewBox="0 0 {work_w} {work_h}" shape-rendering="{shape_rendering}">'
    )
    lines.append(f"  <title>{safe_svg_text(source_name)} vectorized</title>")
    lines.append(f"  <metadata>Generated by vectorize in {time.perf_counter() - t0:.2f}s</metadata>")

    if not settings.preserve_transparency:
        bg = "#{:02x}{:02x}{:02x}".format(*settings.background)
        lines.append(f'  <rect width="100%" height="100%" fill="{bg}"/>')

    for _, _, fill, d in paths:
        if settings.seam_fix:
            lines.append(f'  <path d="{d}" fill="{fill}" stroke="{fill}" stroke-width="{stroke_width}" stroke-linejoin="round"/>')
        else:
            lines.append(f'  <path d="{d}" fill="{fill}"/>')

    lines.append("</svg>")

    progress(1.0, "done")
    return "\n".join(lines)
