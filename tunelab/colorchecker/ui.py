"""Tk workspace for image-driven ColorChecker CC calibration."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional, Sequence

import numpy as np
from PIL import Image, ImageTk

from ..branding import application_icon_path, show_about_dialog, show_workbench_help
from ..ccm.color_science import delta_e_2000, srgb_to_lab
from ..ccm.imatest import infer_cct
from ..ccm.models import CCRegion, Matrix3, OptimizationResult
from ..ccm.optimizer import OptimizationError, evaluate_ccm_correction, optimize_ccm
from ..ccm.qualcomm_xml import QualcommCCDocument, QualcommXMLError
from ..image_inspector.types import ImageData
from ..ui_foundation import (
    FONT_BODY,
    FONT_BODY_BOLD,
    FONT_CARD_TITLE,
    FONT_KPI,
    FONT_MONO,
    FONT_SMALL,
    FONT_SMALL_BOLD,
    FONT_TITLE,
    ROW_HEIGHT,
    TABLE_HEADING_BG,
    configure_action_styles,
    configure_typography,
    fit_window_to_screen,
)
from .engine import (
    ColorCheckerDetection,
    ColorCheckerError,
    PatchPolygon,
    RestorationPlan,
    SimulationResult,
    build_calibrated_restoration_plan,
    build_comparison_dataset,
    detect_colorchecker,
    image_optimization_config,
    restoration_evaluation_config,
    sample_patch_means,
    simulate_correction,
    simulate_restoration_response,
)


WINDOW_TITLE = "TuneLab · ColorChecker 图像校正"
BG = "#F3F5F8"
PANEL = "#FFFFFF"
INK = "#172033"
MUTED = "#667085"
BLUE = "#2563EB"
GREEN = "#0F9D75"
RED = "#D92D20"
AMBER = "#B54708"
PURPLE = "#7F56D9"
CANVAS_BG = "#111827"
IMAGE_FILE_TYPES = [
    ("图片", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.heic *.heif"),
    ("所有文件", "*.*"),
]


def _matrix_changed(before: Matrix3, after: Matrix3) -> bool:
    return any(abs(before[row][column] - after[row][column]) > 1e-9 for row in range(3) for column in range(3))


def _rgb_text(values: Sequence[float]) -> str:
    return " / ".join(f"{value:.1f}" for value in values)


def _red_channel_ratios(values: Sequence[float]) -> tuple[float, float]:
    red = max(float(values[0]), 1e-12)
    return float(values[1]) / red, float(values[2]) / red


def _rgb_delta_e(first: Sequence[float], second: Sequence[float]) -> float:
    first_srgb = tuple(float(value) / 255.0 for value in first)
    second_srgb = tuple(float(value) / 255.0 for value in second)
    return delta_e_2000(srgb_to_lab(first_srgb), srgb_to_lab(second_srgb))  # type: ignore[arg-type]


class MatrixPanel(ttk.Frame):
    def __init__(self, master: tk.Misc, title: str) -> None:
        super().__init__(master, padding=12, style="CheckerCard.TFrame")
        self.title_var = tk.StringVar(value=title)
        ttk.Label(self, textvariable=self.title_var, style="CheckerCardTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        self.values = [[tk.StringVar(value="—") for _column in range(3)] for _row in range(3)]
        for row in range(3):
            for column in range(3):
                ttk.Label(
                    self,
                    textvariable=self.values[row][column],
                    style="CheckerMatrix.TLabel",
                    anchor="e",
                    width=12,
                ).grid(row=row + 1, column=column, padx=3, pady=3, sticky="ew")
                self.columnconfigure(column, weight=1)

    def set_matrix(self, matrix: Optional[Matrix3]) -> None:
        for row in range(3):
            for column in range(3):
                self.values[row][column].set("—" if matrix is None else f"{matrix[row][column]:+0.6f}")


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
        header = ttk.Frame(self, padding=(10, 7), style="CheckerCard.TFrame")
        header.pack(fill="x")
        self.title_var = tk.StringVar(value=title)
        self.meta_var = tk.StringVar(value="尚未加载")
        ttk.Label(header, textvariable=self.title_var, style="CheckerCardTitle.TLabel").pack(anchor="w")
        ttk.Label(header, textvariable=self.meta_var, style="CheckerMutedCard.TLabel").pack(anchor="w", pady=(2, 0))
        self.canvas = tk.Canvas(self, background=CANVAS_BG, highlightthickness=0, height=420)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._schedule_redraw)
        self._draw_placeholder()

    def _draw_placeholder(self) -> None:
        self.canvas.delete("all")
        width = max(240, self.canvas.winfo_width())
        height = max(240, self.canvas.winfo_height())
        self.canvas.create_text(width / 2, height / 2, text="等待图片", fill="#98A2B3", font=FONT_BODY)

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
                self.canvas.create_polygon(*coordinates, outline="#22D3EE", fill="", width=1.4)
                centre_x = sum(point[0] for point in polygon) / 4.0
                centre_y = sum(point[1] for point in polygon) / 4.0
                self.canvas.create_text(
                    left + centre_x * scale,
                    top + centre_y * scale,
                    text=str(index),
                    fill="white",
                    font=FONT_SMALL_BOLD,
                )


class ColorCheckerWorkspace:
    STRATEGY_LABELS = {
        "色彩还原（资料标定）": "calibrated",
        "图像拟合·保守": "conservative",
        "图像拟合·平衡": "balanced",
        "图像拟合·积极": "aggressive",
    }

    def __init__(
        self,
        root: tk.Misc,
        *,
        on_close: Optional[Callable[[], None]] = None,
        on_home: Optional[Callable[[], None]] = None,
        on_gamma: Optional[Callable[[], object]] = None,
        on_image_inspector: Optional[Callable[[], object]] = None,
        on_about: Optional[Callable[[], None]] = None,
    ) -> None:
        self.root = root
        self.on_close = on_close
        self.on_home = on_home
        self.on_gamma = on_gamma
        self.on_image_inspector = on_image_inspector
        self.on_about = on_about
        self.test_detection: Optional[ColorCheckerDetection] = None
        self.reference_detection: Optional[ColorCheckerDetection] = None
        self.document: Optional[QualcommCCDocument] = None
        self.selected_region: Optional[CCRegion] = None
        self.result: Optional[OptimizationResult] = None
        self.simulation: Optional[SimulationResult] = None
        self.restoration_plan: Optional[RestorationPlan] = None
        self.xml_diff = ""
        self.region_display_to_index: dict[str, int] = {}
        self._configure_styles()
        self._build_menu()
        self._build_ui()
        if self.on_close is None:
            fit_window_to_screen(self.root, desired_width=1540, desired_height=980)
            try:
                self.root.protocol("WM_DELETE_WINDOW", self.close)
            except tk.TclError:
                pass
        self.root.title(WINDOW_TITLE)

    def _configure_styles(self) -> None:
        style = configure_typography(self.root)
        style.configure("CheckerRoot.TFrame", background=BG)
        style.configure("CheckerCard.TFrame", background=PANEL)
        style.configure("CheckerCard.TLabel", background=PANEL, foreground=INK, font=FONT_BODY)
        style.configure("CheckerMutedCard.TLabel", background=PANEL, foreground=MUTED, font=FONT_SMALL)
        style.configure("CheckerCardTitle.TLabel", background=PANEL, foreground=INK, font=FONT_CARD_TITLE)
        style.configure("CheckerTitle.TLabel", background=BG, foreground=INK, font=FONT_TITLE)
        style.configure("CheckerSubtitle.TLabel", background=BG, foreground=MUTED, font=FONT_BODY)
        style.configure("CheckerKpi.TLabel", background=PANEL, foreground=INK, font=FONT_KPI)
        style.configure("CheckerStatus.TLabel", background="#F8FAFC", foreground=MUTED, padding=(10, 7), font=FONT_SMALL)
        style.configure("CheckerMatrix.TLabel", background="#F8FAFC", foreground=INK, padding=(5, 5), font=FONT_MONO)
        style.configure(
            "Checker.Treeview",
            rowheight=ROW_HEIGHT,
            background=PANEL,
            fieldbackground=PANEL,
            foreground=INK,
            font=FONT_SMALL,
        )
        style.configure(
            "Checker.Treeview.Heading",
            background=TABLE_HEADING_BG,
            foreground=INK,
            font=FONT_SMALL_BOLD,
        )
        configure_action_styles(style)

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        self.file_menu = tk.Menu(menu, tearoff=False)
        self.file_menu.add_command(label="打开测试 ColorChecker...", command=self.load_test_image)
        self.file_menu.add_command(label="打开目标对比图...", command=self.load_reference_image)
        self.file_menu.add_command(label="打开 Qualcomm CC XML...", command=self.load_xml)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="生成色彩还原 / 仿真", command=self.run_optimization)
        self.file_menu.add_command(label="覆盖保存 CC XML", command=self.save_xml)
        self.file_menu.add_command(label="导出仿真图...", command=self.export_simulation)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="关闭", command=self.close)
        menu.add_cascade(label="文件", menu=self.file_menu)

        tools = tk.Menu(menu, tearoff=False)
        if self.on_home is not None:
            tools.add_command(label="首页", command=self.on_home)
        if self.on_close is not None:
            tools.add_command(label="CC 校正", command=self.on_close)
        if self.on_gamma is not None:
            tools.add_command(label="Gamma 优化", command=self.on_gamma)
        if self.on_image_inspector is not None:
            tools.add_command(label="图像分析器", command=self.on_image_inspector)
        if tools.index("end") is not None:
            menu.add_cascade(label="工具", menu=tools)

        self.help_menu = tk.Menu(menu, tearoff=False)
        self.help_menu.add_command(label="TuneLab 使用说明", command=self._show_workbench_help)
        self.help_menu.add_separator()
        self.help_menu.add_command(label="ColorChecker 图像校正说明", command=self.show_help)
        self.help_menu.add_command(label="关于 TuneLab", command=self._show_about)
        menu.add_cascade(label="帮助", menu=self.help_menu)
        self.root.configure(menu=menu)

    def _build_ui(self) -> None:
        self.outer = ttk.Frame(self.root, padding=(14, 10), style="CheckerRoot.TFrame")
        self.outer.pack(fill="both", expand=True)
        header = ttk.Frame(self.outer, style="CheckerRoot.TFrame")
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="ColorChecker 图像校正", style="CheckerTitle.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="色彩还原 · 实拍标定 Delta CCM · 原图 / 仿真 / 目标对比",
            style="CheckerSubtitle.TLabel",
        ).pack(side="left", padx=(12, 0), pady=(6, 0))

        self.toolbar_panel = ttk.Frame(self.outer, padding=(10, 7), style="CheckerCard.TFrame")
        self.toolbar_panel.pack(fill="x", pady=(0, 6))
        self.toolbar_panel.columnconfigure(3, weight=1)
        ttk.Button(self.toolbar_panel, text="1  测试图", command=self.load_test_image).grid(row=0, column=0, padx=(0, 7))
        ttk.Button(self.toolbar_panel, text="2  目标图", command=self.load_reference_image).grid(row=0, column=1, padx=(0, 7))
        ttk.Button(self.toolbar_panel, text="3  CC XML", command=self.load_xml).grid(row=0, column=2, padx=(0, 10))
        self.input_var = tk.StringVar(value="请依次打开测试色卡、目标对比色卡与 Qualcomm CC XML。")
        ttk.Label(self.toolbar_panel, textvariable=self.input_var, style="CheckerMutedCard.TLabel").grid(
            row=1, column=0, columnspan=6, sticky="ew", pady=(6, 0)
        )
        self.optimize_button = ttk.Button(
            self.toolbar_panel,
            text="4  生成色彩还原",
            command=self.run_optimization,
            style="Primary.TButton",
        )
        self.optimize_button.grid(row=0, column=4, padx=(0, 7))
        self.save_button = ttk.Button(self.toolbar_panel, text="覆盖保存 XML", command=self.save_xml, state="disabled")
        self.save_button.grid(row=0, column=5)

        self.settings_panel = ttk.Frame(self.outer, padding=(10, 7), style="CheckerCard.TFrame")
        self.settings_panel.pack(fill="x", pady=(0, 6))
        ttk.Label(self.settings_panel, text="CCT", style="CheckerCard.TLabel").grid(row=0, column=0, padx=(0, 5))
        self.cct_var = tk.StringVar()
        ttk.Entry(self.settings_panel, textvariable=self.cct_var, width=8).grid(row=0, column=1, padx=(0, 7))
        self.region_match_button = ttk.Button(
            self.settings_panel,
            text="自动匹配 Region",
            command=self.auto_match_region,
            style="RegionMatch.TButton",
        )
        self.region_match_button.grid(row=0, column=2, padx=(0, 7))
        self.region_var = tk.StringVar()
        self.region_combo = ttk.Combobox(self.settings_panel, textvariable=self.region_var, state="readonly", width=28)
        self.region_combo.grid(row=0, column=3, sticky="ew", padx=(0, 12))
        self.region_combo.bind("<<ComboboxSelected>>", self._on_region_selected)
        self.settings_panel.columnconfigure(3, weight=1)
        ttk.Label(self.settings_panel, text="策略", style="CheckerCard.TLabel").grid(row=1, column=0, padx=(0, 5), pady=(7, 0))
        self.strategy_var = tk.StringVar(value="色彩还原（资料标定）")
        ttk.Combobox(
            self.settings_panel,
            textvariable=self.strategy_var,
            values=list(self.STRATEGY_LABELS),
            state="readonly",
            width=21,
        ).grid(row=1, column=1, padx=(0, 10), pady=(7, 0))
        ttk.Label(self.settings_panel, text="最大强度 %", style="CheckerCard.TLabel").grid(row=1, column=2, padx=(0, 5), pady=(7, 0))
        self.strength_var = tk.StringVar(value="100")
        ttk.Spinbox(self.settings_panel, from_=5, to=100, increment=5, textvariable=self.strength_var, width=5).grid(
            row=1, column=3, sticky="w", padx=(0, 10), pady=(7, 0)
        )
        self.show_overlay_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self.settings_panel,
            text="显示色块框",
            variable=self.show_overlay_var,
            command=self._redraw_previews,
        ).grid(row=1, column=4, sticky="w", pady=(7, 0))
        ttk.Label(
            self.settings_panel,
            text="前乘 A × M · 等行和中性保护 · 3000K/4000K 实拍矩阵与响应锚点",
            style="CheckerMutedCard.TLabel",
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(7, 0))

        self.notebook = ttk.Notebook(self.outer)
        self.notebook.pack(fill="both", expand=True)
        self.preview_tab = ttk.Frame(self.notebook, padding=8, style="CheckerRoot.TFrame")
        self.patch_tab = ttk.Frame(self.notebook, padding=8, style="CheckerRoot.TFrame")
        self.matrix_tab = ttk.Frame(self.notebook, padding=8, style="CheckerRoot.TFrame")
        self.diff_tab = ttk.Frame(self.notebook, padding=8, style="CheckerRoot.TFrame")
        self.notebook.add(self.preview_tab, text="图像对比")
        self.notebook.add(self.patch_tab, text="24 色块")
        self.notebook.add(self.matrix_tab, text="矩阵与工程检查")
        self.notebook.add(self.diff_tab, text="XML Diff")

        kpis = ttk.Frame(self.preview_tab, style="CheckerRoot.TFrame")
        kpis.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.kpi_vars = [tk.StringVar(value="—") for _index in range(4)]
        for column, (title, variable) in enumerate(
            zip(("实拍对照 ΔE00", "标准红 G/R · B/R", "中性轴 / Health", "仿真剪切"), self.kpi_vars)
        ):
            card = ttk.Frame(kpis, padding=(14, 8), style="CheckerCard.TFrame")
            card.grid(row=0, column=column, sticky="ew", padx=(0, 6) if column < 3 else 0)
            kpis.columnconfigure(column, weight=1, uniform="checker-kpi")
            ttk.Label(card, text=title, style="CheckerMutedCard.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=variable, style="CheckerKpi.TLabel").pack(anchor="w", pady=(2, 0))

        overlay = lambda: self.show_overlay_var.get()
        self.test_preview = PreviewPane(self.preview_tab, "原始测试图", overlay)
        self.simulation_preview = PreviewPane(self.preview_tab, "CCM 仿真图", overlay)
        self.reference_preview = PreviewPane(self.preview_tab, "目标对比图", overlay)
        for column, pane in enumerate((self.test_preview, self.simulation_preview, self.reference_preview)):
            pane.grid(row=1, column=column, sticky="nsew", padx=(0, 6) if column < 2 else 0)
            self.preview_tab.columnconfigure(column, weight=1, uniform="checker-preview")
        self.preview_tab.rowconfigure(1, weight=1)

        table_frame = ttk.Frame(self.patch_tab, style="CheckerCard.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("zone", "name", "test", "simulation", "target", "before", "after", "change", "status")
        self.patch_tree = ttk.Treeview(table_frame, columns=columns, show="headings", style="Checker.Treeview")
        headings = (
            ("zone", "#", 45),
            ("name", "色块", 110),
            ("test", "测试 RGB", 150),
            ("simulation", "仿真 RGB", 150),
            ("target", "目标 RGB", 150),
            ("before", "原始 ΔE00", 100),
            ("after", "仿真 ΔE00", 100),
            ("change", "改善", 95),
            ("status", "对照", 90),
        )
        for column, caption, width in headings:
            self.patch_tree.heading(column, text=caption)
            self.patch_tree.column(column, width=width, anchor="center" if column != "name" else "w")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.patch_tree.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.patch_tree.xview)
        self.patch_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.patch_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.patch_tree.tag_configure("improved", foreground=GREEN)
        self.patch_tree.tag_configure("regressed", foreground=RED)
        self.patch_tree.tag_configure("neutral", background="#F8FAFC")

        matrices = ttk.Frame(self.matrix_tab, style="CheckerRoot.TFrame")
        matrices.pack(fill="x", pady=(0, 8))
        self.original_panel = MatrixPanel(matrices, "原 CC Matrix")
        self.correction_panel = MatrixPanel(matrices, "Delta correction · A")
        self.optimized_panel = MatrixPanel(matrices, "回写 Matrix · A × M")
        for column, panel in enumerate((self.original_panel, self.correction_panel, self.optimized_panel)):
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, 7) if column < 2 else 0)
            matrices.columnconfigure(column, weight=1, uniform="checker-matrix")
        self.engineering_text = tk.Text(
            self.matrix_tab,
            wrap="word",
            background=PANEL,
            foreground=INK,
            relief="flat",
            padx=14,
            pady=12,
            font=FONT_BODY,
        )
        self.engineering_text.pack(fill="both", expand=True)
        self.engineering_text.insert("1.0", "尚未生成 ColorChecker 图像校正方案。")
        self.engineering_text.configure(state="disabled")

        self.diff_text = tk.Text(
            self.diff_tab,
            wrap="none",
            background="#101828",
            foreground="#F2F4F7",
            insertbackground="white",
            relief="flat",
            padx=12,
            pady=10,
            font=FONT_MONO,
        )
        diff_vertical = ttk.Scrollbar(self.diff_tab, orient="vertical", command=self.diff_text.yview)
        diff_horizontal = ttk.Scrollbar(self.diff_tab, orient="horizontal", command=self.diff_text.xview)
        self.diff_text.configure(yscrollcommand=diff_vertical.set, xscrollcommand=diff_horizontal.set)
        self.diff_text.grid(row=0, column=0, sticky="nsew")
        diff_vertical.grid(row=0, column=1, sticky="ns")
        diff_horizontal.grid(row=1, column=0, sticky="ew")
        self.diff_tab.columnconfigure(0, weight=1)
        self.diff_tab.rowconfigure(0, weight=1)
        self.diff_text.insert("1.0", "尚无 XML 变更。")
        self.diff_text.configure(state="disabled")

        self.status_var = tk.StringVar(value="等待输入。")
        ttk.Label(self.outer, textvariable=self.status_var, style="CheckerStatus.TLabel").pack(fill="x", pady=(6, 0))

    def _redraw_previews(self) -> None:
        for preview in (self.test_preview, self.simulation_preview, self.reference_preview):
            preview.redraw()

    def _clear_result(self) -> None:
        self.result = None
        self.simulation = None
        self.restoration_plan = None
        self.xml_diff = ""
        self.save_button.configure(state="disabled")
        self.simulation_preview.clear()
        self.correction_panel.set_matrix(None)
        self.optimized_panel.set_matrix(None)
        for variable in self.kpi_vars:
            variable.set("—")
        for item in self.patch_tree.get_children():
            self.patch_tree.delete(item)
        self.engineering_text.configure(state="normal")
        self.engineering_text.delete("1.0", "end")
        self.engineering_text.insert("1.0", "尚未生成 ColorChecker 图像校正方案。")
        self.engineering_text.configure(state="disabled")
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        self.diff_text.insert("1.0", "尚无 XML 变更。")
        self.diff_text.configure(state="disabled")

    def _input_summary(self) -> None:
        parts = []
        if self.test_detection is not None:
            parts.append(f"测试：{self.test_detection.image.path.name} ({self.test_detection.method})")
        if self.reference_detection is not None:
            parts.append(f"目标：{self.reference_detection.image.path.name} ({self.reference_detection.method})")
        if self.document is not None:
            region = f" · Region #{self.selected_region.index}" if self.selected_region else ""
            parts.append(f"XML：{self.document.source_path.name}{region}")
        self.input_var.set("  |  ".join(parts) if parts else "请依次打开测试色卡、目标对比色卡与 Qualcomm CC XML。")

    def load_test_image(self, path: Optional[str] = None) -> None:
        selected = path or filedialog.askopenfilename(title="打开测试 ColorChecker", filetypes=IMAGE_FILE_TYPES)
        if not selected:
            return
        self.status_var.set("正在自动识别测试图的 24 个 ColorChecker 色块…")
        self.root.update_idletasks()
        try:
            detection = detect_colorchecker(selected)
        except ColorCheckerError as exc:
            messagebox.showerror("测试图识别失败", str(exc), parent=self.root)
            self.status_var.set("测试图识别失败。")
            return
        self.test_detection = detection
        self._clear_result()
        self.test_preview.set_image(
            detection.image,
            polygons=[patch.polygon for patch in detection.patches],
            meta_suffix=f"{detection.method} · 置信度 {detection.confidence:.0%}",
        )
        inferred = infer_cct(detection.image.path.name)
        if inferred is not None:
            self.cct_var.set(str(inferred))
        self._input_summary()
        self.status_var.set(
            f"测试图已识别 24 色块 · {detection.method} · 置信度 {detection.confidence:.0%}。"
            + (f" {detection.warning}" if detection.warning else "")
        )
        if self.document is not None and self.cct_var.get():
            self.auto_match_region(quiet=True)

    def load_reference_image(self, path: Optional[str] = None) -> None:
        selected = path or filedialog.askopenfilename(title="打开目标对比 ColorChecker", filetypes=IMAGE_FILE_TYPES)
        if not selected:
            return
        self.status_var.set("正在自动识别目标对比图的 24 个 ColorChecker 色块…")
        self.root.update_idletasks()
        try:
            detection = detect_colorchecker(selected)
        except ColorCheckerError as exc:
            messagebox.showerror("目标对比图识别失败", str(exc), parent=self.root)
            self.status_var.set("目标对比图识别失败。")
            return
        self.reference_detection = detection
        self._clear_result()
        self.reference_preview.set_image(
            detection.image,
            polygons=[patch.polygon for patch in detection.patches],
            meta_suffix=f"{detection.method} · 置信度 {detection.confidence:.0%}",
        )
        if not self.cct_var.get():
            inferred = infer_cct(detection.image.path.name)
            if inferred is not None:
                self.cct_var.set(str(inferred))
        self._input_summary()
        self.status_var.set(
            f"目标对比图已识别 24 色块 · {detection.method} · 置信度 {detection.confidence:.0%}。"
            + (f" {detection.warning}" if detection.warning else "")
        )

    def load_xml(self, path: Optional[str] = None) -> None:
        selected = path or filedialog.askopenfilename(
            title="打开 Qualcomm CC XML",
            filetypes=[("XML", "*.xml"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            document = QualcommCCDocument.load(selected)
        except (OSError, QualcommXMLError) as exc:
            messagebox.showerror("CC XML 读取失败", str(exc), parent=self.root)
            return
        self.document = document
        self.selected_region = None
        self.region_display_to_index.clear()
        values = []
        for region in document.regions:
            cct = region.cct_range
            prefix = f"#{region.index} · CCT {cct.start:g}-{cct.end:g}K" if cct else f"#{region.index}"
            display = f"{prefix} · {region.path_label()}"
            values.append(display)
            self.region_display_to_index[display] = region.index
        self.region_combo.configure(values=values)
        self._clear_result()
        if self.cct_var.get():
            self.auto_match_region(quiet=True)
        else:
            self.region_combo.current(0)
            self._select_region(0)
        self._input_summary()
        self.status_var.set(f"已加载 {document.source_path.name} · {len(document.regions)} regions；可手动选择或按 CCT 自动匹配。")

    def auto_match_region(self, *, quiet: bool = False) -> None:
        if self.document is None:
            if not quiet:
                messagebox.showinfo("需要 CC XML", "请先打开 Qualcomm CC XML。", parent=self.root)
            return
        try:
            cct = float(self.cct_var.get())
            region, mode = self.document.find_region_for_cct(cct)
        except (ValueError, QualcommXMLError) as exc:
            if not quiet:
                messagebox.showerror("Region 匹配失败", str(exc), parent=self.root)
            return
        self._select_region(region.index)
        self.status_var.set(
            f"CCT {cct:g}K {'精确命中' if mode == 'exact' else '位于 transition/gap，已选择最近'} Region #{region.index}。"
        )

    def _on_region_selected(self, _event: Optional[tk.Event] = None) -> None:
        display = self.region_var.get()
        if display in self.region_display_to_index:
            self._select_region(self.region_display_to_index[display], update_combo=False)

    def _select_region(self, index: int, *, update_combo: bool = True) -> None:
        if self.document is None:
            return
        self.selected_region = self.document.regions[index]
        if update_combo:
            for display, region_index in self.region_display_to_index.items():
                if region_index == index:
                    self.region_var.set(display)
                    break
        self._clear_result()
        self.original_panel.set_matrix(self.selected_region.matrix)
        self._input_summary()

    def run_optimization(self) -> None:
        if (
            self.test_detection is None
            or self.reference_detection is None
            or self.document is None
            or self.selected_region is None
        ):
            messagebox.showinfo(
                "资料未齐",
                "请打开测试 ColorChecker、目标对比 ColorChecker、CC XML，并选择 Region。",
                parent=self.root,
            )
            return
        try:
            strength = float(self.strength_var.get()) / 100.0
            if not 0.05 <= strength <= 1.0:
                raise ValueError("最大强度必须在 5% 到 100% 之间。")
            strategy = self.STRATEGY_LABELS[self.strategy_var.get()]
            dataset = build_comparison_dataset(self.test_detection, self.reference_detection)
            self.status_var.set("正在生成红轴色彩还原 Delta CCM 与整图仿真…")
            self.root.update_idletasks()
            restoration_plan: Optional[RestorationPlan] = None
            if strategy == "calibrated":
                cct = float(self.cct_var.get())
                restoration_plan = build_calibrated_restoration_plan(
                    self.selected_region.matrix,
                    cct,
                    strength=strength,
                )
                config = restoration_evaluation_config(
                    self.selected_region.matrix,
                    restoration_plan.optimized_matrix,
                )
                result = evaluate_ccm_correction(
                    dataset,
                    self.selected_region.matrix,
                    restoration_plan.correction_matrix,
                    composition="pre",
                    config=config,
                    search_method="calibrated-restoration",
                    blend=strength,
                    prediction_domain="encoded",
                    extra_warnings=(
                        *restoration_plan.warnings,
                        "该 Delta CCM 来自 3000K/4000K 实拍验证；仿真采用配套 Before→After 色块响应，其他场景与 ISP 状态仍需上机重拍确认。",
                    ),
                )
            else:
                config = image_optimization_config(
                    self.selected_region.matrix,
                    strategy=strategy,
                    maximum_strength=strength,
                )
                result = optimize_ccm(
                    dataset,
                    self.selected_region.matrix,
                    composition="pre",
                    config=config,
                )
            if restoration_plan is not None:
                simulation = simulate_restoration_response(
                    self.test_detection.image,
                    restoration_plan,
                )
            else:
                simulation = simulate_correction(
                    self.test_detection.image,
                    result.correction_matrix,
                    domain="linear",
                )
            xml_diff = self.document.diff_with_matrix(self.selected_region.index, result.optimized_matrix)
        except (ValueError, ColorCheckerError, OptimizationError, QualcommXMLError) as exc:
            messagebox.showerror("ColorChecker 图像校正失败", str(exc), parent=self.root)
            self.status_var.set("生成方案失败。")
            return
        self.result = result
        self.simulation = simulation
        self.restoration_plan = restoration_plan
        self.xml_diff = xml_diff
        self._render_result(dataset)

    def _render_result(self, dataset) -> None:
        assert self.result is not None
        assert self.simulation is not None
        assert self.test_detection is not None
        assert self.reference_detection is not None
        result = self.result
        self.original_panel.set_matrix(result.original_matrix)
        self.correction_panel.set_matrix(result.correction_matrix)
        self.optimized_panel.set_matrix(result.optimized_matrix)
        simulation_domain = (
            "实拍 Before→After 响应"
            if self.simulation.domain == "real-shot-response"
            else "linear sRGB"
        )
        self.simulation_preview.set_image(
            self.test_detection.image,
            rgb=self.simulation.rgb,
            polygons=[patch.polygon for patch in self.test_detection.patches],
            meta_suffix=f"{simulation_domain} · clip {self.simulation.clipped_pixel_ratio:.2%}",
        )
        simulated_patch_means = sample_patch_means(self.simulation.rgb, self.test_detection)
        comparison_rows = []
        for source_patch, simulated_rgb, target_patch in zip(
            self.test_detection.patches,
            simulated_patch_means,
            self.reference_detection.patches,
        ):
            before_error = _rgb_delta_e(source_patch.mean_rgb, target_patch.mean_rgb)
            after_error = _rgb_delta_e(simulated_rgb, target_patch.mean_rgb)
            comparison_rows.append((simulated_rgb, before_error, after_error))
        mean_before = float(np.mean([row[1] for row in comparison_rows]))
        mean_after = float(np.mean([row[2] for row in comparison_rows]))
        self.kpi_vars[0].set(f"{mean_before:.2f} → {mean_after:.2f}")
        source_red = self.test_detection.patches[14].mean_rgb
        simulated_red = simulated_patch_means[14]
        before_g_r, before_b_r = _red_channel_ratios(source_red)
        after_g_r, after_b_r = _red_channel_ratios(simulated_red)
        self.kpi_vars[1].set(
            f"{before_g_r:.2f}/{before_b_r:.2f} → {after_g_r:.2f}/{after_b_r:.2f}"
        )
        row_sums = result.matrix_health.row_sums
        neutral_scale = sum(row_sums) / 3.0
        self.kpi_vars[2].set(f"{result.matrix_health.status} · {neutral_scale:.6f}")
        self.kpi_vars[3].set(f"{self.simulation.clipped_pixel_ratio:.2%}")

        for item in self.patch_tree.get_children():
            self.patch_tree.delete(item)
        for patch in result.patch_results:
            target_raw = self.reference_detection.patches[patch.zone - 1].mean_rgb
            simulated_raw, before_error, after_error = comparison_rows[patch.zone - 1]
            improved = after_error <= before_error
            tags = ["neutral"] if patch.zone >= 19 else []
            tags.append("improved" if improved else "regressed")
            if before_error > 1e-9:
                change = f"{(before_error - after_error) / before_error:+.1%}"
            else:
                change = "0.0%" if after_error <= 1e-9 else "N/A"
            self.patch_tree.insert(
                "",
                "end",
                iid=f"checker-patch-{patch.zone}",
                values=(
                    patch.zone,
                    patch.name,
                    _rgb_text(self.test_detection.patches[patch.zone - 1].mean_rgb),
                    _rgb_text(simulated_raw),
                    _rgb_text(target_raw),
                    f"{before_error:.3f}",
                    f"{after_error:.3f}",
                    change,
                    "改善" if improved else "回退",
                ),
                tags=tags,
            )

        if self.restoration_plan is not None:
            workflow_lines = [
                "资料标定色彩还原",
                f"· Profile: {self.restoration_plan.profile_label} · 强度 {self.restoration_plan.strength:.0%}。",
                "· 3000K/4000K Delta CCM 来自资料中的实拍有效矩阵；100% 强度复现已验证终点。",
                "· 使用前乘 M_new = A × M_old；强度通过 A 与单位矩阵插值控制。",
                "· 红色还原优先降低标准红中的 G/B 串色，不通过整体抬高 R 来追饱和度。",
                f"· 最终三行和公共尺度 {self.restoration_plan.neutral_scale:.7f}，spread {self.restoration_plan.neutral_spread:.7g}；等行和保持灰阶中性。",
                "· 仿真使用所给 3000K/4000K Before→After 的 24 色块二次响应，纳入实拍中观察到的 Gamma、Tone Mapping、饱和度和剪切结果。",
                "· 响应模型比直接把 CCM 乘到 JPEG 更接近样例 After；换传感器、曝光或 ISP 配置后仍需重新实拍标定。",
            ]
        else:
            workflow_lines = [
                "图像自动拟合",
                "· 测试图和目标图均自动识别 24 个 ColorChecker 色块。",
                "· 目标色块在线性 sRGB 中逐块匹配测试亮度，避免把曝光/Gamma/Tone Mapping 差异写入 CCM。",
                "· 使用前乘 M_new = A × M_old；Delta A 强制保持中性轴。",
                "· 仿真只表示该线性 Delta CCM 对当前 JPEG 的近似作用，仍需上机重拍验证。",
            ]
        lines = [
            *workflow_lines,
            "",
            f"Real-shot comparison ΔE00: {mean_before:.4f} → {mean_after:.4f}",
            f"Matrix-only chroma estimate ΔE00: {result.mean_before:.4f} → {result.mean_after:.4f}",
            f"Standard red G/R · B/R: {before_g_r:.4f}/{before_b_r:.4f} → {after_g_r:.4f}/{after_b_r:.4f}",
            f"Method: {result.search_method} · lambda={result.regularization:g} · blend={result.blend:.0%}",
            f"Matrix Health: {result.matrix_health.status}",
            f"Row Sum: {', '.join(f'{value:.7f}' for value in result.matrix_health.row_sums)}",
            f"Condition: {result.matrix_health.condition_number:.4f} · determinant={result.matrix_health.determinant:.6f}",
            f"Simulation clipping: {self.simulation.clipped_pixel_ratio:.3%}",
            "",
            "工程检查",
        ]
        lines.extend(
            f"· [{check.status}] {check.name}: {check.value} · {check.message}"
            for check in result.matrix_health.checks
        )
        if result.warnings:
            lines.extend(("", "提示", *(f"· {warning}" for warning in result.warnings)))
        self.engineering_text.configure(state="normal")
        self.engineering_text.delete("1.0", "end")
        self.engineering_text.insert("1.0", "\n".join(lines))
        self.engineering_text.configure(state="disabled")
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        self.diff_text.insert("1.0", self.xml_diff or "No XML changes.")
        self.diff_text.configure(state="disabled")
        writable = result.matrix_health.status != "FAIL" and _matrix_changed(result.original_matrix, result.optimized_matrix)
        self.save_button.configure(state="normal" if writable else "disabled")
        level = "完成" if writable else "完成，但没有可安全写回的矩阵变化"
        if self.restoration_plan is not None:
            self.status_var.set(
                f"{level} · {self.restoration_plan.profile_label} · 标准红 G/R {before_g_r:.3f}→{after_g_r:.3f} · "
                f"中性尺度 {self.restoration_plan.neutral_scale:.6f} · Region #{self.selected_region.index if self.selected_region else '—'}。"
            )
        else:
            self.status_var.set(
                f"{level} · 平均 ΔE00 {result.mean_before:.3f} → {result.mean_after:.3f} · "
                f"Matrix {result.matrix_health.status} · Region #{self.selected_region.index if self.selected_region else '—'}。"
            )

    def save_xml(self) -> None:
        if self.document is None or self.selected_region is None or self.result is None:
            messagebox.showinfo("尚无结果", "请先生成 ColorChecker 图像校正方案。", parent=self.root)
            return
        if self.result.matrix_health.status == "FAIL":
            messagebox.showerror("工程检查未通过", "Matrix Health=FAIL，禁止写回 XML。", parent=self.root)
            return
        path = self.document.source_path
        if not messagebox.askyesno(
            "覆盖原 CC XML",
            f"将只修改当前 Region #{self.selected_region.index} 的 9 个 <c_tab><c> 数值，并覆盖原文件：\n"
            f"{path}\n\n是否继续？",
            parent=self.root,
        ):
            return
        try:
            self.document.save_with_matrix(path, self.selected_region.index, self.result.optimized_matrix)
            # Refresh source_text and Region matrices from disk so a later save
            # to another Region builds on this overwrite instead of rendering
            # from the originally loaded, now-stale XML text.
            selected_index = self.selected_region.index
            self.document = QualcommCCDocument.load(path)
            self.selected_region = self.document.regions[selected_index]
        except (OSError, QualcommXMLError) as exc:
            messagebox.showerror("CC XML 保存失败", str(exc), parent=self.root)
            return
        self.original_panel.set_matrix(self.selected_region.matrix)
        self._input_summary()
        self.save_button.configure(state="disabled")
        self.status_var.set(
            f"已覆盖并回读校验：{path}；仅修改 Region #{self.selected_region.index} 的 c_tab/c。"
        )

    def export_simulation(self) -> None:
        if self.simulation is None or self.test_detection is None:
            messagebox.showinfo("尚无仿真图", "请先生成 ColorChecker 图像校正方案。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            title="导出 CCM 仿真图",
            defaultextension=".png",
            initialfile=f"{self.test_detection.image.path.stem}_CC_simulation.png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg *.jpeg")],
        )
        if not path:
            return
        try:
            image = Image.fromarray(self.simulation.rgb, mode="RGB")
            suffix = Path(path).suffix.lower()
            if suffix in {".jpg", ".jpeg"}:
                image.save(path, quality=95)
            else:
                image.save(path)
        except OSError as exc:
            messagebox.showerror("仿真图导出失败", str(exc), parent=self.root)
            return
        self.status_var.set(f"已导出仿真图：{path}")

    def show_help(self) -> None:
        messagebox.showinfo(
            "ColorChecker 图像校正说明",
            "1. 测试图和目标对比图都应包含完整的 ColorChecker Classic 24。\n"
            "2. 默认“色彩还原（资料标定）”在 100% 强度下复现已实拍验证的 3000K/4000K 矩阵；2800K–4500K 内按 mired 插值，范围外需改用图像拟合。\n"
            "3. 色彩还原优先降低红色里的 G/B 串色；三行和只需彼此相等即可保持灰阶中性，不强制公共尺度为 1。\n"
            "4. 图像拟合模式仍可用于其他资料；它按色块在线性 sRGB 中消除亮度差后做保守拟合。\n"
            "5. 从 CC XML 手动选择 Region；文件名含显式 CCT 或常见光源标签时可自动匹配。\n"
            "6. 资料标定仿真由所给 Before→After 实拍色块响应生成；其他曝光、传感器或 ISP 配置仍需重新上机拍摄确认。\n"
            "7. 保存会确认后覆盖当前加载的原 XML，且只替换所选 Region 的 9 个 c_tab/c 数值。",
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

    def hide(self) -> None:
        if self.is_alive():
            self.outer.pack_forget()

    def show(self) -> bool:
        if not self.is_alive():
            return False
        self._configure_styles()
        self._build_menu()
        self.root.title(WINDOW_TITLE)
        self.outer.pack(fill="both", expand=True)
        return True

    def shutdown(self) -> None:
        self.test_preview._photo = None
        self.simulation_preview._photo = None
        self.reference_preview._photo = None

    def close(self) -> None:
        if self.on_close is not None:
            self.hide()
            self.on_close()
        else:
            self.root.destroy()
