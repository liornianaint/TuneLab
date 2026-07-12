from __future__ import annotations

import json
import tkinter as tk
import tkinter.font as tkfont
import math
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable, Optional

from .color import lab_to_srgb
from .history import load_history, record_from_result, save_history
from .imatest import ImatestCSVError, parse_imatest_csv
from .models import CCRegion, ImatestDataset, Matrix3, OptimizationConfig, OptimizationResult, PatchResult
from .optimizer import OptimizationError, optimize_ccm
from .qualcomm_xml import QualcommCCDocument, QualcommXMLError
from .report import save_analysis_report
from .settings import AppSettings, load_settings, save_settings


APP_TITLE = "MatrixCorrect · Qualcomm CC13"
BG = "#F3F5F8"
PANEL = "#FFFFFF"
INK = "#172033"
MUTED = "#667085"
BLUE = "#2563EB"
GREEN = "#0F9D75"
RED = "#D92D20"
AMBER = "#B54708"
BORDER = "#DDE3EC"
LAB_PLOT_LIGHTNESS = 87.0
FONT_BODY = "MatrixCorrectBodyFont"
FONT_SMALL = "MatrixCorrectSmallFont"
FONT_SMALL_BOLD = "MatrixCorrectSmallBoldFont"
FONT_CARD_TITLE = "MatrixCorrectCardTitleFont"
FONT_KPI = "MatrixCorrectKpiFont"
FONT_TITLE = "MatrixCorrectTitleFont"
FONT_PLOT_TITLE = "MatrixCorrectPlotTitleFont"
FONT_MONO = "MatrixCorrectMonoFont"


class MatrixPanel(ttk.Frame):
    def __init__(self, master: tk.Misc, title: str) -> None:
        super().__init__(master, padding=8, style="Card.TFrame")
        self.title = title
        self.variables = [[tk.StringVar(value="—") for _ in range(3)] for _ in range(3)]
        header = ttk.Frame(self, style="Card.TFrame")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        header.columnconfigure(0, weight=1)
        self.title_var = tk.StringVar(value=title)
        ttk.Label(header, textvariable=self.title_var, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="复制", command=self.copy, style="Quiet.TButton").grid(row=0, column=1)
        for row in range(3):
            for col in range(3):
                ttk.Label(
                    self,
                    textvariable=self.variables[row][col],
                    width=10,
                    anchor="e",
                    style="Matrix.TLabel",
                ).grid(row=row + 1, column=col, padx=2, pady=2, sticky="ew")
                self.columnconfigure(col, weight=1)

    def set_matrix(self, matrix: Optional[Matrix3]) -> None:
        for row in range(3):
            for col in range(3):
                self.variables[row][col].set("—" if matrix is None else f"{matrix[row][col]: .7f}")

    def set_title(self, title: str) -> None:
        self.title = title
        self.title_var.set(title)

    def copy(self) -> None:
        text = "\n".join(" ".join(self.variables[row][col].get() for col in range(3)) for row in range(3))
        self.clipboard_clear()
        self.clipboard_append(text)


def calculate_lab_bounds(
    points: Iterable[tuple[float, float]],
    *,
    minimum_span: float = 60.0,
) -> tuple[float, float, float, float]:
    """Return padded, square a*b* bounds that contain every supplied point."""

    values = list(points)
    if not values:
        half_span = minimum_span / 2.0
        return (-half_span, half_span, -half_span, half_span)
    a_values = [point[0] for point in values]
    b_values = [point[1] for point in values]
    a_low, a_high = min(a_values), max(a_values)
    b_low, b_high = min(b_values), max(b_values)
    raw_span = max(a_high - a_low, b_high - b_low)
    # The fixed unit margin protects Patch symbols, number labels and arrowheads
    # even when all samples are tightly grouped.  The percentage margin keeps
    # wide-gamut datasets comfortable without imposing an arbitrary [-100,100]
    # crop.
    span = max(minimum_span, raw_span * 1.30, raw_span + 16.0)
    a_center = (a_low + a_high) / 2.0
    b_center = (b_low + b_high) / 2.0
    half_span = span / 2.0
    return (
        a_center - half_span,
        a_center + half_span,
        b_center - half_span,
        b_center + half_span,
    )


@dataclass
class LabViewState:
    auto_bounds: tuple[float, float, float, float] = (-30.0, 30.0, -30.0, 30.0)
    bounds: tuple[float, float, float, float] = (-30.0, 30.0, -30.0, 30.0)

    def fit(self, points: Iterable[tuple[float, float]]) -> None:
        self.auto_bounds = calculate_lab_bounds(points)
        self.bounds = self.auto_bounds

    def reset(self) -> None:
        self.bounds = self.auto_bounds

    def zoom(self, factor: float, anchor_a: float, anchor_b: float) -> None:
        a_min, a_max, b_min, b_max = self.bounds
        old_span = a_max - a_min
        auto_a_min, auto_a_max, auto_b_min, auto_b_max = self.auto_bounds
        auto_span = auto_a_max - auto_a_min
        minimum_span = max(8.0, auto_span * 0.20)
        maximum_span = min(800.0, auto_span * 2.0)
        new_span = max(minimum_span, min(old_span * factor, maximum_span))
        ratio_a = (anchor_a - a_min) / old_span
        ratio_b = (anchor_b - b_min) / old_span
        new_a_min = anchor_a - ratio_a * new_span
        new_b_min = anchor_b - ratio_b * new_span
        half_span = new_span / 2.0
        a_center = max(auto_a_min, min(auto_a_max, new_a_min + half_span))
        b_center = max(auto_b_min, min(auto_b_max, new_b_min + half_span))
        self.bounds = (
            a_center - half_span,
            a_center + half_span,
            b_center - half_span,
            b_center + half_span,
        )


def _nice_tick_step(span: float) -> float:
    target = max(span / 7.0, 1e-9)
    magnitude = 10.0 ** math.floor(math.log10(target))
    normalized = target / magnitude
    if normalized <= 1.0:
        multiplier = 1.0
    elif normalized <= 2.0:
        multiplier = 2.0
    elif normalized <= 5.0:
        multiplier = 5.0
    else:
        multiplier = 10.0
    return multiplier * magnitude


