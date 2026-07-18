"""Reusable ColorChecker preview widgets for TuneLab's unified CCM page.

The former standalone ColorChecker workspace was intentionally removed when
its image input was merged into :class:`tunelab.app.TuneLabApp`.  Detection,
optimization, reporting, History and XML write-back now have one owner; this
module only keeps the image preview component reusable and testable.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Sequence

import numpy as np
from PIL import Image, ImageTk

from ..image_inspector.types import ImageData
from ..ui_foundation import FONT_BODY, FONT_SMALL_BOLD, TERTIARY
from .engine import PatchPolygon


CANVAS_BG = "#1C1C1E"


class PreviewPane(ttk.Frame):
    """Responsive fit-to-window image preview with detected patch overlays."""

    def __init__(self, master: tk.Misc, title: str, overlay_enabled: Callable[[], bool]) -> None:
        super().__init__(master, style="CheckerCard.TFrame")
        self.overlay_enabled = overlay_enabled
        self.image_data: Optional[ImageData] = None
        self._rgb: Optional[np.ndarray] = None
        self._pil: Optional[Image.Image] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._polygons: tuple[PatchPolygon, ...] = ()
        self._redraw_after: Optional[str] = None
        header = ttk.Frame(self, padding=(8, 6), style="CheckerCard.TFrame")
        header.pack(fill="x")
        self.title_var = tk.StringVar(value=title)
        self.meta_var = tk.StringVar(value="尚未加载")
        ttk.Label(header, textvariable=self.title_var, style="CheckerCardTitle.TLabel").pack(anchor="w")
        ttk.Label(header, textvariable=self.meta_var, style="CheckerMutedCard.TLabel").pack(anchor="w", pady=(1, 0))
        self.canvas = tk.Canvas(self, background=CANVAS_BG, highlightthickness=0, height=420)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._schedule_redraw)
        self._draw_placeholder()

    def _draw_placeholder(self) -> None:
        self.canvas.delete("all")
        width = max(240, self.canvas.winfo_width())
        height = max(240, self.canvas.winfo_height())
        self.canvas.create_text(width / 2, height / 2, text="等待图片", fill=TERTIARY, font=FONT_BODY)

    def clear(self) -> None:
        self.image_data = None
        self._rgb = None
        self._pil = None
        self._photo = None
        self._polygons = ()
        self.meta_var.set("尚未加载")
        self._draw_placeholder()

    def set_image(
        self,
        image: ImageData,
        *,
        rgb: Optional[np.ndarray] = None,
        polygons: Sequence[PatchPolygon] = (),
        meta_suffix: str = "",
    ) -> None:
        self.image_data = image
        self._rgb = np.ascontiguousarray(image.display_rgb if rgb is None else rgb, dtype=np.uint8)
        self._pil = Image.fromarray(self._rgb, mode="RGB")
        self._polygons = tuple(polygons)
        suffix = f" · {meta_suffix}" if meta_suffix else ""
        self.meta_var.set(f"{image.path.name} · {image.width}×{image.height}{suffix}")
        self.redraw()

    def _schedule_redraw(self, _event: Optional[tk.Event] = None) -> None:
        if self._redraw_after is not None:
            try:
                self.after_cancel(self._redraw_after)
            except tk.TclError:
                pass
        self._redraw_after = self.after(40, self.redraw)

    def redraw(self) -> None:
        self._redraw_after = None
        if self._pil is None or self.image_data is None:
            self._draw_placeholder()
            return
        width = max(80, self.canvas.winfo_width())
        height = max(80, self.canvas.winfo_height())
        scale = min((width - 12) / self.image_data.width, (height - 12) / self.image_data.height)
        scale = max(scale, 0.0001)
        render_width = max(1, int(round(self.image_data.width * scale)))
        render_height = max(1, int(round(self.image_data.height * scale)))
        rendered = self._pil.resize((render_width, render_height), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(rendered, master=self.canvas)
        left = (width - render_width) / 2.0
        top = (height - render_height) / 2.0
        self.canvas.delete("all")
        self.canvas.create_image(left, top, image=self._photo, anchor="nw")
        if self.overlay_enabled():
            for index, polygon in enumerate(self._polygons, start=1):
                coordinates = [
                    coordinate
                    for x_value, y_value in polygon
                    for coordinate in (left + x_value * scale, top + y_value * scale)
                ]
                self.canvas.create_polygon(*coordinates, outline="#64D2FF", fill="", width=1.6)
                centre_x = sum(point[0] for point in polygon) / 4.0
                centre_y = sum(point[1] for point in polygon) / 4.0
                self.canvas.create_text(
                    left + centre_x * scale,
                    top + centre_y * scale,
                    text=str(index),
                    fill="white",
                    font=FONT_SMALL_BOLD,
                )
