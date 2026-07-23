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
    ACTION_BLUE,
    DANGER,
    FONT_BODY,
    FONT_BODY_BOLD,
    FONT_CARD_TITLE,
    FONT_MONO,
    FONT_NAV_SECTION,
    FONT_SMALL,
    FONT_TITLE,
    INFO_BG,
    INK,
    MUTED,
    PANEL_BG,
    SEPARATOR,
    SIDEBAR_BG,
    SUBTLE_SEPARATOR,
    SUCCESS,
    TERTIARY,
    WARNING,
    WINDOW_BG,
    configure_macos_theme,
    default_sources_directory,
    elide_canvas_text,
    fit_window_to_screen,
)
from ..updates import update_controller_for
from .browser import (
    ImageFolderError,
    ThumbnailData,
    discover_images,
    load_thumbnail,
    selected_paths_in_folder_order,
)
from .cache import ImageDataCache
from .constants import (
    MATCH_SEARCH_RANGES,
    MIN_ROI_SIDE,
    MOTION_THROTTLE_MS,
    RENDER_THROTTLE_MS,
    SUPPORTED_EXTENSIONS,
)
from .settings import ImageInspectorSettings, load_image_inspector_settings, save_image_inspector_settings
from .types import ComparisonResult, ImageData, MatchResult, PixelMetrics, ROI, ROIStatistics


LOGGER = logging.getLogger(__name__)
CORE_DEPENDENCY_ERROR: Optional[ImportError] = None
try:
    import numpy as np
    from PIL import Image, ImageTk

    from .export import export_multi_csv
    from .matching import MatchingError, confirm_match, manual_match, match_roi, opencv_available
    from .model import (
        ImageInspectorError,
        analyse_roi,
        compare_statistics,
        load_image,
        pixel_metrics,
        reorient_image,
    )
except ImportError as exc:  # Keep the main TuneLab app importable without image extras.
    CORE_DEPENDENCY_ERROR = exc
    np = None  # type: ignore[assignment]
    Image = ImageTk = None  # type: ignore[assignment]

from .renaming import BatchRenameError, RenameItem, build_rename_plan, execute_rename_plan


WINDOW_TITLE = "TuneLab · 图像分析器"
BG = WINDOW_BG
PANEL = PANEL_BG
BLUE = ACTION_BLUE
GREEN = SUCCESS
AMBER = WARNING
RED = DANGER
BORDER = SEPARATOR
CANVAS_BG = "#1C1C1E"
IMAGE_ROLES = ("before", "after", "compare3", "compare4")
COMPARISON_ROLES = IMAGE_ROLES[1:]
MIN_ANALYSIS_PANEL_RATIO = 0.20
MAX_ANALYSIS_PANEL_RATIO = 0.27
EMPTY_WORKSPACE_STATUS = (
    "请打开 1–4 张图片，或从左侧图库选择；滚轮联动缩放，右键图片可旋转或镜像。"
)
IMAGE_FILE_TYPES = [
    ("图片", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.heic *.heif"),
    ("所有文件", "*.*"),
]
ORIENTATION_LABELS = {
    "rotate_left": "向左旋转 90°",
    "rotate_right": "向右旋转 90°",
    "flip_horizontal": "水平镜像",
    "flip_vertical": "垂直镜像",
}
INVERSE_ORIENTATION = {
    "rotate_left": "rotate_right",
    "rotate_right": "rotate_left",
    "flip_horizontal": "flip_horizontal",
    "flip_vertical": "flip_vertical",
}


def _role_label(role: str) -> str:
    try:
        index = IMAGE_ROLES.index(role)
    except ValueError:
        return role
    return f"图像 {index + 1}"


def _ratio(value: Optional[float]) -> str:
    return "N/A（分母为 0）" if value is None else f"{value:.4f}"


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
        zoom_callback: Optional[
            Callable[[str, float, float, float, float, float], None]
        ] = None,
        context_callback: Optional[Callable[[str, int, int], None]] = None,
        open_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(master, style="InspectorImage.TFrame")
        self.role = role
        self.pixel_callback = pixel_callback
        self.roi_callback = roi_callback
        self.live_enabled = live_enabled
        self.zoom_callback = zoom_callback
        self.context_callback = context_callback
        self.open_callback = open_callback
        self.image_data: Optional[ImageData] = None
        # Keep only the shared NumPy pixels as a render source.  A full Pillow
        # RGB image is a second full-resolution allocation (about 57 MB for one
        # 4928×3840 frame) and multiplied that cost by every visible viewport.
        self._render_pixels: Optional[Any] = None
        self._photo: Optional[Any] = None
        self._photo_key: Optional[Tuple[Any, ...]] = None
        self._render_after_id: Optional[str] = None
        self._motion_after_id: Optional[str] = None
        self._initial_fit_after_id: Optional[str] = None
        self._motion_position: Optional[Tuple[int, int]] = None
        self._needs_initial_fit = False
        self._fit_mode = True
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.roi: Optional[ROI] = None
        self.roi_colour = "#30D158"
        self.sample_point: Optional[Tuple[int, int]] = None
        self._selection_image_start: Optional[Tuple[float, float]] = None
        self._selection_canvas_start: Optional[Tuple[float, float]] = None
        self._pan_start: Optional[Tuple[float, float, float, float]] = None
        self._context_press_position: Optional[Tuple[float, float]] = None
        self._context_dragged = False

        self.title_var = tk.StringVar(value=_role_label(role))
        self.meta_var = tk.StringVar(value="")
        self.zoom_var = tk.StringVar(value="缩放 —")

        self.canvas = tk.Canvas(self, background=CANVAS_BG, highlightthickness=0, cursor="crosshair")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.horizontal = ttk.Scrollbar(
            self,
            orient="horizontal",
            command=self._xview,
            style="Inspector.Horizontal.TScrollbar",
        )
        self.horizontal.grid(row=1, column=0, sticky="ew")
        self.vertical = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self._yview,
            style="Inspector.Vertical.TScrollbar",
        )
        self.vertical.grid(row=0, column=1, sticky="ns")
        self.horizontal.grid_remove()
        self.vertical.grid_remove()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<ButtonPress-1>", self._on_left_press)
        self.canvas.bind("<B1-Motion>", self._on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_release)
        self.canvas.bind("<Shift-ButtonPress-1>", self._on_pan_press)
        self.canvas.bind("<Shift-B1-Motion>", self._on_pan_drag)
        self.canvas.bind("<Shift-ButtonRelease-1>", self._on_pan_release)
        # Tk/Aqua has reported a physical secondary click as either button 2
        # or 3 across Tk and macOS releases.  Treat both identically: a click
        # opens the image menu and a drag pans.  This also preserves middle- or
        # right-drag panning on Windows/Linux.
        for context_button in (2, 3):
            self.canvas.bind(f"<ButtonPress-{context_button}>", self._on_context_press)
            self.canvas.bind(f"<B{context_button}-Motion>", self._on_context_drag)
            self.canvas.bind(f"<ButtonRelease-{context_button}>", self._on_context_release)
        self.canvas.bind("<Control-ButtonPress-1>", self._on_context_press)
        self.canvas.bind("<Control-B1-Motion>", self._on_context_drag)
        self.canvas.bind("<Control-ButtonRelease-1>", self._on_context_release)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        if tk.TkVersion >= 9.0:
            self.canvas.bind("<TouchpadScroll>", self._on_touchpad_scroll)
        self.canvas.bind("<Button-4>", lambda event: self._zoom_at(event.x, event.y, 1.15))
        self.canvas.bind("<Button-5>", lambda event: self._zoom_at(event.x, event.y, 1.0 / 1.15))
        self.canvas.bind("<Destroy>", self._on_destroy)

    def set_title(self, title: str) -> None:
        self.title_var.set(title)
        self._schedule_render()

    def set_image(self, image_data: ImageData) -> None:
        self.image_data = image_data
        self.canvas.configure(cursor="crosshair")
        self._render_pixels = (
            image_data.render_preview if image_data.render_preview is not None else image_data.display_rgb
        )
        self._photo_key = None
        precision = "保留原始精度" if image_data.precision_preserved else "解码后为 8-bit"
        exif = " · EXIF 已转正" if image_data.orientation_applied else ""
        self.meta_var.set(
            f"{image_data.width}×{image_data.height} · {image_data.bit_depth}-bit · {image_data.source_mode} · {precision}{exif}"
        )
        self.roi = None
        self.sample_point = None
        self.request_initial_fit()
        self._update_zoom_label()

    def clear_image(self) -> None:
        self._cancel_initial_fit()
        self.image_data = None
        self._render_pixels = None
        self._photo = None
        self._photo_key = None
        self.roi = None
        self.sample_point = None
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._needs_initial_fit = False
        self._fit_mode = True
        self.meta_var.set("")
        self.zoom_var.set("缩放 —")
        self.canvas.delete("all")
        self._draw_overlays()
        self.horizontal.set(0.0, 1.0)
        self.vertical.set(0.0, 1.0)

    def suspend_rendering(self) -> None:
        """Release display-only buffers while the workspace is hidden."""

        if self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except tk.TclError:
                pass
            self._render_after_id = None
        self._cancel_initial_fit()
        self._photo = None
        self._render_pixels = None
        self._photo_key = None
        self.canvas.delete("rendered-image")

    def resume_rendering(self) -> None:
        """Recreate a render source from the retained analysis pixels."""

        if self.image_data is None or self._render_pixels is not None:
            return
        self._render_pixels = (
            self.image_data.render_preview
            if self.image_data.render_preview is not None
            else self.image_data.display_rgb
        )
        self.request_initial_fit()

    def _cancel_initial_fit(self) -> None:
        if self._initial_fit_after_id is not None:
            try:
                self.after_cancel(self._initial_fit_after_id)
            except tk.TclError:
                pass
            self._initial_fit_after_id = None

    def request_initial_fit(self) -> None:
        """Fit after the canvas geometry has stopped changing briefly."""

        self._needs_initial_fit = True
        self._fit_mode = True
        self._cancel_initial_fit()
        self._initial_fit_after_id = self.after(80, self._finish_initial_fit)

    def _finish_initial_fit(self) -> None:
        self._initial_fit_after_id = None
        self.fit()

    def set_roi(self, roi: Optional[ROI], *, colour: str = "#30D158") -> None:
        self.roi = roi
        self.roi_colour = colour
        self._draw_overlays()

    def set_sample_point(self, point: Optional[Tuple[int, int]]) -> None:
        self.sample_point = point
        self._draw_overlays()

    def fit(self) -> None:
        if self.image_data is None:
            return
        self._cancel_initial_fit()
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 16 or height <= 16:
            # A hidden/newly gridded canvas reports 1×1.  Treat that as a
            # deferred fit instead of committing an arbitrary 0.01% zoom.
            self._needs_initial_fit = True
            return
        self.zoom = max(0.0001, min((width - 16) / self.image_data.width, (height - 16) / self.image_data.height))
        self.pan_x = (width - self.image_data.width * self.zoom) / 2.0
        self.pan_y = (height - self.image_data.height * self.zoom) / 2.0
        self._needs_initial_fit = False
        self._fit_mode = True
        self._update_zoom_label()
        self._schedule_render(immediate=True)

    def one_to_one(self) -> None:
        if self.image_data is None:
            return
        self._cancel_initial_fit()
        self._needs_initial_fit = False
        self._fit_mode = False
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
        if self.image_data is None:
            self._draw_overlays()
            self._update_scrollbars()
            return
        if self._needs_initial_fit or self._fit_mode:
            self.request_initial_fit()
        else:
            self._clamp_pan()
            self._schedule_render()

    def _schedule_render(self, *, immediate: bool = False) -> None:
        if not self.winfo_exists():
            return
        if immediate and self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except tk.TclError:
                pass
            self._render_after_id = None
        if immediate:
            self._render()
        elif self._render_after_id is None:
            self._render_after_id = self.after(RENDER_THROTTLE_MS, self._render)

    def _render(self) -> None:
        self._render_after_id = None
        if not self.canvas.winfo_exists():
            return
        if self.image_data is None or self._render_pixels is None:
            self.canvas.delete("rendered-image")
            self._draw_overlays()
            self._update_scrollbars()
            return
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        x0 = max(0, int(math.floor(-self.pan_x / self.zoom)))
        y0 = max(0, int(math.floor(-self.pan_y / self.zoom)))
        x1 = min(self.image_data.width, int(math.ceil((canvas_width - self.pan_x) / self.zoom)))
        y1 = min(self.image_data.height, int(math.ceil((canvas_height - self.pan_y) / self.zoom)))
        if x1 > x0 and y1 > y0:
            preview = self.image_data.render_preview
            preview_scale = max(1e-9, float(self.image_data.render_preview_scale))
            use_preview = (
                preview is not None
                and preview is not self.image_data.display_rgb
                and self.zoom <= preview_scale * 1.15
            )
            render_pixels = preview if use_preview else self.image_data.display_rgb
            source_scale = preview_scale if use_preview else 1.0
            self._render_pixels = render_pixels
            source_height, source_width = render_pixels.shape[:2]
            source_x0 = max(0, min(source_width, int(math.floor(x0 * source_scale))))
            source_y0 = max(0, min(source_height, int(math.floor(y0 * source_scale))))
            source_x1 = max(source_x0, min(source_width, int(math.ceil(x1 * source_scale))))
            source_y1 = max(source_y0, min(source_height, int(math.ceil(y1 * source_scale))))
            original_x0 = source_x0 / source_scale
            original_y0 = source_y0 / source_scale
            original_x1 = source_x1 / source_scale
            original_y1 = source_y1 / source_scale
            target_width = max(1, int(round((original_x1 - original_x0) * self.zoom)))
            target_height = max(1, int(round((original_y1 - original_y0) * self.zoom)))
            photo_key = (
                id(self.image_data),
                id(render_pixels),
                source_x0,
                source_y0,
                source_x1,
                source_y1,
                target_width,
                target_height,
            )
            if photo_key != self._photo_key or self._photo is None:
                assert Image is not None and np is not None
                # Convert only the currently visible source rectangle to
                # Pillow.  At 1:1 or while panning this is bounded by the
                # viewport instead of duplicating the entire camera frame.
                crop_pixels = np.ascontiguousarray(
                    render_pixels[source_y0:source_y1, source_x0:source_x1],
                    dtype=np.uint8,
                )
                crop = Image.fromarray(crop_pixels, mode="RGB")
                if crop.size != (target_width, target_height):
                    assert Image is not None
                    resampling = Image.Resampling.NEAREST if self.zoom >= 4.0 else Image.Resampling.BILINEAR
                    crop = crop.resize((target_width, target_height), resampling)
                assert ImageTk is not None
                self._photo = ImageTk.PhotoImage(crop, master=self.canvas)
                self._photo_key = photo_key
            left, top = self.image_to_canvas(original_x0, original_y0)
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
        self.canvas.delete("viewer-chrome")
        if self.image_data is None:
            self.canvas.configure(cursor="hand2" if self.open_callback is not None else "arrow")
            width = max(1, self.canvas.winfo_width())
            height = max(1, self.canvas.winfo_height())
            centre_x = width / 2.0
            centre_y = height / 2.0
            self.canvas.create_oval(
                centre_x - 24,
                centre_y - 46,
                centre_x + 24,
                centre_y + 2,
                fill="#2C2C2E",
                outline="#48484A",
                width=1,
                tags=("viewer-chrome", "empty-open"),
            )
            self.canvas.create_text(
                centre_x,
                centre_y - 22,
                text="＋",
                fill="#D1D1D6",
                font=FONT_TITLE,
                tags=("viewer-chrome", "empty-open"),
            )
            self.canvas.create_text(
                centre_x,
                centre_y + 22,
                text="打开图片开始检查",
                fill="#D1D1D6",
                font=FONT_BODY_BOLD,
                tags=("viewer-chrome", "empty-open"),
            )
            self.canvas.create_text(
                centre_x,
                centre_y + 44,
                text="也可以打开文件夹后从图库选择",
                fill="#8E8E93",
                font=FONT_SMALL,
                tags=("viewer-chrome", "empty-open"),
            )
            return
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
            self.canvas.create_line(x - size, y, x + size, y, fill="#FFD60A", width=2, tags=("analysis-overlay",))
            self.canvas.create_line(x, y - size, x, y + size, fill="#FFD60A", width=2, tags=("analysis-overlay",))
        self._draw_viewer_chrome()

    def _draw_viewer_chrome(self) -> None:
        """Paint filename, image metadata and zoom as non-layout overlays."""

        if self.image_data is None:
            return

        width = max(1, self.canvas.winfo_width())
        reserved_zoom = 96
        maximum_text_width = max(40, width - reserved_zoom - 28)
        title = elide_canvas_text(
            self.canvas,
            self.title_var.get(),
            FONT_CARD_TITLE,
            maximum_text_width,
        )
        meta = elide_canvas_text(
            self.canvas,
            self.meta_var.get(),
            FONT_SMALL,
            maximum_text_width,
        )
        title_item = self.canvas.create_text(
            12,
            10,
            text=title,
            anchor="nw",
            fill="white",
            font=FONT_CARD_TITLE,
            tags=("viewer-chrome",),
        )
        meta_item = self.canvas.create_text(
            12,
            30,
            text=meta,
            anchor="nw",
            fill="#D1D1D6",
            font=FONT_SMALL,
            tags=("viewer-chrome",),
        )
        title_box = self.canvas.bbox(title_item) or (12, 10, 12, 10)
        meta_box = self.canvas.bbox(meta_item) or (12, 30, 12, 30)
        info_right = min(width - 8, max(title_box[2], meta_box[2]) + 7)
        info_background = self.canvas.create_rectangle(
            7,
            6,
            info_right,
            max(title_box[3], meta_box[3]) + 6,
            fill="#2C2C2E",
            outline="#545458",
            width=1,
            tags=("viewer-chrome",),
        )
        self.canvas.tag_lower(info_background, title_item)

        zoom_item = self.canvas.create_text(
            width - 12,
            10,
            text=self.zoom_var.get(),
            anchor="ne",
            fill="white",
            font=FONT_SMALL,
            tags=("viewer-chrome",),
        )
        zoom_box = self.canvas.bbox(zoom_item) or (width - 12, 10, width - 12, 10)
        zoom_background = self.canvas.create_rectangle(
            zoom_box[0] - 7,
            6,
            width - 7,
            zoom_box[3] + 6,
            fill="#2C2C2E",
            outline="#545458",
            width=1,
            tags=("viewer-chrome",),
        )
        self.canvas.tag_lower(zoom_background, zoom_item)
        self.canvas.tag_raise("viewer-chrome")

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
            self.horizontal.grid_remove()
            self.vertical.grid_remove()
            return
        canvas_width = max(1.0, float(self.canvas.winfo_width()))
        canvas_height = max(1.0, float(self.canvas.winfo_height()))
        scaled_width = self.image_data.width * self.zoom
        scaled_height = self.image_data.height * self.zoom
        if scaled_width <= canvas_width:
            self.horizontal.set(0.0, 1.0)
            self.horizontal.grid_remove()
        else:
            start = max(0.0, min(1.0, -self.pan_x / scaled_width))
            self.horizontal.set(start, min(1.0, start + canvas_width / scaled_width))
            self.horizontal.grid()
        if scaled_height <= canvas_height:
            self.vertical.set(0.0, 1.0)
            self.vertical.grid_remove()
        else:
            start = max(0.0, min(1.0, -self.pan_y / scaled_height))
            self.vertical.set(start, min(1.0, start + canvas_height / scaled_height))
            self.vertical.grid()

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

    def _on_touchpad_scroll(self, event: tk.Event) -> str:
        """Zoom smoothly for Tk 9 trackpad gestures on macOS and Windows."""

        raw_delta = getattr(event, "delta", 0)
        try:
            decoded = self.canvas.tk.call("tk::PreciseScrollDeltas", raw_delta)
            values = tuple(float(value) for value in self.canvas.tk.splitlist(decoded))
            delta_y = values[1] if len(values) >= 2 else values[0]
        except (tk.TclError, TypeError, ValueError, IndexError):
            delta_y = float(raw_delta or 0.0)
        if abs(delta_y) < 1e-12:
            return "break"
        steps = min(4.0, max(0.15, abs(delta_y) / 40.0))
        factor = 1.15 ** steps if delta_y > 0 else (1.0 / 1.15) ** steps
        return self._zoom_at(event.x, event.y, factor)

    def _zoom_at(self, canvas_x: float, canvas_y: float, factor: float) -> str:
        if self.image_data is None:
            return "break"
        image_x = (canvas_x - self.pan_x) / self.zoom
        image_y = (canvas_y - self.pan_y) / self.zoom
        normalized_x = min(1.0, max(0.0, image_x / max(1, self.image_data.width)))
        normalized_y = min(1.0, max(0.0, image_y / max(1, self.image_data.height)))
        viewport_x = min(1.0, max(0.0, canvas_x / max(1, self.canvas.winfo_width())))
        viewport_y = min(1.0, max(0.0, canvas_y / max(1, self.canvas.winfo_height())))
        if self.zoom_callback is not None:
            self.zoom_callback(
                self.role,
                factor,
                normalized_x,
                normalized_y,
                viewport_x,
                viewport_y,
            )
            return "break"
        self.apply_linked_zoom(factor, normalized_x, normalized_y, viewport_x, viewport_y)
        return "break"

    def apply_linked_zoom(
        self,
        factor: float,
        normalized_x: float,
        normalized_y: float,
        viewport_x: float,
        viewport_y: float,
    ) -> None:
        """Apply one shared zoom gesture using normalized image coordinates."""

        if self.image_data is None:
            return
        self._cancel_initial_fit()
        self._needs_initial_fit = False
        self._fit_mode = False
        canvas_x = viewport_x * max(1, self.canvas.winfo_width())
        canvas_y = viewport_y * max(1, self.canvas.winfo_height())
        image_x = normalized_x * self.image_data.width
        image_y = normalized_y * self.image_data.height
        new_zoom = min(32.0, max(0.0001, self.zoom * factor))
        self.pan_x = canvas_x - image_x * new_zoom
        self.pan_y = canvas_y - image_y * new_zoom
        self.zoom = new_zoom
        self._clamp_pan()
        self._update_zoom_label()
        self._schedule_render()

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
        if self.image_data is None:
            if self.open_callback is not None:
                self.open_callback()
            return "break"
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
            outline="#0A84FF",
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
        self._cancel_initial_fit()
        self._needs_initial_fit = False
        self._fit_mode = False
        self._pan_start = (event.x, event.y, self.pan_x, self.pan_y)
        self.canvas.configure(cursor="fleur")
        self._selection_image_start = None
        self._selection_canvas_start = None
        self.canvas.delete("selection-preview")
        return "break"

    def _on_pan_drag(self, event: tk.Event) -> str:
        if self._pan_start is None:
            return "break"
        previous_pan_x = self.pan_x
        previous_pan_y = self.pan_y
        self.pan_x = self._pan_start[2] + event.x - self._pan_start[0]
        self.pan_y = self._pan_start[3] + event.y - self._pan_start[1]
        self._clamp_pan()
        delta_x = self.pan_x - previous_pan_x
        delta_y = self.pan_y - previous_pan_y
        if delta_x or delta_y:
            # Move the existing tile immediately so the pointer never outruns
            # the image.  The coalesced 60 Hz render then refreshes exposed
            # edges and the exact high-quality crop in the background.
            self.canvas.move("rendered-image", delta_x, delta_y)
            self.canvas.move("analysis-overlay", delta_x, delta_y)
        self._schedule_render()
        return "break"

    def _on_pan_release(self, _event: tk.Event) -> str:
        self._pan_start = None
        self.canvas.configure(cursor="crosshair")
        return "break"

    def _on_context_press(self, event: tk.Event) -> str:
        self._context_press_position = (float(event.x), float(event.y))
        self._context_dragged = False
        return self._on_pan_press(event)

    def _on_context_drag(self, event: tk.Event) -> str:
        if self._context_press_position is None:
            return "break"
        distance = math.hypot(
            float(event.x) - self._context_press_position[0],
            float(event.y) - self._context_press_position[1],
        )
        if not self._context_dragged and distance < 4.0:
            return "break"
        self._context_dragged = True
        return self._on_pan_drag(event)

    def _on_context_release(self, event: tk.Event) -> str:
        dragged = self._context_dragged
        self._context_press_position = None
        self._context_dragged = False
        self._on_pan_release(event)
        if not dragged and self.image_data is not None and self.context_callback is not None:
            self.context_callback(self.role, int(event.x_root), int(event.y_root))
        return "break"

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self.canvas:
            return
        for after_id in (self._render_after_id, self._motion_after_id, self._initial_fit_after_id):
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
        self._render_after_id = None
        self._motion_after_id = None
        self._initial_fit_after_id = None
        # ImageTk objects must be released by the Tk/main thread.  Keeping a
        # dead PhotoImage on a workspace cycle lets a later worker-thread GC
        # trigger Tcl_AsyncDelete on macOS during rapid close/reopen.
        self._photo = None
        self._render_pixels = None
        self._photo_key = None