class LabPlot(ttk.Frame):
    LEFT_MARGIN = 58
    RIGHT_MARGIN = 18
    TOP_MARGIN = 48
    BOTTOM_MARGIN = 50

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        view_state: LabViewState,
        view_changed: Callable[[], None],
        patch_callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(master, style="Card.TFrame")
        self.title = title
        self.view_state = view_state
        self.view_changed = view_changed
        self.patch_callback = patch_callback
        self.patch_results: list[PatchResult] = []
        self.mode = "before"
        self.show_motion = True
        self.selected_zone: Optional[int] = None
        self.focus_zones: set[int] = {13, 14, 15}
        self._resize_after_id: Optional[str] = None
        self._geometry = (self.LEFT_MARGIN, self.TOP_MARGIN, 420.0)
        self.canvas = tk.Canvas(
            self,
            width=500,
            height=500,
            background=PANEL,
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        if tk.TkVersion >= 9.0:
            # Tk 9 emits high-resolution macOS/Windows trackpad gestures as a
            # distinct event; <MouseWheel> alone only covers legacy wheels.
            self.canvas.bind("<TouchpadScroll>", self._on_touchpad_scroll)
        self.canvas.bind("<Button-4>", lambda event: self._zoom_at(event, 0.84))
        self.canvas.bind("<Button-5>", lambda event: self._zoom_at(event, 1.0 / 0.84))
        self.canvas.bind("<Double-Button-1>", self._reset_view)
        self.draw([], mode="before", show_motion=True)

    def _plot_geometry(self) -> tuple[float, float, float]:
        width = float(self.canvas.winfo_width())
        height = float(self.canvas.winfo_height())
        if width <= 1.0:
            width = float(self.canvas.winfo_reqwidth())
        if height <= 1.0:
            height = float(self.canvas.winfo_reqheight())
        available_width = max(20.0, width - self.LEFT_MARGIN - self.RIGHT_MARGIN)
        available_height = max(20.0, height - self.TOP_MARGIN - self.BOTTOM_MARGIN)
        side = min(available_width, available_height)
        left = self.LEFT_MARGIN + max(0.0, (available_width - side) / 2.0)
        top = self.TOP_MARGIN + max(0.0, (available_height - side) / 2.0)
        self._geometry = (left, top, side)
        return self._geometry

    def _x(self, a_value: float) -> float:
        left, _top, side = self._geometry
        a_min, a_max, _b_min, _b_max = self.view_state.bounds
        return left + (a_value - a_min) / (a_max - a_min) * side

    def _y(self, b_value: float) -> float:
        _left, top, side = self._geometry
        _a_min, _a_max, b_min, b_max = self.view_state.bounds
        return top + (b_max - b_value) / (b_max - b_min) * side

    def _data_at(self, x_pos: float, y_pos: float) -> tuple[float, float]:
        left, top, side = self._geometry
        a_min, a_max, b_min, b_max = self.view_state.bounds
        a_value = a_min + (x_pos - left) / side * (a_max - a_min)
        b_value = b_max - (y_pos - top) / side * (b_max - b_min)
        return a_value, b_value

    def draw(
        self,
        patch_results: list[PatchResult],
        *,
        mode: str,
        show_motion: bool,
        focus_zones: Optional[Iterable[int]] = None,
    ) -> None:
        self.patch_results = patch_results
        self.mode = mode
        self.show_motion = show_motion
        if focus_zones is not None:
            self.focus_zones = set(focus_zones)
        self.redraw()

    def redraw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        left, top, side = self._plot_geometry()
        right, bottom = left + side, top + side
        a_min, a_max, b_min, b_max = self.view_state.bounds
        canvas.create_text(left, 16, text=self.title, fill=INK, anchor="w", font=FONT_PLOT_TITLE)

        tile_count = max(12, min(26, round(side / 18.0)))
        for row in range(tile_count):
            tile_top = top + row / tile_count * side
            tile_bottom = top + (row + 1) / tile_count * side + 1
            b_value = b_max - (row + 0.5) / tile_count * (b_max - b_min)
            for column in range(tile_count):
                tile_left = left + column / tile_count * side
                tile_right = left + (column + 1) / tile_count * side + 1
                a_value = a_min + (column + 0.5) / tile_count * (a_max - a_min)
                canvas.create_rectangle(
                    tile_left,
                    tile_top,
                    tile_right,
                    tile_bottom,
                    fill=lab_plane_hex(a_value, b_value),
                    outline="",
                    tags=("plot-background",),
                )

        tick_step = _nice_tick_step(a_max - a_min)
        first_a = math.ceil(a_min / tick_step) * tick_step
        value = first_a
        while value <= a_max + tick_step * 1e-6:
            x_pos = self._x(value)
            canvas.create_line(x_pos, top, x_pos, bottom, fill="#FFFFFF", stipple="gray50")
            canvas.create_text(x_pos, bottom + 17, text=_format_tick(value, tick_step), fill=MUTED, font=FONT_SMALL)
            value += tick_step
        first_b = math.ceil(b_min / tick_step) * tick_step
        value = first_b
        while value <= b_max + tick_step * 1e-6:
            y_pos = self._y(value)
            canvas.create_line(left, y_pos, right, y_pos, fill="#FFFFFF", stipple="gray50")
            canvas.create_text(left - 9, y_pos, text=_format_tick(value, tick_step), fill=MUTED, anchor="e", font=FONT_SMALL)
            value += tick_step
        canvas.create_rectangle(left, top, right, bottom, outline="#101828", width=1)
        canvas.create_text(left + side / 2.0, bottom + 37, text="a*", fill=INK)
        # macOS system Python 3.9 can ship Tk 8.5, whose Canvas text item does
        # not support the newer -angle option.
        canvas.create_text(max(14.0, left - 42.0), top + side / 2.0, text="b*", fill=INK)
        legend_y = top + 14
        canvas.create_rectangle(left + 9, legend_y - 4, left + 18, legend_y + 5, fill="#FFFFFF", outline="#172033")
        canvas.create_text(left + 25, legend_y, text="Ideal", fill=INK, anchor="w", font=FONT_SMALL)
        canvas.create_oval(left + 75, legend_y - 5, left + 87, legend_y + 7, fill="#FFFFFF", outline="#172033")
        canvas.create_text(left + 94, legend_y + 1, text="Camera", fill=INK, anchor="w", font=FONT_SMALL)

        occupied_labels: list[tuple[float, float, float, float]] = [
            (left + 4, top + 3, left + 145, top + 27)
        ]

        def lab_is_visible(lab: tuple[float, float, float]) -> bool:
            return a_min <= lab[1] <= a_max and b_min <= lab[2] <= b_max

        def overlap_count(box: tuple[float, float, float, float]) -> int:
            return sum(
                not (
                    box[2] + 2 < occupied[0]
                    or box[0] - 2 > occupied[2]
                    or box[3] + 2 < occupied[1]
                    or box[1] - 2 > occupied[3]
                )
                for occupied in occupied_labels
            )

        display_patches = sorted(
            self.patch_results,
            key=lambda patch: (patch.zone == self.selected_zone, patch.zone in self.focus_zones),
        )
        for patch in display_patches:
            actual_lab = patch.before_lab if self.mode == "before" else patch.after_lab
            ideal_lab = patch.ideal_lab
            before_visible = lab_is_visible(patch.before_lab)
            actual_visible = lab_is_visible(actual_lab)
            ideal_visible = lab_is_visible(ideal_lab)
            actual_x, actual_y = self._x(actual_lab[1]), self._y(actual_lab[2])
            ideal_x, ideal_y = self._x(ideal_lab[1]), self._y(ideal_lab[2])
            before_x, before_y = self._x(patch.before_lab[1]), self._y(patch.before_lab[2])
            actual_visible = actual_visible and left + 13 <= actual_x <= right - 13 and top + 13 <= actual_y <= bottom - 13
            ideal_visible = ideal_visible and left + 6 <= ideal_x <= right - 6 and top + 6 <= ideal_y <= bottom - 6
            before_visible = before_visible and left + 9 <= before_x <= right - 9 and top + 9 <= before_y <= bottom - 9
            ideal_color = _rgb_hex(patch.ideal_srgb)
            actual_color = _rgb_hex(patch.before_srgb if self.mode == "before" else patch.after_srgb)
            is_focus = patch.zone in self.focus_zones
            is_selected = patch.zone == self.selected_zone
            tag = f"patch-{patch.zone}"
            if self.show_motion:
                if self.mode == "after" and before_visible and actual_visible:
                    canvas.create_line(
                        before_x, before_y, actual_x, actual_y,
                        fill=BLUE, width=1.4, arrow=tk.LAST, arrowshape=(7, 8, 3), tags=(tag, "motion"),
                    )
                if ideal_visible and actual_visible:
                    canvas.create_line(ideal_x, ideal_y, actual_x, actual_y, fill="#667085", width=1, tags=(tag, "motion"))
            if actual_visible and (is_focus or is_selected):
                halo_color = AMBER if is_selected else "#84ADFF"
                halo_width = 3 if is_selected else 2
                canvas.create_oval(
                    actual_x - 11,
                    actual_y - 11,
                    actual_x + 11,
                    actual_y + 11,
                    outline=halo_color,
                    width=halo_width,
                    tags=(tag,),
                )
            if ideal_visible:
                canvas.create_rectangle(
                    ideal_x - 4, ideal_y - 4, ideal_x + 4, ideal_y + 4,
                    fill=ideal_color,
                    outline=AMBER if is_selected else "#172033",
                    width=2 if is_selected else 1,
                    tags=(tag,),
                )
            if actual_visible:
                canvas.create_oval(
                    actual_x - 6, actual_y - 6, actual_x + 6, actual_y + 6,
                    fill=actual_color,
                    outline=AMBER if is_selected else (BLUE if is_focus else "#172033"),
                    width=3 if is_selected else (2 if is_focus else 1),
                    tags=(tag,),
                )
                label_text = str(patch.zone)
                label_width = 10.0 + max(0, len(label_text) - 1) * 7.0
                label_height = 11.0
                candidates: list[tuple[float, float, tuple[float, float, float, float]]] = []
                for offset_x, offset_y in (
                    (15, 10), (15, -11), (-15, 10), (-15, -11),
                    (0, -18), (0, 19), (23, 0), (-23, 0),
                    (25, 15), (-25, 15), (25, -16), (-25, -16),
                ):
                    center_x = min(right - label_width / 2.0 - 3, max(left + label_width / 2.0 + 3, actual_x + offset_x))
                    center_y = min(bottom - label_height / 2.0 - 3, max(top + label_height / 2.0 + 3, actual_y + offset_y))
                    box = (
                        center_x - label_width / 2.0,
                        center_y - label_height / 2.0,
                        center_x + label_width / 2.0,
                        center_y + label_height / 2.0,
                    )
                    candidates.append((center_x, center_y, box))
                label_x, label_y, label_box = min(candidates, key=lambda candidate: overlap_count(candidate[2]))
                occupied_labels.append(label_box)
                canvas.create_line(actual_x, actual_y, label_x, label_y, fill="#98A2B3", width=1, tags=(tag,))
                canvas.create_text(
                    label_x,
                    label_y,
                    text=label_text,
                    fill=AMBER if is_selected else ("#1D4ED8" if is_focus else "#344054"),
                    anchor="center",
                    font=FONT_SMALL_BOLD if is_focus or is_selected else FONT_SMALL,
                    tags=(tag,),
                )
            if self.patch_callback is not None:
                canvas.tag_bind(tag, "<Button-1>", lambda _event, zone=patch.zone: self.patch_callback(zone))
                canvas.tag_bind(tag, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
                canvas.tag_bind(tag, "<Leave>", lambda _event: canvas.configure(cursor=""))

    def _on_resize(self, _event: tk.Event) -> None:
        if self._resize_after_id is not None:
            self.after_cancel(self._resize_after_id)
        self._resize_after_id = self.after(60, self._finish_resize)

    def _finish_resize(self) -> None:
        self._resize_after_id = None
        self.redraw()

    def _on_mousewheel(self, event: tk.Event) -> str:
        delta = getattr(event, "delta", 0)
        if not delta:
            return "break"
        self._zoom_at(event, 0.84 if delta > 0 else 1.0 / 0.84)
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
        # Trackpads can deliver ~60 small deltas per second.  Scale smoothly
        # instead of applying a full wheel notch for every high-resolution event.
        steps = min(4.0, max(0.15, abs(delta_y) / 40.0))
        factor = 0.84 ** steps if delta_y > 0 else (1.0 / 0.84) ** steps
        self._zoom_at(event, factor)
        return "break"

    def _zoom_at(self, event: tk.Event, factor: float) -> str:
        left, top, side = self._geometry
        x_pos = min(left + side, max(left, float(event.x)))
        y_pos = min(top + side, max(top, float(event.y)))
        anchor_a, anchor_b = self._data_at(x_pos, y_pos)
        self.view_state.zoom(factor, anchor_a, anchor_b)
        self.view_changed()
        return "break"

    def _reset_view(self, _event: tk.Event) -> str:
        self.view_state.reset()
        self.view_changed()
        return "break"


def _rgb_hex(rgb: tuple[float, float, float]) -> str:
    values = [max(0, min(255, round(value * 255))) for value in rgb]
    return f"#{values[0]:02x}{values[1]:02x}{values[2]:02x}"


def lab_plane_hex(a_value: float, b_value: float) -> str:
    """Shared Imatest-like a*b* plane colour for empty and populated plots."""

    return _rgb_hex(lab_to_srgb((LAB_PLOT_LIGHTNESS, a_value, b_value)))


def _format_tick(value: float, step: float) -> str:
    if abs(value) < step * 1e-6:
        value = 0.0
    if step >= 1.0:
        return f"{value:.0f}"
    decimals = max(1, int(-math.floor(math.log10(step))))
    return f"{value:.{decimals}f}"


class MatrixCorrectApp:
    COMPOSITION_LABELS = {
        "前乘 A × M（推荐：CC13 行主序）": "pre",
        "后乘 M × Aᵀ（旧 Excel/C7）": "post_transposed",
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.dataset: Optional[ImatestDataset] = None
        self.document: Optional[QualcommCCDocument] = None
        self.selected_region: Optional[CCRegion] = None
        self.result: Optional[OptimizationResult] = None
        self.region_display_to_index: dict[str, int] = {}
        self.settings = load_settings()
        self.history = load_history()
        self.xml_diff = ""
        self.lab_view = LabViewState()
        self._syncing_patch_selection = False
        self._settings_save_after_id: Optional[str] = None
        self._closing = False

        root.title(APP_TITLE)
        root.geometry("1520x980")
        root.minsize(1180, 760)
        root.configure(background=BG)
        self._configure_styles()
        self._build_menu()
        self._build_ui()
        self._install_settings_autosave()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after_idle(self._persist_settings)
        self._set_status("请先打开 Imatest CSV 和 Qualcomm CC XML。")

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        # tkfont.nametofont gained the ``root`` keyword after Python 3.9.
        # Constructing an existing named Font directly works on both the
        # system Python 3.9.6/Tk 8.5 and Homebrew Python 3.14/Tk 9 paths.
        default_font = tkfont.Font(root=self.root, name="TkDefaultFont", exists=True)
        fixed_font = tkfont.Font(root=self.root, name="TkFixedFont", exists=True)
        base_size = max(9, abs(int(default_font.cget("size"))))

        def install_font(name: str, *, size: int, weight: str = "normal", fixed: bool = False) -> None:
            source = fixed_font if fixed else default_font
            try:
                font = tkfont.Font(root=self.root, name=name, exists=True)
            except tk.TclError:
                font = tkfont.Font(root=self.root, name=name, exists=False)
            font.configure(
                family=source.actual("family"),
                size=size,
                weight=weight,
            )

        install_font(FONT_BODY, size=base_size)
        install_font(FONT_SMALL, size=max(8, base_size - 2))
        install_font(FONT_SMALL_BOLD, size=max(8, base_size - 2), weight="bold")
        install_font(FONT_CARD_TITLE, size=base_size, weight="bold")
        install_font(FONT_KPI, size=base_size + 1, weight="bold")
        install_font(FONT_TITLE, size=base_size + 8, weight="bold")
        install_font(FONT_PLOT_TITLE, size=base_size + 1, weight="bold")
        install_font(FONT_MONO, size=max(8, base_size - 1), fixed=True)
        style.configure("Root.TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, relief="flat")
        style.configure("Card.TLabel", background=PANEL, foreground=INK, font=FONT_BODY)
        style.configure("CardTitle.TLabel", background=PANEL, foreground=INK, font=FONT_CARD_TITLE)
        style.configure("Title.TLabel", background=BG, foreground=INK, font=FONT_TITLE)
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=FONT_BODY)
        style.configure("ActiveRegion.TLabel", background=BG, foreground="#1D4ED8", font=FONT_SMALL_BOLD)
        style.configure("Status.TLabel", background="#EAF0FF", foreground="#1D4ED8", padding=(10, 7))
        style.configure("Matrix.TLabel", background="#F8FAFC", foreground=INK, padding=(4, 4), font=FONT_MONO)
        style.configure("Primary.TButton", background=BLUE, foreground="white", padding=(14, 8), borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#1D4ED8"), ("disabled", "#98A2B3")])
        style.configure("Quiet.TButton", padding=(8, 4))
        style.configure("Kpi.TLabel", background=PANEL, foreground=INK, font=FONT_KPI)
        style.configure("KpiCompact.TLabel", background=PANEL, foreground=INK, font=FONT_KPI)
        style.configure("KpiCaption.TLabel", background=PANEL, foreground=MUTED, font=FONT_BODY)
        style.configure("Treeview", rowheight=27, fieldbackground=PANEL, background=PANEL, foreground=INK, font=FONT_BODY)
        style.configure("Treeview.Heading", background="#EAECF0", foreground=INK, font=FONT_SMALL_BOLD)

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="打开 Imatest CSV...", command=self.load_csv)
        file_menu.add_command(label="打开 Qualcomm CC XML...", command=self.load_xml)
        file_menu.add_command(label="保存 XML...", command=self.save_xml)
        file_menu.add_command(label="导出工程报告...", command=self.save_report)
        file_menu.add_command(label="退出", command=self.close)
        self.file_menu = file_menu
        menu.add_cascade(label="文件", menu=file_menu)
        config_menu = tk.Menu(menu, tearoff=False)
        config_menu.add_command(label="导入配置...", command=self.import_settings)
        config_menu.add_command(label="导出配置...", command=self.export_settings)
        self.config_menu = config_menu
        menu.add_cascade(label="配置", menu=config_menu)
        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="Gamma 优化...", command=self.open_gamma_optimizer)
        self.tools_menu = tools_menu
        menu.add_cascade(label="工具", menu=tools_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="算法边界", command=self.show_assumptions)
        help_menu.add_command(label="关于", command=lambda: messagebox.showinfo("关于", f"{APP_TITLE}\n版本 0.2.0"))
        menu.add_cascade(label="帮助", menu=help_menu)
        self.root.configure(menu=menu)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(20, 16), style="Root.TFrame")
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer, style="Root.TFrame")
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="MatrixCorrect", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Qualcomm CC13 色彩还原、模拟与 XML 回写", style="Subtitle.TLabel").pack(side="left", padx=(14, 0), pady=(8, 0))

        controls = ttk.Frame(outer, padding=14, style="Card.TFrame")
        controls.pack(fill="x", pady=(0, 10))
        controls.columnconfigure(5, weight=1)
        ttk.Button(controls, text="1  打开 Imatest CSV", command=self.load_csv).grid(row=0, column=0, rowspan=2, padx=(0, 8), sticky="ns")
        ttk.Button(controls, text="2  打开 CC XML", command=self.load_xml).grid(row=0, column=1, rowspan=2, padx=(0, 16), sticky="ns")
        ttk.Label(controls, text="CCT / K", style="Card.TLabel").grid(row=0, column=2, sticky="w")
        self.cct_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.cct_var, width=9).grid(row=1, column=2, sticky="w", padx=(0, 6))
        ttk.Button(controls, text="自动匹配", command=self.auto_match_region, style="Quiet.TButton").grid(row=1, column=3, sticky="w", padx=(0, 14))
        ttk.Label(controls, text="CCT region / 完整触发路径", style="Card.TLabel").grid(row=0, column=4, columnspan=2, sticky="w")
        self.region_var = tk.StringVar()
        self.region_combo = ttk.Combobox(controls, textvariable=self.region_var, state="readonly", width=58)
        self.region_combo.grid(row=1, column=4, columnspan=2, sticky="ew", padx=(0, 14))
        self.region_combo.bind("<<ComboboxSelected>>", self._on_region_selected)
        ttk.Label(controls, text="组合约定", style="Card.TLabel").grid(row=0, column=6, sticky="w")
        composition_label = next(
            (label for label, value in self.COMPOSITION_LABELS.items() if value == self.settings.composition),
            next(iter(self.COMPOSITION_LABELS)),
        )
        self.composition_var = tk.StringVar(value=composition_label)
        ttk.Combobox(
            controls,
            textvariable=self.composition_var,
            values=list(self.COMPOSITION_LABELS),
            state="readonly",
            width=27,
        ).grid(row=1, column=6, sticky="w", padx=(0, 14))
        ttk.Label(controls, text="最大强度", style="Card.TLabel").grid(row=0, column=7, sticky="w")
        self.strength_var = tk.DoubleVar(value=self.settings.optimization.max_blend * 100.0)
        ttk.Scale(controls, from_=20, to=100, variable=self.strength_var, orient="horizontal", length=100).grid(row=1, column=7, sticky="w", padx=(0, 14))
        self.optimize_button = ttk.Button(controls, text="3  自动优化", command=self.run_optimization, style="Primary.TButton")
        self.optimize_button.grid(row=0, column=8, rowspan=2, sticky="ns")
        self.save_xml_button = ttk.Button(controls, text="保存 XML", command=self.save_xml, state="disabled")
        self.save_xml_button.grid(row=0, column=9, rowspan=2, sticky="ns", padx=(8, 0))

        parameters = ttk.Frame(outer, padding=(14, 10), style="Card.TFrame")
        parameters.pack(fill="x", pady=(0, 10))
        config = self.settings.optimization
        ttk.Label(parameters, text="Optimization Strategy", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.strategy_var = tk.StringVar(value=config.strategy)
        ttk.Combobox(
            parameters,
            textvariable=self.strategy_var,
            values=("conservative", "balanced", "aggressive"),
            state="readonly",
            width=14,
        ).grid(row=1, column=0, sticky="w", padx=(0, 12))
        ttk.Label(parameters, text="Regularization", style="Card.TLabel").grid(row=0, column=1, sticky="w")
        self.regularization_var = tk.StringVar(value="Auto" if config.regularization is None else f"{config.regularization:g}")
        ttk.Entry(parameters, textvariable=self.regularization_var, width=10).grid(row=1, column=1, sticky="w", padx=(0, 12))
        ttk.Label(parameters, text="饱和度系数", style="Card.TLabel").grid(row=0, column=2, sticky="w")
        self.saturation_var = tk.StringVar(value=f"{config.saturation_factor:g}")
        ttk.Entry(parameters, textvariable=self.saturation_var, width=9).grid(row=1, column=2, sticky="w", padx=(0, 12))
        ttk.Label(parameters, text="重点 Patch", style="Card.TLabel").grid(row=0, column=3, sticky="w")
        self.focus_patches_var = tk.StringVar(value=",".join(str(zone) for zone in config.focus_patches))
        ttk.Entry(parameters, textvariable=self.focus_patches_var, width=15).grid(row=1, column=3, sticky="w", padx=(0, 12))
        ttk.Label(parameters, text="重点权重", style="Card.TLabel").grid(row=0, column=4, sticky="w")
        self.focus_weight_var = tk.StringVar(value=f"{config.focus_weight:g}")
        ttk.Entry(parameters, textvariable=self.focus_weight_var, width=9).grid(row=1, column=4, sticky="w", padx=(0, 12))
        ttk.Label(parameters, text="系数 Min / Max", style="Card.TLabel").grid(row=0, column=5, sticky="w")
        bounds = ttk.Frame(parameters, style="Card.TFrame")
        bounds.grid(row=1, column=5, sticky="w", padx=(0, 12))
        self.coefficient_min_var = tk.StringVar(value=f"{config.coefficient_min:g}")
        self.coefficient_max_var = tk.StringVar(value=f"{config.coefficient_max:g}")
        ttk.Entry(bounds, textvariable=self.coefficient_min_var, width=7).pack(side="left")
        ttk.Label(bounds, text=" / ", style="Card.TLabel").pack(side="left")
        ttk.Entry(bounds, textvariable=self.coefficient_max_var, width=7).pack(side="left")
        self.show_motion_var = tk.BooleanVar(value=self.settings.show_motion)
        ttk.Checkbutton(parameters, text="Show Motion", variable=self.show_motion_var, command=self._on_show_motion_changed).grid(row=1, column=6, sticky="w", padx=(0, 12))
        ttk.Button(parameters, text="恢复 a*b* 视图", command=self._reset_lab_view, style="Quiet.TButton").grid(row=1, column=7, sticky="w", padx=(0, 12))
        ttk.Label(parameters, text="参数自动保存 · 图中滚轮缩放 / 双击复位", style="Card.TLabel").grid(row=1, column=8, sticky="e")
        parameters.columnconfigure(8, weight=1)

        info = ttk.Frame(outer, style="Root.TFrame")
        info.pack(fill="x", pady=(0, 10))
        self.csv_label = ttk.Label(info, text="CSV：未加载", style="Subtitle.TLabel")
        self.csv_label.pack(side="left")
        self.xml_label = ttk.Label(info, text="XML：未加载", style="Subtitle.TLabel")
        self.xml_label.pack(side="left", padx=(22, 0))
        self.active_region_var = tk.StringVar(value="当前 Region：未选择")
        ttk.Label(info, textvariable=self.active_region_var, style="ActiveRegion.TLabel").pack(side="right", padx=(18, 0))
        self.status_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel").pack(fill="x", pady=(0, 10))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        overview = ttk.Frame(self.notebook, padding=10, style="Root.TFrame")
        engineering = ttk.Frame(self.notebook, padding=10, style="Root.TFrame")
        advice = ttk.Frame(self.notebook, padding=10, style="Root.TFrame")
        history_tab = ttk.Frame(self.notebook, padding=10, style="Root.TFrame")
        self.notebook.add(overview, text="  色差对比  ")
        self.notebook.add(engineering, text="  工程统计  ")
        self.notebook.add(advice, text="  诊断与解释  ")
        self.notebook.add(history_tab, text="  History / XML Diff  ")

        summary_row = ttk.Frame(overview, style="Root.TFrame")
        summary_row.pack(fill="x", pady=(0, 8))
        for column in range(4):
            summary_row.columnconfigure(column, weight=1, uniform="summary-kpi")
        for column in range(4, 7):
            summary_row.columnconfigure(column, weight=2, uniform="summary-matrix")
        summary_row.rowconfigure(0, weight=1)
        self.kpi_vars: list[tk.StringVar] = []
        for column, caption in enumerate(("平均 ΔE00", "最大 ΔE00", "改善率", "改善 / 退化")):
            frame = ttk.Frame(summary_row, padding=(8, 7), style="Card.TFrame")
            frame.grid(row=0, column=column, sticky="nsew", padx=(0, 6))
            value_var = tk.StringVar(value="—")
            self.kpi_vars.append(value_var)
            ttk.Label(
                frame,
                textvariable=value_var,
                style="Kpi.TLabel",
                justify="left",
            ).pack(anchor="w")
            ttk.Label(frame, text=caption, style="KpiCaption.TLabel").pack(anchor="w")

        self.original_panel = MatrixPanel(summary_row, "改前 CC")
        self.original_panel.grid(row=0, column=4, sticky="nsew", padx=(0, 6))
        self.correction_panel = MatrixPanel(summary_row, "Delta correction A · M新=A×M旧")
        self.correction_panel.grid(row=0, column=5, sticky="nsew", padx=(0, 6))
        self.optimized_panel = MatrixPanel(summary_row, "改后 CC")
        self.optimized_panel.grid(row=0, column=6, sticky="nsew")

        plot_container = ttk.Frame(overview, style="Root.TFrame")
        plot_container.pack(fill="both", expand=True)
        plot_container.columnconfigure(0, weight=1, uniform="lab-plots")
        plot_container.columnconfigure(1, weight=1, uniform="lab-plots")
        plot_container.columnconfigure(2, weight=0, minsize=520)
        plot_container.rowconfigure(0, weight=1)
        self.before_plot = LabPlot(plot_container, "改前：Camera → Ideal", self.lab_view, self._redraw_plots, self._show_patch_detail)
        self.before_plot.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.after_plot = LabPlot(plot_container, "改后模拟：Camera → Ideal", self.lab_view, self._redraw_plots, self._show_patch_detail)
        self.after_plot.grid(row=0, column=1, sticky="nsew", padx=5)

        self.patch_table_panel = ttk.Frame(plot_container, width=520, padding=8, style="Card.TFrame")
        self.patch_table_panel.grid(row=0, column=2, sticky="nsew", padx=(5, 0))
        self.patch_table_panel.grid_propagate(False)
        self.patch_table_panel.columnconfigure(0, weight=1)
        self.patch_table_panel.rowconfigure(0, weight=1)

        table_frame = ttk.Frame(self.patch_table_panel, style="Card.TFrame")
        table_frame.grid(row=0, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("zone", "name", "category", "weight", "before", "after", "change", "dl", "dc", "dh", "regression", "status", "module")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "zone": "Zone",
            "name": "色块",
            "category": "分类",
            "weight": "权重",
            "before": "改前 ΔE00",
            "after": "改后 ΔE00",
            "change": "改善率",
            "dl": "ΔL* 前→后",
            "dc": "ΔC* 前→后",
            "dh": "Δh° 前→后",
            "regression": "Regression",
            "status": "保护状态",
            "module": "建议模块",
        }
        widths = {"zone": 48, "name": 72, "category": 72, "weight": 54, "before": 82, "after": 82, "change": 68, "dl": 108, "dc": 108, "dh": 108, "regression": 88, "status": 84, "module": 170}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=widths[column], stretch=False, anchor="center" if column != "module" else "w")
        self.tree.tag_configure("improved", foreground="#067647")
        self.tree.tag_configure("regressed", foreground="#B42318")
        self.tree.tag_configure("neutral", background="#F8FAFC")
        self.tree.tag_configure("focus", background="#EAF2FF", font=FONT_SMALL_BOLD)
        vertical_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        horizontal_scrollbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical_scrollbar.set, xscrollcommand=horizontal_scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self._on_patch_table_selected)

        stats_top = ttk.Frame(engineering, style="Root.TFrame")
        stats_top.pack(fill="both", expand=True)
        self.engineering_tree = ttk.Treeview(
            stats_top,
            columns=("check", "status", "value", "limit", "meaning"),
            show="headings",
            height=9,
        )
        for column, caption, width in (
            ("check", "Check", 190), ("status", "Status", 90), ("value", "Value", 220),
            ("limit", "Limit", 250), ("meaning", "Meaning", 440),
        ):
            self.engineering_tree.heading(column, text=caption)
            self.engineering_tree.column(column, width=width, anchor="w")
        self.engineering_tree.tag_configure("PASS", foreground=GREEN)
        self.engineering_tree.tag_configure("WARNING", foreground=AMBER)
        self.engineering_tree.tag_configure("FAIL", foreground=RED)
        self.engineering_tree.pack(fill="x")
        self.statistics_text = tk.Text(
            stats_top,
            wrap="word",
            height=16,
            background=PANEL,
            foreground=INK,
            relief="flat",
            padx=16,
            pady=12,
            font=FONT_BODY,
        )
        self.statistics_text.pack(fill="both", expand=True, pady=(8, 0))

        advice_header = ttk.Frame(advice, style="Root.TFrame")
        advice_header.pack(fill="x", pady=(0, 8))
        ttk.Label(advice_header, text="CCM 只能做全局线性修正；工具会把不适合交给 CC 的问题路由到对应模块。", style="Subtitle.TLabel").pack(side="left")
        self.advice_text = tk.Text(advice, wrap="word", background=PANEL, foreground=INK, relief="flat", padx=18, pady=16, font=FONT_BODY)
        self.advice_text.pack(fill="both", expand=True)
        self._set_advice("尚未运行优化。")

        history_actions = ttk.Frame(history_tab, style="Root.TFrame")
        history_actions.pack(fill="x", pady=(0, 8))
        ttk.Label(history_actions, text="每次点击自动优化都会保留一轮参数与 Matrix 结果。", style="Subtitle.TLabel").pack(side="left")
        ttk.Button(history_actions, text="清空历史", command=self.clear_history, style="Quiet.TButton").pack(side="right")
        self.history_tree = ttk.Treeview(
            history_tab,
            columns=("time", "dataset", "strategy", "mean", "pass", "matrix"),
            show="headings",
            height=8,
        )
        for column, caption, width in (
            ("time", "Time", 165), ("dataset", "Dataset", 210), ("strategy", "Strategy", 100),
            ("mean", "Average ΔE00", 150), ("pass", "Pass≤3", 150), ("matrix", "Matrix", 90),
        ):
            self.history_tree.heading(column, text=caption)
            self.history_tree.column(column, width=width, anchor="w")
        self.history_tree.pack(fill="x")
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_selected)
        ttk.Label(history_tab, text="XML Unified Diff（只应出现选中 region 的 9 个 c_tab 数值）", style="Subtitle.TLabel").pack(anchor="w", pady=(10, 4))
        self.diff_text = tk.Text(
            history_tab,
            wrap="none",
            background="#101828",
            foreground="#F2F4F7",
            insertbackground="white",
            relief="flat",
            padx=12,
            pady=10,
            font=FONT_MONO,
        )
        self.diff_text.pack(fill="both", expand=True)
        self._render_history()

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_advice(self, text: str) -> None:
        self.advice_text.configure(state="normal")
        self.advice_text.delete("1.0", "end")
        self.advice_text.insert("1.0", text)
        self.advice_text.configure(state="disabled")

    def _config_from_controls(self) -> OptimizationConfig:
        regularization_text = self.regularization_var.get().strip()
        regularization = None if not regularization_text or regularization_text.lower() == "auto" else float(regularization_text)
        focus = tuple(
            dict.fromkeys(
                int(value.strip())
                for value in self.focus_patches_var.get().replace("，", ",").split(",")
                if value.strip()
            )
        )
        config = replace(
            self.settings.optimization,
            strategy=self.strategy_var.get(),
            regularization=regularization,
            max_blend=max(0.2, min(1.0, self.strength_var.get() / 100.0)),
            saturation_factor=float(self.saturation_var.get()),
            focus_patches=focus,
            focus_weight=float(self.focus_weight_var.get()),
            coefficient_min=float(self.coefficient_min_var.get()),
            coefficient_max=float(self.coefficient_max_var.get()),
        )
        config.validate()
        return config

    def _install_settings_autosave(self) -> None:
        variables: tuple[tk.Variable, ...] = (
            self.composition_var,
            self.strength_var,
            self.strategy_var,
            self.regularization_var,
            self.saturation_var,
            self.focus_patches_var,
            self.focus_weight_var,
            self.coefficient_min_var,
            self.coefficient_max_var,
            self.show_motion_var,
        )
        for variable in variables:
            variable.trace_add("write", self._schedule_settings_save)
        self.focus_patches_var.trace_add("write", self._on_focus_patches_changed)

    def _on_focus_patches_changed(self, *_args: str) -> None:
        # Highlight changes are a display concern and should be visible as soon
        # as the engineer edits the list, even before the next optimization.
        self._redraw_plots()

    def _current_focus_zones(self) -> tuple[int, ...]:
        try:
            values = tuple(
                dict.fromkeys(
                    int(value.strip())
                    for value in self.focus_patches_var.get().replace("，", ",").split(",")
                    if value.strip()
                )
            )
            if values and all(1 <= value <= 24 for value in values):
                return values
        except ValueError:
            pass
        return self.settings.optimization.focus_patches

    def _schedule_settings_save(self, *_args: str) -> None:
        if self._closing:
            return
        if self._settings_save_after_id is not None:
            self.root.after_cancel(self._settings_save_after_id)
        self._settings_save_after_id = self.root.after(650, self._run_scheduled_settings_save)

    def _run_scheduled_settings_save(self) -> None:
        self._settings_save_after_id = None
        self._persist_settings()

    def _persist_settings(self, *, show_error: bool = False) -> bool:
        try:
            config = self._config_from_controls()
            composition = self.COMPOSITION_LABELS[self.composition_var.get()]
            self.settings = AppSettings(
                optimization=config,
                composition=composition,
                show_motion=self.show_motion_var.get(),
                last_report_format=self.settings.last_report_format,
            )
            save_settings(self.settings)
        except (OSError, ValueError) as exc:
            if show_error:
                messagebox.showerror("参数自动保存失败", str(exc))
            return False
        return True

    def _apply_settings_to_controls(self, settings: AppSettings) -> None:
        config = settings.optimization
        composition_label = next(
            (label for label, value in self.COMPOSITION_LABELS.items() if value == settings.composition),
            next(iter(self.COMPOSITION_LABELS)),
        )
        self.settings = settings
        self.composition_var.set(composition_label)
        self.strength_var.set(config.max_blend * 100.0)
        self.strategy_var.set(config.strategy)
        self.regularization_var.set("Auto" if config.regularization is None else f"{config.regularization:g}")
        self.saturation_var.set(f"{config.saturation_factor:g}")
        self.focus_patches_var.set(",".join(str(zone) for zone in config.focus_patches))
        self.focus_weight_var.set(f"{config.focus_weight:g}")
        self.coefficient_min_var.set(f"{config.coefficient_min:g}")
        self.coefficient_max_var.set(f"{config.coefficient_max:g}")
        self.show_motion_var.set(settings.show_motion)
        self._redraw_plots()

    def import_settings(self) -> None:
        path = filedialog.askopenfilename(
            title="导入 MatrixCorrect 配置",
            filetypes=[("JSON 配置", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("配置根节点必须是 JSON object。")
            settings = AppSettings.from_dict(payload)
            settings.optimization.validate()
            self._apply_settings_to_controls(settings)
            save_settings(settings)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("配置导入失败", str(exc))
            return
        self._set_status(f"已导入配置并同步到内部 settings.json：{path}")

    def export_settings(self) -> None:
        try:
            config = self._config_from_controls()
            settings = AppSettings(
                optimization=config,
                composition=self.COMPOSITION_LABELS[self.composition_var.get()],
                show_motion=self.show_motion_var.get(),
                last_report_format=self.settings.last_report_format,
            )
        except ValueError as exc:
            messagebox.showerror("配置无效", str(exc))
            return
        path = filedialog.asksaveasfilename(
            title="导出 MatrixCorrect 配置",
            defaultextension=".json",
            initialfile="MatrixCorrect_settings.json",
            filetypes=[("JSON 配置", "*.json")],
            confirmoverwrite=True,
        )
        if not path:
            return
        try:
            save_settings(settings, path)
        except OSError as exc:
            messagebox.showerror("配置导出失败", str(exc))
            return
        self._set_status(f"已导出标准 JSON 配置：{path}")

    def open_gamma_optimizer(self) -> None:
        from .gamma_app import open_gamma_window

        open_gamma_window(self.root)

    def close(self) -> None:
        self._closing = True
        if self._settings_save_after_id is not None:
            self.root.after_cancel(self._settings_save_after_id)
            self._settings_save_after_id = None
        # settings.json is internal state: save once more on every normal exit.
        self._persist_settings(show_error=False)
        try:
            save_history(self.history)
        except OSError:
            pass
        self.root.destroy()

    def _on_show_motion_changed(self) -> None:
        self._redraw_plots()
        self._schedule_settings_save()

    def _fit_lab_view(self, patches: list[PatchResult]) -> None:
        points = [
            (lab[1], lab[2])
            for patch in patches
            for lab in (patch.ideal_lab, patch.before_lab, patch.after_lab)
        ]
        self.lab_view.fit(points)

    def _reset_lab_view(self) -> None:
        if self.result is not None:
            # Recompute from the complete Ideal/Before/After set so reset also
            # acts as a reliable one-click auto-fit after data or window changes.
            self._fit_lab_view(self.result.patch_results)
        else:
            self.lab_view.reset()
        self._redraw_plots()

    def _redraw_plots(self) -> None:
        patches = self.result.patch_results if self.result is not None else []
        focus_zones = self._current_focus_zones()
        self.before_plot.draw(
            patches,
            mode="before",
            show_motion=self.show_motion_var.get(),
            focus_zones=focus_zones,
        )
        self.after_plot.draw(
            patches,
            mode="after",
            show_motion=self.show_motion_var.get(),
            focus_zones=focus_zones,
        )

    def _show_patch_detail(self, zone: int) -> None:
        if self.result is None:
            return
        patch = next((item for item in self.result.patch_results if item.zone == zone), None)
        if patch is None:
            return
        self.before_plot.selected_zone = zone
        self.after_plot.selected_zone = zone
        self._redraw_plots()
        item = f"patch-{zone}"
        if self.tree.exists(item):
            self._syncing_patch_selection = True
            try:
                if self.tree.selection() != (item,):
                    self.tree.selection_set(item)
                self.tree.focus(item)
                self.tree.see(item)
            finally:
                self._syncing_patch_selection = False

    def _on_patch_table_selected(self, _event: Optional[tk.Event] = None) -> None:
        if self._syncing_patch_selection:
            return
        selection = self.tree.selection()
        if not selection or not selection[0].startswith("patch-"):
            return
        try:
            zone = int(selection[0].split("-", 1)[1])
        except ValueError:
            return
        self._show_patch_detail(zone)

    def _set_statistics(self, text: str) -> None:
        self.statistics_text.configure(state="normal")
        self.statistics_text.delete("1.0", "end")
        self.statistics_text.insert("1.0", text)
        self.statistics_text.configure(state="disabled")

    def _render_history(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for index, record in enumerate(reversed(self.history)):
            self.history_tree.insert(
                "",
                "end",
                iid=f"history-{index}",
                values=(
                    record.timestamp,
                    record.dataset_name,
                    f"{record.strategy} / {record.search_method or 'legacy'}",
                    f"{record.mean_before:.3f} → {record.mean_after:.3f}",
                    f"{record.pass_rate_before_3:.1%} → {record.pass_rate_after_3:.1%}",
                    record.matrix_status,
                ),
            )

    def _on_history_selected(self, _event: Optional[tk.Event] = None) -> None:
        selection = self.history_tree.selection()
        if not selection:
            return
        try:
            reverse_index = int(selection[0].split("-", 1)[1])
            record = self.history[-1 - reverse_index]
        except (ValueError, IndexError):
            return
        matrix_text = "\n".join(" ".join(f"{value:.7f}" for value in row) for row in record.optimized_matrix)
        content = (
            f"Timestamp: {record.timestamp}\nDataset: {record.dataset_name}\nRegion: {record.region_label}\n"
            f"Strategy: {record.strategy}\nMethod: {record.search_method or 'legacy'}\nMatrix Status: {record.matrix_status}\n\nAfter Matrix\n{matrix_text}\n\n"
            f"XML Diff\n{record.xml_diff or 'Not recorded'}"
        )
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        self.diff_text.insert("1.0", content)
        self.diff_text.configure(state="disabled")

    def clear_history(self) -> None:
        if not messagebox.askyesno("清空 Matrix History", "确定清空所有多轮优化记录吗？"):
            return
        self.history = []
        try:
            save_history(self.history)
        except OSError as exc:
            messagebox.showerror("清空失败", str(exc))
            return
        self._render_history()
        self._set_status("Matrix History 已清空。")

    def load_csv(self) -> None:
        path = filedialog.askopenfilename(title="打开 Imatest summary CSV", filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            self.dataset = parse_imatest_csv(path)
        except (OSError, ImatestCSVError) as cc_exc:
            # Stepchart CSV belongs to the independent Gamma workflow.  Route
            # it there when recognized, while retaining the exact CC parser and
            # all A/CWF/D65/TL84 ColorChecker behavior in this window.
            try:
                from .gray_imatest import parse_gray_csv

                parse_gray_csv(path)
            except (OSError, ValueError):
                messagebox.showerror("CSV 读取失败", str(cc_exc))
                return
            from .gamma_app import open_gamma_window

            gamma_app = open_gamma_window(self.root)
            gamma_app.load_csv(path)
            self._set_status(f"{Path(path).name} 是 Gray/Stepchart CSV，已转到独立 Gamma 优化窗口。")
            return
        self.csv_label.configure(text=f"CSV：{Path(path).name} · {len(self.dataset.patches)} patches")
        if self.dataset.inferred_cct is not None:
            self.cct_var.set(str(self.dataset.inferred_cct))
        self.result = None
        self._clear_result()
        self._set_status(f"已加载 {Path(path).name}。" + (f" 已推断 CCT={self.dataset.inferred_cct}K。" if self.dataset.inferred_cct else " 请设置 CCT。"))
        if self.document and self.cct_var.get():
            self.auto_match_region()

    def load_xml(self) -> None:
        path = filedialog.askopenfilename(title="打开 Qualcomm CC XML", filetypes=[("XML", "*.xml"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            self.document = QualcommCCDocument.load(path)
        except (OSError, QualcommXMLError) as exc:
            messagebox.showerror("XML 读取失败", str(exc))
            return
        self.selected_region = None
        self.active_region_var.set("当前 Region：未选择")
        self.original_panel.set_matrix(None)
        self.xml_label.configure(text=f"XML：{Path(path).name} · {len(self.document.regions)} regions")
        values: list[str] = []
        self.region_display_to_index.clear()
        for region in self.document.regions:
            cct = region.cct_range
            prefix = f"#{region.index} · CCT {cct.start:g}-{cct.end:g}K" if cct else f"#{region.index}"
            display = f"{prefix} · {region.path_label()}"
            values.append(display)
            self.region_display_to_index[display] = region.index
        self.region_combo.configure(values=values)
        self.result = None
        self._clear_result()
        if self.cct_var.get():
            self.auto_match_region()
        else:
            self.region_combo.current(0)
            self._select_region(0)
        self._set_status(f"已加载 {Path(path).name}；请选择或自动匹配 CCT region。")

    def auto_match_region(self) -> None:
        if not self.document:
            messagebox.showinfo("需要 XML", "请先打开 Qualcomm CC XML。")
            return
        try:
            cct = float(self.cct_var.get())
        except ValueError:
            messagebox.showerror("CCT 无效", "请输入以 K 为单位的数字色温。")
            return
        try:
            region, mode = self.document.find_region_for_cct(cct)
        except QualcommXMLError as exc:
            messagebox.showerror("匹配失败", str(exc))
            return
        self._select_region(region.index)
        if mode == "exact":
            self._set_status(f"CCT {cct:g}K 精确命中 region #{region.index}: {region.path_label()}")
        else:
            self._set_status(
                f"CCT {cct:g}K 位于 XML transition/gap；已选最近 region #{region.index}。"
                "Qualcomm 运行时可能在相邻矩阵间插值，请确认要写入的端点。"
            )

    def _on_region_selected(self, _event: Optional[tk.Event] = None) -> None:
        display = self.region_var.get()
        if display in self.region_display_to_index:
            self._select_region(self.region_display_to_index[display], update_combo=False)

    def _select_region(self, index: int, *, update_combo: bool = True) -> None:
        if not self.document:
            return
        self.selected_region = self.document.regions[index]
        if update_combo:
            for display, region_index in self.region_display_to_index.items():
                if region_index == index:
                    self.region_var.set(display)
                    break
        self.original_panel.set_matrix(self.selected_region.matrix)
        cct_range = self.selected_region.cct_range
        cct_text = f" · CCT {cct_range.start:g}-{cct_range.end:g}K" if cct_range else ""
        self.active_region_var.set(f"当前 Region：#{self.selected_region.index}{cct_text}")
        self.result = None
        self._clear_result()

    def run_optimization(self) -> None:
        if self.dataset is None or self.document is None or self.selected_region is None:
            messagebox.showinfo("资料未齐", "请先打开 Imatest CSV、CC XML，并选择 CCT region。")
            return
        composition = self.COMPOSITION_LABELS[self.composition_var.get()]
        try:
            config = self._config_from_controls()
        except ValueError as exc:
            messagebox.showerror("优化参数无效", str(exc))
            return
        self.settings = AppSettings(
            optimization=config,
            composition=composition,
            show_motion=self.show_motion_var.get(),
            last_report_format=self.settings.last_report_format,
        )
        self._persist_settings()
        self.optimize_button.configure(state="disabled")
        self._set_status("正在搜索 Regularization、工程边界与 Regression Protection 候选…")
        self.root.update_idletasks()
        try:
            self.result = optimize_ccm(
                self.dataset,
                self.selected_region.matrix,
                composition=composition,
                config=config,
            )
            self.xml_diff = self.document.diff_with_matrix(
                self.selected_region.index,
                self.result.optimized_matrix,
            )
        except (OptimizationError, QualcommXMLError) as exc:
            messagebox.showerror("优化失败", str(exc))
            self.optimize_button.configure(state="normal")
            return
        finally:
            self.optimize_button.configure(state="normal")
        record = record_from_result(
            self.result,
            dataset_name=self.dataset.source_path.name,
            region_label=self.selected_region.path_label(),
            xml_diff=self.xml_diff,
        )
        self.history.append(record)
        try:
            save_history(self.history)
        except OSError:
            self.result.warnings.append("Matrix History 无法写入用户配置目录；当前会话内记录仍保留。")
        self._render_result()
        self._render_history()

    def _clear_result(self) -> None:
        self.lab_view.fit([])
        self.before_plot.selected_zone = None
        self.after_plot.selected_zone = None
        composition = self.COMPOSITION_LABELS.get(self.composition_var.get(), "pre")
        relation = "M新=A×M旧" if composition == "pre" else "M新=M旧×Aᵀ"
        self.correction_panel.set_title(f"Delta correction A · {relation}")
        self.correction_panel.set_matrix(None)
        self.optimized_panel.set_matrix(None)
        self.save_xml_button.configure(state="disabled")
        for variable in self.kpi_vars:
            variable.set("—")
        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.engineering_tree.get_children():
            self.engineering_tree.delete(item)
        focus_zones = self._current_focus_zones()
        self.before_plot.draw([], mode="before", show_motion=self.show_motion_var.get(), focus_zones=focus_zones)
        self.after_plot.draw([], mode="after", show_motion=self.show_motion_var.get(), focus_zones=focus_zones)
        self._set_statistics("尚未运行优化。")
        self.xml_diff = ""
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        self.diff_text.configure(state="disabled")
        self._set_advice("尚未运行优化。")

    def _render_result(self) -> None:
        assert self.result is not None
        result = self.result
        self.before_plot.selected_zone = None
        self.after_plot.selected_zone = None
        self.original_panel.set_matrix(result.original_matrix)
        relation = "M新=A×M旧" if result.composition == "pre" else "M新=M旧×Aᵀ"
        self.correction_panel.set_title(f"Delta correction A · {relation}")
        self.correction_panel.set_matrix(result.correction_matrix)
        self.optimized_panel.set_matrix(result.optimized_matrix)
        self.kpi_vars[0].set(f"{result.mean_before:.2f} → {result.mean_after:.2f}")
        self.kpi_vars[1].set(f"{result.max_before:.2f} → {result.max_after:.2f}")
        self.kpi_vars[2].set(f"{result.mean_improvement_percent:+.1f}%")
        self.kpi_vars[3].set(f"{result.improved_count} / {result.regressed_count}")
        self._fit_lab_view(result.patch_results)
        focus_zones = self._current_focus_zones()
        self.before_plot.draw(
            result.patch_results,
            mode="before",
            show_motion=self.show_motion_var.get(),
            focus_zones=focus_zones,
        )
        self.after_plot.draw(
            result.patch_results,
            mode="after",
            show_motion=self.show_motion_var.get(),
            focus_zones=focus_zones,
        )
        self.save_xml_button.configure(state="normal")
        for item in self.tree.get_children():
            self.tree.delete(item)
        for patch in result.patch_results:
            tags = ["neutral"] if patch.zone >= 19 else []
            if patch.zone in focus_zones:
                tags.append("focus")
            tags.append("improved" if patch.delta_e_after <= patch.delta_e_before else "regressed")
            self.tree.insert(
                "",
                "end",
                iid=f"patch-{patch.zone}",
                values=(
                    patch.zone,
                    patch.name,
                    patch.category,
                    f"{patch.priority_weight:.2f}",
                    f"{patch.delta_e_before:.3f}",
                    f"{patch.delta_e_after:.3f}",
                    patch.improvement_text(1),
                    f"{patch.delta_l_before:+.2f}→{patch.delta_l_after:+.2f}",
                    f"{patch.delta_c_before:+.2f}→{patch.delta_c_after:+.2f}",
                    f"{patch.delta_h_before:+.1f}→{patch.delta_h_after:+.1f}",
                    f"{patch.regression:.3f}",
                    patch.regression_status,
                    patch.module_hint,
                ),
                tags=tags,
            )
        for item in self.engineering_tree.get_children():
            self.engineering_tree.delete(item)
        for index, check in enumerate(result.matrix_health.checks):
            self.engineering_tree.insert(
                "",
                "end",
                iid=f"check-{index}",
                values=(check.name, check.status, check.value, check.limit, check.message),
                tags=(check.status,),
            )
        stats_lines = [
            f"Matrix Health: {result.matrix_health.status}",
            f"det={result.matrix_health.determinant:.6f} · cond={result.matrix_health.condition_number:.4f} · rank={result.matrix_health.rank}",
            f"Row Sum={', '.join(f'{value:.7f}' for value in result.matrix_health.row_sums)}",
            f"Fixed Point max ΔE00={result.matrix_health.fixed_point_max_delta_e:.5f} · coefficient error={result.matrix_health.fixed_point_max_error:.7f}",
            "",
            "Pass Rate",
        ]
        for index, threshold in enumerate(result.pass_rates.thresholds):
            stats_lines.append(
                f"· ΔE00≤{threshold:g}: {result.pass_rates.before_counts[index]}/{result.pass_rates.sample_count} "
                f"({result.pass_rates.before_rate(index):.1%}) → {result.pass_rates.after_counts[index]}/{result.pass_rates.sample_count} "
                f"({result.pass_rates.after_rate(index):.1%})"
            )
        stats_lines.extend(["", "Patch 分类统计"])
        for category in result.category_statistics:
            stats_lines.append(
                f"· {category.category}: n={category.count}, mean {category.mean_before:.3f}→{category.mean_after:.3f}, "
                f"improve={category.improved}, regression={category.regressed}, Pass≤3 {category.pass_rate_before_3:.1%}→{category.pass_rate_after_3:.1%}"
            )
        stats_lines.extend(
            [
                "",
                "Loss Breakdown (Before → After)",
                f"· Total {result.loss_before.total:.3f} → {result.loss_after.total:.3f}",
                f"· ΔE00 {result.loss_before.delta_e:.3f} → {result.loss_after.delta_e:.3f}; ΔC {result.loss_before.delta_c:.3f} → {result.loss_after.delta_c:.3f}; Δh {result.loss_before.delta_h:.3f} → {result.loss_after.delta_h:.3f}",
                f"· Regression {result.loss_before.regression:.3f} → {result.loss_after.regression:.3f}; Saturation {result.loss_before.saturation:.3f} → {result.loss_after.saturation:.3f}",
                f"· Chroma ratio {result.saturation_ratio_before:.3f} → {result.saturation_ratio_after:.3f}; target={result.saturation_factor:.3f}",
                f"· Rejected candidates: {result.rejected_candidates}",
            ]
        )
        self._set_statistics("\n".join(stats_lines))
        strength_description = (
            "直接工程边界坐标搜索（blend 不适用）"
            if result.search_method == "engineering-boundary"
            else f"Delta CCM 强度={result.blend:.0%}"
        )
        lines = [
            "优化摘要",
            f"· Strategy={result.strategy}；Method={result.search_method}；Regularization λ={result.regularization:g}；{strength_description}",
            f"· 彩色色块 1-18 的平均 ΔE00：{result.mean_before:.3f} → {result.mean_after:.3f}",
            f"· 改善 {result.improved_count} 个，回退 {result.regressed_count} 个；平均改善 {result.mean_improvement_percent:+.1f}%",
            f"· Matrix Health={result.matrix_health.status}；Chroma ratio={result.saturation_ratio_before:.3f}→{result.saturation_ratio_after:.3f}",
            "",
            "Explainable Optimization",
        ]
        lines.extend(f"· {line}" for line in result.explainability)
        lines.extend(["", "模块诊断（Confidence / Root Cause）"])
        for diagnosis in result.diagnostics:
            lines.extend(
                [
                    f"· {diagnosis.module} · {diagnosis.confidence:.0%} · {diagnosis.severity}",
                    f"  Root Cause: {diagnosis.root_cause}",
                    f"  Evidence: {'；'.join(diagnosis.evidence)}",
                    f"  Action: {diagnosis.action}",
                ]
            )
        lines.extend(["", "警告与前提"])
        if result.warnings:
            lines.extend(f"· {warning}" for warning in result.warnings)
        else:
            lines.append("· 无额外警告。")
        lines.extend(
            [
                "",
                "模拟边界",
                "· CSV 是经过完整 ISP 的 sRGB 输出；本工具在线性 sRGB 域拟合 Delta CCM，再与 XML 原矩阵组合。",
                "· 该结果适合首轮收敛与方向判断，最终仍需烧录/编译后重拍 ColorChecker 验证。",
                "· Gamma、AWB、CV、SCE、2D LUT 或 TMC 改动后，应重新采集 CSV，不能把其误差全部压给 CC。",
            ]
        )
        self._set_advice("\n".join(lines))
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        self.diff_text.insert("1.0", self.xml_diff or "No XML changes.")
        self.diff_text.configure(state="disabled")
        self._set_status(
            f"优化完成：平均 ΔE00 {result.mean_before:.3f} → {result.mean_after:.3f} "
            f"({result.mean_improvement_percent:+.1f}%)；Matrix={result.matrix_health.status}；已记录第 {len(self.history)} 轮。"
        )

    def save_xml(self) -> None:
        if self.document is None or self.selected_region is None or self.result is None:
            messagebox.showinfo("尚无结果", "请先完成优化。")
            return
        if self.result.matrix_health.status == "FAIL":
            messagebox.showerror("工程检查未通过", "Matrix Health=FAIL，禁止写回 XML。")
            return
        default_name = f"{self.document.source_path.stem}_optimized.xml"
        path = filedialog.asksaveasfilename(
            title="保存优化后的 Qualcomm CC XML",
            defaultextension=".xml",
            initialdir=str(self.document.source_path.parent),
            initialfile=default_name,
            filetypes=[("XML 文件", "*.xml")],
            confirmoverwrite=True,
        )
        if not path:
            return
        try:
            self.document.save_with_matrix(path, self.selected_region.index, self.result.optimized_matrix)
        except (OSError, QualcommXMLError) as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._set_status(f"已保存并回读校验：{path}")
        messagebox.showinfo("保存成功", f"仅 region #{self.selected_region.index} 的 c_tab/c 已更新。\n{path}")

    def save_report(self) -> None:
        if self.dataset is None or self.result is None or self.selected_region is None:
            messagebox.showinfo("尚无结果", "请先完成优化。")
            return
        report_format = self.settings.last_report_format
        default_name = f"{self.dataset.source_path.stem}_ccm_analysis.{report_format}"
        path = filedialog.asksaveasfilename(
            title="导出 MatrixCorrect 工程报告",
            defaultextension=f".{report_format}",
            initialfile=default_name,
            filetypes=[
                ("HTML report", "*.html"),
                ("PDF report", "*.pdf"),
                ("Excel workbook", "*.xlsx"),
                ("CSV", "*.csv"),
            ],
        )
        if not path:
            return
        try:
            save_analysis_report(
                path,
                self.dataset,
                self.result,
                region_label=self.selected_region.path_label(),
                xml_diff=self.xml_diff,
                history=self.history,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        suffix = Path(path).suffix.lower().lstrip(".")
        if suffix in {"csv", "html", "pdf", "xlsx"}:
            self.settings = replace(self.settings, last_report_format=suffix)
            self._persist_settings()
        self._set_status(f"已导出分析报告：{path}")

    def show_assumptions(self) -> None:
        messagebox.showinfo(
            "算法边界",
            "1. 先稳定 AWB、曝光与 Gamma，再优化 CC。\n"
            "2. CSV 需要 R/G/B-meas 与 R/G/B-ideal 的 ColorChecker 段。\n"
            "3. Delta CCM 在去 Gamma 后的线性 sRGB 域拟合，并强制每行和为 1。\n"
            "4. 改后图是模型模拟，不代替上机重拍验证。\n"
            "5. CCT gap 属于运行时插值区，保存时必须明确选择某个端点 region。",
        )


def main() -> None:
    root = tk.Tk()
    MatrixCorrectApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
