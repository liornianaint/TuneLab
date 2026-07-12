from __future__ import annotations

import json
import math
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable, Optional, Sequence

from .branding import application_icon_path, show_about_dialog
from .gamma_models import (
    GammaOptimizationConfig,
    GammaOptimizationResult,
    GammaRegion,
    GrayDataset,
    GrayRangeAnalysis,
)
from .gamma_history import load_gamma_history, record_gamma_result, save_gamma_history
from .gamma_optimizer import GammaOptimizationError, optimize_gamma_lut
from .gamma_report import save_gamma_html_report
from .gamma_settings import load_gamma_settings, save_gamma_settings
from .gray_imatest import GrayCSVError, analyze_gray_range, parse_gray_csv
from .qualcomm_gamma_xml import QualcommGammaDocument, QualcommGammaXMLError


BG = "#F3F5F8"
PANEL = "#FFFFFF"
INK = "#172033"
MUTED = "#667085"
BLUE = "#2563EB"
GREEN = "#0F9D75"
RED = "#D92D20"
AMBER = "#B54708"
PURPLE = "#7F56D9"
FONT_BODY = "MatrixCorrectBodyFont"
FONT_SMALL = "MatrixCorrectSmallFont"
FONT_SMALL_BOLD = "MatrixCorrectSmallBoldFont"
FONT_TITLE = "MatrixCorrectTitleFont"
FONT_CARD_TITLE = "MatrixCorrectCardTitleFont"
FONT_KPI = "MatrixCorrectKpiFont"
FONT_PLOT_TITLE = "MatrixCorrectPlotTitleFont"
FONT_MONO = "MatrixCorrectMonoFont"


class CurvePlot(ttk.Frame):
    def __init__(self, master: tk.Misc, title: str, x_label: str, y_label: str) -> None:
        super().__init__(master, style="GammaCard.TFrame")
        self.title = title
        self.x_label = x_label
        self.y_label = y_label
        self.series: list[tuple[str, tuple[tuple[float, float], ...], str, bool]] = []
        self.canvas = tk.Canvas(self, background=PANEL, highlightthickness=0, height=260)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.draw())

    def set_series(
        self,
        series: Iterable[tuple[str, Sequence[tuple[float, float]], str, bool]],
    ) -> None:
        self.series = [(name, tuple(points), color, dashed) for name, points, color, dashed in series]
        self.draw()

    def draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(320, canvas.winfo_width())
        height = max(220, canvas.winfo_height())
        left, right, top, bottom = 72.0, width - 18.0, 42.0, height - 44.0
        canvas.create_text(14, 15, text=self.title, anchor="w", fill=INK, font=FONT_PLOT_TITLE)
        points = [point for _name, values, _color, _dashed in self.series for point in values]
        if not points:
            canvas.create_text(width / 2, height / 2, text="等待数据", fill=MUTED)
            return
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        x_span = max(x_max - x_min, 1e-6)
        y_span = max(y_max - y_min, 1e-6)
        x_min = max(0.0, x_min - x_span * 0.06) if min(xs) >= 0.0 else x_min - x_span * 0.06
        x_max += x_span * 0.06
        y_min = max(0.0, y_min - y_span * 0.10) if min(ys) >= 0.0 else y_min - y_span * 0.10
        y_max += y_span * 0.10

        def x_pos(value: float) -> float:
            return left + (value - x_min) / (x_max - x_min) * (right - left)

        def y_pos(value: float) -> float:
            return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

        for index in range(6):
            fraction = index / 5.0
            x_value = x_min + fraction * (x_max - x_min)
            y_value = y_min + fraction * (y_max - y_min)
            x_coordinate = x_pos(x_value)
            y_coordinate = y_pos(y_value)
            canvas.create_line(x_coordinate, top, x_coordinate, bottom, fill="#EAECF0")
            canvas.create_line(left, y_coordinate, right, y_coordinate, fill="#EAECF0")
            canvas.create_text(x_coordinate, bottom + 16, text=f"{x_value:.2f}", fill=MUTED, font=FONT_SMALL)
            canvas.create_text(left - 7, y_coordinate, text=f"{y_value:.3f}", anchor="e", fill=MUTED, font=FONT_SMALL)
        canvas.create_rectangle(left, top, right, bottom, outline="#98A2B3")
        canvas.create_text((left + right) / 2, height - 10, text=self.x_label, fill=MUTED)
        canvas.create_text(5, (top + bottom) / 2, text=self.y_label, fill=MUTED, anchor="w")
        legend_x = left + 6
        for name, values, color, dashed in self.series:
            coordinates: list[float] = []
            for x_value, y_value in values:
                coordinates.extend((x_pos(x_value), y_pos(y_value)))
            if len(coordinates) >= 4:
                canvas.create_line(*coordinates, fill=color, width=2.1, dash=(5, 3) if dashed else ())
            for x_value, y_value in values:
                x_coordinate, y_coordinate = x_pos(x_value), y_pos(y_value)
                canvas.create_oval(x_coordinate - 2.5, y_coordinate - 2.5, x_coordinate + 2.5, y_coordinate + 2.5, fill=color, outline="")
            canvas.create_line(legend_x, 29, legend_x + 20, 29, fill=color, width=2, dash=(5, 3) if dashed else ())
            canvas.create_text(legend_x + 24, 29, text=name, anchor="w", fill=INK, font=FONT_SMALL)
            legend_x += 28 + max(52, len(name) * 8)