class FolderThumbnailStrip(ttk.Frame):
    """macOS-style vertical file browser with lazy thumbnail requests."""

    PREVIEW_SIZE = (78, 58)
    CARD_WIDTH = 232
    CARD_HEIGHT = 78
    MAX_CACHED_THUMBNAILS = 72

    def __init__(
        self,
        master: tk.Misc,
        *,
        select_callback: Callable[[Path, bool, bool], None],
        request_callback: Callable[[Sequence[Tuple[int, Path]]], None],
        collapse_callback: Callable[[], None],
        context_callback: Optional[Callable[[Path, int, int], None]] = None,
    ) -> None:
        super().__init__(master, padding=(8, 8), style="InspectorSidebar.TFrame")
        self.select_callback = select_callback
        self.request_callback = request_callback
        self.collapse_callback = collapse_callback
        self.context_callback = context_callback
        self.paths: list[Path] = []
        self.active_paths: set[Path] = set()
        self.cards: list[tk.Canvas] = []
        self._photos: "OrderedDict[int, Any]" = OrderedDict()
        self._requested: set[int] = set()
        self._request_after_id: Optional[str] = None

        header = ttk.Frame(self, style="InspectorSidebar.TFrame")
        header.pack(fill="x", pady=(1, 8))
        ttk.Label(header, text="文件", style="InspectorSidebarTitle.TLabel").pack(side="left")
        self.count_var = tk.StringVar(value="0 张")
        ttk.Button(
            header,
            text="‹",
            width=3,
            command=self.collapse_callback,
            style="Icon.TButton",
        ).pack(side="right")
        ttk.Label(header, textvariable=self.count_var, style="InspectorSidebarMuted.TLabel").pack(
            side="right", padx=(0, 5)
        )

        body = ttk.Frame(self, style="InspectorSidebar.TFrame")
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            body,
            width=self.CARD_WIDTH + 14,
            background=SIDEBAR_BG,
            highlightthickness=0,
        )
        self.scrollbar = ttk.Scrollbar(
            body,
            orient="vertical",
            command=self._yview,
            style="InspectorSidebar.Vertical.TScrollbar",
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(3, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self.inner = ttk.Frame(self.canvas, style="InspectorSidebar.TFrame")
        self._window_item = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda _event: self._scroll_units(-2))
        self.canvas.bind("<Button-5>", lambda _event: self._scroll_units(2))
        if tk.TkVersion >= 9.0:
            self.canvas.bind("<TouchpadScroll>", self._on_touchpad_scroll)
        ttk.Label(
            self,
            text="点按单选 · ⌘/Ctrl 多选",
            style="InspectorSidebarMuted.TLabel",
            anchor="center",
        ).pack(fill="x", pady=(6, 0))

    @staticmethod
    def _rounded_rectangle(canvas: tk.Canvas, width: int, height: int) -> int:
        radius = 11
        points = (
            5 + radius, 4,
            width - 5 - radius, 4,
            width - 5, 4,
            width - 5, 4 + radius,
            width - 5, height - 4 - radius,
            width - 5, height - 4,
            width - 5 - radius, height - 4,
            5 + radius, height - 4,
            5, height - 4,
            5, height - 4 - radius,
            5, 4 + radius,
            5, 4,
        )
        return canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=24,
            fill=SIDEBAR_BG,
            outline="",
            tags=("selection-background",),
        )

    def set_paths(self, paths: Sequence[Path]) -> None:
        self.paths = [Path(path) for path in paths]
        self._photos.clear()
        self._requested.clear()
        for child in self.inner.winfo_children():
            child.destroy()
        self.cards = []
        self.count_var.set(f"{len(self.paths)} 张")
        for index, path in enumerate(self.paths):
            card = tk.Canvas(
                self.inner,
                width=self.CARD_WIDTH,
                height=self.CARD_HEIGHT,
                background=SIDEBAR_BG,
                highlightthickness=0,
                cursor="hand2",
            )
            card.grid(row=index, column=0, sticky="w", padx=1, pady=1)
            self._rounded_rectangle(card, self.CARD_WIDTH, self.CARD_HEIGHT)
            card.create_rectangle(
                11,
                10,
                11 + self.PREVIEW_SIZE[0],
                10 + self.PREVIEW_SIZE[1],
                fill="#D8D8DC",
                outline=SUBTLE_SEPARATOR,
                tags=("thumbnail-placeholder",),
            )
            card.create_text(
                11 + self.PREVIEW_SIZE[0] / 2,
                10 + self.PREVIEW_SIZE[1] / 2,
                text="载入中…",
                fill=MUTED,
                font=FONT_SMALL,
                tags=("thumbnail-placeholder",),
            )
            card.create_text(
                99,
                19,
                text=elide_canvas_text(card, path.name, FONT_BODY_BOLD, self.CARD_WIDTH - 108),
                anchor="nw",
                fill=INK,
                font=FONT_BODY_BOLD,
                tags=("thumbnail-filename",),
            )
            card.create_text(
                99,
                44,
                text="读取尺寸中…",
                anchor="nw",
                fill=MUTED,
                font=FONT_SMALL,
                tags=("thumbnail-meta",),
            )
            card.bind(
                "<Button-1>",
                lambda event, selected=path: self._select_path(event, selected),
            )
            for button in (2, 3):
                card.bind(
                    f"<ButtonRelease-{button}>",
                    lambda event, selected=path: self._show_context(event, selected),
                )
            card.bind(
                "<Control-ButtonRelease-1>",
                lambda event, selected=path: self._show_context(event, selected),
            )
            card.bind("<MouseWheel>", self._on_mousewheel)
            card.bind("<Button-4>", lambda _event: self._scroll_units(-2))
            card.bind("<Button-5>", lambda _event: self._scroll_units(2))
            if tk.TkVersion >= 9.0:
                card.bind("<TouchpadScroll>", self._on_touchpad_scroll)
            self.cards.append(card)
        self.canvas.yview_moveto(0.0)
        self.after_idle(self._schedule_visible_request)

    def _select_path(self, event: tk.Event, path: Path) -> str:
        state = int(getattr(event, "state", 0))
        shift = bool(state & 0x0001)
        # Control is conventional on Windows/Linux; Aqua reports Command as
        # Mod2. Supporting both keeps the macOS selection model cross-platform.
        additive = bool(state & 0x0004 or state & 0x0010)
        self.select_callback(path, additive, shift)
        return "break"

    def _show_context(self, event: tk.Event, path: Path) -> str:
        if path not in self.active_paths:
            self.select_callback(path, False, False)
        if self.context_callback is not None:
            self.context_callback(path, int(event.x_root), int(event.y_root))
        return "break"

    def set_active_paths(self, paths: Sequence[Path]) -> None:
        active = {Path(path) for path in paths}
        self.active_paths = active
        first_active: Optional[int] = None
        for index, card in enumerate(self.cards):
            selected = self.paths[index] in active
            card.itemconfigure("selection-background", fill=BLUE if selected else SIDEBAR_BG)
            card.itemconfigure("thumbnail-filename", fill="white" if selected else INK)
            card.itemconfigure("thumbnail-meta", fill="#E9F3FF" if selected else MUTED)
            if selected and first_active is None:
                first_active = index
        if first_active is not None:
            self.after_idle(lambda index=first_active: self._reveal_index(index))

    def _reveal_index(self, index: int) -> None:
        if not (0 <= index < len(self.cards)):
            return
        top = index * (self.CARD_HEIGHT + 2)
        bottom = top + self.CARD_HEIGHT + 2
        viewport_top = float(self.canvas.canvasy(0))
        viewport_bottom = viewport_top + max(1, self.canvas.winfo_height())
        total_height = max(1, self.inner.winfo_reqheight())
        if top < viewport_top:
            self.canvas.yview_moveto(top / total_height)
        elif bottom > viewport_bottom:
            self.canvas.yview_moveto(max(0.0, (bottom - self.canvas.winfo_height()) / total_height))
        self._schedule_visible_request()

    def apply_thumbnail(self, index: int, thumbnail: ThumbnailData) -> None:
        if not (0 <= index < len(self.cards)) or self.paths[index] != thumbnail.path:
            return
        assert ImageTk is not None
        photo = ImageTk.PhotoImage(thumbnail.image, master=self.cards[index])
        self._photos.pop(index, None)
        self._photos[index] = photo
        card = self.cards[index]
        card.delete("thumbnail-placeholder")
        card.delete("thumbnail-image")
        card.create_image(
            11 + self.PREVIEW_SIZE[0] / 2,
            10 + self.PREVIEW_SIZE[1] / 2,
            image=photo,
            tags=("thumbnail-image",),
        )
        card.itemconfigure(
            "thumbnail-meta",
            text=f"{thumbnail.source_size[0]} × {thumbnail.source_size[1]}",
        )
        card.tag_raise("thumbnail-image", "selection-background")
        card.tag_raise("thumbnail-filename")
        card.tag_raise("thumbnail-meta")
        while len(self._photos) > self.MAX_CACHED_THUMBNAILS:
            evicted, _photo = self._photos.popitem(last=False)
            if 0 <= evicted < len(self.cards):
                evicted_card = self.cards[evicted]
                evicted_card.delete("thumbnail-image")
                evicted_card.delete("thumbnail-placeholder")
                evicted_card.create_rectangle(
                    11,
                    10,
                    11 + self.PREVIEW_SIZE[0],
                    10 + self.PREVIEW_SIZE[1],
                    fill="#D8D8DC",
                    outline=SUBTLE_SEPARATOR,
                    tags=("thumbnail-placeholder",),
                )
                evicted_card.create_text(
                    11 + self.PREVIEW_SIZE[0] / 2,
                    10 + self.PREVIEW_SIZE[1] / 2,
                    text="载入中…",
                    fill=MUTED,
                    font=FONT_SMALL,
                    tags=("thumbnail-placeholder",),
                )
                self._requested.discard(evicted)

    def mark_thumbnail_failed(self, index: int) -> None:
        if 0 <= index < len(self.cards):
            card = self.cards[index]
            card.delete("thumbnail-placeholder")
            card.create_text(
                11 + self.PREVIEW_SIZE[0] / 2,
                10 + self.PREVIEW_SIZE[1] / 2,
                text="无法预览",
                fill=RED,
                font=FONT_SMALL,
                tags=("thumbnail-placeholder",),
            )

    def _on_inner_configure(self, _event: tk.Event) -> None:
        bounds = self.canvas.bbox(self._window_item)
        if bounds is not None:
            self.canvas.configure(scrollregion=bounds)
        self._schedule_visible_request()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window_item, width=max(self.CARD_WIDTH + 4, event.width))
        self._schedule_visible_request()

    def _yview(self, *args: str) -> None:
        self.canvas.yview(*args)
        self._schedule_visible_request()

    def _scroll_units(self, units: int) -> str:
        self.canvas.yview_scroll(units, "units")
        self._schedule_visible_request()
        return "break"

    def _scroll_by_pixels(self, pixels: float) -> str:
        bounds = self.canvas.bbox("all")
        if bounds is None:
            return "break"
        content_height = max(1.0, float(bounds[3] - bounds[1]))
        viewport_height = max(1.0, float(self.canvas.winfo_height()))
        if content_height <= viewport_height:
            return "break"
        current = float(self.canvas.yview()[0])
        maximum = max(0.0, 1.0 - viewport_height / content_height)
        target = min(maximum, max(0.0, current + float(pixels) / content_height))
        self.canvas.yview_moveto(target)
        self._schedule_visible_request()
        return "break"

    def _on_mousewheel(self, event: tk.Event) -> str:
        delta = float(getattr(event, "delta", 0.0) or 0.0)
        if abs(delta) >= 120.0:
            return self._scroll_by_pixels(-delta / 120.0 * 56.0)
        if abs(delta) > 1e-12:
            return self._scroll_by_pixels(-delta * 3.0)
        return "break"

    def _on_touchpad_scroll(self, event: tk.Event) -> str:
        raw_delta = getattr(event, "delta", 0)
        try:
            decoded = self.canvas.tk.call("tk::PreciseScrollDeltas", raw_delta)
            values = tuple(float(value) for value in self.canvas.tk.splitlist(decoded))
            delta_y = values[1] if len(values) >= 2 else values[0]
        except (tk.TclError, TypeError, ValueError, IndexError):
            delta_y = float(raw_delta or 0.0)
        if abs(delta_y) < 1e-12:
            return "break"
        return self._scroll_by_pixels(-delta_y)

    def _schedule_visible_request(self) -> None:
        if self._request_after_id is not None:
            try:
                self.after_cancel(self._request_after_id)
            except tk.TclError:
                pass
        self._request_after_id = self.after_idle(self._request_visible)

    def _request_visible(self) -> None:
        self._request_after_id = None
        if not self.paths:
            return
        top = max(0.0, float(self.canvas.canvasy(0)))
        bottom = top + max(1, self.canvas.winfo_height())
        first = max(0, int(top // (self.CARD_HEIGHT + 2)) - 3)
        last = min(len(self.paths), int(math.ceil(bottom / (self.CARD_HEIGHT + 2))) + 4)
        for index in range(first, last):
            if index in self._photos:
                self._photos.move_to_end(index)
        pending = [
            (index, self.paths[index])
            for index in range(first, last)
            if index not in self._requested
        ]
        if pending:
            self._requested.update(index for index, _path in pending)
            self.request_callback(pending)

    def shutdown(self) -> None:
        if self._request_after_id is not None:
            try:
                self.after_cancel(self._request_after_id)
            except tk.TclError:
                pass
            self._request_after_id = None
        self._photos.clear()
        self._requested.clear()
        for child in self.inner.winfo_children():
            child.destroy()
        self.cards.clear()
        self.paths.clear()
        self.active_paths.clear()


class HistogramCanvas(ttk.Frame):
    def __init__(self, master: tk.Misc, *, luminance: bool = False) -> None:
        super().__init__(master, style="InspectorCard.TFrame")
        self.luminance = luminance
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
        title = "亮度直方图" if self.luminance else "RGB 直方图"
        self.canvas.create_text(12, 10, text=f"{title} · {self.scope}", anchor="nw", fill=INK, font=FONT_CARD_TITLE)
        left, top, right, bottom = 42, 42, width - 16, height - 28
        self.canvas.create_rectangle(left, top, right, bottom, outline=BORDER)
        if self.histogram is None or right <= left or bottom <= top:
            self.canvas.create_text(width / 2, height / 2, text="打开图片或选择 ROI 后显示", fill=MUTED, font=FONT_BODY)
            return
        values = np.log1p(np.asarray(self.histogram, dtype=np.float64))
        maximum = float(np.max(values))
        if maximum <= 0:
            return
        plots = ((values.reshape(-1), INK),) if self.luminance else tuple(
            (values[channel], colour)
            for channel, colour in enumerate(("#FF453A", "#30D158", "#0A84FF"))
        )
        for channel_values, colour in plots:
            points = []
            for index in range(256):
                x = left + index / 255.0 * (right - left)
                y = bottom - channel_values[index] / maximum * (bottom - top)
                points.extend((x, y))
            self.canvas.create_line(*points, fill=colour, width=2)
        self.canvas.create_text(left, bottom + 7, text="0", anchor="nw", fill=MUTED, font=FONT_SMALL)
        self.canvas.create_text(right, bottom + 7, text="255", anchor="ne", fill=MUTED, font=FONT_SMALL)


class BatchRenameDialog:
    """Live-preview batch rename sheet modelled after Finder's rename panel."""

    def __init__(
        self,
        parent: tk.Misc,
        paths: Sequence[Path],
        *,
        initially_selected: Optional[Sequence[Path]] = None,
        on_complete: Callable[[dict[Path, Path]], None],
    ) -> None:
        self.parent = parent
        self.paths: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            resolved = Path(path).resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            self.paths.append(resolved)
        selected = self.paths if initially_selected is None else [Path(path).resolve() for path in initially_selected]
        self.selected_paths = {path for path in selected if path in seen}
        self.on_complete = on_complete
        self.plan: list[RenameItem] = []

        dialog = tk.Toplevel(parent)
        self.dialog = dialog
        dialog.title("批量重命名")
        dialog.transient(parent)
        dialog.resizable(True, True)
        dialog.minsize(680, 480)
        dialog.protocol("WM_DELETE_WINDOW", self.close)

        outer = ttk.Frame(dialog, padding=(22, 18), style="InspectorRoot.TFrame")
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="批量重命名", style="InspectorTitle.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="先选择要重命名的图片；使用 {n} 插入序号，使用 {name} 保留原名。扩展名始终保留。",
            style="InspectorSubtitle.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        form = ttk.Frame(outer, padding=(14, 12), style="InspectorCard.TFrame")
        form.pack(fill="x", pady=(0, 12))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="命名格式", style="InspectorCard.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.template_var = tk.StringVar(value="{n}_{name}")
        template_entry = ttk.Entry(form, textvariable=self.template_var)
        template_entry.grid(row=0, column=1, columnspan=3, sticky="ew")
        ttk.Label(form, text="起始序号", style="InspectorCard.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        self.start_var = tk.StringVar(value="1")
        ttk.Spinbox(form, from_=0, to=999999, textvariable=self.start_var, width=8).grid(
            row=1, column=1, sticky="w", pady=(10, 0)
        )
        ttk.Label(form, text="序号位数", style="InspectorCard.TLabel").grid(
            row=1, column=2, sticky="e", padx=(18, 8), pady=(10, 0)
        )
        self.digits_var = tk.StringVar(value=str(max(2, len(str(len(self.paths))))))
        ttk.Spinbox(form, from_=1, to=9, textvariable=self.digits_var, width=6).grid(
            row=1, column=3, sticky="e", pady=(10, 0)
        )

        preview_frame = ttk.Frame(outer, style="InspectorCard.TFrame")
        preview_frame.pack(fill="both", expand=True)
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(1, weight=1)
        selection_bar = ttk.Frame(preview_frame, style="InspectorCard.TFrame")
        selection_bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(9, 2))
        selection_bar.columnconfigure(0, weight=1)
        self.selection_var = tk.StringVar(value="")
        ttk.Label(selection_bar, textvariable=self.selection_var, style="InspectorMutedCard.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(selection_bar, text="全选", command=self.select_all, style="Quiet.TButton").grid(
            row=0, column=1, padx=(8, 4)
        )
        ttk.Button(selection_bar, text="清除选择", command=self.clear_selection, style="Quiet.TButton").grid(
            row=0, column=2
        )
        self.preview = ttk.Treeview(
            preview_frame,
            columns=("selected", "before", "after"),
            show="headings",
            selectmode="none",
            style="InspectorComparison.Treeview",
        )
        self.preview.heading("selected", text="选择")
        self.preview.heading("before", text="原文件名")
        self.preview.heading("after", text="新文件名")
        self.preview.column("selected", width=52, minwidth=52, stretch=False, anchor="center")
        self.preview.column("before", width=255, minwidth=150, anchor="w")
        self.preview.column("after", width=255, minwidth=150, anchor="w")
        self.preview.tag_configure("included", foreground=INK)
        self.preview.tag_configure("excluded", foreground=MUTED)
        self.preview.bind("<Button-1>", self._on_preview_click, add="+")
        scroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=scroll.set)
        self.preview.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(4, 10))
        scroll.grid(row=1, column=1, sticky="ns", padx=(0, 8), pady=(4, 10))

        footer = ttk.Frame(outer, style="InspectorRoot.TFrame")
        footer.pack(fill="x", pady=(12, 0))
        self.validation_var = tk.StringVar(value="")
        ttk.Label(footer, textvariable=self.validation_var, style="InspectorSubtitle.TLabel").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(footer, text="取消", command=self.close, style="Quiet.TButton").pack(side="right")
        self.rename_button = ttk.Button(footer, text="重命名", command=self.apply, style="Primary.TButton")
        self.rename_button.pack(side="right", padx=(0, 8))

        for variable in (self.template_var, self.start_var, self.digits_var):
            variable.trace_add("write", lambda *_args: self.refresh())
        dialog.bind("<Escape>", lambda _event: self.close())
        dialog.bind("<Return>", lambda _event: self.apply())
        self.refresh()
        template_entry.focus_set()
        dialog.after_idle(self._centre)
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def _centre(self) -> None:
        try:
            self.dialog.update_idletasks()
            width = max(680, self.dialog.winfo_reqwidth())
            height = max(480, self.dialog.winfo_reqheight())
            parent_x = self.parent.winfo_rootx()
            parent_y = self.parent.winfo_rooty()
            parent_width = self.parent.winfo_width()
            parent_height = self.parent.winfo_height()
            x = parent_x + max(0, (parent_width - width) // 2)
            y = parent_y + max(0, (parent_height - height) // 2)
            self.dialog.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            return

    def select_all(self) -> None:
        self.selected_paths = set(self.paths)
        self.refresh()

    def clear_selection(self) -> None:
        self.selected_paths.clear()
        self.refresh()

    def toggle_path(self, path: Path) -> None:
        resolved = Path(path).resolve()
        if resolved not in self.paths:
            return
        if resolved in self.selected_paths:
            self.selected_paths.remove(resolved)
        else:
            self.selected_paths.add(resolved)
        self.refresh()

    def _on_preview_click(self, event: tk.Event) -> Optional[str]:
        row = self.preview.identify_row(event.y)
        if not row:
            return None
        try:
            path = self.paths[int(row)]
        except (ValueError, IndexError):
            return None
        self.toggle_path(path)
        return "break"

    def _populate_preview(self, destinations: Optional[dict[Path, str]] = None, *, invalid: bool = False) -> None:
        position = self.preview.yview()[0] if self.preview.get_children() else 0.0
        for item in self.preview.get_children():
            self.preview.delete(item)
        destinations = destinations or {}
        for index, path in enumerate(self.paths):
            included = path in self.selected_paths
            if included:
                after = destinations.get(path, "命名格式有误" if invalid else path.name)
            else:
                after = "不重命名"
            self.preview.insert(
                "",
                "end",
                iid=str(index),
                values=("✓" if included else "", path.name, after),
                tags=("included" if included else "excluded",),
            )
        if position:
            self.preview.yview_moveto(position)

    def refresh(self) -> None:
        selected = [path for path in self.paths if path in self.selected_paths]
        self.selection_var.set(f"已选择 {len(selected)} / {len(self.paths)} 张 · 点击列表行勾选或取消")
        if not selected:
            self.plan = []
            self.validation_var.set("请至少选择一张图片。")
            self.rename_button.configure(state="disabled")
            self._populate_preview()
            return
        try:
            start = int(self.start_var.get())
            digits = int(self.digits_var.get())
            plan = build_rename_plan(selected, self.template_var.get(), start=start, digits=digits)
        except (BatchRenameError, ValueError) as exc:
            self.plan = []
            self.validation_var.set(str(exc))
            self.rename_button.configure(state="disabled")
            self._populate_preview(invalid=True)
            return
        self.plan = plan
        self.validation_var.set(f"将更改 {sum(item.changed for item in plan)} 个文件名")
        self.rename_button.configure(state="normal" if any(item.changed for item in plan) else "disabled")
        destinations = {item.source.resolve(): item.destination.name for item in plan}
        self._populate_preview(destinations)

    def apply(self) -> None:
        if not self.plan:
            return
        changed = sum(item.changed for item in self.plan)
        if changed == 0:
            return
        if not messagebox.askyesno(
            "确认批量重命名",
            f"将重命名 {changed} 张图片。图片内容和扩展名不会改变。\n\n是否继续？",
            parent=self.dialog,
        ):
            return
        try:
            mapping = execute_rename_plan(self.plan)
        except BatchRenameError as exc:
            messagebox.showerror("批量重命名失败", str(exc), parent=self.dialog)
            self.refresh()
            return
        self.close()
        self.on_complete(mapping)

    def close(self) -> None:
        try:
            self.dialog.grab_release()
        except tk.TclError:
            pass
        try:
            self.dialog.destroy()
        except tk.TclError:
            pass


class ImageInspectorWorkspace:
    def __init__(
        self,
        root: tk.Misc,
        *,
        on_close: Optional[Callable[[], None]] = None,
        on_home: Optional[Callable[[], None]] = None,
        on_gamma: Optional[Callable[[], object]] = None,
        on_colorchecker: Optional[Callable[[], object]] = None,
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
        self.on_colorchecker = on_colorchecker
        self.on_about = on_about
        self.settings = load_image_inspector_settings()
        self.last_directory = self.settings.last_directory
        self.active_roles: Tuple[str, ...] = (IMAGE_ROLES[0],)
        self.images: Dict[str, Optional[ImageData]] = {role: None for role in IMAGE_ROLES}
        self.rois: Dict[str, Optional[ROI]] = {role: None for role in IMAGE_ROLES}
        self.roi_statistics: Dict[str, Optional[ROIStatistics]] = {role: None for role in IMAGE_ROLES}
        self.fixed_pixels: Dict[str, Optional[PixelMetrics]] = {role: None for role in IMAGE_ROLES}
        self.match_results: Dict[str, Optional[MatchResult]] = {role: None for role in IMAGE_ROLES}
        self.comparisons: Dict[str, Optional[ComparisonResult]] = {role: None for role in COMPARISON_ROLES}
        self.roi_anchor_role: Optional[str] = None
        # Compatibility aliases for the first comparison while the public data
        # model remains pair-oriented.
        self.match_result: Optional[MatchResult] = None
        self.comparison: Optional[ComparisonResult] = None
        self.dual_mode = False
        self.folder_paths: list[Path] = []
        self.folder_groups: list[list[Path]] = []
        self.folder_group_index = 0
        self.current_paths: list[Path] = []
        self.folder_group_start = 0
        self.folder_group_size = len(IMAGE_ROLES)
        self.folder_group_mode = False
        self.folder_groups_aligned = True
        self.folder_selection_anchor: Optional[Path] = None
        self._folder_browser_available = False
        self._folder_thumbnail_generation = 0
        self._metric_mode = "none"
        self.image_transform_ops: Dict[str, list[str]] = {role: [] for role in IMAGE_ROLES}
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="TuneLabImage")
        # Decoding two 19 MP JPEGs concurrently briefly doubles codec,
        # histogram and channel-conversion buffers.  One dedicated decoder is
        # fast enough for the supplied camera frames and keeps first-open memory
        # comfortably below the system pressure threshold.
        self._load_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TuneLabDecode")
        self._result_queue: "queue.Queue[Tuple[str, int, str, Future[Any], Dict[str, Any]]]" = queue.Queue()
        self._futures = set()
        self._pending = 0
        self._load_tokens = {role: 0 for role in IMAGE_ROLES}
        self._analysis_tokens = {role: 0 for role in IMAGE_ROLES}
        self._match_tokens = {role: 0 for role in IMAGE_ROLES}
        self._matching_roles: set[str] = set()
        self._poll_after_id: Optional[str] = None
        self._polling_background = False
        self._fit_after_id: Optional[str] = None
        self._image_cache = ImageDataCache()
        self._batch_rename_dialog: Optional[BatchRenameDialog] = None
        self.status_var = tk.StringVar(value=EMPTY_WORKSPACE_STATUS)

        self._configure_styles()
        self._build_ui()
        self._install_shortcuts()
        self.root.title(WINDOW_TITLE)
        if self.on_close is None:
            self.window_placement = fit_window_to_screen(self.root, desired_width=1320, desired_height=820)
            try:
                self.root.protocol("WM_DELETE_WINDOW", self.close)
            except tk.TclError:
                pass
        if not opencv_available():
            self.status_var.set(
                "OpenCV 未能导入：像素与选区统计仍可使用，自动匹配暂用较慢的 NumPy FFT 后备路径。"
            )
        # Background completion polling starts on demand in _submit so an
        # idle or hidden inspector does not wake Tk twenty times per second.
        self._poll_after_id = None

    def _configure_styles(self) -> None:
        style = configure_macos_theme(self.root)
        style.configure("InspectorRoot.TFrame", background=BG)
        style.configure(
            "InspectorCard.TFrame",
            background=PANEL,
            relief="flat",
            borderwidth=0,
            bordercolor=SUBTLE_SEPARATOR,
            lightcolor=SUBTLE_SEPARATOR,
            darkcolor=SUBTLE_SEPARATOR,
        )
        style.configure("InspectorSurface.TFrame", background=PANEL)
        style.configure("InspectorSidebar.TFrame", background=SIDEBAR_BG, borderwidth=0)
        style.configure(
            "InspectorSidebarTitle.TLabel",
            background=SIDEBAR_BG,
            foreground=INK,
            font=FONT_CARD_TITLE,
        )
        style.configure(
            "InspectorSidebarMuted.TLabel",
            background=SIDEBAR_BG,
            foreground=MUTED,
            font=FONT_SMALL,
        )
        style.configure(
            "InspectorSidebar.Vertical.TScrollbar",
            width=8,
            arrowsize=0,
            background="#C7C7CC",
            troughcolor=SIDEBAR_BG,
            borderwidth=0,
        )
        style.configure("InspectorImage.TFrame", background=CANVAS_BG, borderwidth=0, relief="flat")
        style.configure("InspectorCard.TLabel", background=PANEL, foreground=INK, font=FONT_BODY)
        style.configure("InspectorMutedCard.TLabel", background=PANEL, foreground=MUTED, font=FONT_SMALL)
        style.configure("InspectorCardTitle.TLabel", background=PANEL, foreground=INK, font=FONT_CARD_TITLE)
        style.configure("InspectorToolbar.TFrame", background=PANEL, borderwidth=0)
        style.configure(
            "InspectorToolbar.TLabel",
            background=PANEL,
            foreground=INK,
            font=FONT_SMALL,
        )
        style.configure(
            "InspectorToolbarMuted.TLabel",
            background=PANEL,
            foreground=MUTED,
            font=FONT_SMALL,
        )
        style.configure("InspectorToolbar.TButton", padding=(7, 2))
        style.configure("InspectorToolbarIcon.TButton", padding=(4, 2))
        style.configure("InspectorToolbar.TEntry", padding=(6, 2))
        style.configure("InspectorToolbar.TCombobox", padding=(5, 2))
        primary_options = style.configure("Primary.TButton")
        if primary_options:
            style.configure("InspectorToolbarPrimary.TButton", **primary_options)
        style.configure("InspectorToolbarPrimary.TButton", padding=(9, 3))
        primary_map = style.map("Primary.TButton")
        if primary_map:
            style.map("InspectorToolbarPrimary.TButton", **primary_map)
        style.configure(
            "InspectorMetric.TLabel",
            background=PANEL,
            foreground=INK,
            font=FONT_SMALL,
            padding=(1, 0),
        )
        style.configure("InspectorTitle.TLabel", background=BG, foreground=INK, font=FONT_TITLE)
        style.configure("InspectorSubtitle.TLabel", background=BG, foreground=MUTED, font=FONT_BODY)
        style.configure("InspectorEyebrow.TLabel", background=BG, foreground=TERTIARY, font=FONT_NAV_SECTION)
        style.configure(
            "InspectorStatus.TLabel",
            background=PANEL,
            foreground=MUTED,
            padding=(0, 0),
            font=FONT_SMALL,
        )
        style.configure("InspectorMatchHigh.TLabel", background=PANEL, foreground=GREEN, font=FONT_BODY_BOLD)
        style.configure("InspectorMatchMedium.TLabel", background=PANEL, foreground=AMBER, font=FONT_BODY_BOLD)
        style.configure("InspectorMatchLow.TLabel", background=PANEL, foreground=RED, font=FONT_BODY_BOLD)
        style.configure("InspectorComparison.Treeview", rowheight=28, font=FONT_BODY)
        style.configure("InspectorComparison.Treeview.Heading", font=FONT_BODY_BOLD)
        style.configure(
            "Inspector.Vertical.TScrollbar",
            width=8,
            arrowsize=8,
            background="#545458",
            troughcolor=CANVAS_BG,
            borderwidth=0,
        )
        style.configure(
            "Inspector.Horizontal.TScrollbar",
            width=8,
            arrowsize=8,
            background="#545458",
            troughcolor=CANVAS_BG,
            borderwidth=0,
        )

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        self.file_menu = tk.Menu(menu, tearoff=False)
        try:
            aqua = self.root.tk.call("tk", "windowingsystem") == "aqua"
        except tk.TclError:
            aqua = False
        self.file_menu.add_command(
            label="打开图片...",
            command=self.open_images,
            accelerator="⌘O" if aqua else "Ctrl+O",
        )
        self.file_menu.add_command(
            label="打开图片文件夹...",
            command=self.open_folder,
            accelerator="⇧⌘O" if aqua else "Ctrl+Shift+O",
        )
        self.file_menu.add_command(label="重新载入当前组", command=self.load_selected_images)
        self.file_menu.add_command(label="批量重命名当前文件夹...", command=self.batch_rename_folder)
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
        self.view_menu.add_command(label="显示 / 隐藏图库", command=self.toggle_folder_sidebar)
        self.view_menu.add_command(label="显示 / 隐藏信息栏", command=self.toggle_analysis_sidebar)
        self.view_menu.add_separator()
        self.view_menu.add_command(label="上一组图片", command=self.show_previous_group)
        self.view_menu.add_command(label="下一组图片", command=self.show_next_group)
        menu.add_cascade(label="视图", menu=self.view_menu)

        self.analysis_menu = tk.Menu(menu, tearoff=False)
        self.analysis_menu.add_command(label="清除选区", command=self.clear_roi)
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
            tools_menu.add_command(label="CCM / ColorChecker 校正", command=self.on_close)
        if self.on_gamma is not None:
            tools_menu.add_command(label="Gamma 优化", command=self.on_gamma)
        if tools_menu.index("end") is not None:
            menu.add_cascade(label="工具", menu=tools_menu)

        self.help_menu = tk.Menu(menu, tearoff=False)
        self.help_menu.add_command(label="TuneLab 使用说明", command=self._show_workbench_help)
        self.help_menu.add_separator()
        self.help_menu.add_command(label="图像分析器边界", command=self.show_help)
        self.help_menu.add_separator()
        self.help_menu.add_command(label="检查更新...", command=self._check_for_updates)
        self.help_menu.add_command(label="关于 TuneLab", command=self._show_about)
        menu.add_cascade(label="帮助", menu=self.help_menu)
        self.root.configure(menu=menu)

    def _install_shortcuts(self) -> None:
        """Install the conventional image-viewer open shortcuts."""

        try:
            aqua = self.root.tk.call("tk", "windowingsystem") == "aqua"
        except tk.TclError:
            aqua = False
        modifier = "Command" if aqua else "Control"

        def invoke(action: Callable[[], None]) -> Callable[[tk.Event], Optional[str]]:
            def handler(_event: tk.Event) -> Optional[str]:
                if not self.outer.winfo_ismapped():
                    return None
                action()
                return "break"

            return handler

        self.root.bind(f"<{modifier}-o>", invoke(self.open_images), add="+")
        self.root.bind(f"<{modifier}-Shift-O>", invoke(self.open_folder), add="+")
        self.root.bind(f"<{modifier}-Shift-R>", invoke(self.batch_rename_folder), add="+")
        self.root.bind("<Alt-Left>", invoke(self.show_previous_group), add="+")
        self.root.bind("<Alt-Right>", invoke(self.show_next_group), add="+")

    def _build_ui(self) -> None:
        self.outer = ttk.Frame(self.root, padding=(10, 8), style="InspectorRoot.TFrame")
        self.outer.pack(fill="both", expand=True)

        self.top_bar = ttk.Frame(self.outer, style="InspectorToolbar.TFrame")
        self.top_bar.pack(fill="x", pady=(0, 5))

        toolbar = ttk.Frame(self.top_bar, padding=(6, 2), style="InspectorToolbar.TFrame")
        self.toolbar_panel = toolbar
        toolbar.pack(fill="x")
        ttk.Button(
            toolbar,
            text="打开…",
            command=self.open_images,
            style="InspectorToolbarPrimary.TButton",
        ).grid(row=0, column=0, padx=(0, 3))
        ttk.Button(
            toolbar,
            text="文件夹…",
            command=self.open_folder,
            style="InspectorToolbar.TButton",
        ).grid(row=0, column=1, padx=(0, 3))
        ttk.Button(
            toolbar,
            text="批量重命名…",
            command=self.batch_rename_folder,
            style="InspectorToolbar.TButton",
        ).grid(row=0, column=2, padx=(0, 7))
        ttk.Separator(toolbar, orient="vertical").grid(row=0, column=3, sticky="ns", padx=(0, 6))
        ttk.Button(
            toolbar,
            text="−",
            command=self.zoom_out,
            width=3,
            style="InspectorToolbarIcon.TButton",
        ).grid(row=0, column=4, padx=(0, 1))
        ttk.Button(
            toolbar,
            text="1:1",
            command=self.one_to_one,
            style="InspectorToolbar.TButton",
        ).grid(row=0, column=5, padx=1)
        ttk.Button(
            toolbar,
            text="+",
            command=self.zoom_in,
            width=3,
            style="InspectorToolbarIcon.TButton",
        ).grid(row=0, column=6, padx=1)
        ttk.Button(
            toolbar,
            text="适应",
            command=self.fit_images,
            style="InspectorToolbar.TButton",
        ).grid(row=0, column=7, padx=(1, 7))
        ttk.Separator(toolbar, orient="vertical").grid(row=0, column=8, sticky="ns", padx=(0, 6))
        ttk.Button(
            toolbar,
            text="清除选区",
            command=self.clear_roi,
            style="InspectorToolbar.TButton",
        ).grid(row=0, column=9, padx=(0, 6))
        ttk.Label(toolbar, text="范围", style="InspectorToolbar.TLabel").grid(
            row=0,
            column=10,
            padx=(0, 3),
        )
        self.search_range_var = tk.StringVar(value=f"±{self.settings.search_range}")
        self.search_combo = ttk.Combobox(
            toolbar,
            textvariable=self.search_range_var,
            values=[f"±{value}" for value in MATCH_SEARCH_RANGES],
            width=6,
            state="readonly",
            style="InspectorToolbar.TCombobox",
        )
        self.search_combo.grid(row=0, column=11, padx=(0, 6))
        self.search_combo.bind("<<ComboboxSelected>>", lambda _event: self._settings_changed())
        self.live_pixel_var = tk.BooleanVar(value=self.settings.live_pixel)
        self.show_histogram_var = tk.BooleanVar(value=self.settings.show_histogram)
        self.show_luminance_histogram_var = tk.BooleanVar(
            value=self.settings.show_luminance_histogram
        )
        self.show_exif_var = tk.BooleanVar(value=self.settings.show_exif)
        self.include_full_path_var = tk.BooleanVar(value=self.settings.include_full_path)
        # Rebuild now that the Tk variable exists; Tk checkbutton menu variables
        # cannot be created safely before _build_ui.
        self._build_menu()
        self.match_status_var = tk.StringVar(value="匹配：—")
        toolbar.columnconfigure(12, weight=1)
        self.progress = ttk.Progressbar(toolbar, mode="indeterminate", length=80)
        self.progress.grid(row=0, column=12, sticky="e")
        self.progress.grid_remove()
        self.workbench_home_button: Optional[ttk.Button] = None
        if self.on_home is not None:
            self.workbench_home_button = ttk.Button(
                toolbar,
                text="⌂  首页",
                command=self.on_home,
                style="InspectorToolbar.TButton",
            )
            self.workbench_home_button.grid(row=0, column=13, padx=(6, 0))

        ttk.Separator(self.top_bar, orient="horizontal").pack(fill="x", padx=6)

        location_bar = ttk.Frame(self.top_bar, padding=(6, 2), style="InspectorToolbar.TFrame")
        self.location_bar = location_bar
        location_bar.pack(fill="x")
        self.location_home_button = ttk.Button(
            location_bar,
            text="⌂",
            width=3,
            command=self.reset_workspace,
            style="InspectorToolbarIcon.TButton",
        )
        self.location_home_button.grid(row=0, column=0, padx=(0, 4))
        # Keep the remembered folder for native open dialogs, but do not make
        # an unopened workspace look as though that directory is already active.
        self.folder_path_var = tk.StringVar(value="")
        self.folder_address_entry = ttk.Entry(
            location_bar,
            textvariable=self.folder_path_var,
            style="InspectorToolbar.TEntry",
        )
        self.folder_address_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.folder_address_entry.bind("<Return>", self._open_folder_from_address)
        self.folder_sidebar_toggle_button = ttk.Button(
            location_bar,
            text="显示图库",
            command=self.toggle_folder_sidebar,
            state="disabled",
            style="InspectorToolbar.TButton",
        )
        self.folder_sidebar_toggle_button.grid(row=0, column=2, padx=(0, 3))
        self.previous_group_button = ttk.Button(
            location_bar,
            text="‹ 上一组",
            command=self.show_previous_group,
            state="disabled",
            style="InspectorToolbar.TButton",
        )
        self.previous_group_button.grid(row=0, column=3, padx=(0, 3))
        self.group_status_var = tk.StringVar(value="")
        ttk.Label(
            location_bar,
            textvariable=self.group_status_var,
            style="InspectorToolbarMuted.TLabel",
            width=22,
            anchor="center",
        ).grid(row=0, column=4, padx=3)
        self.next_group_button = ttk.Button(
            location_bar,
            text="下一组 ›",
            command=self.show_next_group,
            state="disabled",
            style="InspectorToolbar.TButton",
        )
        self.next_group_button.grid(row=0, column=5, padx=3)
        self.sidebar_toggle_button = ttk.Button(
            location_bar,
            text="收起检查器",
            command=self.toggle_analysis_sidebar,
            style="InspectorToolbar.TButton",
        )
        self.sidebar_toggle_button.grid(row=0, column=6, padx=(4, 0))
        location_bar.columnconfigure(1, weight=1)

        status_bar = ttk.Frame(self.outer, padding=(5, 2), style="InspectorSurface.TFrame")
        self.status_bar = status_bar
        status_bar.pack(side="bottom", fill="x", pady=(4, 0))
        ttk.Label(
            status_bar,
            textvariable=self.status_var,
            style="InspectorStatus.TLabel",
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        ttk.Label(
            status_bar,
            textvariable=self.match_status_var,
            style="InspectorStatus.TLabel",
            anchor="e",
        ).pack(side="right", padx=(12, 0))

        self.main_pane = ttk.Panedwindow(self.outer, orient="horizontal")
        self.main_pane.pack(fill="both", expand=True)
        self.viewer_pane = self.main_pane
        self.viewer_container = ttk.Frame(self.main_pane, style="InspectorRoot.TFrame")
        self.image_grid = ttk.Frame(self.viewer_container, style="InspectorRoot.TFrame")
        self.image_grid.grid(row=0, column=0, sticky="nsew")
        self.metrics_grid = ttk.Frame(
            self.viewer_container,
            padding=(1, 0),
            style="InspectorSurface.TFrame",
        )
        self.metrics_grid.grid(row=1, column=0, sticky="ew", pady=(1, 0))
        self.viewer_container.columnconfigure(0, weight=1)
        self.viewer_container.rowconfigure(0, weight=1)
        self.viewer_container.rowconfigure(1, weight=0)
        self.views: Dict[str, ImageCanvas] = {}
        for role in IMAGE_ROLES:
            self.views[role] = ImageCanvas(
                self.image_grid,
                role,
                pixel_callback=self._on_pixel,
                roi_callback=self._on_roi,
                live_enabled=self.live_pixel_var.get,
                zoom_callback=self._zoom_views_from,
                context_callback=self._show_image_context_menu,
                open_callback=self.open_images,
            )
        self.before_view = self.views["before"]
        self.after_view = self.views["after"]
        self.metric_vars: Dict[str, tk.StringVar] = {}
        self.metric_labels: Dict[str, ttk.Label] = {}
        for role in IMAGE_ROLES:
            variable = tk.StringVar(value=self._empty_metric_text(role))
            label = ttk.Label(
                self.metrics_grid,
                textvariable=variable,
                style="InspectorMetric.TLabel",
                anchor="center",
            )
            self.metric_vars[role] = variable
            self.metric_labels[role] = label
        self.folder_thumbnail_strip = FolderThumbnailStrip(
            self.main_pane,
            select_callback=self._on_thumbnail_selected,
            request_callback=self._request_folder_thumbnails,
            collapse_callback=lambda: self._set_folder_sidebar_visible(False),
            context_callback=self._show_thumbnail_context_menu,
        )
        self.main_pane.add(self.viewer_container, weight=4)

        self.sidebar_frame = ttk.Frame(
            self.main_pane,
            width=310,
            padding=(8, 0, 0, 0),
            style="InspectorSurface.TFrame",
        )
        sidebar_header = ttk.Frame(self.sidebar_frame, style="InspectorSurface.TFrame")
        sidebar_header.pack(fill="x", pady=(0, 4))
        ttk.Label(sidebar_header, text="检查器", style="InspectorCardTitle.TLabel").pack(side="left")
        self.sidebar_header_toggle_button = ttk.Button(
            sidebar_header,
            text="›",
            width=3,
            command=self.toggle_analysis_sidebar,
            style="Icon.TButton",
        )
        self.sidebar_header_toggle_button.pack(side="right")
        self.notebook = ttk.Notebook(self.sidebar_frame)
        self.notebook.pack(fill="both", expand=True)
        self.info_tab = ttk.Frame(self.notebook, padding=6, style="InspectorSurface.TFrame")
        self.compare_tab = ttk.Frame(self.notebook, padding=6, style="InspectorRoot.TFrame")
        self.notebook.add(self.info_tab, text="直方图 / EXIF")
        self.notebook.add(self.compare_tab, text="对比")
        self.main_pane.add(self.sidebar_frame, weight=0)
        self._sidebar_visible = True
        self._folder_sidebar_requested = False
        self._sidebar_ratio = self.settings.panel_ratio

        self._build_information_panel()
        self._build_comparison_panel()
        self._set_image_count(1)
        self.root.after_idle(self._restore_panel_ratio)
        self.root.after(140, self._restore_panel_ratio)

    def _build_information_panel(self) -> None:
        controls = ttk.Frame(self.info_tab, padding=(9, 8), style="InspectorCard.TFrame")
        controls.pack(fill="x", pady=(0, 6))
        ttk.Label(controls, text="逐图显示", style="InspectorCardTitle.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 4)
        )
        controls.columnconfigure(0, weight=1)
        global_row = ttk.Frame(controls, style="InspectorSurface.TFrame")
        global_row.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 3))
        global_row.columnconfigure(0, weight=1)
        ttk.Label(global_row, text="全部图片", style="InspectorCard.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 5)
        )
        ttk.Checkbutton(
            global_row,
            text="RGB",
            variable=self.show_histogram_var,
            command=lambda: self._on_global_info_visibility_changed("rgb"),
        ).grid(row=0, column=1, padx=(3, 2))
        ttk.Checkbutton(
            global_row,
            text="亮度",
            variable=self.show_luminance_histogram_var,
            command=lambda: self._on_global_info_visibility_changed("luminance"),
        ).grid(row=0, column=2, padx=2)
        ttk.Checkbutton(
            global_row,
            text="EXIF",
            variable=self.show_exif_var,
            command=lambda: self._on_global_info_visibility_changed("exif"),
        ).grid(row=0, column=3, padx=(2, 0))
        self.info_global_row = global_row
        self.histogram_visible_vars: Dict[str, tk.BooleanVar] = {}
        self.luminance_histogram_visible_vars: Dict[str, tk.BooleanVar] = {}
        self.exif_visible_vars: Dict[str, tk.BooleanVar] = {}
        self.info_name_vars: Dict[str, tk.StringVar] = {}
        self.info_control_rows: Dict[str, ttk.Frame] = {}
        for index, role in enumerate(IMAGE_ROLES, start=2):
            row = ttk.Frame(controls, style="InspectorSurface.TFrame")
            row.grid(row=index, column=0, columnspan=4, sticky="ew", pady=1)
            row.columnconfigure(0, weight=1)
            name_var = tk.StringVar(value=_role_label(role))
            ttk.Label(row, textvariable=name_var, style="InspectorCard.TLabel").grid(
                row=0, column=0, sticky="w", padx=(0, 5)
            )
            histogram_var = tk.BooleanVar(value=self.settings.show_histogram)
            luminance_histogram_var = tk.BooleanVar(
                value=self.settings.show_luminance_histogram
            )
            exif_var = tk.BooleanVar(value=self.settings.show_exif)
            ttk.Checkbutton(
                row,
                text="RGB",
                variable=histogram_var,
                command=lambda selected=role: self._on_info_visibility_changed(selected),
            ).grid(row=0, column=1, padx=(3, 2))
            ttk.Checkbutton(
                row,
                text="亮度",
                variable=luminance_histogram_var,
                command=lambda selected=role: self._on_info_visibility_changed(selected),
            ).grid(row=0, column=2, padx=2)
            ttk.Checkbutton(
                row,
                text="EXIF",
                variable=exif_var,
                command=lambda selected=role: self._on_info_visibility_changed(selected),
            ).grid(row=0, column=3, padx=(2, 0))
            self.histogram_visible_vars[role] = histogram_var
            self.luminance_histogram_visible_vars[role] = luminance_histogram_var
            self.exif_visible_vars[role] = exif_var
            self.info_name_vars[role] = name_var
            self.info_control_rows[role] = row

        body = ttk.Frame(self.info_tab, style="InspectorSurface.TFrame")
        body.pack(fill="both", expand=True)
        self.info_canvas = tk.Canvas(body, background=PANEL, highlightthickness=0)
        info_scroll = ttk.Scrollbar(body, orient="vertical", command=self.info_canvas.yview)
        self.info_canvas.configure(yscrollcommand=info_scroll.set)
        self.info_canvas.grid(row=0, column=0, sticky="nsew")
        info_scroll.grid(row=0, column=1, sticky="ns")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.info_inner = ttk.Frame(self.info_canvas, style="InspectorSurface.TFrame")
        self._info_window_item = self.info_canvas.create_window((0, 0), window=self.info_inner, anchor="nw")
        self.info_inner.bind("<Configure>", self._on_info_inner_configure)
        self.info_canvas.bind("<Configure>", self._on_info_canvas_configure)

        self.info_sections: Dict[str, ttk.Frame] = {}
        self.histogram_canvases: Dict[str, HistogramCanvas] = {}
        self.luminance_histogram_canvases: Dict[str, HistogramCanvas] = {}
        self.exif_frames: Dict[str, ttk.Frame] = {}
        self.exif_vars: Dict[str, tk.StringVar] = {}
        for role in IMAGE_ROLES:
            section = ttk.Frame(self.info_inner, padding=(9, 8), style="InspectorCard.TFrame")
            title_var = self.info_name_vars[role]
            ttk.Label(section, textvariable=title_var, style="InspectorCardTitle.TLabel").pack(
                anchor="w", pady=(0, 5)
            )
            histogram = HistogramCanvas(section)
            histogram.configure(height=154)
            histogram.pack_propagate(False)
            luminance_histogram = HistogramCanvas(section, luminance=True)
            luminance_histogram.configure(height=154)
            luminance_histogram.pack_propagate(False)
            exif_frame = ttk.Frame(section, style="InspectorSurface.TFrame")
            ttk.Label(exif_frame, text="EXIF", style="InspectorCardTitle.TLabel").pack(
                anchor="w", pady=(7, 3)
            )
            exif_var = tk.StringVar(value="—")
            exif_label = ttk.Label(
                exif_frame,
                textvariable=exif_var,
                style="InspectorMutedCard.TLabel",
                justify="left",
                anchor="nw",
                wraplength=300,
            )
            exif_label.pack(fill="x")
            self.info_sections[role] = section
            self.histogram_canvases[role] = histogram
            self.luminance_histogram_canvases[role] = luminance_histogram
            self.exif_frames[role] = exif_frame
            self.exif_vars[role] = exif_var
        self._bind_info_scrolling(self.info_tab)

    def _on_info_inner_configure(self, _event: tk.Event) -> None:
        bounds = self.info_canvas.bbox(self._info_window_item)
        if bounds is not None:
            self.info_canvas.configure(scrollregion=bounds)

    def _on_info_canvas_configure(self, event: tk.Event) -> None:
        self.info_canvas.itemconfigure(self._info_window_item, width=max(1, int(event.width)))

    def _bind_info_scrolling(self, widget: tk.Misc) -> None:
        """Make the entire information surface scroll like a native inspector."""

        widget.bind("<MouseWheel>", self._on_info_mousewheel, add="+")
        widget.bind("<Button-4>", lambda _event: self._scroll_info_by_pixels(-52.0), add="+")
        widget.bind("<Button-5>", lambda _event: self._scroll_info_by_pixels(52.0), add="+")
        if tk.TkVersion >= 9.0:
            widget.bind("<TouchpadScroll>", self._on_info_touchpad_scroll, add="+")
        for child in widget.winfo_children():
            self._bind_info_scrolling(child)

    def _scroll_info_by_pixels(self, pixels: float) -> str:
        bounds = self.info_canvas.bbox("all")
        if bounds is None:
            return "break"
        content_height = max(1.0, float(bounds[3] - bounds[1]))
        viewport_height = max(1.0, float(self.info_canvas.winfo_height()))
        if content_height <= viewport_height:
            return "break"
        top = float(self.info_canvas.yview()[0])
        maximum_top = max(0.0, 1.0 - viewport_height / content_height)
        target = min(maximum_top, max(0.0, top + float(pixels) / content_height))
        self.info_canvas.yview_moveto(target)
        return "break"

    def _on_info_mousewheel(self, event: tk.Event) -> str:
        delta = float(getattr(event, "delta", 0.0) or 0.0)
        if abs(delta) < 1e-12:
            return "break"
        if abs(delta) >= 120.0:
            pixels = -delta / 120.0 * 52.0
        else:
            pixels = -math.copysign(min(52.0, max(10.0, abs(delta) * 8.0)), delta)
        return self._scroll_info_by_pixels(pixels)

    def _on_info_touchpad_scroll(self, event: tk.Event) -> str:
        raw_delta = getattr(event, "delta", 0)
        try:
            decoded = self.info_canvas.tk.call("tk::PreciseScrollDeltas", raw_delta)
            values = tuple(float(value) for value in self.info_canvas.tk.splitlist(decoded))
            delta_y = values[1] if len(values) >= 2 else values[0]
        except (tk.TclError, TypeError, ValueError, IndexError):
            delta_y = float(raw_delta or 0.0)
        if abs(delta_y) < 1e-12:
            return "break"
        return self._scroll_info_by_pixels(-delta_y)

    def _on_info_visibility_changed(self, _role: str) -> None:
        self.show_histogram_var.set(any(variable.get() for variable in self.histogram_visible_vars.values()))
        self.show_luminance_histogram_var.set(
            any(variable.get() for variable in self.luminance_histogram_visible_vars.values())
        )
        self.show_exif_var.set(any(variable.get() for variable in self.exif_visible_vars.values()))
        self._refresh_information_sidebar()
        self._settings_changed()

    def _build_comparison_panel(self) -> None:
        controls = ttk.Frame(self.compare_tab, padding=(9, 7), style="InspectorCard.TFrame")
        controls.pack(fill="x", pady=(0, 6))
        controls.columnconfigure(5, weight=1)
        ttk.Label(controls, text="图像 A", style="InspectorCard.TLabel").grid(row=0, column=0, sticky="w")
        self.comparison_base_var = tk.StringVar(value="")
        self.comparison_base_combo = ttk.Combobox(
            controls,
            textvariable=self.comparison_base_var,
            state="readonly",
            width=7,
        )
        self.comparison_base_combo.grid(row=0, column=1, sticky="w", padx=(5, 8))
        self.comparison_base_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_comparison_pair_changed("base")
        )
        ttk.Label(controls, text="图像 B", style="InspectorCard.TLabel").grid(row=0, column=2, sticky="w")
        self.comparison_role_var = tk.StringVar(value="")
        self.comparison_role_combo = ttk.Combobox(
            controls,
            textvariable=self.comparison_role_var,
            state="readonly",
            width=7,
        )
        self.comparison_role_combo.grid(row=0, column=3, sticky="w", padx=(5, 6))
        self.comparison_role_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_comparison_pair_changed("target")
        )
        ttk.Button(
            controls,
            text="交换",
            command=self._swap_comparison_pair,
            style="Quiet.TButton",
        ).grid(row=0, column=4, sticky="w")
        self._comparison_pair_guard = False
        self.comparison_files_var = tk.StringVar(value="请选择 2–4 张图片并框选区域")
        ttk.Label(
            controls,
            textvariable=self.comparison_files_var,
            style="InspectorMutedCard.TLabel",
            wraplength=315,
        ).grid(row=1, column=0, columnspan=6, sticky="ew", pady=(6, 3))
        self.comparison_gate_var = tk.StringVar(value="匹配置信度：—")
        self.comparison_gate_label = ttk.Label(
            controls,
            textvariable=self.comparison_gate_var,
            style="InspectorMatchLow.TLabel",
            wraplength=315,
        )
        self.comparison_gate_label.grid(row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))
        swatches = ttk.Frame(controls, style="InspectorSurface.TFrame")
        swatches.grid(row=2, column=0, columnspan=6, sticky="w")
        ttk.Label(swatches, text="选区平均色", style="InspectorMutedCard.TLabel").pack(side="left", padx=(0, 4))
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
            "reference": "图像 A",
            "target": "图像 B",
            "delta": "Delta",
            "change": "变化方向",
        }
        widths = {"metric": 72, "reference": 54, "target": 54, "delta": 54, "change": 70}
        for column in columns:
            alignment = "w" if column == "metric" else "e"
            self.compare_tree.heading(column, text=headings[column], anchor=alignment)
            self.compare_tree.column(
                column,
                width=widths[column],
                minwidth=46,
                anchor=alignment,
                stretch=False,
            )
        compare_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.compare_tree.yview)
        compare_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.compare_tree.xview)
        self.compare_tree.configure(yscrollcommand=compare_y.set, xscrollcommand=compare_x.set)
        self.compare_tree.grid(row=0, column=0, sticky="nsew")
        compare_y.grid(row=0, column=1, sticky="ns")
        compare_x.grid(row=1, column=0, sticky="ew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

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

    def _empty_metric_text(self, role: str) -> str:
        _ = role
        return ""

    def _metric_text(self, role: str, rgb: Tuple[float, float, float]) -> str:
        red, green, blue = rgb
        r_over_g = None if abs(green) <= 1e-12 else red / green
        r_over_b = None if abs(blue) <= 1e-12 else red / blue
        ratio_rg = "—" if r_over_g is None else f"{r_over_g:.3f}"
        ratio_rb = "—" if r_over_b is None else f"{r_over_b:.3f}"
        index = IMAGE_ROLES.index(role) + 1
        return (
            f"图{index} · R:{red:.0f} G:{green:.0f} B:{blue:.0f} · "
            f"R/G:{ratio_rg} R/B:{ratio_rb}"
        )

    def _refresh_metric_strip(self) -> None:
        has_values = False
        for role in IMAGE_ROLES:
            text = self._empty_metric_text(role)
            if role in self.active_roles:
                if self._metric_mode == "roi" and self.roi_statistics[role] is not None:
                    text = self._metric_text(role, self.roi_statistics[role].mean_rgb)
                elif self._metric_mode == "pixel" and self.fixed_pixels[role] is not None:
                    text = self._metric_text(role, self.fixed_pixels[role].rgb)
            has_values = has_values or bool(text)
            self.metric_vars[role].set(text)
        if has_values:
            self.metrics_grid.grid()
        else:
            self.metrics_grid.grid_remove()

    def _set_image_count(self, count: int) -> None:
        count = min(4, max(1, int(count)))
        self.active_roles = IMAGE_ROLES[:count]
        self.dual_mode = count > 1
        for view in self.views.values():
            view.grid_forget()
        for index in range(3):
            column_visible = count == 3 or index < 2
            self.image_grid.columnconfigure(
                index,
                weight=1 if column_visible else 0,
                uniform="image-columns" if column_visible else "",
            )
        for index in range(2):
            self.image_grid.rowconfigure(index, weight=1, uniform="image-rows")
        if count == 1:
            self.views["before"].grid(row=0, column=0, columnspan=2, rowspan=2, sticky="nsew")
        elif count == 2:
            self.views["before"].grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 1))
            self.views["after"].grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(1, 0))
        elif count == 3:
            for index, role in enumerate(self.active_roles):
                self.views[role].grid(
                    row=0,
                    column=index,
                    rowspan=2,
                    sticky="nsew",
                    padx=((0, 1) if index == 0 else ((1, 0) if index == 2 else (1, 1))),
                )
        else:
            for index, role in enumerate(self.active_roles):
                row, column = index // 2, index % 2
                self.views[role].grid(
                    row=row,
                    column=column,
                    sticky="nsew",
                    padx=((0, 1) if column == 0 else (1, 0)),
                    pady=((0, 1) if row == 0 else (1, 0)),
                )
        for role in self.active_roles:
            self.views[role].set_title(_role_label(role))
        for label in self.metric_labels.values():
            label.grid_forget()
        for index in range(4):
            self.metrics_grid.columnconfigure(
                index,
                weight=1 if index < count else 0,
                uniform="metric-columns" if index < count else "",
            )
        for index, role in enumerate(self.active_roles):
            self.metric_labels[role].grid(row=0, column=index, sticky="ew", padx=1)
        self._refresh_metric_strip()
        self._refresh_information_sidebar()
        if hasattr(self, "comparison_role_combo"):
            comparison_labels = tuple(_role_label(role) for role in self.active_roles)
            self.comparison_base_combo.configure(values=comparison_labels)
            self.comparison_role_combo.configure(values=comparison_labels)
            if self.comparison_base_var.get() not in comparison_labels:
                self.comparison_base_var.set(comparison_labels[0] if comparison_labels else "")
            if (
                self.comparison_role_var.get() not in comparison_labels
                or self.comparison_role_var.get() == self.comparison_base_var.get()
            ):
                self.comparison_role_var.set(comparison_labels[1] if len(comparison_labels) > 1 else "")
            self._refresh_comparison_table()
        if hasattr(self, "match_status_var"):
            self._refresh_match_status()
        self.root.after_idle(self.fit_images)

    @staticmethod
    def _format_exif_panel(image_data: Optional[ImageData]) -> str:
        if image_data is None:
            return "—"
        lines = [
            f"文件  {image_data.filename}",
            f"尺寸  {image_data.width} × {image_data.height}",
            f"位深  {image_data.bit_depth}-bit",
            f"模式  {image_data.source_mode}",
        ]
        if image_data.exif:
            lines.append("")
            lines.extend(f"{name}  {value}" for name, value in image_data.exif)
        else:
            lines.extend(("", "无可用 EXIF 元数据"))
        return "\n".join(lines)

    def _refresh_information_sidebar(self) -> None:
        if not hasattr(self, "info_sections"):
            return
        for role in IMAGE_ROLES:
            self.info_control_rows[role].grid_remove()
            self.info_sections[role].pack_forget()
        for role in self.active_roles:
            image_data = self.images.get(role)
            if image_data is None:
                continue
            name = f"{_role_label(role)} · {image_data.filename}"
            self.info_name_vars[role].set(name)
            self.info_control_rows[role].grid()
            show_histogram = self.histogram_visible_vars[role].get()
            show_luminance_histogram = self.luminance_histogram_visible_vars[role].get()
            show_exif = self.exif_visible_vars[role].get()
            if not (show_histogram or show_luminance_histogram or show_exif):
                continue
            section = self.info_sections[role]
            section.pack(fill="x", pady=(0, 6))
            histogram = self.histogram_canvases[role]
            if show_histogram:
                histogram.pack(fill="x", pady=(0, 2))
                statistics = self.roi_statistics.get(role)
                values = None
                scope = name
                if statistics is not None:
                    values = statistics.histogram
                    scope += " · 选区"
                elif image_data is not None:
                    values = image_data.histogram
                    scope += " · 整图"
                histogram.set_histogram(values, scope)
            else:
                histogram.pack_forget()
            luminance_histogram = self.luminance_histogram_canvases[role]
            if show_luminance_histogram:
                luminance_histogram.pack(fill="x", pady=(0, 2))
                statistics = self.roi_statistics.get(role)
                values = None
                scope = name
                if statistics is not None:
                    values = statistics.luminance_histogram
                    scope += " · 选区"
                elif image_data is not None:
                    values = image_data.luminance_histogram
                    scope += " · 整图"
                luminance_histogram.set_histogram(values, scope)
            else:
                luminance_histogram.pack_forget()
            exif_frame = self.exif_frames[role]
            if show_exif:
                self.exif_vars[role].set(self._format_exif_panel(image_data))
                exif_frame.pack(fill="x")
            else:
                exif_frame.pack_forget()
        self.root.after_idle(self._on_info_inner_configure, None)

    def _restore_panel_ratio(self) -> None:
        try:
            width = self.main_pane.winfo_width()
            panes = [str(pane) for pane in self.main_pane.panes()]
            if width <= 10 or len(panes) <= 1:
                return
            left_visible = str(self.folder_thumbnail_strip) in panes
            right_visible = str(self.sidebar_frame) in panes
            if left_visible:
                # Match the calm, narrow source list used by Photos/Finder.
                left_width = min(266, max(242, int(width * 0.165)))
                self.main_pane.sashpos(0, left_width)
            if right_visible:
                ratio = min(
                    MAX_ANALYSIS_PANEL_RATIO,
                    max(MIN_ANALYSIS_PANEL_RATIO, float(self._sidebar_ratio)),
                )
                right_width = int(width * ratio)
                right_sash = len(panes) - 2
                self.main_pane.sashpos(right_sash, width - right_width)
        except (AttributeError, tk.TclError):
            pass

    def _analysis_sidebar_is_visible(self) -> bool:
        try:
            return str(self.sidebar_frame) in {str(pane) for pane in self.main_pane.panes()}
        except (AttributeError, tk.TclError):
            return False

    def _folder_sidebar_is_visible(self) -> bool:
        try:
            return str(self.folder_thumbnail_strip) in {str(pane) for pane in self.main_pane.panes()}
        except (AttributeError, tk.TclError):
            return False

    def _set_analysis_sidebar_visible(self, visible: bool) -> None:
        visible = bool(visible)
        current = self._analysis_sidebar_is_visible()
        if visible and not current:
            self.main_pane.add(self.sidebar_frame, weight=0)
        elif not visible and current:
            width = self.main_pane.winfo_width()
            if width > 10 and len(self.main_pane.panes()) > 1:
                try:
                    sash_index = len(self.main_pane.panes()) - 2
                    sash = self.main_pane.sashpos(sash_index)
                    self._sidebar_ratio = min(
                        MAX_ANALYSIS_PANEL_RATIO,
                        max(MIN_ANALYSIS_PANEL_RATIO, (width - sash) / width),
                    )
                except tk.TclError:
                    pass
            self.main_pane.forget(self.sidebar_frame)
        self._sidebar_visible = visible
        self.sidebar_toggle_button.configure(text="收起检查器" if visible else "显示检查器")
        if visible:
            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass
            self._restore_panel_ratio()
        self.root.after_idle(self._restore_panel_ratio)
        self.root.after_idle(self.fit_images)

    def toggle_analysis_sidebar(self) -> None:
        self._set_analysis_sidebar_visible(not self._sidebar_visible)

    def _set_folder_sidebar_visible(self, visible: bool) -> None:
        if visible and not self._folder_browser_available:
            return
        visible = bool(visible)
        self._folder_sidebar_requested = visible
        current = self._folder_sidebar_is_visible()
        if visible != current:
            if visible:
                self.main_pane.insert(0, self.folder_thumbnail_strip, weight=0)
            else:
                self.main_pane.forget(self.folder_thumbnail_strip)
        if hasattr(self, "folder_sidebar_toggle_button"):
            self.folder_sidebar_toggle_button.configure(
                text="收起图库" if visible else "显示图库",
                state="normal" if self._folder_browser_available else "disabled",
            )
        if visible:
            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass
            self._restore_panel_ratio()
        self.root.after_idle(self._restore_panel_ratio)
        self.root.after_idle(self.fit_images)

    def toggle_folder_sidebar(self) -> None:
        self._set_folder_sidebar_visible(not self._folder_sidebar_requested)

    def _show_folder_thumbnails(self, paths: Sequence[Path]) -> None:
        self._folder_thumbnail_generation += 1
        self._folder_browser_available = True
        self.folder_thumbnail_strip.set_paths(paths)
        self.folder_thumbnail_strip.set_active_paths(self.current_paths)
        self._set_folder_sidebar_visible(True)

    def _hide_folder_thumbnails(self) -> None:
        self._folder_thumbnail_generation += 1
        if hasattr(self, "folder_thumbnail_strip"):
            self._set_folder_sidebar_visible(False)
            self._folder_browser_available = False
            self.folder_selection_anchor = None
            self.folder_thumbnail_strip.set_paths(())
            self.folder_sidebar_toggle_button.configure(text="显示图库", state="disabled")

    def _request_folder_thumbnails(self, items: Sequence[Tuple[int, Path]]) -> None:
        generation = self._folder_thumbnail_generation
        for index, path in items:
            self._submit(
                "thumbnail",
                generation,
                "thumbnail",
                load_thumbnail,
                path,
                FolderThumbnailStrip.PREVIEW_SIZE,
                meta={"generation": generation, "index": index, "path": path},
            )

    def _configure_folder_selection(self, selected_paths: Sequence[Path]) -> None:
        selected = selected_paths_in_folder_order(self.folder_paths, selected_paths)
        if not selected:
            return
        self.folder_group_size = len(selected)
        selected_set = set(selected)
        remaining = [path for path in self.folder_paths if path not in selected_set]
        groups = [
            remaining[index : index + self.folder_group_size]
            for index in range(0, len(remaining), self.folder_group_size)
        ]
        groups.append(list(selected))
        groups.sort(key=lambda group: min(self.folder_paths.index(path) for path in group))
        self.folder_groups = groups
        self.folder_group_index = groups.index(list(selected))
        self.folder_group_mode = len(groups) > 1
        self.folder_group_start = min(self.folder_paths.index(path) for path in selected)
        self.folder_groups_aligned = len(selected) == 1
        self.load_selected_images(selected)

    def _on_thumbnail_selected(self, path: Path, additive: bool = False, shift: bool = False) -> None:
        try:
            index = self.folder_paths.index(path)
        except ValueError:
            return
        selected: list[Path]
        if shift and self.folder_selection_anchor in self.folder_paths:
            anchor_index = self.folder_paths.index(self.folder_selection_anchor)
            low, high = sorted((anchor_index, index))
            selected = self.folder_paths[low : high + 1]
            if len(selected) > len(IMAGE_ROLES):
                selected = selected[: len(IMAGE_ROLES)]
                messagebox.showinfo("最多选择 4 张", "连续选择已保留前 4 张图片。", parent=self.root)
        elif additive:
            selected = [item for item in self.current_paths if item in self.folder_paths]
            if path in selected:
                if len(selected) == 1:
                    return
                selected.remove(path)
            else:
                if len(selected) >= len(IMAGE_ROLES):
                    messagebox.showinfo("最多选择 4 张", "请先取消一张，再加入新的图片。", parent=self.root)
                    return
                selected.append(path)
            selected = selected_paths_in_folder_order(self.folder_paths, selected)
        else:
            selected = [path]
        self.folder_selection_anchor = path
        self._configure_folder_selection(selected)

    def _open_folder_from_address(self, _event: Optional[tk.Event] = None) -> str:
        """Open the directory typed into the viewer-style location bar."""

        selected = self.folder_path_var.get().strip()
        if selected:
            self.open_folder(selected)
        return "break"

    def reset_workspace(self) -> None:
        """Return the inspector to its unopened state without touching files."""

        if self._fit_after_id is not None:
            try:
                self.root.after_cancel(self._fit_after_id)
            except tk.TclError:
                pass
            self._fit_after_id = None
        for future in tuple(self._futures):
            future.cancel()

        self._hide_folder_thumbnails()
        self.folder_path_var.set("")
        self.folder_paths = []
        self.folder_groups = []
        self.folder_group_index = 0
        self.current_paths = []
        self.folder_group_start = 0
        self.folder_group_size = len(IMAGE_ROLES)
        self.folder_group_mode = False
        self.folder_groups_aligned = True
        self._metric_mode = "none"

        for role in IMAGE_ROLES:
            self._invalidate_role(role, clear_image=True, refresh=False)
        self._image_cache.clear()
        for histogram in (
            *self.histogram_canvases.values(),
            *self.luminance_histogram_canvases.values(),
        ):
            histogram.set_histogram(None, "尚无数据")

        self._set_image_count(1)
        self._update_group_navigation()
        self._refresh_match_status()
        self._refresh_outputs()
        self.notebook.select(self.info_tab)
        self.status_var.set(EMPTY_WORKSPACE_STATUS)

    def _update_group_navigation(self) -> None:
        """Keep the address-bar navigation state in sync with loaded images."""

        loaded = len(self.current_paths)
        if self.folder_group_mode and self.folder_paths and self.folder_groups:
            total = len(self.folder_paths)
            group_count = len(self.folder_groups)
            group_index = self.folder_group_index
            indices = sorted(self.folder_paths.index(path) for path in self.current_paths)
            first = indices[0] + 1
            last = indices[-1] + 1
            contiguous = indices == list(range(indices[0], indices[0] + len(indices)))
            if contiguous:
                self.group_status_var.set(
                    f"第 {group_index + 1}/{group_count} 组 · {first}–{last} / {total}"
                )
            else:
                self.group_status_var.set(f"第 {group_index + 1}/{group_count} 组 · {loaded} 张 / {total}")
            self.previous_group_button.configure(
                state="normal" if self.folder_group_index > 0 else "disabled"
            )
            self.next_group_button.configure(
                state="normal" if self.folder_group_index + 1 < group_count else "disabled"
            )
            return

        self.previous_group_button.configure(state="disabled")
        self.next_group_button.configure(state="disabled")
        self.group_status_var.set(f"已载入 {loaded} 张图片" if loaded else "")

    def _load_group_index(self, index: int) -> None:
        if not self.folder_groups:
            self.current_paths = []
            self._update_group_navigation()
            return
        normalized = min(len(self.folder_groups) - 1, max(0, int(index)))
        self.folder_group_index = normalized
        paths = self.folder_groups[normalized]
        self.folder_group_start = min(self.folder_paths.index(path) for path in paths)
        self.load_selected_images(paths)

    def _load_folder_group(self, start: int) -> None:
        """Load the natural folder group containing a folder-list index."""

        if not self.folder_paths:
            self.current_paths = []
            self._update_group_navigation()
            return
        normalized = min(len(self.folder_paths) - 1, max(0, int(start)))
        selected_path = self.folder_paths[normalized]
        group_index = next(
            (index for index, group in enumerate(self.folder_groups) if selected_path in group),
            0,
        )
        self._load_group_index(group_index)

    def show_previous_group(self) -> None:
        if self.folder_group_mode and self.folder_group_index > 0:
            self._load_group_index(self.folder_group_index - 1)

    def show_next_group(self) -> None:
        if self.folder_group_mode and self.folder_group_index + 1 < len(self.folder_groups):
            self._load_group_index(self.folder_group_index + 1)

    def show_comparison(self) -> None:
        """Expose the comparison workspace regardless of how images were opened."""

        if len(self.current_paths) < 2:
            messagebox.showinfo("需要多张图片", "请先打开至少 2 张图片。", parent=self.root)
            return
        self._set_analysis_sidebar_visible(True)
        self.notebook.select(self.compare_tab)

    def open_images(
        self,
        paths: Optional[Sequence[Union[str, Path]]] = None,
    ) -> None:
        """Open one to four images directly, mirroring desktop image viewers."""

        chosen = list(paths) if paths is not None else list(
            filedialog.askopenfilenames(
                title="打开 1–4 张图片",
                initialdir=self.last_directory or str(default_sources_directory()),
                filetypes=IMAGE_FILE_TYPES,
            )
        )
        if not chosen:
            return
        self._hide_folder_thumbnails()
        selected_paths = [Path(item).expanduser().resolve() for item in chosen]
        unsupported = [path.name for path in selected_paths if path.suffix.casefold() not in SUPPORTED_EXTENSIONS]
        if unsupported:
            messagebox.showerror(
                "不支持的图片",
                "以下文件格式不受支持：\n" + "\n".join(unsupported),
                parent=self.root,
            )
            return
        if len(selected_paths) > 4:
            messagebox.showinfo(
                "最多打开 4 张图片",
                "一次最多比较 4 张图片，本次将载入前 4 张。",
                parent=self.root,
            )
            selected_paths = selected_paths[:4]

        parents = {path.parent for path in selected_paths}
        if len(parents) == 1:
            parent = next(iter(parents))
            self.last_directory = str(parent)
            label = str(parent)
            try:
                discovered = [path.resolve() for path in discover_images(parent)]
            except ImageFolderError:
                discovered = list(selected_paths)
            ordered_selected = selected_paths_in_folder_order(discovered, selected_paths)
            if len(ordered_selected) == len(selected_paths):
                selected_paths = ordered_selected
            self.folder_group_size = len(selected_paths)
            self.folder_paths = list(discovered)
            selected_set = set(selected_paths)
            remaining = [path for path in discovered if path not in selected_set]
            groups = [
                remaining[index : index + self.folder_group_size]
                for index in range(0, len(remaining), self.folder_group_size)
            ]
            groups.append(list(selected_paths))
            groups.sort(key=lambda group: min(discovered.index(path) for path in group))
            self.folder_groups = groups
            self.folder_group_index = groups.index(list(selected_paths))
            self.folder_group_mode = len(groups) > 1
            self.folder_group_start = min(discovered.index(path) for path in selected_paths)
            self.folder_groups_aligned = False
        else:
            # Keep the editable location bar usable even when the native file
            # dialog returns files from more than one directory.
            self.last_directory = str(selected_paths[0].parent)
            label = self.last_directory
            self.folder_group_size = len(selected_paths)
            self.folder_group_mode = False
            self.folder_paths = list(selected_paths)
            self.folder_groups = [list(selected_paths)]
            self.folder_group_index = 0
            self.folder_group_start = 0
            self.folder_groups_aligned = True
        self.folder_path_var.set(label)
        self.load_selected_images(selected_paths)
        if len(parents) > 1:
            self.group_status_var.set(f"{len(selected_paths)} 张 · {len(parents)} 个位置")

    def open_folder(self, path: Optional[Union[str, Path]] = None) -> None:
        selected = str(path) if path is not None else filedialog.askdirectory(
            title="打开图片文件夹",
            initialdir=self.last_directory or str(default_sources_directory()),
        )
        if not selected:
            return
        directory = Path(selected).expanduser().resolve()
        try:
            paths = [image_path.resolve() for image_path in discover_images(directory)]
        except ImageFolderError as exc:
            messagebox.showerror("无法打开图片文件夹", str(exc), parent=self.root)
            return
        self.last_directory = str(directory)
        self.folder_path_var.set(str(directory))
        self.folder_group_mode = True
        self.folder_group_start = 0
        self.folder_group_size = 1
        self.folder_paths = list(paths)
        self.folder_groups = [[image_path] for image_path in self.folder_paths]
        self.folder_group_index = 0
        self.folder_groups_aligned = True
        self.folder_selection_anchor = self.folder_paths[0] if self.folder_paths else None
        if not paths:
            self._show_folder_thumbnails(())
            self.current_paths = []
            for role in IMAGE_ROLES:
                self._invalidate_role(role, clear_image=True, refresh=False)
            self._set_image_count(1)
            self._refresh_outputs()
            self._update_group_navigation()
            self.status_var.set("所选文件夹中没有可预览图片。")
            return
        self._load_folder_group(0)
        self._show_folder_thumbnails(paths)
        self.status_var.set(
            f"已打开文件夹，共发现 {len(paths)} 张图片；使用上一组/下一组连续查看。"
        )

    def load_selected_images(
        self,
        paths: Optional[Sequence[Union[str, Path]]] = None,
    ) -> None:
        selected_paths = (
            list(self.current_paths)
            if paths is None
            else [Path(item).expanduser().resolve() for item in paths]
        )
        if not 1 <= len(selected_paths) <= 4:
            messagebox.showinfo("请选择图片", "请打开 1–4 张图片。", parent=self.root)
            return
        self.current_paths = list(selected_paths)
        self._metric_mode = "none"
        if self._fit_after_id is not None:
            try:
                self.root.after_cancel(self._fit_after_id)
            except tk.TclError:
                pass
            self._fit_after_id = None
        for role in IMAGE_ROLES:
            self._invalidate_role(role, clear_image=True, refresh=False)
        self._set_image_count(len(selected_paths))
        self._refresh_outputs()
        for role, image_path in zip(self.active_roles, selected_paths):
            self.views[role].set_title(f"{_role_label(role)} · {image_path.name}")
            self._load_async(role, str(image_path), invalidate=False)
        self.folder_thumbnail_strip.set_active_paths(selected_paths)
        self._update_group_navigation()
        self.status_var.set(f"正在加载当前 {len(selected_paths)} 张图片…")

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
        executor = self._load_executor if kind in {"load", "thumbnail"} else self._executor
        future = executor.submit(function, *args, **(kwargs or {}))
        self._futures.add(future)
        self._pending += 1
        if self._pending == 1:
            self.progress.grid()
            self.progress.start(12)
        details = meta or {}
        future.add_done_callback(
            lambda done, k=kind, t=token, r=role, m=details: self._result_queue.put((k, t, r, done, m))
        )
        if self._poll_after_id is None and not self._polling_background:
            self._poll_after_id = self.root.after(50, self._poll_background)

    def _poll_background(self) -> None:
        self._poll_after_id = None
        if self._closed:
            return
        self._polling_background = True
        while True:
            try:
                kind, token, role, future, meta = self._result_queue.get_nowait()
            except queue.Empty:
                break
            self._futures.discard(future)
            self._pending = max(0, self._pending - 1)
            try:
                if kind == "load":
                    self._finish_load(token, role, future, meta)
                elif kind == "thumbnail":
                    self._finish_thumbnail(token, future, meta)
                elif kind == "analysis":
                    self._finish_analysis(token, role, future)
                elif kind == "match":
                    self._finish_match(token, role, future)
            except (tk.TclError, RuntimeError):
                LOGGER.exception("Image Inspector background result handling failed")
        if self._pending == 0:
            self.progress.stop()
            self.progress.grid_remove()
        self._polling_background = False
        if self._pending > 0:
            self._poll_after_id = self.root.after(50, self._poll_background)

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

    def _finish_thumbnail(self, token: int, future: Future[Any], meta: Dict[str, Any]) -> None:
        generation = int(meta.get("generation", token))
        if generation != self._folder_thumbnail_generation:
            return
        index = int(meta.get("index", -1))
        try:
            thumbnail = future.result()
        except Exception:
            LOGGER.debug("Folder thumbnail generation failed", exc_info=True)
            self.folder_thumbnail_strip.mark_thumbnail_failed(index)
            return
        self.folder_thumbnail_strip.apply_thumbnail(index, thumbnail)

    def _apply_loaded_image(self, token: int, role: str, image_data: ImageData, *, from_cache: bool) -> None:
        if token != self._load_tokens[role] or self._closed:
            return
        self.images[role] = image_data
        self.image_transform_ops[role] = []
        view = self.views[role]
        view.set_image(image_data)
        view.set_title(f"{_role_label(role)} · {image_data.filename}")
        precision_note = "" if image_data.precision_preserved else "；该格式经 Pillow 解码后为 8-bit 显示精度"
        cache_note = "（缓存）" if from_cache else ""
        self.status_var.set(
            f"已打开{cache_note} {image_data.filename}：{image_data.width}×{image_data.height}，"
            f"原始位深 {image_data.bit_depth}-bit{precision_note}。"
        )
        self._refresh_information_sidebar()
        if all(self.images[active_role] is not None for active_role in self.active_roles):
            if self._fit_after_id is not None:
                try:
                    self.root.after_cancel(self._fit_after_id)
                except tk.TclError:
                    pass
            self._fit_after_id = self.root.after_idle(self._fit_loaded_images)
        anchor_role = self.roi_anchor_role
        if (
            anchor_role is not None
            and role != anchor_role
            and self.rois.get(anchor_role) is not None
            and self.images.get(anchor_role) is not None
        ):
            self._start_match(role)

    def _fit_loaded_images(self) -> None:
        self._fit_after_id = None
        if self._closed:
            return
        if all(self.images[role] is not None for role in self.active_roles):
            for role in self.active_roles:
                self.views[role].request_initial_fit()

    def _invalidate_role(self, role: str, *, clear_image: bool, refresh: bool = True) -> None:
        was_anchor = role == self.roi_anchor_role
        self._load_tokens[role] += 1
        self._analysis_tokens[role] += 1
        self._match_tokens[role] += 1
        self._matching_roles.discard(role)
        self.rois[role] = None
        self.roi_statistics[role] = None
        self.fixed_pixels[role] = None
        self.match_results[role] = None
        view = self.views[role]
        view.set_roi(None)
        view.set_sample_point(None)
        if clear_image:
            self.images[role] = None
            self.image_transform_ops[role] = []
            view.clear_image()
        if was_anchor:
            self.roi_anchor_role = None
            for target in IMAGE_ROLES:
                if target == role:
                    continue
                self._analysis_tokens[target] += 1
                self._match_tokens[target] += 1
                self._matching_roles.discard(target)
                self.rois[target] = None
                self.roi_statistics[target] = None
                self.match_results[target] = None
                self.views[target].set_roi(None)
            for target in COMPARISON_ROLES:
                self.comparisons[target] = None
        else:
            if role == "before":
                for target in COMPARISON_ROLES:
                    self.comparisons[target] = None
            elif role in COMPARISON_ROLES:
                self.comparisons[role] = None
        self._sync_pair_aliases()
        if refresh:
            self._refresh_outputs()

    def _sync_pair_aliases(self) -> None:
        if self.roi_anchor_role == "before":
            self.match_result = self.match_results["after"]
        elif self.roi_anchor_role == "after":
            self.match_result = self.match_results["before"]
        else:
            self.match_result = None
        self.comparison = self.comparisons["after"]

    def _on_pixel(self, role: str, x: int, y: int, fixed: bool) -> None:
        image_data = self.images.get(role)
        if image_data is None or not fixed:
            return
        normalized_x = x / max(1, image_data.width - 1)
        normalized_y = y / max(1, image_data.height - 1)
        for target in self.active_roles:
            target_image = self.images.get(target)
            if target_image is None:
                continue
            target_x = min(target_image.width - 1, max(0, int(round(normalized_x * (target_image.width - 1)))))
            target_y = min(target_image.height - 1, max(0, int(round(normalized_y * (target_image.height - 1)))))
            try:
                metrics = pixel_metrics(target_image, target_x, target_y)
            except ImageInspectorError:
                continue
            self.fixed_pixels[target] = metrics
            self.views[target].set_sample_point((target_x, target_y))
        self._metric_mode = "pixel"
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
        anchor_role = self.roi_anchor_role
        if anchor_role is None or self.rois.get(anchor_role) is None:
            anchor_role = role
            self.roi_anchor_role = role
        self._metric_mode = "roi"
        if role == anchor_role:
            for target in IMAGE_ROLES:
                self._match_tokens[target] += 1
                self._matching_roles.discard(target)
                self.match_results[target] = None
                if target == anchor_role:
                    continue
                self._analysis_tokens[target] += 1
                self.rois[target] = None
                self.roi_statistics[target] = None
                self.views[target].set_roi(None)
            for target in COMPARISON_ROLES:
                self.comparisons[target] = None
            self.rois[role] = named
            self.roi_statistics[role] = None
            self.views[role].set_roi(named, colour="#30D158")
        else:
            anchor_roi = self.rois[anchor_role]
            assert anchor_roi is not None
            self._match_tokens[role] += 1
            self._matching_roles.discard(role)
            self.rois[role] = named
            self.roi_statistics[role] = None
            self.views[role].set_roi(named, colour="#FF9F0A")
            self.match_results[role] = manual_match(anchor_roi, named)
            self._refresh_match_status()
        self._sync_pair_aliases()
        self._refresh_outputs()
        self._start_analysis(role, named)
        if role == anchor_role:
            for target in self.active_roles:
                if target == anchor_role:
                    continue
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
            messagebox.showerror("选区统计失败", str(exc), parent=self.root)
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
        anchor_role = self.roi_anchor_role
        if anchor_role is None or role == anchor_role or role not in self.active_roles:
            return
        anchor_image = self.images[anchor_role]
        target_image = self.images[role]
        anchor_roi = self.rois[anchor_role]
        if anchor_image is None or target_image is None or anchor_roi is None:
            return
        self._match_tokens[role] += 1
        token = self._match_tokens[role]
        self._matching_roles.add(role)
        self._refresh_match_status()
        self.status_var.set(
            f"正在从 {_role_label(anchor_role)} 匹配 {_role_label(role)} 邻近区域..."
        )
        self._submit(
            "match",
            token,
            role,
            match_roi,
            anchor_image.rgb,
            target_image.rgb,
            anchor_roi,
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
        self.views[role].set_roi(result.after_roi, colour="#30D158" if result.reliable else "#FF453A")
        self._sync_pair_aliases()
        self._refresh_match_status()
        self._start_analysis(role, result.after_roi)
        if result.warning:
            self.status_var.set(result.warning)

    def _refresh_match_status(self) -> None:
        targets = tuple(role for role in self.active_roles if role != self.roi_anchor_role)
        if not targets:
            self.match_status_var.set("匹配：—")
            return
        sections = []
        for role in targets:
            if role in self._matching_roles:
                sections.append(f"{_role_label(role)}: 匹配中")
                continue
            result = self.match_results[role]
            if result is None:
                sections.append(f"{_role_label(role)}: —")
            elif result.manually_confirmed and result.method == "用户手动选择":
                sections.append(f"{_role_label(role)}: 手动确认")
            elif result.manually_confirmed:
                sections.append(f"{_role_label(role)}: {result.score * 100.0:.1f}% 已接受")
            else:
                gate = "" if result.reliable else " 未通过门禁"
                sections.append(f"{_role_label(role)}: {result.score * 100.0:.1f}% {result.confidence}{gate}")
        self.match_status_var.set("匹配 · " + " | ".join(sections))

    def accept_match(self) -> None:
        roles = [
            role
            for role in self.active_roles
            if role != self.roi_anchor_role and self.match_results[role] is not None
        ]
        if not roles:
            messagebox.showinfo("尚无匹配", "请先在任意图片中框选 ROI 并等待匹配完成。", parent=self.root)
            return
        for role in roles:
            result = self.match_results[role]
            assert result is not None
            confirmed = confirm_match(result)
            self.match_results[role] = confirmed
            self.views[role].set_roi(confirmed.after_roi, colour="#30D158")
        self._sync_pair_aliases()
        self._refresh_match_status()
        self.status_var.set(f"已接受 {len(roles)} 个选区匹配。")
        self._refresh_outputs()

    def clear_roi(self) -> None:
        for role in IMAGE_ROLES:
            self._analysis_tokens[role] += 1
            self._match_tokens[role] += 1
            self._matching_roles.discard(role)
            self.rois[role] = None
            self.roi_statistics[role] = None
            self.match_results[role] = None
            self.views[role].set_roi(None)
        self.roi_anchor_role = None
        for target in COMPARISON_ROLES:
            self.comparisons[target] = None
        self._sync_pair_aliases()
        self._refresh_match_status()
        self._metric_mode = "pixel" if any(self.fixed_pixels.values()) else "none"
        self.status_var.set("ROI 已清除。")
        self._refresh_outputs()

    def _refresh_outputs(self) -> None:
        self._refresh_metric_strip()
        reference = self.roi_statistics["before"]
        for role in COMPARISON_ROLES:
            if role not in self.active_roles:
                self.comparisons[role] = None
                continue
            target = self.roi_statistics[role]
            if reference is None or target is None:
                self.comparisons[role] = None
                continue
            reliable, score, manually_confirmed, _style = self._comparison_pair_gate("before", role)
            self.comparisons[role] = compare_statistics(
                reference,
                target,
                reliable=reliable,
                match_score=score,
                manually_confirmed=manually_confirmed,
            )
        self._sync_pair_aliases()
        self._refresh_comparison_table()
        self._refresh_information_sidebar()

    def _role_from_label(self, label: str) -> Optional[str]:
        return next((role for role in self.active_roles if _role_label(role) == label), None)

    def _on_comparison_pair_changed(self, changed: str) -> None:
        if self._comparison_pair_guard:
            return
        labels = tuple(_role_label(role) for role in self.active_roles)
        base_label = self.comparison_base_var.get()
        target_label = self.comparison_role_var.get()
        if base_label and base_label == target_label and len(labels) > 1:
            replacement = next(label for label in labels if label != base_label)
            self._comparison_pair_guard = True
            if changed == "base":
                self.comparison_role_var.set(replacement)
            else:
                self.comparison_base_var.set(replacement)
            self._comparison_pair_guard = False
        self._refresh_comparison_table()

    def _swap_comparison_pair(self) -> None:
        base_label = self.comparison_base_var.get()
        target_label = self.comparison_role_var.get()
        if not base_label or not target_label:
            return
        self._comparison_pair_guard = True
        self.comparison_base_var.set(target_label)
        self.comparison_role_var.set(base_label)
        self._comparison_pair_guard = False
        self._refresh_comparison_table()

    def _comparison_pair_gate(
        self,
        base_role: str,
        target_role: str,
    ) -> Tuple[bool, Optional[float], bool, str]:
        anchor_role = self.roi_anchor_role
        if anchor_role is None:
            return False, None, False, "low"
        required_roles = tuple(role for role in (base_role, target_role) if role != anchor_role)
        matches = [self.match_results[role] for role in required_roles]
        if any(match is None for match in matches):
            return False, None, False, "low"
        resolved = [match for match in matches if match is not None]
        reliable = bool(resolved) and all(match.reliable for match in resolved)
        score = min((match.score for match in resolved), default=None)
        manually_confirmed = bool(resolved) and all(match.manually_confirmed for match in resolved)
        if manually_confirmed or (reliable and all(match.confidence == "高" for match in resolved)):
            style = "high"
        elif reliable:
            style = "medium"
        else:
            style = "low"
        return reliable, score, manually_confirmed, style

    @staticmethod
    def _percentage_change(
        before: Optional[float],
        delta: Optional[float],
        *,
        tolerance: float = 1e-9,
    ) -> str:
        if before is None or delta is None:
            return "—"
        if abs(delta) <= tolerance:
            return "≈ 0.00%"
        direction = "↑" if delta > 0.0 else "↓"
        percentage = (
            math.copysign(100.0, delta)
            if abs(before) <= 1e-12
            else delta / abs(before) * 100.0
        )
        return f"{direction} {percentage:+.2f}%"

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
        base_label = self.comparison_base_var.get()
        target_label = self.comparison_role_var.get()
        base_role = self._role_from_label(base_label)
        target_role = self._role_from_label(target_label)
        self.compare_tree.heading("reference", text=base_label or "图像 A")
        self.compare_tree.heading("target", text=target_label or "图像 B")
        if base_role is None or target_role is None or base_role == target_role:
            self.reference_swatch.configure(background=BORDER)
            self.target_swatch.configure(background=BORDER)
            self.comparison_files_var.set("请选择 2–4 张图片并框选区域")
            self.comparison_gate_var.set("匹配置信度：—")
            self.comparison_gate_label.configure(style="InspectorMatchLow.TLabel")
            self.compare_tree.insert("", "end", values=("等待对比数据", "—", "—", "—", "—"))
            return

        reference_image = self.images[base_role]
        target_image = self.images[target_role]
        reference_name = "尚未加载" if reference_image is None else reference_image.filename
        target_name = "尚未加载" if target_image is None else target_image.filename
        self.comparison_files_var.set(f"{reference_name}  →  {target_name}")
        reliable, score, manually_confirmed, gate_style = self._comparison_pair_gate(base_role, target_role)
        if score is None:
            self.comparison_gate_var.set("匹配置信度：等待选区")
            self.comparison_gate_label.configure(style="InspectorMatchLow.TLabel")
        elif manually_confirmed:
            self.comparison_gate_var.set(f"匹配置信度：已手动确认 · 组合最低 {score * 100.0:.1f}%")
            self.comparison_gate_label.configure(style="InspectorMatchHigh.TLabel")
        else:
            gate = "允许保守解释" if reliable else "未通过门禁，仅显示数值"
            self.comparison_gate_var.set(f"匹配置信度：组合最低 {score * 100.0:.1f}% · {gate}")
            self.comparison_gate_label.configure(
                style={
                    "high": "InspectorMatchHigh.TLabel",
                    "medium": "InspectorMatchMedium.TLabel",
                    "low": "InspectorMatchLow.TLabel",
                }[gate_style]
            )
        base_stats = self.roi_statistics[base_role]
        target_stats = self.roi_statistics[target_role]
        result = None
        if base_stats is not None and target_stats is not None:
            result = compare_statistics(
                base_stats,
                target_stats,
                reliable=reliable,
                match_score=score,
                manually_confirmed=manually_confirmed,
            )
        if result is None:
            self.reference_swatch.configure(background=BORDER)
            self.target_swatch.configure(background=BORDER)
            self.compare_tree.insert("", "end", values=("等待选区", "—", "—", "—", "—"))
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
        ) -> None:
            rows.append(
                (
                    label,
                    self._comparison_value(before, digits),
                    self._comparison_value(after, digits),
                    self._comparison_value(delta, digits, signed=True),
                    self._percentage_change(before, delta, tolerance=10 ** (-(digits + 1))),
                )
            )

        for index, channel in enumerate("RGB"):
            add(
                f"Mean {channel}（0–255）",
                result.before.mean_rgb[index],
                result.after.mean_rgb[index],
                result.delta_rgb[index],
                digits=2,
            )
        for index, channel in enumerate("RGB"):
            delta_share = result.delta_normalized_rgb[index] * 100.0
            add(
                f"{channel} 占比 %",
                result.before.normalized_rgb[index] * 100.0,
                result.after.normalized_rgb[index] * 100.0,
                delta_share,
                digits=3,
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
            "绝对亮度（0–255）",
            result.before.display_luminance,
            result.after.display_luminance,
            result.delta_display_luminance,
            digits=2,
        )
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
        )
        add(
            "暗部像素 %",
            result.before.dark_ratio * 100.0,
            result.after.dark_ratio * 100.0,
            dark_delta,
            digits=3,
        )
        for row in rows:
            self.compare_tree.insert("", "end", values=row)

    def fit_images(self) -> None:
        for role in self.active_roles:
            self.views[role].fit()

    def one_to_one(self) -> None:
        for role in self.active_roles:
            self.views[role].one_to_one()

    def _zoom_views_from(
        self,
        _source_role: str,
        factor: float,
        normalized_x: float,
        normalized_y: float,
        viewport_x: float,
        viewport_y: float,
    ) -> None:
        """Apply one wheel/trackpad gesture to every displayed comparison."""

        for role in self.active_roles:
            self.views[role].apply_linked_zoom(
                factor,
                normalized_x,
                normalized_y,
                viewport_x,
                viewport_y,
            )

    def _zoom_active(self, factor: float) -> None:
        for role in self.active_roles:
            if self.views[role].image_data is not None:
                self.views[role].zoom_by(factor)
                return

    def zoom_in(self) -> None:
        self._zoom_active(1.25)

    def zoom_out(self) -> None:
        self._zoom_active(1.0 / 1.25)

    def _show_image_context_menu(self, role: str, x_root: int, y_root: int) -> None:
        """Open native image-orientation actions for the clicked viewport."""

        image_data = self.images.get(role)
        if role not in self.active_roles or image_data is None:
            return
        menu = getattr(self, "_image_context_menu", None)
        try:
            menu_alive = menu is not None and bool(menu.winfo_exists())
        except tk.TclError:
            menu_alive = False
        if not menu_alive:
            menu = tk.Menu(self.root, tearoff=False)
            self._image_context_menu = menu
        else:
            menu.delete(0, "end")
        menu.add_command(label=image_data.filename, state="disabled")
        if len(self.active_roles) > 1:
            menu.add_command(
                label="设为对比基准",
                command=lambda selected=role: self._set_comparison_base(selected),
            )
        if image_data.path in self.folder_paths:
            menu.add_command(
                label="在图库中显示",
                command=lambda path=image_data.path: self._reveal_in_gallery(path),
            )
        menu.add_separator()
        menu.add_command(
            label=ORIENTATION_LABELS["rotate_left"],
            command=lambda selected=role: self._apply_image_orientation(selected, "rotate_left"),
        )
        menu.add_command(
            label=ORIENTATION_LABELS["rotate_right"],
            command=lambda selected=role: self._apply_image_orientation(selected, "rotate_right"),
        )
        menu.add_separator()
        menu.add_command(
            label=ORIENTATION_LABELS["flip_horizontal"],
            command=lambda selected=role: self._apply_image_orientation(selected, "flip_horizontal"),
        )
        menu.add_command(
            label=ORIENTATION_LABELS["flip_vertical"],
            command=lambda selected=role: self._apply_image_orientation(selected, "flip_vertical"),
        )
        menu.add_separator()
        menu.add_command(
            label="还原图片方向",
            command=lambda selected=role: self._reset_image_orientation(selected),
            state="normal" if self.image_transform_ops[role] else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label="重命名此图片...",
            command=lambda path=image_data.path: self.batch_rename_paths([path]),
        )
        if len(self.current_paths) > 1:
            menu.add_command(
                label=f"重命名当前所选 {len(self.current_paths)} 张...",
                command=lambda: self.batch_rename_paths(self.current_paths),
            )
        try:
            menu.tk_popup(int(x_root), int(y_root))
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    def _set_comparison_base(self, role: str) -> None:
        if role not in self.active_roles or not hasattr(self, "comparison_base_var"):
            return
        label = _role_label(role)
        self.comparison_base_var.set(label)
        if self.comparison_role_var.get() == label:
            alternative = next((_role_label(item) for item in self.active_roles if item != role), "")
            self.comparison_role_var.set(alternative)
        self._refresh_comparison_table()

    def _reveal_in_gallery(self, path: Path) -> None:
        if path not in self.folder_paths:
            return
        self._set_folder_sidebar_visible(True)
        self.folder_thumbnail_strip.set_active_paths(self.current_paths)

    def _show_thumbnail_context_menu(self, path: Path, x_root: int, y_root: int) -> None:
        menu = getattr(self, "_thumbnail_context_menu", None)
        try:
            menu_alive = menu is not None and bool(menu.winfo_exists())
        except tk.TclError:
            menu_alive = False
        if not menu_alive:
            menu = tk.Menu(self.root, tearoff=False)
            self._thumbnail_context_menu = menu
        else:
            menu.delete(0, "end")
        menu.add_command(label=path.name, state="disabled")
        menu.add_command(
            label="只查看此图片",
            command=lambda selected=path: self._on_thumbnail_selected(selected, False, False),
        )
        selected_paths = self.current_paths if path in self.current_paths else [path]
        menu.add_command(
            label=f"重命名所选 {len(selected_paths)} 张...",
            command=lambda values=tuple(selected_paths): self.batch_rename_paths(values),
        )
        if self.folder_paths:
            menu.add_command(label="批量重命名当前文件夹...", command=self.batch_rename_folder)
        role = next(
            (
                active_role
                for active_role in self.active_roles
                if self.images.get(active_role) is not None
                and self.images[active_role].path == path  # type: ignore[union-attr]
            ),
            None,
        )
        if role is not None:
            menu.add_separator()
            menu.add_command(
                label=ORIENTATION_LABELS["rotate_left"],
                command=lambda selected=role: self._apply_image_orientation(selected, "rotate_left"),
            )
            menu.add_command(
                label=ORIENTATION_LABELS["rotate_right"],
                command=lambda selected=role: self._apply_image_orientation(selected, "rotate_right"),
            )
            menu.add_command(
                label=ORIENTATION_LABELS["flip_horizontal"],
                command=lambda selected=role: self._apply_image_orientation(selected, "flip_horizontal"),
            )
            menu.add_command(
                label=ORIENTATION_LABELS["flip_vertical"],
                command=lambda selected=role: self._apply_image_orientation(selected, "flip_vertical"),
            )
        try:
            menu.tk_popup(int(x_root), int(y_root))
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    def batch_rename_folder(self) -> None:
        paths = self.folder_paths or self.current_paths
        if not paths:
            messagebox.showinfo("尚无图片", "请先打开图片或图片文件夹。", parent=self.root)
            return
        initially_selected = self.current_paths if self.folder_paths else paths
        self.batch_rename_paths(paths, initially_selected=initially_selected)

    def batch_rename_paths(
        self,
        paths: Sequence[Path],
        *,
        initially_selected: Optional[Sequence[Path]] = None,
    ) -> None:
        unique_paths = selected_paths_in_folder_order(self.folder_paths, paths) if self.folder_paths else [
            Path(path) for path in paths
        ]
        if not unique_paths:
            unique_paths = [Path(path) for path in paths]
        existing = self._batch_rename_dialog
        if existing is not None:
            try:
                if existing.dialog.winfo_exists():
                    existing.dialog.lift()
                    existing.dialog.focus_force()
                    return
            except tk.TclError:
                pass
        self._batch_rename_dialog = BatchRenameDialog(
            self.root,
            unique_paths,
            initially_selected=initially_selected,
            on_complete=self._apply_renamed_paths,
        )

    def _apply_renamed_paths(self, mapping: dict[Path, Path]) -> None:
        self._batch_rename_dialog = None
        resolved = {Path(source).resolve(): Path(destination).resolve() for source, destination in mapping.items()}

        def renamed(path: Path) -> Path:
            return resolved.get(Path(path).resolve(), Path(path).resolve())

        self.folder_paths = [renamed(path) for path in self.folder_paths]
        self.folder_groups = [[renamed(path) for path in group] for group in self.folder_groups]
        self.current_paths = [renamed(path) for path in self.current_paths]
        if self.folder_selection_anchor is not None:
            self.folder_selection_anchor = renamed(self.folder_selection_anchor)
        self._image_cache.clear()
        for role in self.active_roles:
            image_data = self.images.get(role)
            if image_data is None:
                continue
            image_data.path = renamed(image_data.path)
            self._image_cache.put(image_data)
            self.views[role].set_title(f"{_role_label(role)} · {image_data.filename}")
        if self.folder_paths:
            self.folder_thumbnail_strip.set_paths(self.folder_paths)
            self.folder_thumbnail_strip.set_active_paths(self.current_paths)
        self._refresh_information_sidebar()
        self._update_group_navigation()
        changed = sum(source != destination for source, destination in resolved.items())
        self.status_var.set(f"已安全重命名 {changed} 张图片；图片内容与扩展名未改变。")

    def _replace_oriented_image(
        self,
        role: str,
        image_data: ImageData,
        operations: Sequence[str],
        description: str,
    ) -> None:
        self._invalidate_role(role, clear_image=False, refresh=False)
        self.images[role] = image_data
        self.image_transform_ops[role] = list(operations)
        view = self.views[role]
        view.set_image(image_data)
        view.set_title(f"{_role_label(role)} · {image_data.filename}")
        for active_role in self.active_roles:
            self.fixed_pixels[active_role] = None
            self.views[active_role].set_sample_point(None)
        self._metric_mode = "roi" if any(self.roi_statistics.values()) else "none"
        self._refresh_outputs()
        self._refresh_information_sidebar()
        anchor_role = self.roi_anchor_role
        if (
            anchor_role is not None
            and role != anchor_role
            and self.rois.get(anchor_role) is not None
            and self.images.get(anchor_role) is not None
        ):
            self._start_match(role)
        self.root.after_idle(self.fit_images)
        self.status_var.set(f"{_role_label(role)} 已{description}；仅改变当前查看，不修改原文件。")

    def _apply_image_orientation(self, role: str, operation: str) -> None:
        image_data = self.images.get(role)
        if image_data is None:
            return
        try:
            oriented = reorient_image(image_data, operation)
        except ImageInspectorError as exc:
            messagebox.showerror("图片方向调整失败", str(exc), parent=self.root)
            return
        operations = [*self.image_transform_ops[role], operation]
        self._replace_oriented_image(role, oriented, operations, ORIENTATION_LABELS[operation])

    def _reset_image_orientation(self, role: str) -> None:
        image_data = self.images.get(role)
        operations = self.image_transform_ops.get(role, [])
        if image_data is None or not operations:
            return
        original = self._image_cache.get(image_data.path)
        if original is None:
            original = image_data
            for operation in reversed(operations):
                original = reorient_image(original, INVERSE_ORIENTATION[operation])
            self._image_cache.put(original)
        self._replace_oriented_image(role, original, (), "还原图片方向")

    def _on_histogram_visibility_changed(self) -> None:
        """Backward-compatible entry point for the RGB global switch."""

        self._on_global_info_visibility_changed("rgb")

    def _on_global_info_visibility_changed(self, category: str) -> None:
        groups = {
            "rgb": (self.show_histogram_var, self.histogram_visible_vars),
            "luminance": (
                self.show_luminance_histogram_var,
                self.luminance_histogram_visible_vars,
            ),
            "exif": (self.show_exif_var, self.exif_visible_vars),
        }
        if category not in groups:
            return
        master, variables = groups[category]
        for variable in variables.values():
            variable.set(master.get())
        self._refresh_information_sidebar()
        self._settings_changed()

    def _settings_changed(self) -> None:
        self._persist_settings()

    def _persist_settings(self) -> None:
        if self._closed:
            return
        panel_ratio = self._sidebar_ratio
        try:
            panes = [str(pane) for pane in self.main_pane.panes()]
            if str(self.sidebar_frame) in panes and len(panes) > 1 and self.main_pane.winfo_width() > 0:
                width = self.main_pane.winfo_width()
                panel_ratio = (width - self.main_pane.sashpos(len(panes) - 2)) / width
                panel_ratio = min(
                    MAX_ANALYSIS_PANEL_RATIO,
                    max(MIN_ANALYSIS_PANEL_RATIO, panel_ratio),
                )
                self._sidebar_ratio = panel_ratio
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
            show_luminance_histogram=self.show_luminance_histogram_var.get(),
            show_exif=self.show_exif_var.get(),
            live_pixel=self.live_pixel_var.get(),
            default_roi_name=self.settings.default_roi_name,
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
            "本工具可直接打开或从左侧图库选择 1–4 张普通 JPG/JPEG/PNG/BMP/TIFF；"
            "点按单选，⌘/Ctrl 点按多选。底部显示同步像素或选区的 R/G/B、R/G 与 R/B，"
            "右侧检查器可逐图显示直方图和 EXIF。多图时可任意选择图像 A/B；"
            "顶部总开关可统一控制 RGB、亮度与 EXIF；右键图片可做非破坏旋转或镜像。"
            "首次框选的图片作为选区锚点，其他图片仍可逐张手动调整，文件名和角色不会被预设。\n\n"
            f"自动匹配：{backend}；支持轻微平移、很小裁切以及轻微曝光/颜色变化。"
            "旋转、透视、大幅缩放、物体移动、遮挡或景深变化可能导致失败。\n\n"
            "低于配置阈值时对比表只显示原始变化数值。"
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

    def _check_for_updates(self) -> None:
        update_controller_for(self.root).check(manual=True)

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
        for view in self.views.values():
            view.resume_rendering()
        self.root.after_idle(self.fit_images)
        return True

    def hide(self) -> None:
        if self.is_alive():
            self._persist_settings()
            for view in self.views.values():
                view.suspend_rendering()
            self._image_cache.retain(list(self.current_paths))
            self.outer.pack_forget()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._persist_settings()
        self._closed = True
        self._folder_thumbnail_generation += 1
        self.folder_thumbnail_strip.shutdown()
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        if self._fit_after_id is not None:
            try:
                self.root.after_cancel(self._fit_after_id)
            except tk.TclError:
                pass
            self._fit_after_id = None
        for future in tuple(self._futures):
            future.cancel()
        self._image_cache.clear()
        # Future callbacks capture the workspace in order to enqueue results.
        # Let running workers release those callbacks before Tk widgets and
        # PhotoImages are destroyed; otherwise Tcl objects can be finalized by
        # a worker thread during rapid close/reopen or a test-suite teardown.
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._load_executor.shutdown(wait=True, cancel_futures=True)
        self._futures.clear()
        while True:
            try:
                self._result_queue.get_nowait()
            except queue.Empty:
                break
        for role, view in self.views.items():
            view.clear_image()
            self.images[role] = None
            self.rois[role] = None
            self.roi_statistics[role] = None
            self.fixed_pixels[role] = None
            self.match_results[role] = None
        for role in COMPARISON_ROLES:
            self.comparisons[role] = None
        for histogram in (
            *self.histogram_canvases.values(),
            *self.luminance_histogram_canvases.values(),
        ):
            histogram.histogram = None
        context_menu = getattr(self, "_image_context_menu", None)
        thumbnail_menu = getattr(self, "_thumbnail_context_menu", None)
        for menu in (context_menu, thumbnail_menu):
            if menu is None:
                continue
            try:
                menu.destroy()
            except tk.TclError:
                pass
        if self._batch_rename_dialog is not None:
            self._batch_rename_dialog.close()
            self._batch_rename_dialog = None

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
