"""Tk desktop UI for folder-based, one-to-four-image ROI inspection."""

from __future__ import annotations

import logging
import math
import queue
import re
import tkinter as tk
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

from ..branding import application_icon_path, show_about_dialog, show_workbench_help
from ..ui_foundation import (
    FONT_BODY,
    FONT_BODY_BOLD,
    FONT_CARD_TITLE,
    FONT_MONO,
    FONT_SMALL,
    FONT_TITLE,
    configure_action_styles,
    configure_typography,
    fit_window_to_screen,
)
from .browser import THUMBNAIL_PREFETCH_ROWS, ImageFolderError, discover_images, load_thumbnail, selected_paths_in_folder_order
from .cache import ImageDataCache
from .constants import MATCH_SEARCH_RANGES, MIN_ROI_SIDE, MOTION_THROTTLE_MS, RENDER_THROTTLE_MS
from .settings import ImageInspectorSettings, load_image_inspector_settings, save_image_inspector_settings
from .types import ComparisonResult, ImageData, MatchResult, PixelMetrics, ROI, ROIStatistics


LOGGER = logging.getLogger(__name__)
CORE_DEPENDENCY_ERROR: Optional[ImportError] = None
try:
    import numpy as np
    from PIL import Image, ImageTk

    from .export import export_multi_csv
    from .matching import MatchingError, confirm_match, manual_match, match_roi, opencv_available
    from .model import ImageInspectorError, analyse_roi, compare_statistics, load_image, pixel_metrics
except ImportError as exc:  # Keep the main TuneLab app importable without image extras.
    CORE_DEPENDENCY_ERROR = exc
    np = None  # type: ignore[assignment]
    Image = ImageTk = None  # type: ignore[assignment]


WINDOW_TITLE = "TuneLab - 图像分析器"
BG = "#F3F5F8"
PANEL = "#FFFFFF"
INK = "#172033"
MUTED = "#667085"
BLUE = "#2563EB"
GREEN = "#0F9D75"
AMBER = "#B54708"
RED = "#D92D20"
BORDER = "#DDE3EC"
CANVAS_BG = "#111827"
IMAGE_ROLES = ("before", "after", "compare3", "compare4")
COMPARISON_ROLES = IMAGE_ROLES[1:]
THUMBNAIL_CACHE_ITEMS = 128


def _role_label(role: str) -> str:
    try:
        index = IMAGE_ROLES.index(role)
    except ValueError:
        return role
    return "参考图 1" if index == 0 else f"对比图 {index + 1}"


def _ratio(value: Optional[float]) -> str:
    return "N/A（分母为 0）" if value is None else f"{value:.4f}"


def _triplet(values: Tuple[float, float, float], digits: int = 2) -> str:
    return " / ".join(f"{value:.{digits}f}" for value in values)


def _percent_triplet(values: Tuple[float, float, float]) -> str:
    return " / ".join(f"{value * 100.0:.2f}%" for value in values)


