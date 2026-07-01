"""
Vectorize GUI - standalone raster-to-SVG vectorizer.

Install:
    python -m pip install pillow numpy opencv-python tkinterdnd2

Run:
    python vectorize.py

Outputs are written to the folder containing this script.

Changelog vs. the previous version:
    - Estimated time left is now computed from a smoothed rolling progress
      rate instead of a single elapsed/fraction ratio, so it no longer
      jumps around when the pipeline moves between stages of very
      different cost (see ETATracker).
    - Added a Cancel button that stops the active job between checkpoints
      (color clustering / speckle cleanup / path tracing) via a
      threading.Event, instead of only being able to wait it out.
    - Progress messages are a little more specific about which file/stage
      is active when cancelling.
"""

from __future__ import annotations

import math
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except Exception:
    TkinterDnD = None
    DND_FILES = None
    DND_AVAILABLE = False

try:
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
except Exception as exc:
    cv2 = None
    np = None
    Image = None
    ImageOps = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


APP_NAME = "Vectorize"
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

BG = "#000000"
PANEL_2 = "#0e0e0e"
LINE = "#292929"
LINE_2 = "#3a3a3a"
TEXT = "#f3f3f3"
MUTED = "#8a8a8a"
MUTED_2 = "#5f5f5f"
ACCENT = "#a9231f"
BUTTON = "#101010"
BUTTON_HOVER = "#171717"
WHITE_BUTTON = "#f4f4f4"
WHITE_BUTTON_TEXT = "#151515"

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def fmt_seconds(seconds: Optional[float]) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "--"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def open_folder(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        messagebox.showerror("Open folder failed", str(exc))


def safe_svg_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def unique_output_path(input_path: Path, suffix: str = ".svg") -> Path:
    base = OUTPUT_DIR / f"{input_path.stem}_vectorized{suffix}"
    if not base.exists():
        return base
    for i in range(2, 10000):
        candidate = OUTPUT_DIR / f"{input_path.stem}_vectorized_{i}{suffix}"
        if not candidate.exists():
            return candidate
    return OUTPUT_DIR / f"{input_path.stem}_vectorized_{int(time.time())}{suffix}"


def parse_drop_files(data: str, widget: tk.Misc) -> List[Path]:
    try:
        items = widget.tk.splitlist(data)
    except Exception:
        items = data.split()

    out: List[Path] = []
    for item in items:
        p = Path(item.strip())
        if p.suffix.lower() in SUPPORTED_EXTS and p.exists():
            out.append(p)
    return out


def rounded_rect(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs) -> int:
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=12, **kwargs)


class GhostButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        width: int = 86,
        height: int = 31,
        fill: str = BUTTON,
        hover: str = BUTTON_HOVER,
        fg: str = TEXT,
        border: str = LINE_2,
        radius: int = 8,
        font: Tuple[str, int] = ("Segoe UI", 9),
    ) -> None:
        super().__init__(master, width=width, height=height, bg=BG, bd=0, highlightthickness=0)
        self.text = text
        self.command = command
        self.width = width
        self.height = height
        self.fill = fill
        self.hover = hover
        self.fg = fg
        self.border = border
        self.radius = radius
        self.font = font
        self.enabled = True
        self.hovered = False

        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda _e: self.draw())
        self.draw()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.draw()

    def set_text(self, text: str) -> None:
        self.text = text
        self.draw()

    def _enter(self, _event: tk.Event) -> None:
        self.hovered = True
        self.draw()

    def _leave(self, _event: tk.Event) -> None:
        self.hovered = False
        self.draw()

    def _click(self, _event: tk.Event) -> None:
        if self.enabled:
            self.command()

    def draw(self) -> None:
        self.delete("all")
        w = max(1, self.winfo_width() or self.width)
        h = max(1, self.winfo_height() or self.height)

        fill = self.hover if self.hovered and self.enabled else self.fill
        fg = self.fg if self.enabled else MUTED_2
        border = self.border if self.enabled else "#202020"

        rounded_rect(self, 1, 1, w - 2, h - 2, self.radius, fill=fill, outline=border, width=1)
        self.create_text(w // 2, h // 2, text=self.text, fill=fg, font=self.font)


class MiniButton(GhostButton):
    def __init__(self, master: tk.Misc, text: str, command: Callable[[], None], width: int = 43, height: int = 21):
        super().__init__(master, text, command, width=width, height=height, radius=6, font=("Segoe UI", 8))


class CanvasSlider(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        minimum: int,
        maximum: int,
        value: int,
        command: Callable[[int], None],
        width: int = 200,
        height: int = 22,
    ) -> None:
        super().__init__(master, width=width, height=height, bg=BG, bd=0, highlightthickness=0)
        self.minimum = minimum
        self.maximum = maximum
        self.value = value
        self.command = command
        self.width = width
        self.height = height

        self.bind("<Button-1>", self._update_from_event)
        self.bind("<B1-Motion>", self._update_from_event)
        self.bind("<Configure>", lambda _e: self.draw())
        self.draw()

    def set(self, value: int, call: bool = True) -> None:
        value = int(round(clamp(value, self.minimum, self.maximum)))
        if value != self.value:
            self.value = value
            if call:
                self.command(value)
        self.draw()

    def fraction(self) -> float:
        return (self.value - self.minimum) / max(1, self.maximum - self.minimum)

    def _update_from_event(self, event: tk.Event) -> None:
        w = max(1, self.winfo_width() or self.width)
        pad = 4
        f = clamp((event.x - pad) / max(1, w - 2 * pad), 0.0, 1.0)
        self.set(int(round(self.minimum + f * (self.maximum - self.minimum))))

    def draw(self) -> None:
        self.delete("all")
        w = max(1, self.winfo_width() or self.width)
        h = max(1, self.winfo_height() or self.height)
        pad = 4
        y = h // 2

        self.create_line(pad, y, w - pad, y, fill=LINE, width=4)
        x = pad + self.fraction() * (w - 2 * pad)
        self.create_line(pad, y, x, y, fill=ACCENT, width=4)
        self.create_oval(x - 5, y - 5, x + 5, y + 5, fill=ACCENT, outline=ACCENT)


class ProgressCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc, width: int = 210, height: int = 9) -> None:
        super().__init__(master, width=width, height=height, bg=BG, bd=0, highlightthickness=0)
        self.value = 0.0
        self.bind("<Configure>", lambda _e: self.draw())
        self.draw()

    def set(self, value: float) -> None:
        self.value = clamp(value, 0.0, 1.0)
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        w = max(1, self.winfo_width())
        h = max(1, self.winfo_height())

        self.create_rectangle(0, h // 2 - 2, w, h // 2 + 2, fill=LINE, outline="")
        self.create_rectangle(0, h // 2 - 2, int(w * self.value), h // 2 + 2, fill=ACCENT, outline="")


class ToggleBox(tk.Canvas):
    def __init__(self, master: tk.Misc, text: str, checked: bool, command: Callable[[bool], None], width: int = 210) -> None:
        super().__init__(master, width=width, height=24, bg=BG, bd=0, highlightthickness=0)
        self.text = text
        self.checked = checked
        self.command = command
        self.width = width

        self.bind("<Button-1>", self._click)
        self.draw()

    def set(self, value: bool, call: bool = True) -> None:
        self.checked = bool(value)
        if call:
            self.command(self.checked)
        self.draw()

    def _click(self, _event: tk.Event) -> None:
        self.set(not self.checked)

    def draw(self) -> None:
        self.delete("all")
        size = 7
        x, y = 0, 9

        outline = ACCENT if self.checked else MUTED_2
        fill = ACCENT if self.checked else BG

        self.create_rectangle(x, y, x + size, y + size, outline=outline, fill=fill, width=1)
        self.create_text(13, y + size // 2, text=self.text, anchor="w", fill=TEXT, font=("Segoe UI", 8))


class SegmentedTabs(tk.Canvas):
    def __init__(self, master: tk.Misc, tabs: Sequence[str], command: Callable[[int], None], width: int = 180, height: int = 31) -> None:
        super().__init__(master, width=width, height=height, bg=BG, bd=0, highlightthickness=0)
        self.tabs = list(tabs)
        self.command = command
        self.index = 0
        self.width = width
        self.height = height

        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda _e: self.draw())
        self.draw()

    def _click(self, event: tk.Event) -> None:
        w = max(1, self.winfo_width() or self.width)
        idx = int(event.x / max(1, w / len(self.tabs)))
        idx = max(0, min(len(self.tabs) - 1, idx))
        self.index = idx
        self.command(idx)
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        w = max(1, self.winfo_width() or self.width)
        h = max(1, self.winfo_height() or self.height)

        rounded_rect(self, 1, 1, w - 2, h - 2, 6, fill=PANEL_2, outline=LINE_2, width=1)

        tab_w = w / len(self.tabs)
        for i, label in enumerate(self.tabs):
            x1 = int(i * tab_w)
            x2 = int((i + 1) * tab_w)
            selected = i == self.index

            if selected:
                rounded_rect(self, x1 + 1, 1, x2 - 1, h - 2, 6, fill=WHITE_BUTTON, outline=WHITE_BUTTON, width=1)

            self.create_text(
                (x1 + x2) // 2,
                h // 2,
                text=label,
                fill=WHITE_BUTTON_TEXT if selected else MUTED,
                font=("Segoe UI", 8),
            )


class FlatSelect(tk.Frame):
    def __init__(self, master: tk.Misc, values: Sequence[str], value: str, command: Callable[[str], None], width: int = 200) -> None:
        super().__init__(master, bg=BG)
        self.values = list(values)
        self.value = value
        self.command = command
        self.width = width

        self.canvas = tk.Canvas(self, width=width, height=24, bg=BG, bd=0, highlightthickness=0)
        self.canvas.pack(fill="x")
        self.canvas.bind("<Button-1>", self.open_menu)

        self.menu = tk.Menu(
            self,
            tearoff=False,
            bg=PANEL_2,
            fg=TEXT,
            activebackground=LINE,
            activeforeground=TEXT,
            bd=0,
        )

        for item in self.values:
            self.menu.add_command(label=item, command=lambda v=item: self.set(v))

        self.draw()

    def set(self, value: str) -> None:
        self.value = value
        self.command(value)
        self.draw()

    def open_menu(self, _event: tk.Event) -> None:
        try:
            self.menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.canvas.winfo_height())
        finally:
            self.menu.grab_release()

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w = self.width
        h = 24

        rounded_rect(c, 1, 1, w - 2, h - 2, 5, fill=PANEL_2, outline=LINE_2, width=1)
        c.create_text(10, h // 2, text=self.value, anchor="w", fill=TEXT, font=("Segoe UI", 8))
        c.create_text(w - 13, h // 2 - 1, text="⌄", fill=MUTED, font=("Segoe UI", 9))


@dataclass
class VectorizeSettings:
    detail: int = 7
    colors: int = 72
    seam_fix: bool = True
    preserve_transparency: bool = True
    background: Tuple[int, int, int] = (255, 255, 255)


ProgressCallback = Callable[[float, str], None]


class VectorizeCancelled(Exception):
    """Raised internally when a job's cancel event is set mid-run."""


class ETATracker:
    """Smoothed estimated-time-remaining tracker.

    A single elapsed/fraction estimate is noisy because the pipeline's
    stages cost very different amounts of wall-clock time per unit of
    progress (color clustering vs. speckle cleanup vs. path tracing), so
    the ETA used to jump every time a job crossed a stage boundary.

    This tracker keeps a short rolling window of (time, progress) samples,
    derives a rate estimate from that window, and smooths the rate with an
    exponential moving average so the displayed ETA changes gradually.
    """

    def __init__(self, window_seconds: float = 4.0, smoothing: float = 0.25) -> None:
        self.window_seconds = window_seconds
        self.smoothing = smoothing
        self.samples: List[Tuple[float, float]] = []
        self.smoothed_rate: Optional[float] = None

    def reset(self) -> None:
        self.samples.clear()
        self.smoothed_rate = None

    def update(self, progress: float) -> Optional[float]:
        now = time.perf_counter()
        self.samples.append((now, progress))

        cutoff = now - self.window_seconds
        self.samples = [s for s in self.samples if s[0] >= cutoff]

        if len(self.samples) < 2:
            return None

        t0, p0 = self.samples[0]
        dt = now - t0
        dp = progress - p0

        if dt <= 0 or dp <= 0:
            return None

        instant_rate = dp / dt

        if self.smoothed_rate is None:
            self.smoothed_rate = instant_rate
        else:
            self.smoothed_rate = (
                self.smoothing * instant_rate + (1 - self.smoothing) * self.smoothed_rate
            )

        if self.smoothed_rate <= 1e-9:
            return None

        remaining = max(0.0, 1.0 - progress)
        return remaining / self.smoothed_rate


def _check_dependencies() -> None:
    if IMPORT_ERROR is not None:
        raise RuntimeError(
            "Missing dependency. Install with:\n"
            "python -m pip install pillow numpy opencv-python tkinterdnd2\n\n"
            f"Original error: {IMPORT_ERROR}"
        )


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise VectorizeCancelled()


def _load_image_rgba(path: Path):
    assert Image is not None and ImageOps is not None and np is not None
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGBA")
    w, h = img.size
    arr = np.array(img, dtype=np.uint8)
    return arr, w, h


def _resize_for_work(arr_rgba, detail: int):
    assert cv2 is not None

    h, w = arr_rgba.shape[:2]
    max_side = int(360 + detail * 135)
    scale = min(1.0, max_side / max(w, h))

    if scale >= 0.999:
        return arr_rgba.copy()

    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(arr_rgba, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _composite_rgb(arr_rgba, bg: Tuple[int, int, int]):
    assert np is not None

    rgb = arr_rgba[:, :, :3].astype(np.float32)
    alpha = arr_rgba[:, :, 3:4].astype(np.float32) / 255.0
    bg_arr = np.array(bg, dtype=np.float32).reshape(1, 1, 3)

    out = rgb * alpha + bg_arr * (1.0 - alpha)
    return np.clip(out, 0, 255).astype(np.uint8)


def _edge_preserve_smooth(rgb, detail: int):
    assert cv2 is not None

    if detail >= 9:
        diameter, sigma_color, sigma_space = 3, 20, 20
    elif detail >= 6:
        diameter, sigma_color, sigma_space = 5, 30, 30
    else:
        diameter, sigma_color, sigma_space = 7, 45, 45

    return cv2.bilateralFilter(rgb, diameter, sigma_color, sigma_space)


def _kmeans_lab_quantize(rgb_smooth, original_rgb, k: int, detail: int, cancel_event: Optional[threading.Event] = None):
    assert cv2 is not None and np is not None

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


def _remove_tiny_components(
    label_map,
    colors,
    detail: int,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
):
    assert cv2 is not None and np is not None

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
    cancel_event: Optional[threading.Event] = None,
):
    assert cv2 is not None and np is not None

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


def vectorize_to_svg(
    path: Path,
    settings: VectorizeSettings,
    progress: ProgressCallback,
    cancel_event: Optional[threading.Event] = None,
) -> Path:
    _check_dependencies()
    assert cv2 is not None and np is not None

    t0 = time.perf_counter()

    _check_cancel(cancel_event)
    progress(0.01, "loading image")
    arr_rgba, original_w, original_h = _load_image_rgba(path)

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
    output_path = unique_output_path(path)

    stroke_width = "0.65" if settings.seam_fix else "0"
    shape_rendering = "geometricPrecision"

    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{original_w}" height="{original_h}" '
        f'viewBox="0 0 {work_w} {work_h}" shape-rendering="{shape_rendering}">'
    )
    lines.append(f"  <title>{safe_svg_text(path.name)} vectorized</title>")
    lines.append(f"  <metadata>Generated by Vectorize GUI in {time.perf_counter() - t0:.2f}s</metadata>")

    if not settings.preserve_transparency:
        bg = "#{:02x}{:02x}{:02x}".format(*settings.background)
        lines.append(f'  <rect width="100%" height="100%" fill="{bg}"/>')

    for _, _, fill, d in paths:
        if settings.seam_fix:
            lines.append(f'  <path d="{d}" fill="{fill}" stroke="{fill}" stroke-width="{stroke_width}" stroke-linejoin="round"/>')
        else:
            lines.append(f'  <path d="{d}" fill="{fill}"/>')

    lines.append("</svg>")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    progress(1.0, "done")

    return output_path


class VectorizeApp:
    def __init__(self) -> None:
        root_cls = TkinterDnD.Tk if DND_AVAILABLE and TkinterDnD is not None else tk.Tk

        self.root = root_cls()
        self.root.title(APP_NAME)
        self.root.geometry("600x420")
        self.root.minsize(600, 420)
        self.root.configure(bg=BG)

        self.files: List[Path] = []
        self.settings = VectorizeSettings()
        self.bus: queue.Queue[tuple] = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.running = False
        self.start_time: Optional[float] = None
        self.last_output: Optional[Path] = None
        self.progress_value = 0.0
        self.eta_tracker = ETATracker()
        self.cancel_event: Optional[threading.Event] = None
        self.cancelling = False

        self.container = tk.Frame(self.root, bg=BG)
        self.container.pack(fill="both", expand=True)

        self.show_home()
        self.setup_drop_target()

        self.root.after(80, self.poll_bus)

    def clear(self) -> None:
        for child in self.container.winfo_children():
            child.destroy()

    def setup_drop_target(self) -> None:
        if not DND_AVAILABLE:
            return

        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self.on_drop)
        except Exception:
            pass

    def on_drop(self, event: tk.Event) -> None:
        files = parse_drop_files(getattr(event, "data", ""), self.root)

        if files:
            self.set_files(files)
            self.show_config()

    def show_home(self) -> None:
        self.clear()

        outer = tk.Frame(self.container, bg=BG)
        outer.pack(fill="both", expand=True)

        title = tk.Label(outer, text="vectorize", bg=BG, fg=TEXT, font=("Times New Roman", 25), bd=0)
        title.place(relx=0.5, rely=0.23, anchor="center")

        open_btn = GhostButton(outer, "Open files", self.pick_files, width=88, height=31)
        open_btn.place(relx=0.5, rely=0.50, anchor="center")

        drop_hint = tk.Label(outer, text="or drop them anywhere", bg=BG, fg=TEXT, font=("Segoe UI", 9), bd=0)
        drop_hint.place(relx=0.5, rely=0.60, anchor="center")

        if not DND_AVAILABLE:
            small = tk.Label(outer, text="install tkinterdnd2 for drag + drop", bg=BG, fg=MUTED_2, font=("Segoe UI", 8), bd=0)
            small.place(relx=0.5, rely=0.68, anchor="center")

        config_btn = GhostButton(outer, "Config", self.show_config, width=68, height=31)
        config_btn.place(relx=0.5, rely=0.90, anchor="center")

    def show_config(self) -> None:
        self.clear()

        self.config_frame = tk.Frame(self.container, bg=BG)
        self.config_frame.pack(fill="both", expand=True)

        self.left = tk.Frame(self.config_frame, bg=BG, width=235)
        self.left.pack(side="left", fill="y", padx=(13, 8), pady=(32, 5))
        self.left.pack_propagate(False)

        divider = tk.Frame(self.config_frame, bg=LINE_2, width=2)
        divider.pack(side="left", fill="y", pady=(58, 280))

        self.right = tk.Frame(self.config_frame, bg=BG)
        self.right.pack(side="left", fill="both", expand=True, padx=(28, 0), pady=(58, 0))

        self.build_left_panel()
        self.build_right_panel()
        self.build_bottom_buttons()
        self.refresh_file_text()
        self.refresh_settings_labels()

    def build_left_panel(self) -> None:
        header = tk.Frame(self.left, bg=BG)
        header.pack(fill="x", pady=(0, 10))

        bullet = tk.Canvas(header, width=9, height=13, bg=BG, bd=0, highlightthickness=0)
        bullet.pack(side="left")
        bullet.create_rectangle(0, 4, 6, 10, fill=ACCENT, outline=ACCENT)

        tk.Label(header, text="vectorize", bg=BG, fg=TEXT, font=("Segoe UI", 8)).pack(side="left", padx=(2, 0))

        row = tk.Frame(self.left, bg=BG)
        row.pack(fill="x", pady=(0, 2))

        self.detail_label = tk.Label(row, text="detail: 7", bg=BG, fg=TEXT, font=("Segoe UI", 8))
        self.detail_label.pack(side="left")

        MiniButton(row, "max", lambda: self.detail_slider.set(10), width=43, height=21).pack(side="right", padx=(0, 22))

        self.detail_slider = CanvasSlider(self.left, 1, 10, self.settings.detail, self.set_detail, width=205)
        self.detail_slider.pack(anchor="w", pady=(0, 8))

        self.colors_label = tk.Label(self.left, text="color accuracy: 72 colors", bg=BG, fg=TEXT, font=("Segoe UI", 8))
        self.colors_label.pack(anchor="w")

        self.colors_slider = CanvasSlider(self.left, 8, 192, self.settings.colors, self.set_colors, width=205)
        self.colors_slider.pack(anchor="w", pady=(0, 10))

        tk.Label(self.left, text="vector weighting", bg=BG, fg=TEXT, font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 6))

        self.mode_select = FlatSelect(
            self.left,
            values=["balanced", "high_detail", "smooth_regions"],
            value="balanced",
            command=self.set_mode,
            width=200,
        )
        self.mode_select.pack(anchor="w", pady=(0, 14))

        self.seam_toggle = ToggleBox(self.left, "anti-gap overlap", self.settings.seam_fix, self.set_seam)
        self.seam_toggle.pack(anchor="w", pady=(0, 2))

        self.trans_toggle = ToggleBox(self.left, "preserve transparency", self.settings.preserve_transparency, self.set_transparency)
        self.trans_toggle.pack(anchor="w", pady=(0, 12))

        tk.Label(self.left, text="output folder", bg=BG, fg=TEXT, font=("Segoe UI", 8)).pack(anchor="w")

        output = str(OUTPUT_DIR)
        if len(output) > 34:
            output = "..." + output[-31:]

        self.output_path = tk.Label(
            self.left,
            text=output,
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=210,
            justify="left",
        )
        self.output_path.pack(anchor="w", pady=(2, 12))

        self.status_left = tk.Label(self.left, text="ready", bg=BG, fg=MUTED_2, font=("Segoe UI", 8))
        self.status_left.pack(anchor="w", pady=(4, 0))

    def build_right_panel(self) -> None:
        self.tabs = SegmentedTabs(self.right, ["output svg", "settings"], self.switch_tab, width=180, height=31)
        self.tabs.pack(anchor="n", pady=(2, 70))

        self.file_text = tk.Label(self.right, text="No files selected", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.file_text.pack(anchor="center", pady=(0, 14))

        self.progress_bar = ProgressCanvas(self.right, width=210, height=10)
        self.progress_bar.pack(anchor="center", pady=(0, 7))

        self.progress_label = tk.Label(self.right, text="Idle", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.progress_label.pack(anchor="center")

        self.eta_label = tk.Label(self.right, text="estimated time left: --", bg=BG, fg=MUTED_2, font=("Segoe UI", 8))
        self.eta_label.pack(anchor="center", pady=(4, 30))

        buttons = tk.Frame(self.right, bg=BG)
        buttons.pack(anchor="center")

        self.start_button = GhostButton(buttons, "Vectorize", self.start_vectorize, width=75, height=31)
        self.start_button.pack(side="left", padx=(0, 12))

        self.cancel_button = GhostButton(buttons, "Cancel", self.cancel_vectorize, width=65, height=31)
        self.cancel_button.pack(side="left", padx=(0, 12))
        self.cancel_button.set_enabled(False)

        GhostButton(buttons, "Import", self.pick_files, width=65, height=31).pack(side="left")

        GhostButton(self.right, "Open output folder", lambda: open_folder(OUTPUT_DIR), width=138, height=31).pack(anchor="center", pady=(8, 0))

    def build_bottom_buttons(self) -> None:
        bottom = tk.Frame(self.config_frame, bg=BG)
        bottom.place(relx=0.50, rely=0.91, anchor="center")

        GhostButton(bottom, "Back", self.show_home, width=55, height=31).pack(side="left", padx=(0, 13))
        GhostButton(bottom, "Restore defaults", self.restore_defaults, width=122, height=31).pack(side="left")

    def switch_tab(self, index: int) -> None:
        if index == 1:
            self.file_text.configure(text=f"Script output: {OUTPUT_DIR}")
        else:
            self.refresh_file_text()

    def set_detail(self, value: int) -> None:
        self.settings.detail = value
        self.refresh_settings_labels()

    def set_colors(self, value: int) -> None:
        self.settings.colors = value
        self.refresh_settings_labels()

    def set_seam(self, value: bool) -> None:
        self.settings.seam_fix = value

    def set_transparency(self, value: bool) -> None:
        self.settings.preserve_transparency = value

    def set_mode(self, value: str) -> None:
        if value == "high_detail":
            self.detail_slider.set(9)
            self.colors_slider.set(128)
        elif value == "smooth_regions":
            self.detail_slider.set(5)
            self.colors_slider.set(48)
        else:
            self.detail_slider.set(7)
            self.colors_slider.set(72)

    def refresh_settings_labels(self) -> None:
        if hasattr(self, "detail_label"):
            self.detail_label.configure(text=f"detail: {self.settings.detail}")
        if hasattr(self, "colors_label"):
            self.colors_label.configure(text=f"color accuracy: {self.settings.colors} colors")

    def refresh_file_text(self) -> None:
        if not hasattr(self, "file_text"):
            return

        if not self.files:
            self.file_text.configure(text="No files selected")
        elif len(self.files) == 1:
            self.file_text.configure(text=self.files[0].name)
        else:
            self.file_text.configure(text=f"{len(self.files)} files selected")

    def pick_files(self) -> None:
        names = filedialog.askopenfilenames(
            title="Select images",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
                ("All files", "*.*"),
            ],
        )

        files = [Path(n) for n in names if Path(n).suffix.lower() in SUPPORTED_EXTS]

        if files:
            self.set_files(files)
            self.show_config()

    def set_files(self, files: Sequence[Path]) -> None:
        seen = set()
        clean: List[Path] = []

        for p in files:
            try:
                rp = p.resolve()
            except Exception:
                rp = p

            if rp not in seen and rp.suffix.lower() in SUPPORTED_EXTS:
                seen.add(rp)
                clean.append(rp)

        self.files = clean

    def restore_defaults(self) -> None:
        self.settings = VectorizeSettings()

        if hasattr(self, "detail_slider"):
            self.detail_slider.set(self.settings.detail, call=False)
            self.colors_slider.set(self.settings.colors, call=False)
            self.seam_toggle.set(self.settings.seam_fix, call=False)
            self.trans_toggle.set(self.settings.preserve_transparency, call=False)
            self.mode_select.set("balanced")

        self.refresh_settings_labels()

    def start_vectorize(self) -> None:
        if self.running:
            return

        if IMPORT_ERROR is not None:
            messagebox.showerror(
                "Missing dependency",
                "Install dependencies first:\n\n"
                "python -m pip install pillow numpy opencv-python tkinterdnd2\n\n"
                f"Error: {IMPORT_ERROR}",
            )
            return

        if not self.files:
            self.pick_files()
            if not self.files:
                return

        self.running = True
        self.cancelling = False
        self.cancel_event = threading.Event()
        self.start_time = time.perf_counter()
        self.progress_value = 0.0
        self.last_output = None
        self.eta_tracker.reset()

        self.set_progress(0.0, "starting")

        if hasattr(self, "start_button"):
            self.start_button.set_enabled(False)
        if hasattr(self, "cancel_button"):
            self.cancel_button.set_enabled(True)
            self.cancel_button.set_text("Cancel")

        files = list(self.files)

        settings = VectorizeSettings(
            detail=self.settings.detail,
            colors=self.settings.colors,
            seam_fix=self.settings.seam_fix,
            preserve_transparency=self.settings.preserve_transparency,
            background=self.settings.background,
        )

        self.worker = threading.Thread(
            target=self.worker_run, args=(files, settings, self.cancel_event), daemon=True
        )
        self.worker.start()

    def cancel_vectorize(self) -> None:
        if not self.running or self.cancel_event is None or self.cancelling:
            return

        self.cancelling = True
        self.cancel_event.set()

        if hasattr(self, "cancel_button"):
            self.cancel_button.set_text("Cancelling…")
            self.cancel_button.set_enabled(False)

        if hasattr(self, "status_left"):
            self.status_left.configure(text="cancelling…")

    def worker_run(self, files: List[Path], settings: VectorizeSettings, cancel_event: threading.Event) -> None:
        outputs: List[Path] = []

        try:
            total = max(1, len(files))

            for index, path in enumerate(files):
                base = index / total
                span = 1.0 / total

                def cb(local_p: float, stage: str, idx: int = index, pth: Path = path) -> None:
                    global_p = base + clamp(local_p, 0.0, 1.0) * span
                    prefix = f"{idx + 1}/{total} {pth.name}: " if total > 1 else ""
                    self.bus.put(("progress", global_p, prefix + stage))

                out = vectorize_to_svg(path, settings, cb, cancel_event=cancel_event)
                outputs.append(out)
                self.bus.put(("file_done", out))

            self.bus.put(("done", outputs))

        except VectorizeCancelled:
            self.bus.put(("cancelled", outputs))

        except Exception as exc:
            self.bus.put(("error", str(exc)))

    def poll_bus(self) -> None:
        try:
            while True:
                item = self.bus.get_nowait()
                kind = item[0]

                if kind == "progress":
                    self.set_progress(float(item[1]), str(item[2]))

                elif kind == "file_done":
                    self.last_output = item[1]

                elif kind == "done":
                    self.running = False
                    self.cancelling = False
                    outputs: List[Path] = item[1]
                    self.set_progress(1.0, f"done - {len(outputs)} svg file(s) saved")

                    if hasattr(self, "start_button"):
                        self.start_button.set_enabled(True)
                    if hasattr(self, "cancel_button"):
                        self.cancel_button.set_enabled(False)
                        self.cancel_button.set_text("Cancel")

                    if outputs:
                        self.status_left.configure(text=f"saved: {outputs[-1].name}")

                elif kind == "cancelled":
                    self.running = False
                    self.cancelling = False
                    outputs = item[1]
                    self.set_progress(self.progress_value, "cancelled")

                    if hasattr(self, "start_button"):
                        self.start_button.set_enabled(True)
                    if hasattr(self, "cancel_button"):
                        self.cancel_button.set_enabled(False)
                        self.cancel_button.set_text("Cancel")

                    done_note = f" ({len(outputs)} file(s) saved before cancelling)" if outputs else ""
                    self.status_left.configure(text=f"cancelled{done_note}")

                elif kind == "error":
                    self.running = False
                    self.cancelling = False

                    if hasattr(self, "start_button"):
                        self.start_button.set_enabled(True)
                    if hasattr(self, "cancel_button"):
                        self.cancel_button.set_enabled(False)
                        self.cancel_button.set_text("Cancel")

                    self.set_progress(self.progress_value, "error")
                    messagebox.showerror("Vectorize failed", item[1])

        except queue.Empty:
            pass

        self.root.after(80, self.poll_bus)

    def set_progress(self, value: float, stage: str) -> None:
        self.progress_value = clamp(value, 0.0, 1.0)

        if hasattr(self, "progress_bar"):
            self.progress_bar.set(self.progress_value)

        percent = int(round(self.progress_value * 100))

        if hasattr(self, "progress_label"):
            self.progress_label.configure(text=f"{stage}  {percent}%")

        if hasattr(self, "status_left") and not self.cancelling:
            self.status_left.configure(text=stage[:34])

        if hasattr(self, "eta_label"):
            eta: Optional[float] = None

            if self.running:
                eta = self.eta_tracker.update(self.progress_value)
            elif self.progress_value >= 1.0:
                eta = 0

            self.eta_label.configure(text=f"estimated time left: {fmt_seconds(eta)}")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    VectorizeApp().run()