class GammaOptimizationApp:
    RANGE_LABELS = {"自动识别": "auto", "全部灰阶（仍执行工程排除）": "all", "手动指定": "manual"}
    RGB_LABELS = {"RGB 联动（推荐）": "linked", "R/G/B 独立（高级）": "independent"}

    def __init__(
        self,
        root: tk.Misc,
        *,
        on_close: Optional[Callable[[], None]] = None,
        on_home: Optional[Callable[[], None]] = None,
        on_about: Optional[Callable[[], None]] = None,
    ) -> None:
        self.root = root
        self.on_close = on_close
        self.on_home = on_home
        self.on_about = on_about
        self.dataset: Optional[GrayDataset] = None
        self.analysis: Optional[GrayRangeAnalysis] = None
        self.document: Optional[QualcommGammaDocument] = None
        self.selected_region: Optional[GammaRegion] = None
        self.result: Optional[GammaOptimizationResult] = None
        self.region_display_to_index: dict[str, int] = {}
        self.settings = load_gamma_settings()
        self.history = load_gamma_history()
        self.xml_diff = ""
        self._configure_styles()
        self._build_menu()
        self._build_ui()
        # Embedded Gamma shares the application's one native window.  Its red
        # close button must continue to close TuneLab, not destroy this module
        # and leave the root with a stale callback.
        if self.on_close is None:
            try:
                self.root.protocol("WM_DELETE_WINDOW", self.close)
            except tk.TclError:
                pass

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        self.file_menu = tk.Menu(menu, tearoff=False)
        self.file_menu.add_command(label="打开 Imatest Gray CSV...", command=self.load_csv)
        self.file_menu.add_command(label="打开 Qualcomm Gamma XML...", command=self.load_xml)
        self.file_menu.add_command(label="保存 Gamma XML...", command=self.save_xml)
        self.file_menu.add_command(label="导出 Gamma 工程报告...", command=self.export_report)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="关闭", command=self.close)
        menu.add_cascade(label="文件", menu=self.file_menu)

        self.config_menu = tk.Menu(menu, tearoff=False)
        self.config_menu.add_command(label="导入 Gamma 配置...", command=self.import_config)
        self.config_menu.add_command(label="导出 Gamma 配置...", command=self.export_config)
        menu.add_cascade(label="配置", menu=self.config_menu)

        self.functions_menu = tk.Menu(menu, tearoff=False)
        if self.on_home is not None:
            self.functions_menu.add_command(label="首页", command=self.on_home)
        if self.on_close is not None:
            self.functions_menu.add_command(label="CC 校正", command=self.on_close)
        menu.add_cascade(label="工具", menu=self.functions_menu)

        self.help_menu = tk.Menu(menu, tearoff=False)
        self.help_menu.add_command(label="参数说明", command=self.show_help)
        self.help_menu.add_command(label="关于 TuneLab", command=self._show_about)
        menu.add_cascade(label="帮助", menu=self.help_menu)
        self.root.configure(menu=menu)

    def _show_about(self) -> None:
        if self.on_about is not None:
            self.on_about()
            return
        show_about_dialog(self.root, application_icon_path())

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if self.on_close is None and "clam" in style.theme_names():
            # Aqua follows macOS dark appearance and otherwise supplies white
            # Treeview text, which clashes with the explicit light engineering
            # cards/tags used by this cross-platform tool.
            style.theme_use("clam")
        default_font = tkfont.Font(root=self.root, name="TkDefaultFont", exists=True)
        text_font = tkfont.Font(root=self.root, name="TkTextFont", exists=True)
        heading_font = tkfont.Font(root=self.root, name="TkHeadingFont", exists=True)
        fixed_font = tkfont.Font(root=self.root, name="TkFixedFont", exists=True)
        base_size = max(11, min(12, abs(int(default_font.cget("size")))))

        def install_font(name: str, *, size: int, weight: str = "normal", source: tkfont.Font = default_font) -> None:
            try:
                font = tkfont.Font(root=self.root, name=name, exists=True)
            except tk.TclError:
                font = tkfont.Font(root=self.root, name=name, exists=False)
            font.configure(family=source.actual("family"), size=size, weight=weight)

        install_font(FONT_BODY, size=base_size, source=text_font)
        install_font(FONT_SMALL, size=max(10, base_size - 1), source=text_font)
        install_font(FONT_SMALL_BOLD, size=max(10, base_size - 1), weight="bold", source=heading_font)
        install_font(FONT_CARD_TITLE, size=13, weight="bold", source=heading_font)
        install_font(FONT_KPI, size=base_size + 2, weight="bold", source=heading_font)
        install_font(FONT_TITLE, size=19, weight="bold", source=heading_font)
        install_font(FONT_PLOT_TITLE, size=13, weight="bold", source=heading_font)
        install_font(FONT_MONO, size=max(10, base_size - 1), source=fixed_font)
        style.configure("GammaRoot.TFrame", background=BG)
        style.configure("GammaCard.TFrame", background=PANEL)
        style.configure("GammaCard.TLabel", background=PANEL, foreground=INK, font=FONT_BODY)
        style.configure("GammaTitle.TLabel", background=BG, foreground=INK, font=FONT_TITLE)
        style.configure("GammaMuted.TLabel", background=BG, foreground=MUTED, font=FONT_BODY)
        style.configure("GammaKpi.TLabel", background=PANEL, foreground=INK, font=FONT_KPI)
        style.configure("GammaPrimary.TButton", background=BLUE, foreground="white", padding=(14, 8), font=FONT_BODY)
        style.configure(
            "Gamma.Treeview",
            rowheight=25,
            background=PANEL,
            fieldbackground=PANEL,
            foreground=INK,
            font=FONT_SMALL,
        )
        style.configure(
            "Gamma.Treeview.Heading",
            background="#EAECF0",
            foreground=INK,
            font=FONT_SMALL_BOLD,
        )

    def _build_ui(self) -> None:
        self.root.title("TuneLab · Qualcomm Gamma 1.5 LUT 优化")
        try:
            self.root.geometry("1540x980")
            self.root.minsize(1180, 760)
        except tk.TclError:
            pass
        outer = ttk.Frame(self.root, padding=(18, 14), style="GammaRoot.TFrame")
        self.outer = outer
        outer.pack(fill="both", expand=True)
        title = ttk.Frame(outer, style="GammaRoot.TFrame")
        title.pack(fill="x", pady=(0, 10))
        ttk.Label(title, text="Gamma 优化", style="GammaTitle.TLabel").pack(side="left")
        ttk.Label(title, text="Imatest Stepchart · Qualcomm Gamma LUT（点数/位宽由 XML 决定）", style="GammaMuted.TLabel").pack(side="left", padx=(12, 0), pady=(5, 0))

        toolbar = ttk.Frame(outer, padding=10, style="GammaCard.TFrame")
        toolbar.pack(fill="x", pady=(0, 8))
        toolbar.columnconfigure(5, weight=1)
        ttk.Button(toolbar, text="打开 Gray CSV", command=self.load_csv).grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 7))
        ttk.Button(toolbar, text="打开 Gamma XML", command=self.load_xml).grid(row=0, column=1, rowspan=2, sticky="ns", padx=(0, 12))
        ttk.Label(toolbar, text="CCT / K", style="GammaCard.TLabel").grid(row=0, column=2, sticky="w")
        self.cct_var = tk.StringVar(value="6500")
        ttk.Entry(toolbar, textvariable=self.cct_var, width=8).grid(row=1, column=2, sticky="w")
        ttk.Button(toolbar, text="自动匹配 Region", command=self.auto_match_region).grid(row=1, column=3, sticky="w", padx=(6, 12))
        ttk.Label(toolbar, text="当前 Region / 完整触发路径", style="GammaCard.TLabel").grid(row=0, column=4, columnspan=2, sticky="w")
        self.region_var = tk.StringVar()
        self.region_combo = ttk.Combobox(toolbar, textvariable=self.region_var, state="readonly", width=62)
        self.region_combo.grid(row=1, column=4, columnspan=2, sticky="ew", padx=(0, 12))
        self.region_combo.bind("<<ComboboxSelected>>", self._on_region_selected)
        self.optimize_button = ttk.Button(toolbar, text="Gamma 优化", command=self.run_optimization, style="GammaPrimary.TButton")
        self.optimize_button.grid(row=0, column=6, rowspan=2, sticky="ns", padx=(0, 7))
        self.save_button = ttk.Button(toolbar, text="保存 Gamma XML", command=self.save_xml, state="disabled")
        self.save_button.grid(row=0, column=7, rowspan=2, sticky="ns")

        settings = ttk.Frame(outer, padding=10, style="GammaCard.TFrame")
        settings.pack(fill="x", pady=(0, 8))
        labels = (
            ("Gamma 提亮系数", 0), ("目标可识别阶数", 1), ("最大调整强度", 2),
            ("高光保护", 3), ("暗部保护", 4), ("RGB 模式", 5),
            ("灰阶范围", 6), ("识别阈值 / Pixel", 7), ("手动起止 Zone", 8),
        )
        for text, column in labels:
            ttk.Label(settings, text=text, style="GammaCard.TLabel").grid(row=0, column=column, sticky="w", padx=(0, 12))
        config = self.settings
        self.target_gamma_var = tk.StringVar(value=f"{config.target_gamma:g}")
        ttk.Entry(settings, textvariable=self.target_gamma_var, width=9).grid(row=1, column=0, sticky="w", padx=(0, 12))
        self.target_steps_var = tk.StringVar(value="自动" if config.target_step_count is None else str(config.target_step_count))
        ttk.Entry(settings, textvariable=self.target_steps_var, width=9).grid(row=1, column=1, sticky="w", padx=(0, 12))
        self.strength_var = tk.DoubleVar(value=config.maximum_adjustment * 100.0)
        ttk.Scale(settings, from_=0, to=100, variable=self.strength_var, orient="horizontal", length=105).grid(row=1, column=2, sticky="w", padx=(0, 12))
        self.highlight_var = tk.DoubleVar(value=config.highlight_protection * 100.0)
        ttk.Scale(settings, from_=0, to=100, variable=self.highlight_var, orient="horizontal", length=95).grid(row=1, column=3, sticky="w", padx=(0, 12))
        self.shadow_var = tk.DoubleVar(value=config.shadow_protection * 100.0)
        ttk.Scale(settings, from_=0, to=100, variable=self.shadow_var, orient="horizontal", length=95).grid(row=1, column=4, sticky="w", padx=(0, 12))
        rgb_label = next((label for label, value in self.RGB_LABELS.items() if value == config.rgb_mode), next(iter(self.RGB_LABELS)))
        self.rgb_mode_var = tk.StringVar(value=rgb_label)
        ttk.Combobox(settings, textvariable=self.rgb_mode_var, values=list(self.RGB_LABELS), state="readonly", width=18).grid(row=1, column=5, sticky="w", padx=(0, 12))
        range_label = next((label for label, value in self.RANGE_LABELS.items() if value == config.range_mode), next(iter(self.RANGE_LABELS)))
        self.range_mode_var = tk.StringVar(value=range_label)
        range_combo = ttk.Combobox(settings, textvariable=self.range_mode_var, values=list(self.RANGE_LABELS), state="readonly", width=22)
        range_combo.grid(row=1, column=6, sticky="w", padx=(0, 12))
        range_combo.bind("<<ComboboxSelected>>", lambda _event: self._range_changed())
        self.threshold_var = tk.StringVar(value=f"{config.threshold:g}")
        threshold_entry = ttk.Entry(settings, textvariable=self.threshold_var, width=9)
        threshold_entry.grid(row=1, column=7, sticky="w", padx=(0, 12))
        threshold_entry.bind("<Return>", lambda _event: self.reanalyze())
        threshold_entry.bind("<FocusOut>", lambda _event: self.reanalyze(quiet=True))
        manual = ttk.Frame(settings, style="GammaCard.TFrame")
        manual.grid(row=1, column=8, sticky="w")
        self.manual_start_var = tk.StringVar(value=str(config.manual_start_zone or 1))
        self.manual_end_var = tk.StringVar(value=str(config.manual_end_zone or 12))
        self.manual_start_entry = ttk.Entry(manual, textvariable=self.manual_start_var, width=5, state="disabled")
        self.manual_start_entry.pack(side="left")
        ttk.Label(manual, text=" – ", style="GammaCard.TLabel").pack(side="left")
        self.manual_end_entry = ttk.Entry(manual, textvariable=self.manual_end_var, width=5, state="disabled")
        self.manual_end_entry.pack(side="left")
        settings.columnconfigure(9, weight=1)

        self.info_var = tk.StringVar(value="请打开 Gray CSV 与 Gamma XML。")
        ttk.Label(outer, textvariable=self.info_var, style="GammaMuted.TLabel").pack(fill="x", pady=(0, 6))
        self.status_var = tk.StringVar(value="等待数据")
        ttk.Label(outer, textvariable=self.status_var, foreground=BLUE, background="#EAF0FF", padding=(9, 6)).pack(fill="x", pady=(0, 8))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.curve_tab = ttk.Frame(self.notebook, padding=8, style="GammaRoot.TFrame")
        self.engineering_tab = ttk.Frame(self.notebook, padding=8, style="GammaRoot.TFrame")
        self.diagnosis_tab = ttk.Frame(self.notebook, padding=8, style="GammaRoot.TFrame")
        self.history_tab = ttk.Frame(self.notebook, padding=8, style="GammaRoot.TFrame")
        self.notebook.add(self.curve_tab, text="  曲线对比  ")
        self.notebook.add(self.engineering_tab, text="  工程统计  ")
        self.notebook.add(self.diagnosis_tab, text="  诊断与解释  ")
        self.notebook.add(self.history_tab, text="  History / XML Diff  ")

        kpis = ttk.Frame(self.curve_tab, style="GammaRoot.TFrame")
        kpis.pack(fill="x", pady=(0, 8))
        for column in range(8):
            kpis.columnconfigure(column, weight=1, uniform="gamma-kpi")
        captions = ("原始 Zone", "可识别 Before→After", "目标阶数", "Global Gamma", "RMSE", "RGB 偏差", "LUT Format", "Curve Health")
        self.kpi_vars: list[tk.StringVar] = []
        for column, caption in enumerate(captions):
            card = ttk.Frame(kpis, padding=(9, 7), style="GammaCard.TFrame")
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 5 if column < 7 else 0))
            value = tk.StringVar(value="—")
            self.kpi_vars.append(value)
            ttk.Label(card, textvariable=value, style="GammaKpi.TLabel").pack(anchor="w")
            ttk.Label(card, text=caption, style="GammaCard.TLabel", foreground=MUTED).pack(anchor="w")

        charts = ttk.Frame(self.curve_tab, style="GammaRoot.TFrame")
        charts.pack(fill="both", expand=True, pady=(0, 8))
        for column in range(3):
            charts.columnconfigure(column, weight=1, uniform="gamma-chart")
        charts.rowconfigure(0, weight=1)
        self.response_plot = CurvePlot(charts, "灰阶响应：Before / Target / After", "-Log Exposure", "Density")
        self.response_plot.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.local_plot = CurvePlot(charts, "Local Gamma", "Zone", "Gamma")
        self.local_plot.grid(row=0, column=1, sticky="nsew", padx=5)
        self.lut_plot = CurvePlot(charts, "Gamma LUT", "LUT Index", "XML Value")
        self.lut_plot.grid(row=0, column=2, sticky="nsew", padx=(5, 0))

        details = ttk.Panedwindow(self.curve_tab, orient="horizontal")
        details.pack(fill="both", expand=True)
        pair_panel = ttk.Frame(details, padding=7, style="GammaCard.TFrame", width=410)
        zone_panel = ttk.Frame(details, padding=7, style="GammaCard.TFrame")
        details.add(pair_panel, weight=1)
        details.add(zone_panel, weight=2)
        ttk.Label(pair_panel, text="相邻灰阶可区分性", style="GammaCard.TLabel").pack(anchor="w", pady=(0, 5))
        self.pair_tree = ttk.Treeview(
            pair_panel,
            columns=("pair", "before", "target", "after", "status"),
            show="headings",
            height=8,
            style="Gamma.Treeview",
        )
        for column, title, width in (
            ("pair", "Zone i→i+1", 110), ("before", "Before Δ", 78),
            ("target", "Target Δ", 78), ("after", "After Δ", 78), ("status", "识别", 100),
        ):
            self.pair_tree.heading(column, text=title)
            self.pair_tree.column(column, width=width, anchor="center")
        pair_scroll = ttk.Scrollbar(pair_panel, orient="vertical", command=self.pair_tree.yview)
        self.pair_tree.configure(yscrollcommand=pair_scroll.set)
        self.pair_tree.pack(side="left", fill="both", expand=True)
        pair_scroll.pack(side="right", fill="y")
        self.pair_tree.tag_configure("invalid", foreground=RED)
        self.pair_tree.tag_configure("valid", foreground=GREEN)

        ttk.Label(zone_panel, text="每个 Zone 的误差与改善", style="GammaCard.TLabel").pack(anchor="w", pady=(0, 5))
        zone_frame = ttk.Frame(zone_panel, style="GammaCard.TFrame")
        zone_frame.pack(fill="both", expand=True)
        columns = ("zone", "used", "pixel", "target", "after", "before_error", "after_error", "improve", "local_before", "local_after")
        self.zone_tree = ttk.Treeview(
            zone_frame,
            columns=columns,
            show="headings",
            height=8,
            style="Gamma.Treeview",
        )
        headings = ("Zone", "Fit", "Pixel", "Target", "After", "Err Before", "Err After", "Improve", "Local Before", "Local After")
        widths = (54, 70, 76, 76, 76, 88, 88, 84, 96, 96)
        for column, title, width in zip(columns, headings, widths):
            self.zone_tree.heading(column, text=title)
            self.zone_tree.column(
                column,
                width=width,
                minwidth=width,
                stretch=column == "local_after",
                anchor="center",
            )
        zone_v = ttk.Scrollbar(zone_frame, orient="vertical", command=self.zone_tree.yview)
        zone_h = ttk.Scrollbar(zone_frame, orient="horizontal", command=self.zone_tree.xview)
        self.zone_tree.configure(yscrollcommand=zone_v.set, xscrollcommand=zone_h.set)
        self.zone_tree.grid(row=0, column=0, sticky="nsew")
        zone_v.grid(row=0, column=1, sticky="ns")
        zone_h.grid(row=1, column=0, sticky="ew")
        zone_frame.columnconfigure(0, weight=1)
        zone_frame.rowconfigure(0, weight=1)
        self.zone_tree.tag_configure("fit", background="#ECFDF3")
        self.zone_tree.tag_configure("constraint", background="#FFF7E8")
        self.zone_tree.tag_configure("excluded", foreground=MUTED)

        self.engineering_tree = ttk.Treeview(
            self.engineering_tab,
            columns=("check", "status", "value", "limit", "meaning"),
            show="headings",
            height=8,
            style="Gamma.Treeview",
        )
        for column, caption, width in (
            ("check", "Check", 190), ("status", "Status", 90), ("value", "Value", 220),
            ("limit", "Limit", 240), ("meaning", "Meaning", 500),
        ):
            self.engineering_tree.heading(column, text=caption)
            self.engineering_tree.column(column, width=width, anchor="w")
        self.engineering_tree.tag_configure("PASS", foreground=GREEN)
        self.engineering_tree.tag_configure("WARNING", foreground=AMBER)
        self.engineering_tree.tag_configure("FAIL", foreground=RED)
        self.engineering_tree.pack(fill="x")
        self.engineering_text = tk.Text(
            self.engineering_tab,
            wrap="word",
            background=PANEL,
            foreground=INK,
            relief="flat",
            padx=14,
            pady=12,
            font=FONT_MONO,
        )
        self.engineering_text.pack(fill="both", expand=True, pady=(8, 0))
        self.engineering_text.insert("1.0", "尚未运行 Gamma 优化。")
        self.engineering_text.configure(state="disabled")

        self.diagnosis_text = tk.Text(
            self.diagnosis_tab,
            wrap="word",
            background=PANEL,
            foreground=INK,
            relief="flat",
            padx=16,
            pady=14,
            font=FONT_BODY,
        )
        self.diagnosis_text.pack(fill="both", expand=True)
        self.diagnosis_text.insert("1.0", "尚未运行 Gamma 优化。")
        self.diagnosis_text.configure(state="disabled")

        history_actions = ttk.Frame(self.history_tab, style="GammaRoot.TFrame")
        history_actions.pack(fill="x", pady=(0, 8))
        ttk.Label(history_actions, text="每次 Gamma 优化自动记录目标、阶数、RMSE、Curve Health 与 XML Diff。", style="GammaMuted.TLabel").pack(side="left")
        ttk.Button(history_actions, text="清空历史", command=self.clear_history).pack(side="right")
        self.history_tree = ttk.Treeview(
            self.history_tab,
            columns=("time", "dataset", "target", "steps", "rmse", "format", "health"),
            show="headings",
            height=7,
            style="Gamma.Treeview",
        )
        for column, caption, width in (
            ("time", "Time", 165), ("dataset", "Dataset", 190), ("target", "Gamma Factor", 105),
            ("steps", "Steps", 110), ("rmse", "RMSE", 150), ("format", "LUT", 110), ("health", "Health", 85),
        ):
            self.history_tree.heading(column, text=caption)
            self.history_tree.column(column, width=width, anchor="w")
        self.history_tree.pack(fill="x")
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_selected)
        self.diff_text = tk.Text(
            self.history_tab,
            wrap="none",
            background="#101828",
            foreground="#F2F4F7",
            insertbackground="white",
            relief="flat",
            padx=12,
            pady=10,
            font=FONT_MONO,
        )
        self.diff_text.pack(fill="both", expand=True, pady=(8, 0))
        self._range_changed()
        self._render_history()

    def _range_changed(self) -> None:
        manual = self.RANGE_LABELS[self.range_mode_var.get()] == "manual"
        state = "normal" if manual else "disabled"
        self.manual_start_entry.configure(state=state)
        self.manual_end_entry.configure(state=state)

    def load_csv(self, path: Optional[str] = None) -> None:
        selected = path or filedialog.askopenfilename(
            title="打开 Imatest Gray / Stepchart CSV",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            self.dataset = parse_gray_csv(selected)
        except (OSError, GrayCSVError) as exc:
            messagebox.showerror("Gray CSV 读取失败", str(exc), parent=self.root)
            return
        self.result = None
        self.save_button.configure(state="disabled")
        self.reanalyze()

    def reanalyze(self, *, quiet: bool = False) -> None:
        if self.dataset is None:
            return
        try:
            threshold = float(self.threshold_var.get())
            self.analysis = analyze_gray_range(self.dataset, threshold)
        except (ValueError, GrayCSVError) as exc:
            if not quiet:
                messagebox.showerror("灰阶识别失败", str(exc), parent=self.root)
            return
        analysis = self.analysis
        self.manual_start_var.set(str(analysis.start_zone or ""))
        self.manual_end_var.set(str(analysis.end_zone or ""))
        for item in self.pair_tree.get_children():
            self.pair_tree.delete(item)
        for pair in analysis.pairs:
            self.pair_tree.insert(
                "", "end",
                values=(
                    f"{pair.from_zone} → {pair.to_zone}",
                    f"{pair.delta_pixel:.1f}",
                    "—",
                    "—",
                    "可区分" if pair.distinguishable else "不可区分",
                ),
                tags=("valid" if pair.distinguishable else "invalid",),
            )
        self.kpi_vars[0].set(str(len(self.dataset.zones)))
        self.kpi_vars[1].set(f"{analysis.effective_count} → —")
        target_text = self.target_steps_var.get().strip()
        self.kpi_vars[2].set(str(analysis.effective_count) if target_text in {"", "自动", "auto", "Auto"} else target_text)
        self.info_var.set(
            f"CSV：{self.dataset.source_path.name} · 原始 {len(self.dataset.zones)} Zone · "
            f"阈值 {analysis.threshold:g} · 连续有效 {analysis.effective_count} 阶 "
            f"({analysis.start_zone}–{analysis.end_zone})"
        )
        self.status_var.set("灰阶范围已重新识别；红色位置为不可区分，后续 Zone 不会跨断点累计。")
        self._render_raw_charts()

    def load_xml(self, path: Optional[str] = None) -> None:
        selected = path or filedialog.askopenfilename(
            title="打开 Qualcomm Gamma XML",
            filetypes=[("XML", "*.xml"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            self.document = QualcommGammaDocument.load(selected)
        except (OSError, QualcommGammaXMLError) as exc:
            messagebox.showerror("Gamma XML 读取失败", str(exc), parent=self.root)
            return
        values: list[str] = []
        self.region_display_to_index.clear()
        for region in self.document.regions:
            cct = region.cct_range
            prefix = f"#{region.index} · CCT {cct.start:g}-{cct.end:g}K" if cct else f"#{region.index}"
            display = f"{prefix} · {region.path_label()}"
            values.append(display)
            self.region_display_to_index[display] = region.index
        self.region_combo.configure(values=values)
        self.region_combo.current(0)
        self._select_region(0)
        self.auto_match_region(quiet=True)

    def auto_match_region(self, *, quiet: bool = False) -> None:
        if self.document is None:
            if not quiet:
                messagebox.showinfo("需要 Gamma XML", "请先打开 Qualcomm Gamma XML。", parent=self.root)
            return
        try:
            cct = float(self.cct_var.get())
            region, mode = self.document.find_region_for_cct(cct)
        except (ValueError, QualcommGammaXMLError) as exc:
            if not quiet:
                messagebox.showerror("Region 匹配失败", str(exc), parent=self.root)
            return
        self._select_region(region.index)
        self.status_var.set(
            f"CCT {cct:g}K {'精确命中' if mode == 'exact' else '选择最近'} Gamma region #{region.index}。"
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
        self.result = None
        self.save_button.configure(state="disabled")
        self.kpi_vars[6].set(f"{self.selected_region.length} / {self.selected_region.maximum}")
        self.lut_plot.title = f"{self.selected_region.length} 点 Gamma LUT · 0–{self.selected_region.maximum}"
        self._render_raw_charts()
        self.info_var.set(
            (self.info_var.get().split(" · XML：", 1)[0])
            + f" · XML：{self.document.source_path.name} · Region #{index} · "
            f"{self.selected_region.length} 点 / 0–{self.selected_region.maximum}"
        )

    def _config(self) -> GammaOptimizationConfig:
        mode = self.RANGE_LABELS[self.range_mode_var.get()]
        target_steps_text = self.target_steps_var.get().strip()
        target_steps = None if target_steps_text.lower() in {"", "自动", "auto"} else int(target_steps_text)
        if target_steps is not None and self.dataset is not None and target_steps > len(self.dataset.zones) - 1:
            raise ValueError(f"目标可识别阶数最多为 {len(self.dataset.zones) - 1}。")
        config = GammaOptimizationConfig(
            target_gamma=float(self.target_gamma_var.get()),
            target_step_count=target_steps,
            maximum_adjustment=self.strength_var.get() / 100.0,
            highlight_protection=self.highlight_var.get() / 100.0,
            shadow_protection=self.shadow_var.get() / 100.0,
            rgb_mode=self.RGB_LABELS[self.rgb_mode_var.get()],
            threshold=float(self.threshold_var.get()),
            range_mode=mode,
            manual_start_zone=int(self.manual_start_var.get()) if mode == "manual" else None,
            manual_end_zone=int(self.manual_end_var.get()) if mode == "manual" else None,
        )
        config.validate()
        return config

    def run_optimization(self) -> None:
        if self.dataset is None or self.analysis is None or self.document is None or self.selected_region is None:
            messagebox.showinfo("资料未齐", "请打开 Gray CSV、Gamma XML 并选择 Region。", parent=self.root)
            return
        try:
            config = self._config()
            if abs(config.threshold - self.analysis.threshold) > 1e-9:
                self.analysis = analyze_gray_range(self.dataset, config.threshold)
            self.result = optimize_gamma_lut(self.dataset, self.selected_region, self.analysis, config=config)
            self.xml_diff = self.document.diff_with_luts(
                self.selected_region.index,
                self.result.after_r,
                self.result.after_g,
                self.result.after_b,
            )
        except (ValueError, GammaOptimizationError) as exc:
            messagebox.showerror("Gamma 优化失败", str(exc), parent=self.root)
            return
        self.settings = config
        try:
            save_gamma_settings(config)
        except OSError:
            pass
        self.history.append(
            record_gamma_result(
                self.result,
                dataset_name=self.dataset.source_path.name,
                xml_name=self.document.source_path.name,
                region_label=self.selected_region.path_label(),
                xml_diff=self.xml_diff,
            )
        )
        try:
            save_gamma_history(self.history)
        except OSError:
            pass
        self.save_button.configure(state="normal" if self.result.health.status != "FAIL" else "disabled")
        self._render_result()
        self._render_history()

    def _render_raw_charts(self) -> None:
        if self.dataset is not None:
            response = [(-zone.log_exposure, zone.density) for zone in self.dataset.zones]
            local = [(zone.zone, zone.slope) for zone in self.dataset.zones if zone.slope is not None]
            self.response_plot.set_series((("Before", response, BLUE, False),))
            self.local_plot.set_series((("Before", local, BLUE, False),))
        if self.selected_region is not None:
            step = max(1, self.selected_region.length // 64)
            before = [(index, self.selected_region.channel_g[index]) for index in range(0, self.selected_region.length, step)]
            if before[-1][0] != self.selected_region.length - 1:
                before.append((self.selected_region.length - 1, self.selected_region.channel_g[-1]))
            self.lut_plot.set_series((("Before G", before, BLUE, False),))

    def _render_result(self) -> None:
        assert self.result is not None and self.dataset is not None and self.analysis is not None
        result = self.result
        metrics = result.metrics
        self.kpi_vars[1].set(f"{metrics.distinguishable_before} → {metrics.distinguishable_after}")
        self.kpi_vars[2].set(str(metrics.distinguishable_target))
        self.kpi_vars[3].set(
            f"{metrics.global_gamma_before:.3f}→{metrics.global_gamma_after:.3f}"
        )
        self.kpi_vars[4].set(f"{metrics.rmse_before:.4f}→{metrics.rmse_after:.4f}")
        self.kpi_vars[5].set(f"{metrics.rgb_gray_deviation_before:.4f}→{metrics.rgb_gray_deviation_after:.4f}")
        self.kpi_vars[6].set(f"{result.lut_length} / {result.maximum_value}")
        self.kpi_vars[7].set(result.health.status)
        response_before = [(-next(zone.log_exposure for zone in self.dataset.zones if zone.zone == item.zone), item.density_before) for item in result.zone_results]
        response_target = [(-next(zone.log_exposure for zone in self.dataset.zones if zone.zone == item.zone), item.density_target) for item in result.zone_results]
        response_after = [(-next(zone.log_exposure for zone in self.dataset.zones if zone.zone == item.zone), item.density_after) for item in result.zone_results]
        self.response_plot.set_series(
            (("Before", response_before, BLUE, False), ("Target", response_target, AMBER, True), ("After", response_after, GREEN, False))
        )
        local_before = [(item.zone, item.local_gamma_before) for item in result.zone_results if item.local_gamma_before is not None]
        local_target = [(item.zone, item.local_gamma_target) for item in result.zone_results if item.local_gamma_target is not None]
        local_after = [(item.zone, item.local_gamma_after) for item in result.zone_results if item.local_gamma_after is not None]
        self.local_plot.set_series(
            (("Before", local_before, BLUE, False), ("Target", local_target, AMBER, True), ("After", local_after, GREEN, False))
        )
        step = max(1, len(result.before_g) // 64)
        indexes = list(range(0, len(result.before_g), step))
        if indexes[-1] != len(result.before_g) - 1:
            indexes.append(len(result.before_g) - 1)
        self.lut_plot.set_series(
            (
                ("Before G", [(index, result.before_g[index]) for index in indexes], BLUE, False),
                ("Target", [(index, result.target_lut[index]) for index in indexes], AMBER, True),
                ("After G", [(index, result.after_g[index]) for index in indexes], GREEN, False),
                ("After R/B", [(index, (result.after_r[index] + result.after_b[index]) / 2.0) for index in indexes], PURPLE, True),
            )
        )
        for tree_item in self.pair_tree.get_children():
            self.pair_tree.delete(tree_item)
        for pair in result.pair_results:
            status = "达到目标" if pair.target_required and pair.after_distinguishable else "未达到" if pair.target_required else "可区分" if pair.after_distinguishable else "不可区分"
            tag = "valid" if pair.after_distinguishable else "invalid"
            self.pair_tree.insert(
                "",
                "end",
                values=(
                    f"{pair.from_zone} → {pair.to_zone}",
                    f"{pair.delta_before:.1f}",
                    f"{pair.delta_target:.1f}",
                    f"{pair.delta_after:.1f}",
                    status,
                ),
                tags=(tag,),
            )
        for item in self.zone_tree.get_children():
            self.zone_tree.delete(item)
        for item in result.zone_results:
            improve = "N/A" if item.improvement_percent is None else f"{item.improvement_percent:+.1f}%"
            self.zone_tree.insert(
                "", "end",
                values=(
                    item.zone, item.status, f"{item.pixel_before:.1f}", f"{item.pixel_target:.1f}",
                    f"{item.pixel_after:.1f}", f"{item.error_before:+.4f}", f"{item.error_after:+.4f}", improve,
                    "—" if item.local_gamma_before is None else f"{item.local_gamma_before:.3f}",
                    "—" if item.local_gamma_after is None else f"{item.local_gamma_after:.3f}",
                ),
                tags=("fit" if item.used else "constraint" if item.status == "CONSTRAINT" else "excluded",),
            )
        self.status_var.set(
            f"Gamma 优化完成：可识别 {metrics.distinguishable_before}→{metrics.distinguishable_after} 阶，目标 {metrics.distinguishable_target}；"
            f"RMSE {metrics.rmse_before:.5f}→{metrics.rmse_after:.5f}；Curve Health={result.health.status}；"
            f"提亮系数={result.target_gamma_factor:.3f}，实际强度={result.applied_strength:.0%}，LUT={result.lut_length}点/0–{result.maximum_value}。"
        )
        self._render_engineering()
        self._render_diagnostics()

    def _render_engineering(self) -> None:
        if self.result is None:
            return
        for item in self.engineering_tree.get_children():
            self.engineering_tree.delete(item)
        for index, check in enumerate(self.result.health.checks):
            self.engineering_tree.insert(
                "",
                "end",
                iid=f"gamma-check-{index}",
                values=(check.name, check.status, check.value, check.limit, check.message),
                tags=(check.status,),
            )
        metrics = self.result.metrics
        loss_before = self.result.loss_before
        loss_after = self.result.loss_after
        lines = [
            f"Curve Health: {self.result.health.status}",
            f"LUT: {self.result.lut_length} points · integer 0–{self.result.maximum_value}",
            f"Gamma lift factor: {self.result.target_gamma_factor:.3f} (1.0 nominal; larger is brighter)",
            f"Recognizable steps: Before {metrics.distinguishable_before} · Target {metrics.distinguishable_target} · After {metrics.distinguishable_after}",
            f"Global Gamma: {metrics.global_gamma_before:.5f} → {metrics.global_gamma_after:.5f} · target response {metrics.global_gamma_target:.5f}",
            f"RMSE: {metrics.rmse_before:.6f} → {metrics.rmse_after:.6f}",
            f"Max gray error: {metrics.maximum_error_before:.6f} → {metrics.maximum_error_after:.6f}",
            f"Local Gamma error: {metrics.local_gamma_error_before:.6f} → {metrics.local_gamma_error_after:.6f}",
            f"RGB gray deviation: {metrics.rgb_gray_deviation_before:.6f} → {metrics.rgb_gray_deviation_after:.6f}",
            f"Monotonic: {self.result.health.monotonic} · reversals={self.result.health.reversal_count}",
            f"Maximum LUT jump: {self.result.health.maximum_jump} · quantization error={self.result.health.quantization_error:.8f}",
            "",
            "Multi-objective Loss",
            f"Total {loss_before.total:.6f} → {loss_after.total:.6f}",
            f"Gray target={loss_after.gray_target:.6f} · Local Gamma={loss_after.local_gamma:.6f}",
            f"Smoothness={loss_after.lut_smoothness:.6f} · LUT change={loss_after.lut_change:.6f}",
            f"Highlight={loss_after.highlight:.6f} · Shadow={loss_after.shadow:.6f}",
            f"RGB bias={loss_after.rgb_bias:.6f} · Step separation={loss_after.step_separation:.6f}",
        ]
        if self.result.warnings:
            lines.extend(("", "Warnings", *(f"· {warning}" for warning in self.result.warnings)))
        self.engineering_text.configure(state="normal")
        self.engineering_text.delete("1.0", "end")
        self.engineering_text.insert("1.0", "\n".join(lines))
        self.engineering_text.configure(state="disabled")

    def _render_diagnostics(self) -> None:
        if self.result is None:
            return
        lines = ["Explainable Optimization", ""]
        lines.extend(f"· {line}" for line in self.result.explainability)
        for diagnosis in self.result.diagnostics:
            lines.extend(
                (
                    "",
                    f"[{diagnosis.severity}] {diagnosis.module} · Confidence {diagnosis.confidence:.0%}",
                    f"Root Cause: {diagnosis.root_cause}",
                    *(f"  Evidence: {item}" for item in diagnosis.evidence),
                    f"Action: {diagnosis.action}",
                )
            )
        self.diagnosis_text.configure(state="normal")
        self.diagnosis_text.delete("1.0", "end")
        self.diagnosis_text.insert("1.0", "\n".join(lines))
        self.diagnosis_text.configure(state="disabled")

    def _render_history(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for reverse_index, record in enumerate(reversed(self.history)):
            self.history_tree.insert(
                "",
                "end",
                iid=f"gamma-history-{reverse_index}",
                values=(
                    record.timestamp,
                    record.dataset_name,
                    f"{record.target_gamma_factor:.3f}",
                    f"{record.before_steps}→{record.after_steps} / {record.target_steps}",
                    f"{record.rmse_before:.5f}→{record.rmse_after:.5f}",
                    f"{record.lut_length}/{record.maximum_value}",
                    record.curve_status,
                ),
            )

    def _on_history_selected(self, _event: Optional[tk.Event] = None) -> None:
        selection = self.history_tree.selection()
        if not selection:
            return
        try:
            reverse_index = int(selection[0].rsplit("-", 1)[1])
            record = self.history[-1 - reverse_index]
        except (ValueError, IndexError):
            return
        content = (
            f"Timestamp: {record.timestamp}\nDataset: {record.dataset_name}\nXML: {record.xml_name}\n"
            f"Region: {record.region_label}\nGamma Factor: {record.target_gamma_factor:.3f}\n"
            f"Steps: {record.before_steps} → {record.after_steps} / target {record.target_steps}\n"
            f"RMSE: {record.rmse_before:.6f} → {record.rmse_after:.6f}\n"
            f"LUT: {record.lut_length} points / 0–{record.maximum_value}\nCurve Health: {record.curve_status}\n\n"
            f"XML Diff\n{record.xml_diff or 'Not recorded'}"
        )
        self.diff_text.delete("1.0", "end")
        self.diff_text.insert("1.0", content)

    def show_history(self) -> None:
        self.notebook.select(self.history_tab)

    def clear_history(self) -> None:
        if not messagebox.askyesno("清空 Gamma History", "确定清空所有 Gamma 优化记录吗？", parent=self.root):
            return
        self.history = []
        try:
            save_gamma_history(self.history)
        except OSError as exc:
            messagebox.showerror("清空失败", str(exc), parent=self.root)
            return
        self._render_history()
        self.diff_text.delete("1.0", "end")

    def _apply_config(self, config: GammaOptimizationConfig) -> None:
        self.settings = config
        self.target_gamma_var.set(f"{config.target_gamma:g}")
        self.target_steps_var.set("自动" if config.target_step_count is None else str(config.target_step_count))
        self.strength_var.set(config.maximum_adjustment * 100.0)
        self.highlight_var.set(config.highlight_protection * 100.0)
        self.shadow_var.set(config.shadow_protection * 100.0)
        self.rgb_mode_var.set(next(label for label, value in self.RGB_LABELS.items() if value == config.rgb_mode))
        self.range_mode_var.set(next(label for label, value in self.RANGE_LABELS.items() if value == config.range_mode))
        self.threshold_var.set(f"{config.threshold:g}")
        if config.manual_start_zone is not None:
            self.manual_start_var.set(str(config.manual_start_zone))
        if config.manual_end_zone is not None:
            self.manual_end_var.set(str(config.manual_end_zone))
        self._range_changed()
        self.reanalyze(quiet=True)

    def import_config(self) -> None:
        path = filedialog.askopenfilename(
            title="导入 Gamma 配置",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            config = load_gamma_settings(path)
            if not isinstance(payload, dict):
                raise ValueError("配置根节点必须是 JSON object。")
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return
        self._apply_config(config)
        self.status_var.set(f"已导入 Gamma 配置：{path}")

    def export_config(self) -> None:
        try:
            config = self._config()
        except ValueError as exc:
            messagebox.showerror("配置无效", str(exc), parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            title="导出 Gamma 配置",
            defaultextension=".json",
            initialfile="gamma_settings.json",
            filetypes=[("JSON", "*.json")],
            parent=self.root,
        )
        if not path:
            return
        try:
            save_gamma_settings(config, path)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)
            return
        self.status_var.set(f"已导出 Gamma 配置：{path}")

    def export_report(self) -> None:
        if self.dataset is None or self.selected_region is None or self.result is None:
            messagebox.showinfo("尚无结果", "请先完成 Gamma 优化。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            title="导出 Gamma 工程报告",
            defaultextension=".html",
            initialfile=f"{self.dataset.source_path.stem}_gamma_report.html",
            filetypes=[("HTML", "*.html")],
            parent=self.root,
        )
        if not path:
            return
        try:
            save_gamma_html_report(path, self.dataset, self.selected_region, self.result)
        except OSError as exc:
            messagebox.showerror("报告导出失败", str(exc), parent=self.root)
            return
        self.status_var.set(f"Gamma 工程报告已导出：{path}")

    def show_help(self) -> None:
        messagebox.showinfo(
            "Gamma 参数说明",
            "Gamma 提亮系数默认 1.0：1.0 保持标称亮度，数值越大 LUT 中间调越亮；"
            "首尾点和 XML 整数上限保持不变。\n\n"
            "目标可识别阶数使用 ΔPixel 阈值约束；自动表示至少保持当前连续阶数。"
            "若目标与暗部/高光保护冲突，工具保留工程门禁并明确提示未达到。\n\n"
            "LUT 点数与最大值从当前 Qualcomm XML 读取；本工程样例为 257 点、0–1023。",
            parent=self.root,
        )

    def close(self) -> None:
        try:
            save_gamma_settings(self._config())
            save_gamma_history(self.history)
        except (OSError, ValueError, tk.TclError):
            pass
        if self.on_close is not None:
            self.outer.destroy()
            self.on_close()
        else:
            self.root.destroy()

    def is_alive(self) -> bool:
        """Use Tcl's existence query: Python widget wrappers can outlive Tk."""
        try:
            return bool(int(self.root.tk.call("winfo", "exists", str(self.outer))))
        except tk.TclError:
            return False

    def hide(self) -> bool:
        if not self.is_alive():
            return False
        try:
            self.outer.pack_forget()
        except tk.TclError:
            return False
        return True

    def show(self) -> bool:
        if not self.is_alive():
            return False
        try:
            self.outer.pack(fill="both", expand=True)
            self.root.title("TuneLab · Qualcomm Gamma 1.5 LUT 优化")
            self._build_menu()
        except tk.TclError:
            return False
        return True

    def save_xml(self) -> None:
        if self.document is None or self.result is None or self.selected_region is None:
            messagebox.showinfo("尚无结果", "请先完成 Gamma 优化。", parent=self.root)
            return
        if self.result.health.status == "FAIL":
            messagebox.showerror("Curve Health 未通过", "Curve Health=FAIL，禁止写回 Gamma XML。", parent=self.root)
            return
        path = self.document.source_path
        if not messagebox.askyesno(
            "覆盖原 Gamma XML",
            f"将只更新当前 Gamma Region 的 LUT，并覆盖原文件：\n{path}\n\n是否继续？",
            parent=self.root,
        ):
            return
        try:
            self.document.save_with_luts(
                path,
                self.selected_region.index,
                self.result.after_r,
                self.result.after_g,
                self.result.after_b,
            )
        except (OSError, QualcommGammaXMLError) as exc:
            messagebox.showerror("Gamma XML 保存失败", str(exc), parent=self.root)
            return
        self.status_var.set(f"已覆盖并回读校验：{path}；只修改 Gamma region #{self.selected_region.index}。")


def open_gamma_window(master: tk.Misc) -> GammaOptimizationApp:
    window = tk.Toplevel(master)
    application = GammaOptimizationApp(window)
    setattr(window, "_matrixcorrect_gamma_app", application)
    return application


def main() -> None:
    root = tk.Tk()
    GammaOptimizationApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