class ImageCanvas(ttk.Frame):
    """Viewport-tiled image renderer with exact original-coordinate mapping."""

    def __init__(
        self,
        master: tk.Misc,
        role: str,
        *,
        pixel_callback: Callable[[str, int, int, bool], None],
        roi_callback: Callable[[str, ROI], None],
        live_enabled: Callable[[], bool],
    ) -> None:
        super().__init__(master, style="InspectorCard.TFrame")
        self.role = role
        self.pixel_callback = pixel_callback
        self.roi_callback = roi_callback
        self.live_enabled = live_enabled
        self.image_data: Optional[ImageData] = None
        self._pil_image: Optional[Any] = None
        self._photo: Optional[Any] = None
        self._photo_key: Optional[Tuple[Any, ...]] = None
        self._render_after_id: Optional[str] = None
        self._motion_after_id: Optional[str] = None
        self._motion_position: Optional[Tuple[int, int]] = None
        self._needs_initial_fit = False
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.roi: Optional[ROI] = None
        self.roi_colour = "#22C55E"
        self.sample_point: Optional[Tuple[int, int]] = None
        self._selection_image_start: Optional[Tuple[float, float]] = None
        self._selection_canvas_start: Optional[Tuple[float, float]] = None
        self._pan_start: Optional[Tuple[float, float, float, float]] = None

        header = ttk.Frame(self, padding=(9, 5), style="InspectorCard.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.title_var = tk.StringVar(value=_role_label(role))
        self.meta_var = tk.StringVar(value="尚未打开图片")
        self.zoom_var = tk.StringVar(value="缩放 —")
        ttk.Label(header, textvariable=self.title_var, style="InspectorCardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.zoom_var, style="InspectorCardTitle.TLabel").grid(row=0, column=1, sticky="e")
        ttk.Label(header, textvariable=self.meta_var, style="InspectorMutedCard.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )

        self.canvas = tk.Canvas(self, background=CANVAS_BG, highlightthickness=0, cursor="crosshair")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.horizontal = ttk.Scrollbar(self, orient="horizontal", command=self._xview)
        self.horizontal.grid(row=2, column=0, sticky="ew")
        self.vertical = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.vertical.grid(row=1, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<ButtonPress-1>", self._on_left_press)
        self.canvas.bind("<B1-Motion>", self._on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_release)
        self.canvas.bind("<Shift-ButtonPress-1>", self._on_pan_press)
        self.canvas.bind("<Shift-B1-Motion>", self._on_pan_drag)
        self.canvas.bind("<Shift-ButtonRelease-1>", self._on_pan_release)
        for button in (2, 3):
            self.canvas.bind(f"<ButtonPress-{button}>", self._on_pan_press)
            self.canvas.bind(f"<B{button}-Motion>", self._on_pan_drag)
            self.canvas.bind(f"<ButtonRelease-{button}>", self._on_pan_release)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda event: self._zoom_at(event.x, event.y, 1.15))
        self.canvas.bind("<Button-5>", lambda event: self._zoom_at(event.x, event.y, 1.0 / 1.15))
        self.canvas.bind("<Destroy>", self._on_destroy)

    def set_title(self, title: str) -> None:
        self.title_var.set(title)

    def set_image(self, image_data: ImageData) -> None:
        self.image_data = image_data
        assert Image is not None
        self._pil_image = Image.fromarray(image_data.display_rgb, mode="RGB")
        self._photo_key = None
        precision = "保留原始精度" if image_data.precision_preserved else "解码后为 8-bit"
        exif = " · EXIF 已转正" if image_data.orientation_applied else ""
        self.meta_var.set(
            f"{image_data.width}×{image_data.height} · {image_data.bit_depth}-bit · {image_data.source_mode} · {precision}{exif}"
        )
        self.roi = None
        self.sample_point = None
        self._needs_initial_fit = True
        self._update_zoom_label()
        self.after_idle(self.fit)

    def clear_image(self) -> None:
        self.image_data = None
        self._pil_image = None
        self._photo = None
        self._photo_key = None
        self.roi = None
        self.sample_point = None
        self.meta_var.set("尚未打开图片")
        self.zoom_var.set("缩放 —")
        self.canvas.delete("all")
        self.horizontal.set(0.0, 1.0)
        self.vertical.set(0.0, 1.0)

    def set_roi(self, roi: Optional[ROI], *, colour: str = "#22C55E") -> None:
        self.roi = roi
        self.roi_colour = colour
        self._draw_overlays()

    def set_sample_point(self, point: Optional[Tuple[int, int]]) -> None:
        self.sample_point = point
        self._draw_overlays()

    def fit(self) -> None:
        if self.image_data is None:
            return
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        self.zoom = max(0.0001, min((width - 16) / self.image_data.width, (height - 16) / self.image_data.height))
        self.pan_x = (width - self.image_data.width * self.zoom) / 2.0
        self.pan_y = (height - self.image_data.height * self.zoom) / 2.0
        self._needs_initial_fit = False
        self._update_zoom_label()
        self._schedule_render(immediate=True)

    def one_to_one(self) -> None:
        if self.image_data is None:
            return
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        self.zoom = 1.0
        self.pan_x = (width - self.image_data.width) / 2.0
        self.pan_y = (height - self.image_data.height) / 2.0
        self._clamp_pan()
        self._update_zoom_label()
        self._schedule_render(immediate=True)

    def zoom_by(self, factor: float) -> None:
        """Zoom around the centre of this canvas."""

        if self.image_data is None:
            return
        self._zoom_at(
            max(1.0, self.canvas.winfo_width() / 2.0),
            max(1.0, self.canvas.winfo_height() / 2.0),
            factor,
        )

    def _update_zoom_label(self) -> None:
        if self.image_data is None:
            self.zoom_var.set("缩放 —")
            return
        percentage = self.zoom * 100.0
        self.zoom_var.set(f"缩放 {percentage:.0f}%" if percentage >= 10.0 else f"缩放 {percentage:.1f}%")

    def canvas_to_image(self, canvas_x: float, canvas_y: float) -> Optional[Tuple[float, float]]:
        if self.image_data is None or self.zoom <= 0:
            return None
        x = (canvas_x - self.pan_x) / self.zoom
        y = (canvas_y - self.pan_y) / self.zoom
        if 0.0 <= x < self.image_data.width and 0.0 <= y < self.image_data.height:
            return x, y
        return None

    def image_to_canvas(self, image_x: float, image_y: float) -> Tuple[float, float]:
        return self.pan_x + image_x * self.zoom, self.pan_y + image_y * self.zoom

    def _on_configure(self, _event: tk.Event) -> None:
        if self._needs_initial_fit:
            self.fit()
        else:
            self._clamp_pan()
            self._schedule_render()

    def _schedule_render(self, *, immediate: bool = False) -> None:
        if not self.winfo_exists():
            return
        if self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except tk.TclError:
                pass
            self._render_after_id = None
        if immediate:
            self._render()
        else:
            self._render_after_id = self.after(RENDER_THROTTLE_MS, self._render)

    def _render(self) -> None:
        self._render_after_id = None
        if self.image_data is None or self._pil_image is None or not self.canvas.winfo_exists():
            return
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        x0 = max(0, int(math.floor(-self.pan_x / self.zoom)))
        y0 = max(0, int(math.floor(-self.pan_y / self.zoom)))
        x1 = min(self.image_data.width, int(math.ceil((canvas_width - self.pan_x) / self.zoom)))
        y1 = min(self.image_data.height, int(math.ceil((canvas_height - self.pan_y) / self.zoom)))
        if x1 > x0 and y1 > y0:
            target_width = max(1, int(round((x1 - x0) * self.zoom)))
            target_height = max(1, int(round((y1 - y0) * self.zoom)))
            photo_key = (id(self.image_data), x0, y0, x1, y1, target_width, target_height)
            if photo_key != self._photo_key or self._photo is None:
                crop = self._pil_image.crop((x0, y0, x1, y1))
                if crop.size != (target_width, target_height):
                    assert Image is not None
                    resampling = Image.Resampling.NEAREST if self.zoom >= 4.0 else Image.Resampling.BILINEAR
                    crop = crop.resize((target_width, target_height), resampling)
                assert ImageTk is not None
                self._photo = ImageTk.PhotoImage(crop, master=self.canvas)
                self._photo_key = photo_key
            left, top = self.image_to_canvas(x0, y0)
            rendered = self.canvas.find_withtag("rendered-image")
            if rendered:
                self.canvas.coords(rendered[0], left, top)
                self.canvas.itemconfigure(rendered[0], image=self._photo)
            else:
                self.canvas.create_image(left, top, image=self._photo, anchor="nw", tags=("rendered-image",))
                self.canvas.tag_lower("rendered-image")
        else:
            self.canvas.delete("rendered-image")
        self._draw_overlays()
        self._update_scrollbars()

    def _draw_overlays(self) -> None:
        if not self.canvas.winfo_exists():
            return
        self.canvas.delete("analysis-overlay")
        if self.roi is not None:
            left, top = self.image_to_canvas(self.roi.x, self.roi.y)
            right, bottom = self.image_to_canvas(self.roi.right, self.roi.bottom)
            self.canvas.create_rectangle(
                left,
                top,
                right,
                bottom,
                outline=self.roi_colour,
                width=2,
                tags=("analysis-overlay",),
            )
            self.canvas.create_text(
                left + 4,
                top + 4,
                text=self.roi.name,
                anchor="nw",
                fill="white",
                font=FONT_SMALL,
                tags=("analysis-overlay",),
            )
        if self.sample_point is not None:
            x, y = self.image_to_canvas(self.sample_point[0] + 0.5, self.sample_point[1] + 0.5)
            size = 9
            self.canvas.create_line(x - size, y, x + size, y, fill="#FDE047", width=2, tags=("analysis-overlay",))
            self.canvas.create_line(x, y - size, x, y + size, fill="#FDE047", width=2, tags=("analysis-overlay",))

    def _clamp_pan(self) -> None:
        if self.image_data is None:
            return
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        scaled_width = self.image_data.width * self.zoom
        scaled_height = self.image_data.height * self.zoom
        if scaled_width <= canvas_width:
            self.pan_x = (canvas_width - scaled_width) / 2.0
        else:
            self.pan_x = min(0.0, max(canvas_width - scaled_width, self.pan_x))
        if scaled_height <= canvas_height:
            self.pan_y = (canvas_height - scaled_height) / 2.0
        else:
            self.pan_y = min(0.0, max(canvas_height - scaled_height, self.pan_y))

    def _update_scrollbars(self) -> None:
        if self.image_data is None:
            self.horizontal.set(0.0, 1.0)
            self.vertical.set(0.0, 1.0)
            return
        canvas_width = max(1.0, float(self.canvas.winfo_width()))
        canvas_height = max(1.0, float(self.canvas.winfo_height()))
        scaled_width = self.image_data.width * self.zoom
        scaled_height = self.image_data.height * self.zoom
        if scaled_width <= canvas_width:
            self.horizontal.set(0.0, 1.0)
        else:
            start = max(0.0, min(1.0, -self.pan_x / scaled_width))
            self.horizontal.set(start, min(1.0, start + canvas_width / scaled_width))
        if scaled_height <= canvas_height:
            self.vertical.set(0.0, 1.0)
        else:
            start = max(0.0, min(1.0, -self.pan_y / scaled_height))
            self.vertical.set(start, min(1.0, start + canvas_height / scaled_height))

    def _scroll(self, axis: str, *args: str) -> None:
        if self.image_data is None:
            return
        scaled = (self.image_data.width if axis == "x" else self.image_data.height) * self.zoom
        viewport = self.canvas.winfo_width() if axis == "x" else self.canvas.winfo_height()
        pan = self.pan_x if axis == "x" else self.pan_y
        if args[0] == "moveto":
            pan = -float(args[1]) * scaled
        elif args[0] == "scroll":
            unit = max(20.0, viewport * (0.08 if args[2] == "units" else 0.8))
            pan -= float(args[1]) * unit
        if axis == "x":
            self.pan_x = pan
        else:
            self.pan_y = pan
        self._clamp_pan()
        self._schedule_render()

    def _xview(self, *args: str) -> None:
        self._scroll("x", *args)

    def _yview(self, *args: str) -> None:
        self._scroll("y", *args)

    def _on_mousewheel(self, event: tk.Event) -> str:
        delta = getattr(event, "delta", 0)
        if delta:
            factor = 1.15 if delta > 0 else 1.0 / 1.15
            self._zoom_at(event.x, event.y, factor)
        return "break"

    def _zoom_at(self, canvas_x: float, canvas_y: float, factor: float) -> str:
        if self.image_data is None:
            return "break"
        image_x = (canvas_x - self.pan_x) / self.zoom
        image_y = (canvas_y - self.pan_y) / self.zoom
        new_zoom = min(32.0, max(0.0001, self.zoom * factor))
        self.pan_x = canvas_x - image_x * new_zoom
        self.pan_y = canvas_y - image_y * new_zoom
        self.zoom = new_zoom
        self._clamp_pan()
        self._update_zoom_label()
        self._schedule_render()
        return "break"

    def _on_motion(self, event: tk.Event) -> None:
        point = self.canvas_to_image(event.x, event.y)
        self._motion_position = None if point is None else (int(point[0]), int(point[1]))
        if self._motion_after_id is None:
            self._motion_after_id = self.after(MOTION_THROTTLE_MS, self._emit_motion)

    def _emit_motion(self) -> None:
        self._motion_after_id = None
        if self._motion_position is not None and self.live_enabled():
            self.pixel_callback(self.role, self._motion_position[0], self._motion_position[1], False)

    def _on_leave(self, _event: tk.Event) -> None:
        self._motion_position = None

    def _on_left_press(self, event: tk.Event) -> Optional[str]:
        point = self.canvas_to_image(event.x, event.y)
        if point is None:
            return None
        self._selection_image_start = point
        self._selection_canvas_start = (event.x, event.y)
        self.canvas.delete("selection-preview")
        return None

    def _on_left_drag(self, event: tk.Event) -> None:
        if self._selection_image_start is None or self._selection_canvas_start is None:
            return
        start_x, start_y = self._selection_canvas_start
        self.canvas.delete("selection-preview")
        self.canvas.create_rectangle(
            start_x,
            start_y,
            event.x,
            event.y,
            outline="#60A5FA",
            width=2,
            dash=(5, 3),
            tags=("selection-preview",),
        )

    def _on_left_release(self, event: tk.Event) -> None:
        start_image = self._selection_image_start
        start_canvas = self._selection_canvas_start
        self._selection_image_start = None
        self._selection_canvas_start = None
        self.canvas.delete("selection-preview")
        if start_image is None or start_canvas is None or self.image_data is None:
            return
        movement = math.hypot(event.x - start_canvas[0], event.y - start_canvas[1])
        end_x = min(self.image_data.width, max(0.0, (event.x - self.pan_x) / self.zoom))
        end_y = min(self.image_data.height, max(0.0, (event.y - self.pan_y) / self.zoom))
        if movement < 4.0:
            x = min(self.image_data.width - 1, max(0, int(start_image[0])))
            y = min(self.image_data.height - 1, max(0, int(start_image[1])))
            self.pixel_callback(self.role, x, y, True)
            return
        left = int(math.floor(min(start_image[0], end_x)))
        top = int(math.floor(min(start_image[1], end_y)))
        right = int(math.ceil(max(start_image[0], end_x)))
        bottom = int(math.ceil(max(start_image[1], end_y)))
        roi = ROI(left, top, right - left, bottom - top)
        self.roi_callback(self.role, roi)

    def _on_pan_press(self, event: tk.Event) -> str:
        self._pan_start = (event.x, event.y, self.pan_x, self.pan_y)
        self.canvas.configure(cursor="fleur")
        self._selection_image_start = None
        self._selection_canvas_start = None
        self.canvas.delete("selection-preview")
        return "break"

    def _on_pan_drag(self, event: tk.Event) -> str:
        if self._pan_start is None:
            return "break"
        self.pan_x = self._pan_start[2] + event.x - self._pan_start[0]
        self.pan_y = self._pan_start[3] + event.y - self._pan_start[1]
        self._clamp_pan()
        self._schedule_render()
        return "break"

    def _on_pan_release(self, _event: tk.Event) -> str:
        self._pan_start = None
        self.canvas.configure(cursor="crosshair")
        return "break"

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self.canvas:
            return
        for after_id in (self._render_after_id, self._motion_after_id):
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass


class HistogramCanvas(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, style="InspectorCard.TFrame")
        self.histogram: Optional[Any] = None
        self.scope = "尚无数据"
        self._after_id: Optional[str] = None
        self.canvas = tk.Canvas(self, background=PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_configure)

    def set_histogram(self, histogram: Optional[Any], scope: str) -> None:
        self.histogram = histogram
        self.scope = scope
        self._draw()

    def _on_configure(self, _event: tk.Event) -> None:
        if self._after_id is not None:
            self.after_cancel(self._after_id)
        self._after_id = self.after(40, self._draw)

    def _draw(self) -> None:
        self._after_id = None
        self.canvas.delete("all")
        width = max(20, self.canvas.winfo_width())
        height = max(20, self.canvas.winfo_height())
        self.canvas.create_text(12, 10, text=f"RGB 直方图 · {self.scope}", anchor="nw", fill=INK, font=FONT_CARD_TITLE)
        left, top, right, bottom = 42, 42, width - 16, height - 28
        self.canvas.create_rectangle(left, top, right, bottom, outline=BORDER)
        if self.histogram is None or right <= left or bottom <= top:
            self.canvas.create_text(width / 2, height / 2, text="打开图片或选择 ROI 后显示", fill=MUTED, font=FONT_BODY)
            return
        values = np.log1p(np.asarray(self.histogram, dtype=np.float64))
        maximum = float(np.max(values))
        if maximum <= 0:
            return
        for channel, colour in enumerate(("#EF4444", "#16A34A", "#2563EB")):
            points = []
            for index in range(256):
                x = left + index / 255.0 * (right - left)
                y = bottom - values[channel, index] / maximum * (bottom - top)
                points.extend((x, y))
            self.canvas.create_line(*points, fill=colour, width=2)
        self.canvas.create_text(left, bottom + 7, text="0", anchor="nw", fill=MUTED, font=FONT_SMALL)
        self.canvas.create_text(right, bottom + 7, text="255", anchor="ne", fill=MUTED, font=FONT_SMALL)


class ImageInspectorWorkspace:
    def __init__(
        self,
        root: tk.Misc,
        *,
        on_close: Optional[Callable[[], None]] = None,
        on_home: Optional[Callable[[], None]] = None,
        on_gamma: Optional[Callable[[], object]] = None,
        on_about: Optional[Callable[[], None]] = None,
    ) -> None:
        if CORE_DEPENDENCY_ERROR is not None:
            raise RuntimeError(
                "TuneLab 默认图像依赖未完整安装；请运行 python3 run_tunelab.py，"
                "或在工程虚拟环境中执行 python -m pip install -e .。"
            ) from CORE_DEPENDENCY_ERROR
        self.root = root
        self.on_close = on_close
        self.on_home = on_home
        self.on_gamma = on_gamma
        self.on_about = on_about
        self.settings = load_image_inspector_settings()
        self.last_directory = self.settings.last_directory
        self.active_roles: Tuple[str, ...] = (IMAGE_ROLES[0],)
        self.images: Dict[str, Optional[ImageData]] = {role: None for role in IMAGE_ROLES}
        self.rois: Dict[str, Optional[ROI]] = {role: None for role in IMAGE_ROLES}
        self.roi_statistics: Dict[str, Optional[ROIStatistics]] = {role: None for role in IMAGE_ROLES}
        self.fixed_pixels: Dict[str, Optional[PixelMetrics]] = {role: None for role in IMAGE_ROLES}
        self.match_results: Dict[str, Optional[MatchResult]] = {role: None for role in COMPARISON_ROLES}
        self.comparisons: Dict[str, Optional[ComparisonResult]] = {role: None for role in COMPARISON_ROLES}
        # Compatibility aliases for the first comparison while the public data
        # model remains pair-oriented.
        self.match_result: Optional[MatchResult] = None
        self.comparison: Optional[ComparisonResult] = None
        self.dual_mode = False
        self.folder_paths: list[Path] = []
        self._folder_items: Dict[str, Path] = {}
        self._folder_token = 0
        self._thumbnail_photos: Dict[str, Any] = {}
        self._thumbnail_cache: "OrderedDict[Path, Any]" = OrderedDict()
        self._thumbnail_requested: set[str] = set()
        self._visible_thumbnail_items: set[str] = set()
        self._selection_guard = False
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="TuneLabImage")
        self._load_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="TuneLabDecode")
        self._thumbnail_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="TuneLabThumb")
        self._result_queue: "queue.Queue[Tuple[str, int, str, Future[Any], Dict[str, Any]]]" = queue.Queue()
        self._futures = set()
        self._thumbnail_futures = set()
        self._pending = 0
        self._load_tokens = {role: 0 for role in IMAGE_ROLES}
        self._analysis_tokens = {role: 0 for role in IMAGE_ROLES}
        self._match_tokens = {role: 0 for role in COMPARISON_ROLES}
        self._matching_roles: set[str] = set()
        self._poll_after_id: Optional[str] = None
        self._image_cache = ImageDataCache()

        self._configure_styles()
        self._build_ui()
        self.root.title(WINDOW_TITLE)
        if self.on_close is None:
            self.window_placement = fit_window_to_screen(self.root, desired_width=1540, desired_height=980)
            self._apply_saved_window_size()
            try:
                self.root.protocol("WM_DELETE_WINDOW", self.close)
            except tk.TclError:
                pass
        if not opencv_available():
            self.status_var.set(
                "OpenCV 未能导入：像素与 ROI 分析仍可使用，自动匹配暂用较慢的 NumPy FFT 后备路径。"
            )
        self._poll_after_id = self.root.after(50, self._poll_background)

    def _configure_styles(self) -> None:
        style = configure_typography(self.root)
        style.configure("InspectorRoot.TFrame", background=BG)
        style.configure("InspectorCard.TFrame", background=PANEL)
        style.configure("InspectorCard.TLabel", background=PANEL, foreground=INK, font=FONT_BODY)
        style.configure("InspectorMutedCard.TLabel", background=PANEL, foreground=MUTED, font=FONT_SMALL)
        style.configure("InspectorCardTitle.TLabel", background=PANEL, foreground=INK, font=FONT_CARD_TITLE)
        style.configure("InspectorTitle.TLabel", background=BG, foreground=INK, font=FONT_TITLE)
        style.configure("InspectorSubtitle.TLabel", background=BG, foreground=MUTED, font=FONT_BODY)
        style.configure("InspectorStatus.TLabel", background="#F8FAFC", foreground=MUTED, padding=(9, 6), font=FONT_SMALL)
        style.configure("InspectorMatchHigh.TLabel", background=PANEL, foreground=GREEN, font=FONT_BODY_BOLD)
        style.configure("InspectorMatchMedium.TLabel", background=PANEL, foreground=AMBER, font=FONT_BODY_BOLD)
        style.configure("InspectorMatchLow.TLabel", background=PANEL, foreground=RED, font=FONT_BODY_BOLD)
        style.configure("ImageBrowser.Treeview", rowheight=88, font=FONT_SMALL)
        style.configure("ImageBrowser.Treeview.Heading", font=FONT_SMALL)
        style.configure("InspectorComparison.Treeview", rowheight=25, font=FONT_BODY)
        style.configure("InspectorComparison.Treeview.Heading", font=FONT_BODY_BOLD)
        style.configure("TButton", font=FONT_BODY)
        configure_action_styles(style)

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        self.file_menu = tk.Menu(menu, tearoff=False)
        self.file_menu.add_command(label="打开图片文件夹...", command=self.open_folder)
        self.file_menu.add_command(label="显示所选图片", command=self.load_selected_images)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="导出 CSV...", command=self.export_current)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="关闭", command=self.close)
        menu.add_cascade(label="文件", menu=self.file_menu)

        self.view_menu = tk.Menu(menu, tearoff=False)
        self.view_menu.add_command(label="适应窗口", command=self.fit_images)
        self.view_menu.add_command(label="1:1 显示", command=self.one_to_one)
        self.view_menu.add_separator()
        self.view_menu.add_command(label="放大", command=self.zoom_in)
        self.view_menu.add_command(label="缩小", command=self.zoom_out)
        self.view_menu.add_separator()
        self.view_menu.add_checkbutton(
            label="实时显示鼠标像素",
            variable=self.live_pixel_var,
            command=self._settings_changed,
        )
        self.view_menu.add_checkbutton(
            label="显示 RGB 直方图",
            variable=self.show_histogram_var,
            command=self._on_histogram_visibility_changed,
        )
        menu.add_cascade(label="视图", menu=self.view_menu)

        self.analysis_menu = tk.Menu(menu, tearoff=False)
        self.analysis_menu.add_command(label="清除 ROI", command=self.clear_roi)
        self.analysis_menu.add_command(label="接受当前全部匹配", command=self.accept_match)
        menu.add_cascade(label="分析", menu=self.analysis_menu)

        self.export_menu = tk.Menu(menu, tearoff=False)
        self.export_menu.add_command(label="导出当前分析 CSV...", command=self.export_current)
        self.export_menu.add_checkbutton(
            label="CSV 包含完整图片路径",
            variable=self.include_full_path_var,
            command=self._settings_changed,
        )
        menu.add_cascade(label="导出", menu=self.export_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        if self.on_home is not None:
            tools_menu.add_command(label="首页", command=self.on_home)
        if self.on_close is not None:
            tools_menu.add_command(label="CC 校正", command=self.on_close)
        if self.on_gamma is not None:
            tools_menu.add_command(label="Gamma 优化", command=self.on_gamma)
        if tools_menu.index("end") is not None:
            menu.add_cascade(label="工具", menu=tools_menu)

        self.help_menu = tk.Menu(menu, tearoff=False)
        self.help_menu.add_command(label="TuneLab 使用说明", command=self._show_workbench_help)
        self.help_menu.add_separator()
        self.help_menu.add_command(label="图像分析器边界", command=self.show_help)
        self.help_menu.add_command(label="关于 TuneLab", command=self._show_about)
        menu.add_cascade(label="帮助", menu=self.help_menu)
        self.root.configure(menu=menu)

    def _build_ui(self) -> None:
        self.outer = ttk.Frame(self.root, padding=(14, 10), style="InspectorRoot.TFrame")
        self.outer.pack(fill="both", expand=True)
        header = ttk.Frame(self.outer, style="InspectorRoot.TFrame")
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="图像分析器", style="InspectorTitle.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="普通 JPG / PNG / BMP / TIFF · 文件夹预览、1–4 图 ROI 与最终输出对比",
            style="InspectorSubtitle.TLabel",
        ).pack(side="left", padx=(12, 0), pady=(6, 0))

        toolbar = ttk.Frame(self.outer, padding=(9, 7), style="InspectorCard.TFrame")
        self.toolbar_panel = toolbar
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="打开文件夹", command=self.open_folder).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(toolbar, text="显示所选（1–4 张）", command=self.load_selected_images).grid(row=0, column=1, padx=(0, 10))
        ttk.Button(toolbar, text="−", command=self.zoom_out, width=3, style="Quiet.TButton").grid(row=0, column=2, padx=(0, 3))
        ttk.Button(toolbar, text="+", command=self.zoom_in, width=3, style="Quiet.TButton").grid(row=0, column=3, padx=(0, 4))
        ttk.Button(toolbar, text="1:1", command=self.one_to_one, style="Quiet.TButton").grid(row=0, column=4, padx=(0, 4))
        ttk.Button(toolbar, text="适应窗口", command=self.fit_images, style="Quiet.TButton").grid(row=0, column=5, padx=(0, 4))
        ttk.Button(toolbar, text="清除 ROI", command=self.clear_roi, style="Quiet.TButton").grid(row=0, column=6, padx=(0, 10))
        ttk.Label(toolbar, text="搜索", style="InspectorCard.TLabel").grid(row=0, column=7, padx=(0, 4))
        self.search_range_var = tk.StringVar(value=f"±{self.settings.search_range}")
        self.search_combo = ttk.Combobox(
            toolbar,
            textvariable=self.search_range_var,
            values=[f"±{value}" for value in MATCH_SEARCH_RANGES],
            width=6,
            state="readonly",
        )
        self.search_combo.grid(row=0, column=8, padx=(0, 8))
        self.search_combo.bind("<<ComboboxSelected>>", lambda _event: self._settings_changed())
        self.neutral_mode_var = tk.BooleanVar(value=self.settings.neutral_mode)
        ttk.Checkbutton(
            toolbar,
            text="将当前 ROI 视为中性区域",
            variable=self.neutral_mode_var,
            command=self._on_neutral_mode_changed,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(5, 0), padx=(0, 10))
        self.live_pixel_var = tk.BooleanVar(value=self.settings.live_pixel)
        self.show_histogram_var = tk.BooleanVar(value=self.settings.show_histogram)
        self.include_full_path_var = tk.BooleanVar(value=self.settings.include_full_path)
        # Rebuild now that the Tk variable exists; Tk checkbutton menu variables
        # cannot be created safely before _build_ui.
        self._build_menu()
        self.match_status_var = tk.StringVar(value="ROI Match Score: —")
        self.match_status_label = ttk.Label(
            toolbar,
            textvariable=self.match_status_var,
            style="InspectorMatchLow.TLabel",
            wraplength=760,
        )
        self.match_status_label.grid(row=1, column=4, columnspan=5, sticky="w", pady=(5, 0), padx=(6, 6))
        toolbar.columnconfigure(9, weight=1)
        self.progress = ttk.Progressbar(toolbar, mode="indeterminate", length=80)
        self.progress.grid(row=1, column=9, sticky="e", pady=(5, 0))

        self.main_pane = ttk.Panedwindow(self.outer, orient="vertical")
        self.main_pane.pack(fill="both", expand=True)
        self.viewer_pane = ttk.Panedwindow(self.main_pane, orient="horizontal")
        self.folder_panel = ttk.Frame(self.viewer_pane, padding=(8, 7), style="InspectorCard.TFrame")
        ttk.Label(self.folder_panel, text="图片文件夹", style="InspectorCardTitle.TLabel").pack(anchor="w")
        self.folder_path_var = tk.StringVar(value="尚未打开文件夹")
        ttk.Label(
            self.folder_panel,
            textvariable=self.folder_path_var,
            style="InspectorMutedCard.TLabel",
            wraplength=235,
        ).pack(fill="x", pady=(3, 6))
        browser_frame = ttk.Frame(self.folder_panel, style="InspectorCard.TFrame")
        browser_frame.pack(fill="both", expand=True)
        self.folder_tree = ttk.Treeview(
            browser_frame,
            columns=("size",),
            show="tree headings",
            selectmode="extended",
            style="ImageBrowser.Treeview",
        )
        self.folder_tree.heading("#0", text="图片")
        self.folder_tree.heading("size", text="尺寸")
        self.folder_tree.column("#0", width=185, stretch=True)
        self.folder_tree.column("size", width=74, anchor="center", stretch=False)
        folder_scrollbar = ttk.Scrollbar(browser_frame, orient="vertical", command=self._folder_yview)
        self.folder_tree.configure(yscrollcommand=folder_scrollbar.set)
        self.folder_tree.pack(side="left", fill="both", expand=True)
        folder_scrollbar.pack(side="right", fill="y")
        self.folder_tree.bind("<<TreeviewSelect>>", self._on_folder_selection)
        self.folder_tree.bind("<Double-1>", lambda _event: self.load_selected_images())
        self.folder_tree.bind("<Configure>", lambda _event: self.root.after_idle(self._schedule_visible_thumbnails))
        self.folder_tree.bind("<MouseWheel>", lambda _event: self.root.after_idle(self._schedule_visible_thumbnails), add="+")
        self.folder_tree.bind("<Button-4>", lambda _event: self.root.after_idle(self._schedule_visible_thumbnails), add="+")
        self.folder_tree.bind("<Button-5>", lambda _event: self.root.after_idle(self._schedule_visible_thumbnails), add="+")
        self.folder_tree.bind("<Control-Button-1>", self._toggle_folder_item)
        try:
            self.folder_tree.bind("<Command-Button-1>", self._toggle_folder_item)
        except tk.TclError:
            pass
        self.folder_selection_var = tk.StringVar(value="Ctrl/⌘ 多选，最多 4 张；列表首张作为参考图")
        ttk.Label(
            self.folder_panel,
            textvariable=self.folder_selection_var,
            style="InspectorMutedCard.TLabel",
            wraplength=235,
        ).pack(fill="x", pady=(6, 0))

        self.image_grid = ttk.Frame(self.viewer_pane, style="InspectorRoot.TFrame")
        self.views: Dict[str, ImageCanvas] = {}
        for role in IMAGE_ROLES:
            self.views[role] = ImageCanvas(
                self.image_grid,
                role,
                pixel_callback=self._on_pixel,
                roi_callback=self._on_roi,
                live_enabled=self.live_pixel_var.get,
            )
        self.before_view = self.views["before"]
        self.after_view = self.views["after"]
        self.viewer_pane.add(self.folder_panel, weight=0)
        self.viewer_pane.add(self.image_grid, weight=4)
        self.main_pane.add(self.viewer_pane, weight=3)

        self.notebook = ttk.Notebook(self.main_pane)
        self.pixel_tab = ttk.Frame(self.notebook, padding=7, style="InspectorRoot.TFrame")
        self.compare_tab = ttk.Frame(self.notebook, padding=7, style="InspectorRoot.TFrame")
        self.histogram_tab = ttk.Frame(self.notebook, padding=7, style="InspectorRoot.TFrame")
        self.conclusion_tab = ttk.Frame(self.notebook, padding=7, style="InspectorRoot.TFrame")
        self.notebook.add(self.pixel_tab, text="像素 / ROI 数据")
        self.notebook.add(self.compare_tab, text="多图对比")
        self.notebook.add(self.histogram_tab, text="RGB 直方图")
        self.notebook.add(self.conclusion_tab, text="分析结论")
        if not self.show_histogram_var.get():
            self.notebook.hide(self.histogram_tab)
        self.main_pane.add(self.notebook, weight=2)

        self.pixel_text = self._make_text(self.pixel_tab)
        self._build_comparison_panel()
        histogram_controls = ttk.Frame(self.histogram_tab, style="InspectorRoot.TFrame")
        histogram_controls.pack(fill="x", pady=(0, 4))
        self.histogram_scope_var = tk.StringVar(value="当前 ROI")
        self.histogram_role_var = tk.StringVar(value=_role_label("before"))
        for label in ("当前 ROI", "整图"):
            ttk.Radiobutton(
                histogram_controls,
                text=label,
                value=label,
                variable=self.histogram_scope_var,
                command=self._refresh_histogram,
            ).pack(side="left", padx=(0, 8))
        self.histogram_role_combo = ttk.Combobox(
            histogram_controls,
            textvariable=self.histogram_role_var,
            values=(_role_label("before"),),
            state="readonly",
            width=16,
        )
        self.histogram_role_combo.pack(side="left", padx=(8, 0))
        self.histogram_role_var.trace_add("write", lambda *_args: self._refresh_histogram())
        self._set_image_count(1)
        self.root.after_idle(self._restore_panel_ratio)
        self.histogram_canvas = HistogramCanvas(self.histogram_tab)
        self.histogram_canvas.pack(fill="both", expand=True)
        self.conclusion_text = self._make_text(self.conclusion_tab)

        status = ttk.Frame(self.outer, style="InspectorRoot.TFrame")
        status.pack(fill="x")
        self.status_var = tk.StringVar(
            value="请打开图片文件夹并选择 1–4 张图片。左键点击固定像素，拖动框选 ROI；中键/右键或 Shift+左键平移。"
        )
        self.pixel_status_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.status_var, style="InspectorStatus.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Label(status, textvariable=self.pixel_status_var, style="InspectorStatus.TLabel", font=FONT_MONO).pack(side="right")
        self._set_text(self.pixel_text, "尚未选择像素或 ROI。")
        self._set_text(self.compare_text, "选择 2–4 张图片后，在参考图中框选 ROI 即可显示对比。")
        self._set_text(self.conclusion_text, "结论将受 ROI 匹配置信度门禁约束。")

    def _build_comparison_panel(self) -> None:
        controls = ttk.Frame(self.compare_tab, padding=(9, 7), style="InspectorCard.TFrame")
        controls.pack(fill="x", pady=(0, 6))
        ttk.Label(controls, text="查看", style="InspectorCard.TLabel").pack(side="left")
        self.comparison_role_var = tk.StringVar(value="")
        self.comparison_role_combo = ttk.Combobox(
            controls,
            textvariable=self.comparison_role_var,
            state="readonly",
            width=14,
        )
        self.comparison_role_combo.pack(side="left", padx=(6, 12))
        self.comparison_role_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_comparison_table())
        self.comparison_files_var = tk.StringVar(value="请选择 2–4 张图片并完成 ROI 分析")
        ttk.Label(
            controls,
            textvariable=self.comparison_files_var,
            style="InspectorMutedCard.TLabel",
        ).pack(side="left", fill="x", expand=True)
        self.comparison_gate_var = tk.StringVar(value="匹配置信度：—")
        self.comparison_gate_label = ttk.Label(
            controls,
            textvariable=self.comparison_gate_var,
            style="InspectorMatchLow.TLabel",
        )
        self.comparison_gate_label.pack(side="right", padx=(10, 0))
        swatches = ttk.Frame(controls, style="InspectorCard.TFrame")
        swatches.pack(side="right", padx=(8, 2))
        ttk.Label(swatches, text="ROI 平均色", style="InspectorMutedCard.TLabel").pack(side="left", padx=(0, 4))
        self.reference_swatch = tk.Canvas(
            swatches,
            width=30,
            height=18,
            background=BORDER,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.reference_swatch.pack(side="left")
        ttk.Label(swatches, text="→", style="InspectorMutedCard.TLabel").pack(side="left", padx=3)
        self.target_swatch = tk.Canvas(
            swatches,
            width=30,
            height=18,
            background=BORDER,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.target_swatch.pack(side="left")

        table_frame = ttk.Frame(self.compare_tab, style="InspectorCard.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("metric", "reference", "target", "delta", "change")
        self.compare_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=11,
            style="InspectorComparison.Treeview",
        )
        headings = {
            "metric": "指标",
            "reference": "参考图 1",
            "target": "对比图",
            "delta": "Delta",
            "change": "变化方向",
        }
        widths = {"metric": 130, "reference": 112, "target": 112, "delta": 105, "change": 125}
        for column in columns:
            self.compare_tree.heading(column, text=headings[column])
            self.compare_tree.column(
                column,
                width=widths[column],
                minwidth=82,
                anchor="w" if column == "metric" else "e",
                stretch=True,
            )
        compare_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.compare_tree.yview)
        compare_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.compare_tree.xview)
        self.compare_tree.configure(yscrollcommand=compare_y.set, xscrollcommand=compare_x.set)
        self.compare_tree.grid(row=0, column=0, sticky="nsew")
        compare_y.grid(row=0, column=1, sticky="ns")
        compare_x.grid(row=1, column=0, sticky="ew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        ttk.Label(self.compare_tab, text="详细数据", style="InspectorCardTitle.TLabel").pack(anchor="w", pady=(7, 3))
        self.compare_text = self._make_text(self.compare_tab, expand=False, height=7)

    def _make_text(self, parent: tk.Misc, *, expand: bool = True, height: Optional[int] = None) -> tk.Text:
        frame = ttk.Frame(parent, style="InspectorCard.TFrame")
        frame.pack(fill="both" if expand else "x", expand=expand)
        options: Dict[str, Any] = {
            "wrap": "word",
            "relief": "flat",
            "background": PANEL,
            "foreground": INK,
            "font": FONT_MONO,
            "padx": 10,
            "pady": 8,
            "state": "disabled",
        }
        if height is not None:
            options["height"] = height
        text = tk.Text(frame, **options)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return text

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _apply_saved_window_size(self) -> None:
        match = re.match(r"^(\d+)x(\d+)", self.settings.window_geometry)
        if not match:
            return
        width = min(int(match.group(1)), self.root.winfo_screenwidth())
        height = min(int(match.group(2)), self.root.winfo_screenheight())
        if width >= 900 and height >= 600:
            try:
                self.root.geometry(f"{width}x{height}")
            except tk.TclError:
                pass

    def _set_image_count(self, count: int) -> None:
        count = min(4, max(1, int(count)))
        self.active_roles = IMAGE_ROLES[:count]
        self.dual_mode = count > 1
        for view in self.views.values():
            view.grid_forget()
        for index in range(2):
            self.image_grid.columnconfigure(index, weight=1, uniform="image-columns")
            self.image_grid.rowconfigure(index, weight=1, uniform="image-rows")
        if count == 1:
            self.views["before"].grid(row=0, column=0, columnspan=2, rowspan=2, sticky="nsew", padx=2, pady=2)
        elif count == 2:
            self.views["before"].grid(row=0, column=0, rowspan=2, sticky="nsew", padx=2, pady=2)
            self.views["after"].grid(row=0, column=1, rowspan=2, sticky="nsew", padx=2, pady=2)
        elif count == 3:
            self.views["before"].grid(row=0, column=0, rowspan=2, sticky="nsew", padx=2, pady=2)
            self.views["after"].grid(row=0, column=1, sticky="nsew", padx=2, pady=2)
            self.views["compare3"].grid(row=1, column=1, sticky="nsew", padx=2, pady=2)
        else:
            for index, role in enumerate(self.active_roles):
                self.views[role].grid(row=index // 2, column=index % 2, sticky="nsew", padx=2, pady=2)
        for role in self.active_roles:
            self.views[role].set_title(_role_label(role))
        histogram_labels = tuple(_role_label(role) for role in self.active_roles)
        self.histogram_role_combo.configure(values=histogram_labels)
        if self.histogram_role_var.get() not in histogram_labels:
            self.histogram_role_var.set(histogram_labels[0])
        if hasattr(self, "comparison_role_combo"):
            comparison_labels = tuple(_role_label(role) for role in self.active_roles[1:])
            self.comparison_role_combo.configure(values=comparison_labels)
            if self.comparison_role_var.get() not in comparison_labels:
                self.comparison_role_var.set(comparison_labels[0] if comparison_labels else "")
            self._refresh_comparison_table()
        if hasattr(self, "match_status_var"):
            self._refresh_match_status()
        self.root.after_idle(self.fit_images)

    def _restore_panel_ratio(self) -> None:
        try:
            width = self.viewer_pane.winfo_width()
            if width > 10 and len(self.viewer_pane.panes()) > 1:
                ratio = self.settings.panel_ratio if self.settings.panel_ratio <= 0.35 else 0.24
                self.viewer_pane.sashpos(0, int(width * ratio))
        except (AttributeError, tk.TclError):
            pass

    def _folder_yview(self, *args: str) -> None:
        self.folder_tree.yview(*args)
        self.root.after_idle(self._schedule_visible_thumbnails)

    def _schedule_visible_thumbnails(self) -> None:
        if self._closed or not self.folder_paths or not self.folder_tree.winfo_exists():
            return
        children = self.folder_tree.get_children()
        if not children:
            return
        first_fraction, _last_fraction = self.folder_tree.yview()
        first_index = min(len(children) - 1, max(0, int(first_fraction * len(children))))
        visible_rows = max(1, self.folder_tree.winfo_height() // 88 + 2)
        start = max(0, first_index - THUMBNAIL_PREFETCH_ROWS)
        end = min(len(children), first_index + visible_rows + THUMBNAIL_PREFETCH_ROWS)
        desired = set(children[start:end])
        self._visible_thumbnail_items = desired

        for item_id in tuple(self._thumbnail_photos):
            if item_id in desired:
                continue
            if self.folder_tree.exists(item_id):
                self.folder_tree.item(item_id, image="")
            self._thumbnail_photos.pop(item_id, None)
            self._thumbnail_requested.discard(item_id)
        for item_id in children[start:end]:
            if item_id in self._thumbnail_requested or item_id not in self._folder_items:
                continue
            source = self._folder_items[item_id].resolve()
            cached = self._thumbnail_cache.get(source)
            if cached is not None:
                self._thumbnail_cache.move_to_end(source)
                self._thumbnail_requested.add(item_id)
                self._display_thumbnail(item_id, cached)
                continue
            self._thumbnail_requested.add(item_id)
            self._submit("thumbnail", self._folder_token, item_id, load_thumbnail, self._folder_items[item_id])

    def open_folder(self, path: Optional[Union[str, Path]] = None) -> None:
        selected = str(path) if path is not None else filedialog.askdirectory(
            title="打开图片文件夹",
            initialdir=self.last_directory or None,
        )
        if not selected:
            return
        try:
            paths = discover_images(selected)
        except ImageFolderError as exc:
            messagebox.showerror("无法打开图片文件夹", str(exc), parent=self.root)
            return
        self.last_directory = str(Path(selected).expanduser())
        self.folder_paths = paths
        self.folder_path_var.set(self.last_directory)
        self._folder_token += 1
        for future in tuple(self._thumbnail_futures):
            future.cancel()
        self._thumbnail_futures.clear()
        self._thumbnail_requested.clear()
        self._visible_thumbnail_items.clear()
        self._folder_items.clear()
        self._thumbnail_photos.clear()
        self._thumbnail_cache.clear()
        for item in self.folder_tree.get_children():
            self.folder_tree.delete(item)
        for index, image_path in enumerate(paths):
            item_id = f"image-{index}"
            self._folder_items[item_id] = image_path
            self.folder_tree.insert("", "end", iid=item_id, text=image_path.name, values=("…",))
        if not paths:
            for role in IMAGE_ROLES:
                self._invalidate_role(role, clear_image=True, refresh=False)
            self._set_image_count(1)
            self._refresh_outputs()
            self.folder_selection_var.set("该文件夹没有支持的 JPG、PNG、BMP 或 TIFF 图片")
            self.status_var.set("所选文件夹中没有可预览图片。")
            return
        first_item = "image-0"
        self._selection_guard = True
        self.folder_tree.selection_set(first_item)
        self.folder_tree.focus(first_item)
        self._selection_guard = False
        self.folder_selection_var.set(f"共 {len(paths)} 张 · 已选 1 张 · 第 1 张作为参考图")
        self.status_var.set(f"已打开文件夹，共发现 {len(paths)} 张图片。")
        self.load_selected_images()
        self.root.after_idle(self._schedule_visible_thumbnails)

    def _on_folder_selection(self, _event: Optional[tk.Event] = None) -> None:
        if self._selection_guard:
            return
        selected = list(self.folder_tree.selection())
        if len(selected) > 4:
            self._selection_guard = True
            self.folder_tree.selection_remove(*selected[4:])
            self._selection_guard = False
            selected = selected[:4]
            self.status_var.set("最多同时查看 4 张图片，多余选择已取消。")
        if selected:
            self.folder_selection_var.set(f"已选 {len(selected)} 张 · 按列表顺序排列，第 1 张作为参考图")
        else:
            self.folder_selection_var.set("请选择 1–4 张图片；第 1 张作为参考图")

    def _toggle_folder_item(self, event: tk.Event) -> str:
        item = self.folder_tree.identify_row(event.y)
        if not item:
            return "break"
        selected = set(self.folder_tree.selection())
        if item in selected:
            self.folder_tree.selection_remove(item)
        elif len(selected) < 4:
            self.folder_tree.selection_add(item)
            self.folder_tree.focus(item)
        else:
            self.status_var.set("最多同时查看 4 张图片。")
        self._on_folder_selection()
        return "break"

    def load_selected_images(
        self,
        paths: Optional[Sequence[Union[str, Path]]] = None,
    ) -> None:
        if paths is None:
            selected_items = self.folder_tree.selection()
            selected_paths = selected_paths_in_folder_order(
                self.folder_paths,
                (self._folder_items[item] for item in selected_items if item in self._folder_items),
            )
        else:
            selected_paths = [Path(item).expanduser() for item in paths]
        if not 1 <= len(selected_paths) <= 4:
            messagebox.showinfo("请选择图片", "请在文件夹预览中选择 1–4 张图片。", parent=self.root)
            return
        for role in IMAGE_ROLES:
            self._invalidate_role(role, clear_image=True, refresh=False)
        self._set_image_count(len(selected_paths))
        self._refresh_outputs()
        for role, image_path in zip(self.active_roles, selected_paths):
            self.views[role].set_title(f"{_role_label(role)} · {image_path.name}")
            self._load_async(role, str(image_path), invalidate=False)
        self.status_var.set(f"正在加载所选 {len(selected_paths)} 张图片…")

    def _load_async(self, role: str, path: str, *, invalidate: bool = True) -> None:
        if invalidate:
            self._invalidate_role(role, clear_image=False)
        self._load_tokens[role] += 1
        token = self._load_tokens[role]
        self.last_directory = str(Path(path).expanduser().parent)
        cached = self._image_cache.get(path)
        if cached is not None:
            self._apply_loaded_image(token, role, cached, from_cache=True)
            return
        self.status_var.set(f"正在后台加载 {Path(path).name}...")
        self._submit("load", token, role, load_image, path)

    def _submit(
        self,
        kind: str,
        token: int,
        role: str,
        function: Callable[..., Any],
        *args: Any,
        meta: Optional[Dict[str, Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._closed:
            return
        executor = (
            self._thumbnail_executor
            if kind == "thumbnail"
            else self._load_executor
            if kind == "load"
            else self._executor
        )
        future = executor.submit(function, *args, **(kwargs or {}))
        self._futures.add(future)
        if kind == "thumbnail":
            self._thumbnail_futures.add(future)
        self._pending += 1
        if self._pending == 1:
            self.progress.start(12)
        details = meta or {}
        future.add_done_callback(
            lambda done, k=kind, t=token, r=role, m=details: self._result_queue.put((k, t, r, done, m))
        )

    def _poll_background(self) -> None:
        self._poll_after_id = None
        if self._closed:
            return
        while True:
            try:
                kind, token, role, future, meta = self._result_queue.get_nowait()
            except queue.Empty:
                break
            self._futures.discard(future)
            if kind == "thumbnail":
                self._thumbnail_futures.discard(future)
            self._pending = max(0, self._pending - 1)
            try:
                if kind == "load":
                    self._finish_load(token, role, future, meta)
                elif kind == "thumbnail":
                    self._finish_thumbnail(token, role, future)
                elif kind == "analysis":
                    self._finish_analysis(token, role, future)
                elif kind == "match":
                    self._finish_match(token, role, future)
            except (tk.TclError, RuntimeError):
                LOGGER.exception("Image Inspector background result handling failed")
        if self._pending == 0:
            self.progress.stop()
        self._poll_after_id = self.root.after(50, self._poll_background)

    def _finish_thumbnail(self, token: int, item_id: str, future: Future[Any]) -> None:
        if token != self._folder_token or item_id not in self._folder_items:
            return
        if item_id not in self._visible_thumbnail_items:
            self._thumbnail_requested.discard(item_id)
            return
        try:
            thumbnail = future.result()
        except Exception:
            LOGGER.debug("Thumbnail decode failed for %s", self._folder_items[item_id], exc_info=True)
            if self.folder_tree.exists(item_id):
                self.folder_tree.set(item_id, "size", "无法预览")
            return
        if not self.folder_tree.exists(item_id):
            return
        source = thumbnail.path.resolve()
        self._thumbnail_cache[source] = thumbnail
        self._thumbnail_cache.move_to_end(source)
        while len(self._thumbnail_cache) > THUMBNAIL_CACHE_ITEMS:
            self._thumbnail_cache.popitem(last=False)
        self._display_thumbnail(item_id, thumbnail)

    def _display_thumbnail(self, item_id: str, thumbnail: Any) -> None:
        if not self.folder_tree.exists(item_id):
            return
        assert ImageTk is not None
        photo = ImageTk.PhotoImage(thumbnail.image, master=self.root)
        self._thumbnail_photos[item_id] = photo
        self.folder_tree.item(item_id, image=photo)
        self.folder_tree.set(item_id, "size", f"{thumbnail.source_size[0]}×{thumbnail.source_size[1]}")

    def _finish_load(self, token: int, role: str, future: Future[Any], meta: Dict[str, Any]) -> None:
        if token != self._load_tokens[role]:
            return
        try:
            image_data = future.result()
        except Exception as exc:
            LOGGER.exception("Image load failed")
            messagebox.showerror("图片读取失败", str(exc), parent=self.root)
            self.status_var.set("图片加载失败。")
            return
        self._image_cache.put(image_data)
        self._apply_loaded_image(token, role, image_data, from_cache=False)

    def _apply_loaded_image(self, token: int, role: str, image_data: ImageData, *, from_cache: bool) -> None:
        if token != self._load_tokens[role] or self._closed:
            return
        self.images[role] = image_data
        view = self.views[role]
        view.set_image(image_data)
        view.set_title(f"{_role_label(role)} · {image_data.filename}")
        precision_note = "" if image_data.precision_preserved else "；该格式经 Pillow 解码后为 8-bit 显示精度"
        cache_note = "（缓存）" if from_cache else ""
        self.status_var.set(
            f"已打开{cache_note} {image_data.filename}：{image_data.width}×{image_data.height}，"
            f"原始位深 {image_data.bit_depth}-bit{precision_note}。"
        )
        self._refresh_histogram()
        if role in COMPARISON_ROLES and self.rois["before"] is not None:
            self._start_match(role)

    def _invalidate_role(self, role: str, *, clear_image: bool, refresh: bool = True) -> None:
        self._load_tokens[role] += 1
        self._analysis_tokens[role] += 1
        self.rois[role] = None
        self.roi_statistics[role] = None
        self.fixed_pixels[role] = None
        view = self.views[role]
        view.set_roi(None)
        view.set_sample_point(None)
        if clear_image:
            self.images[role] = None
            view.clear_image()
        if role == "before":
            for target in COMPARISON_ROLES:
                self._match_tokens[target] += 1
                self._matching_roles.discard(target)
                self.match_results[target] = None
                self.comparisons[target] = None
        else:
            self._match_tokens[role] += 1
            self._matching_roles.discard(role)
            self.match_results[role] = None
            self.comparisons[role] = None
        self._sync_pair_aliases()
        if refresh:
            self._refresh_outputs()

    def _sync_pair_aliases(self) -> None:
        self.match_result = self.match_results["after"]
        self.comparison = self.comparisons["after"]

    def _on_pixel(self, role: str, x: int, y: int, fixed: bool) -> None:
        image_data = self.images.get(role)
        if image_data is None:
            return
        try:
            metrics = pixel_metrics(image_data, x, y)
        except ImageInspectorError:
            return
        self.pixel_status_var.set(
            f"{_role_label(role)}  X={x}  Y={y}   R={metrics.rgb[0]:.0f}  G={metrics.rgb[1]:.0f}  B={metrics.rgb[2]:.0f}   "
            f"R/G={_ratio(metrics.r_over_g)}  B/G={_ratio(metrics.b_over_g)}  主导={metrics.maximum_channel}"
        )
        if fixed:
            self.fixed_pixels[role] = metrics
            view = self.views[role]
            view.set_sample_point((x, y))
            self._refresh_outputs()

    def _on_roi(self, role: str, roi: ROI) -> None:
        image_data = self.images.get(role)
        if image_data is None:
            return
        clipped = roi.clipped(image_data.width, image_data.height)
        if clipped.width < MIN_ROI_SIDE or clipped.height < MIN_ROI_SIDE:
            messagebox.showwarning(
                "ROI 太小",
                f"ROI 至少需要 {MIN_ROI_SIDE}×{MIN_ROI_SIDE} 个原图像素；当前为 {clipped.width}×{clipped.height}。",
                parent=self.root,
            )
            return
        named = ROI(clipped.x, clipped.y, clipped.width, clipped.height, self.settings.default_roi_name)
        if role != "before" and self.rois["before"] is None:
            messagebox.showinfo("需要参考 ROI", "请先在参考图 1 中框选 ROI。", parent=self.root)
            return
        self.rois[role] = named
        view = self.views[role]
        view.set_roi(named, colour="#22C55E" if role == "before" else "#F59E0B")
        if role == "before":
            for target in COMPARISON_ROLES:
                self._analysis_tokens[target] += 1
                self._match_tokens[target] += 1
                self._matching_roles.discard(target)
                self.rois[target] = None
                self.roi_statistics[target] = None
                self.views[target].set_roi(None)
                self.match_results[target] = None
                self.comparisons[target] = None
        else:
            assert self.rois["before"] is not None
            self._match_tokens[role] += 1
            self._matching_roles.discard(role)
            self.match_results[role] = manual_match(self.rois["before"], named)
            self._refresh_match_status()
        self._sync_pair_aliases()
        self._start_analysis(role, named)
        if role == "before":
            for target in self.active_roles[1:]:
                if self.images[target] is not None:
                    self._start_match(target)

    def _start_analysis(self, role: str, roi: ROI) -> None:
        image_data = self.images.get(role)
        if image_data is None:
            return
        self._analysis_tokens[role] += 1
        token = self._analysis_tokens[role]
        self.status_var.set(f"正在后台统计 {_role_label(role)} {roi.name}...")
        self._submit("analysis", token, role, analyse_roi, image_data, roi)

    def _finish_analysis(self, token: int, role: str, future: Future[Any]) -> None:
        if token != self._analysis_tokens[role]:
            return
        try:
            statistics = future.result()
        except Exception as exc:
            LOGGER.exception("ROI analysis failed")
            messagebox.showerror("ROI 分析失败", str(exc), parent=self.root)
            return
        self.roi_statistics[role] = statistics
        self.status_var.set(
            f"{_role_label(role)} ROI 已分析：({statistics.roi.x}, {statistics.roi.y}) "
            f"{statistics.roi.width}×{statistics.roi.height}，{statistics.pixel_count:,} 像素。"
        )
        self._refresh_outputs()

    def _search_range(self) -> int:
        try:
            return int(self.search_range_var.get().replace("±", ""))
        except ValueError:
            return self.settings.search_range

    def _start_match(self, role: str) -> None:
        if role not in COMPARISON_ROLES:
            return
        before_image = self.images["before"]
        after_image = self.images[role]
        before_roi = self.rois["before"]
        if before_image is None or after_image is None or before_roi is None:
            return
        self._match_tokens[role] += 1
        token = self._match_tokens[role]
        self._matching_roles.add(role)
        self._refresh_match_status()
        self.match_status_label.configure(style="InspectorMatchMedium.TLabel")
        self.status_var.set(f"正在后台搜索 {_role_label(role)} 邻近区域...")
        self._submit(
            "match",
            token,
            role,
            match_roi,
            before_image.rgb,
            after_image.rgb,
            before_roi,
            kwargs={
                "search_range": self._search_range(),
                "reliable_threshold": self.settings.match_threshold,
            },
        )

    def _finish_match(self, token: int, role: str, future: Future[Any]) -> None:
        if token != self._match_tokens[role]:
            return
        self._matching_roles.discard(role)
        try:
            result = future.result()
        except Exception as exc:
            LOGGER.exception("ROI matching failed")
            messagebox.showerror("ROI 匹配失败", str(exc), parent=self.root)
            self.match_results[role] = None
            self._refresh_match_status()
            return
        self.match_results[role] = result
        self.rois[role] = result.after_roi
        self.views[role].set_roi(result.after_roi, colour="#22C55E" if result.reliable else "#EF4444")
        self._sync_pair_aliases()
        self._refresh_match_status()
        self._start_analysis(role, result.after_roi)
        if result.warning:
            self.status_var.set(result.warning)

    def _refresh_match_status(self) -> None:
        targets = self.active_roles[1:]
        if not targets:
            self.match_status_var.set("ROI Match Score: —")
            self.match_status_label.configure(style="InspectorMatchLow.TLabel")
            return
        sections = []
        worst = "high"
        for role in targets:
            if role in self._matching_roles:
                sections.append(f"{_role_label(role)}: 匹配中")
                worst = "medium" if worst == "high" else worst
                continue
            result = self.match_results[role]
            if result is None:
                sections.append(f"{_role_label(role)}: —")
                worst = "low"
            elif result.manually_confirmed and result.method == "用户手动选择":
                sections.append(f"{_role_label(role)}: 手动确认")
            elif result.manually_confirmed:
                sections.append(f"{_role_label(role)}: {result.score * 100.0:.1f}% 已接受")
            else:
                gate = "" if result.reliable else " 未通过门禁"
                sections.append(f"{_role_label(role)}: {result.score * 100.0:.1f}% {result.confidence}{gate}")
                if not result.reliable:
                    worst = "low"
                elif result.confidence == "中" and worst == "high":
                    worst = "medium"
        self.match_status_var.set("ROI Match · " + " | ".join(sections))
        style = {
            "high": "InspectorMatchHigh.TLabel",
            "medium": "InspectorMatchMedium.TLabel",
            "low": "InspectorMatchLow.TLabel",
        }[worst]
        self.match_status_label.configure(style=style)

    def accept_match(self) -> None:
        roles = [role for role in self.active_roles[1:] if self.match_results[role] is not None]
        if not roles:
            messagebox.showinfo("尚无匹配", "请先在参考图中框选 ROI 并等待匹配完成。", parent=self.root)
            return
        for role in roles:
            result = self.match_results[role]
            assert result is not None
            confirmed = confirm_match(result)
            self.match_results[role] = confirmed
            self.views[role].set_roi(confirmed.after_roi, colour="#22C55E")
        self._sync_pair_aliases()
        self._refresh_match_status()
        self.status_var.set(f"已接受 {len(roles)} 个对比 ROI；结论仍以各 ROI 属于同一物体区域为前提。")
        self._refresh_outputs()

    def clear_roi(self) -> None:
        for target in COMPARISON_ROLES:
            self._match_tokens[target] += 1
            self._matching_roles.discard(target)
        for role in IMAGE_ROLES:
            self._analysis_tokens[role] += 1
            self.rois[role] = None
            self.roi_statistics[role] = None
            self.views[role].set_roi(None)
        for target in COMPARISON_ROLES:
            self.match_results[target] = None
            self.comparisons[target] = None
        self._sync_pair_aliases()
        self._refresh_match_status()
        self.status_var.set("ROI 已清除。")
        self._refresh_outputs()

    def _format_pixel(self, role: str, metrics: PixelMetrics) -> str:
        alpha = "" if metrics.alpha is None else f"\nAlpha: {metrics.alpha:.2f}"
        return (
            f"{_role_label(role)} 固定像素\n"
            f"坐标: X={metrics.x}  Y={metrics.y}\n"
            f"RGB: {_triplet(metrics.rgb)}\n"
            f"归一化 RGB: {_percent_triplet(metrics.normalized_rgb)}\n"
            f"R-G / R-B / G-B: {_triplet(metrics.channel_differences)}\n"
            f"R/G: {_ratio(metrics.r_over_g)}    B/G: {_ratio(metrics.b_over_g)}\n"
            f"HSV: H={metrics.hsv[0]:.2f}°  S={metrics.hsv[1]:.4f}  V={metrics.hsv[2]:.4f}\n"
            f"CIE Lab (D65): L*={metrics.lab[0]:.3f}  a*={metrics.lab[1]:.3f}  b*={metrics.lab[2]:.3f}\n"
            f"相对亮度: {metrics.relative_luminance:.6f}\n"
            f"最大 / 最小通道: {metrics.maximum_channel} / {metrics.minimum_channel}\n"
            f"最大通道差: {metrics.maximum_channel_difference:.3f}\n"
            f"接近中性: {'是' if metrics.near_neutral else '否'}\n"
            f"颜色倾向: {metrics.color_tendency}{alpha}"
        )

    def _format_roi(self, role: str, stats: ROIStatistics) -> str:
        neutral = f"\n中性区域判断: {stats.neutral_assessment}" if self.neutral_mode_var.get() else ""
        return (
            f"{_role_label(role)} {stats.roi.name}\n"
            f"坐标: x={stats.roi.x}, y={stats.roi.y}, width={stats.roi.width}, height={stats.roi.height}\n"
            f"像素数量: {stats.pixel_count:,}\n"
            f"Mean RGB: {_triplet(stats.mean_rgb)}\n"
            f"Median RGB: {_triplet(stats.median_rgb)}\n"
            f"Std RGB: {_triplet(stats.std_rgb)}\n"
            f"Min RGB: {_triplet(stats.min_rgb)}\n"
            f"Max RGB: {_triplet(stats.max_rgb)}\n"
            f"R/G: {_ratio(stats.r_over_g)}    B/G: {_ratio(stats.b_over_g)}\n"
            f"归一化 RGB: {_percent_triplet(stats.normalized_rgb)}\n"
            f"Mean HSV: H={stats.hsv_mean[0]:.2f}°  S={stats.hsv_mean[1]:.4f}  V={stats.hsv_mean[2]:.4f}\n"
            f"Mean Lab (D65): L*={stats.lab_mean[0]:.3f}  a*={stats.lab_mean[1]:.3f}  b*={stats.lab_mean[2]:.3f}\n"
            f"相对亮度: {stats.relative_luminance:.6f}    饱和度: {stats.saturation:.4f}\n"
            f"最大通道: {stats.maximum_channel}    最大通道差: {stats.maximum_channel_difference:.3f}\n"
            f"接近高光剪切比例: {stats.clipped_ratio * 100.0:.3f}%\n"
            f"暗部像素比例: {stats.dark_ratio * 100.0:.3f}%\n"
            f"区域稳定性: {stats.stability}（仅表示内部颜色一致性，不代表匹配准确度）\n"
            f"颜色倾向: {stats.color_tendency}{neutral}"
        )

    def _refresh_outputs(self) -> None:
        sections = []
        for role in self.active_roles:
            if self.fixed_pixels[role] is not None:
                sections.append(self._format_pixel(role, self.fixed_pixels[role]))
            if self.roi_statistics[role] is not None:
                sections.append(self._format_roi(role, self.roi_statistics[role]))
        self._set_text(self.pixel_text, "\n\n".join(sections) if sections else "尚未选择像素或 ROI。")

        reference = self.roi_statistics["before"]
        comparison_sections = []
        conclusion_sections = []
        for role in self.active_roles[1:]:
            target = self.roi_statistics[role]
            match = self.match_results[role]
            if reference is None or target is None:
                self.comparisons[role] = None
                if match is not None and not match.reliable and match.warning:
                    conclusion_sections.append(f"{_role_label(role)}\n{match.warning}")
                continue
            result = compare_statistics(
                reference,
                target,
                reliable=match is not None and match.reliable,
                match_score=None if match is None else match.score,
                manually_confirmed=False if match is None else match.manually_confirmed,
            )
            self.comparisons[role] = result
            comparison_sections.append(self._format_comparison(role, result))
            conclusion_sections.append(f"{_role_label(role)}\n" + "\n\n".join(result.conclusions))
        self._sync_pair_aliases()
        if comparison_sections:
            self._set_text(self.compare_text, ("\n\n" + "─" * 72 + "\n\n").join(comparison_sections))
        else:
            self._set_text(self.compare_text, "选择 2–4 张图片后，在参考图中框选 ROI 即可显示对比。")
        self._set_text(
            self.conclusion_text,
            ("\n\n" + "─" * 72 + "\n\n").join(conclusion_sections)
            if conclusion_sections
            else "结论将分别受每张对比图的 ROI 匹配置信度门禁约束。",
        )
        self._refresh_comparison_table()
        self._refresh_histogram()

    @staticmethod
    def _change_direction(delta: Optional[float], *, tolerance: float = 1e-9) -> str:
        if delta is None:
            return "—"
        if delta > tolerance:
            return "↑ 上升"
        if delta < -tolerance:
            return "↓ 下降"
        return "≈ 基本不变"

    @staticmethod
    def _comparison_value(value: Optional[float], digits: int, *, signed: bool = False) -> str:
        if value is None:
            return "N/A"
        sign = "+" if signed else ""
        return f"{value:{sign}.{digits}f}"

    def _refresh_comparison_table(self) -> None:
        if not hasattr(self, "compare_tree"):
            return
        for item in self.compare_tree.get_children():
            self.compare_tree.delete(item)
        selected_label = self.comparison_role_var.get()
        role = next((item for item in self.active_roles[1:] if _role_label(item) == selected_label), None)
        self.compare_tree.heading("target", text=selected_label or "对比图")
        if role is None:
            self.reference_swatch.configure(background=BORDER)
            self.target_swatch.configure(background=BORDER)
            self.comparison_files_var.set("请选择 2–4 张图片并完成 ROI 分析")
            self.comparison_gate_var.set("匹配置信度：—")
            self.comparison_gate_label.configure(style="InspectorMatchLow.TLabel")
            self.compare_tree.insert("", "end", values=("等待对比数据", "—", "—", "—", "—"))
            return

        reference_image = self.images["before"]
        target_image = self.images[role]
        reference_name = "尚未加载" if reference_image is None else reference_image.filename
        target_name = "尚未加载" if target_image is None else target_image.filename
        self.comparison_files_var.set(f"{reference_name}  →  {target_name}")
        result = self.comparisons[role]
        match = self.match_results[role]
        if match is None:
            self.comparison_gate_var.set("匹配置信度：等待 ROI")
            self.comparison_gate_label.configure(style="InspectorMatchLow.TLabel")
        elif match.manually_confirmed:
            score = "" if match.method == "用户手动选择" else f" · {match.score * 100.0:.1f}%"
            self.comparison_gate_var.set(f"匹配置信度：已手动确认{score}")
            self.comparison_gate_label.configure(style="InspectorMatchHigh.TLabel")
        else:
            gate = "允许保守解释" if match.reliable else "未通过门禁，仅显示数值"
            self.comparison_gate_var.set(f"匹配置信度：{match.score * 100.0:.1f}% · {match.confidence} · {gate}")
            style = (
                "InspectorMatchHigh.TLabel"
                if match.reliable and match.confidence == "高"
                else "InspectorMatchMedium.TLabel"
                if match.reliable
                else "InspectorMatchLow.TLabel"
            )
            self.comparison_gate_label.configure(style=style)
        if result is None:
            self.reference_swatch.configure(background=BORDER)
            self.target_swatch.configure(background=BORDER)
            self.compare_tree.insert("", "end", values=("等待 ROI 统计", "—", "—", "—", "—"))
            return

        def swatch_colour(values: Tuple[float, float, float]) -> str:
            channels = [min(255, max(0, int(round(value)))) for value in values]
            return "#{:02X}{:02X}{:02X}".format(*channels)

        self.reference_swatch.configure(background=swatch_colour(result.before.mean_rgb))
        self.target_swatch.configure(background=swatch_colour(result.after.mean_rgb))

        rows: list[Tuple[str, str, str, str, str]] = []

        def add(
            label: str,
            before: Optional[float],
            after: Optional[float],
            delta: Optional[float],
            *,
            digits: int = 3,
            change: Optional[str] = None,
        ) -> None:
            rows.append(
                (
                    label,
                    self._comparison_value(before, digits),
                    self._comparison_value(after, digits),
                    self._comparison_value(delta, digits, signed=True),
                    change or self._change_direction(delta, tolerance=10 ** (-(digits + 1))),
                )
            )

        for index, channel in enumerate("RGB"):
            percentage = result.delta_rgb_percent[index]
            direction = self._change_direction(result.delta_rgb[index], tolerance=0.005)
            change = f"{direction} · " + ("N/A" if percentage is None else f"{percentage:+.2f}%")
            add(
                f"Mean {channel}",
                result.before.mean_rgb[index],
                result.after.mean_rgb[index],
                result.delta_rgb[index],
                digits=2,
                change=change,
            )
        for index, channel in enumerate("RGB"):
            median_delta = result.after.median_rgb[index] - result.before.median_rgb[index]
            add(
                f"Median {channel}",
                result.before.median_rgb[index],
                result.after.median_rgb[index],
                median_delta,
                digits=2,
            )
        add("R/G", result.before.r_over_g, result.after.r_over_g, result.delta_r_over_g, digits=4)
        add("B/G", result.before.b_over_g, result.after.b_over_g, result.delta_b_over_g, digits=4)
        for index, label in enumerate(("HSV H°", "HSV S", "HSV V")):
            add(label, result.before.hsv_mean[index], result.after.hsv_mean[index], result.delta_hsv[index], digits=4)
        for index, label in enumerate(("Lab L*", "Lab a*", "Lab b*")):
            add(label, result.before.lab_mean[index], result.after.lab_mean[index], result.delta_lab[index], digits=3)
        add(
            "相对亮度",
            result.before.relative_luminance,
            result.after.relative_luminance,
            result.delta_luminance,
            digits=6,
        )
        add("饱和度", result.before.saturation, result.after.saturation, result.delta_saturation, digits=4)
        clipped_delta = (result.after.clipped_ratio - result.before.clipped_ratio) * 100.0
        dark_delta = (result.after.dark_ratio - result.before.dark_ratio) * 100.0
        add(
            "高光剪切 %",
            result.before.clipped_ratio * 100.0,
            result.after.clipped_ratio * 100.0,
            clipped_delta,
            digits=3,
            change=self._change_direction(clipped_delta, tolerance=0.0005) + " · 百分点",
        )
        add(
            "暗部像素 %",
            result.before.dark_ratio * 100.0,
            result.after.dark_ratio * 100.0,
            dark_delta,
            digits=3,
            change=self._change_direction(dark_delta, tolerance=0.0005) + " · 百分点",
        )
        for row in rows:
            self.compare_tree.insert("", "end", values=row)

    def _format_comparison(self, role: str, result: ComparisonResult) -> str:
        percent = ["N/A" if value is None else f"{value:+.2f}%" for value in result.delta_rgb_percent]
        reference_name = self.images["before"].filename if self.images["before"] is not None else _role_label("before")
        target_name = self.images[role].filename if self.images[role] is not None else _role_label(role)
        return (
            f"参考图 1 · {reference_name}\n"
            f"Mean RGB: {_triplet(result.before.mean_rgb)}    Median RGB: {_triplet(result.before.median_rgb)}\n"
            f"R/G={_ratio(result.before.r_over_g)}  B/G={_ratio(result.before.b_over_g)}\n"
            f"HSV={_triplet(result.before.hsv_mean, 4)}    Lab={_triplet(result.before.lab_mean, 3)}\n"
            f"亮度={result.before.relative_luminance:.6f}  饱和度={result.before.saturation:.4f}\n\n"
            f"{_role_label(role)} · {target_name}\n"
            f"Mean RGB: {_triplet(result.after.mean_rgb)}    Median RGB: {_triplet(result.after.median_rgb)}\n"
            f"R/G={_ratio(result.after.r_over_g)}  B/G={_ratio(result.after.b_over_g)}\n"
            f"HSV={_triplet(result.after.hsv_mean, 4)}    Lab={_triplet(result.after.lab_mean, 3)}\n"
            f"亮度={result.after.relative_luminance:.6f}  饱和度={result.after.saturation:.4f}\n\n"
            "Delta\n"
            f"ΔR / ΔG / ΔB: {_triplet(result.delta_rgb)}\n"
            f"ΔR% / ΔG% / ΔB%: {' / '.join(percent)}\n"
            f"ΔR/G={_ratio(result.delta_r_over_g)}  ΔB/G={_ratio(result.delta_b_over_g)}\n"
            f"ΔH / ΔS / ΔV: {_triplet(result.delta_hsv, 4)}\n"
            f"ΔL* / Δa* / Δb*: {_triplet(result.delta_lab, 3)}\n"
            f"Δ亮度={result.delta_luminance:+.6f}  Δ饱和度={result.delta_saturation:+.4f}\n"
            f"结论门禁: {'允许保守解释' if result.reliable else '低置信度，仅显示原始数值'}"
        )

    def _refresh_histogram(self) -> None:
        selected_label = self.histogram_role_var.get()
        role = next((item for item in self.active_roles if _role_label(item) == selected_label), "before")
        image_data = self.images.get(role)
        stats = self.roi_statistics.get(role)
        if self.histogram_scope_var.get() == "当前 ROI":
            histogram = None if stats is None else stats.histogram
            scope = f"{_role_label(role)} 当前 ROI" if stats is not None else f"{_role_label(role)} 尚无 ROI"
        else:
            histogram = None if image_data is None else image_data.histogram
            scope = f"{_role_label(role)} 整图" if image_data is not None else f"{_role_label(role)} 尚无图片"
        self.histogram_canvas.set_histogram(histogram, scope)

    def fit_images(self) -> None:
        for role in self.active_roles:
            self.views[role].fit()

    def one_to_one(self) -> None:
        for role in self.active_roles:
            self.views[role].one_to_one()

    def zoom_in(self) -> None:
        for role in self.active_roles:
            self.views[role].zoom_by(1.25)

    def zoom_out(self) -> None:
        for role in self.active_roles:
            self.views[role].zoom_by(1.0 / 1.25)

    def _on_neutral_mode_changed(self) -> None:
        self._settings_changed()
        self._refresh_outputs()

    def _on_histogram_visibility_changed(self) -> None:
        if self.show_histogram_var.get():
            self.notebook.add(self.histogram_tab, text="RGB 直方图")
            self.notebook.insert(2, self.histogram_tab)
        else:
            self.notebook.hide(self.histogram_tab)
        self._settings_changed()

    def _settings_changed(self) -> None:
        self._persist_settings()

    def _persist_settings(self) -> None:
        if self._closed:
            return
        panel_ratio = self.settings.panel_ratio
        try:
            if len(self.viewer_pane.panes()) > 1 and self.viewer_pane.winfo_width() > 0:
                panel_ratio = self.viewer_pane.sashpos(0) / self.viewer_pane.winfo_width()
        except (AttributeError, tk.TclError):
            pass
        try:
            geometry = self.root.geometry()
        except tk.TclError:
            geometry = self.settings.window_geometry
        self.settings = ImageInspectorSettings(
            last_directory=self.last_directory,
            search_range=self._search_range(),
            match_threshold=self.settings.match_threshold,
            show_histogram=self.show_histogram_var.get(),
            live_pixel=self.live_pixel_var.get(),
            default_roi_name=self.settings.default_roi_name,
            neutral_mode=self.neutral_mode_var.get(),
            window_geometry=geometry,
            panel_ratio=panel_ratio,
            include_full_path=self.include_full_path_var.get(),
        ).validated()
        try:
            save_image_inspector_settings(self.settings)
        except OSError:
            LOGGER.exception("Unable to save Image Inspector settings")

    def export_current(self, path: Optional[str] = None) -> None:
        active_images = [self.images[role] for role in self.active_roles]
        active_stats = [self.roi_statistics[role] for role in self.active_roles]
        if active_images[0] is None or active_stats[0] is None:
            messagebox.showinfo("尚无可导出分析", "请先打开图片并框选一个 ROI。", parent=self.root)
            return
        if any(image is None for image in active_images) or any(stats is None for stats in active_stats):
            messagebox.showinfo("等待对比 ROI", "请等待所有图片加载和 ROI 匹配完成，或手动框选缺失的对比 ROI。", parent=self.root)
            return
        before_image = active_images[0]
        assert before_image is not None
        selected = path or filedialog.asksaveasfilename(
            title="导出图像分析 CSV",
            initialdir=self.last_directory or None,
            initialfile=f"{before_image.path.stem}_image_analysis.csv",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            confirmoverwrite=True,
        )
        if not selected:
            return
        try:
            destination = export_multi_csv(
                selected,
                [image for image in active_images if image is not None],
                [stats for stats in active_stats if stats is not None],
                matches=[self.match_results[role] for role in self.active_roles[1:]],
                comparisons=[self.comparisons[role] for role in self.active_roles[1:]],
                include_full_path=self.include_full_path_var.get(),
            )
        except OSError as exc:
            LOGGER.exception("CSV export failed")
            messagebox.showerror(
                "CSV 导出失败",
                f"无法写入文件，文件可能正被 Excel 占用或目录不可写：\n{exc}",
                parent=self.root,
            )
            return
        self.status_var.set(f"已导出 UTF-8 BOM CSV：{destination}")

    def show_help(self) -> None:
        backend = "OpenCV 灰度 NCC" if opencv_available() else "NumPy FFT 灰度 NCC 后备路径（OpenCV 未安装）"
        messagebox.showinfo(
            "图像分析器边界",
            "本工具通过文件夹浏览选择 1–4 张普通 JPG/JPEG/PNG/BMP/TIFF，检查最终 sRGB 像素。"
            "多图时第 1 张是参考图，其余图片分别与参考图比较。\n\n"
            f"自动匹配：{backend}；支持轻微平移、很小裁切以及轻微曝光/颜色变化。"
            "旋转、透视、大幅缩放、物体移动、遮挡或景深变化可能导致失败。\n\n"
            "低于配置阈值时只显示原始变化数值，禁止输出确定性颜色改善结论。"
            "结果不能直接解释为 RAW、AWB Gain、CCM、CV、SCE 或其他 ISP 模块参数。",
            parent=self.root,
        )

    def _show_about(self) -> None:
        if self.on_about is not None:
            self.on_about()
        else:
            show_about_dialog(self.root, application_icon_path())

    def _show_workbench_help(self) -> None:
        show_workbench_help(self.root)

    def is_alive(self) -> bool:
        try:
            return bool(self.outer.winfo_exists())
        except tk.TclError:
            return False

    def show(self) -> bool:
        if not self.is_alive():
            return False
        self._configure_styles()
        self._build_menu()
        self.root.title(WINDOW_TITLE)
        self.outer.pack(fill="both", expand=True)
        return True

    def hide(self) -> None:
        if self.is_alive():
            self._persist_settings()
            self.outer.pack_forget()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._persist_settings()
        self._closed = True
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        for future in tuple(self._futures):
            future.cancel()
        self._image_cache.clear()
        self._thumbnail_cache.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._load_executor.shutdown(wait=False, cancel_futures=True)
        self._thumbnail_executor.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        if self.on_close is not None:
            self.hide()
            self.on_close()
            return
        self.shutdown()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
