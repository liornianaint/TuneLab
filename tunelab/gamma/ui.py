from __future__ import annotations

import json
import math
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable, Optional, Sequence

from ..branding import application_icon_path, show_about_dialog, show_workbench_help
from ..ui_foundation import (
    ACTION_BLUE,
    DANGER,
    FONT_BODY,
    FONT_CARD_TITLE,
    FONT_KPI,
    FONT_MONO,
    FONT_NAV_SECTION,
    FONT_PLOT_TITLE,
    FONT_SMALL,
    FONT_SMALL_BOLD,
    FONT_TITLE,
    INFO_BG,
    INK,
    MUTED,
    PANEL_BG,
    ROW_HEIGHT,
    SUBTLE_SEPARATOR,
    SUCCESS,
    TABLE_HEADING_BG,
    TERTIARY,
    WARNING,
    WINDOW_BG,
    bind_responsive_wrap,
    configure_macos_theme,
    default_sources_directory,
    elide_canvas_text,
    fit_window_to_screen,
)
from ..updates import update_controller_for
from .models import (
    GammaOptimizationConfig,
    GammaOptimizationResult,
    GammaRegion,
    GrayDataset,
    GrayRangeAnalysis,
)
from .history import load_gamma_history, record_gamma_result, save_gamma_history
from .optimizer import GammaOptimizationError, minimum_continuity_gap, optimize_gamma_lut
from .reporting import save_gamma_html_report
from .settings import load_gamma_settings, save_gamma_settings
from .imatest import GrayCSVError, analyze_gray_range, parse_gray_csv
from .qualcomm_xml import QualcommGammaDocument, QualcommGammaXMLError


BG = WINDOW_BG
PANEL = PANEL_BG
BLUE = ACTION_BLUE
GREEN = SUCCESS
RED = DANGER
AMBER = WARNING
PURPLE = "#7F56D9"
class CurvePlot(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        title: str,
        x_label: str,
        y_label: str,
        *,
        show_markers: bool = True,
    ) -> None:
        super().__init__(master, style="GammaCard.TFrame")
        self.title = title
        self.x_label = x_label
        self.y_label = y_label
        self.show_markers = show_markers
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
        canvas.create_text(
            14,
            15,
            text=elide_canvas_text(
                canvas,
                self.title,
                FONT_PLOT_TITLE,
                max(80, width - 28),
            ),
            anchor="w",
            fill=INK,
            font=FONT_PLOT_TITLE,
        )
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
            canvas.create_line(x_coordinate, top, x_coordinate, bottom, fill=SUBTLE_SEPARATOR)
            canvas.create_line(left, y_coordinate, right, y_coordinate, fill=SUBTLE_SEPARATOR)
            canvas.create_text(x_coordinate, bottom + 16, text=f"{x_value:.2f}", fill=MUTED, font=FONT_SMALL)
            canvas.create_text(left - 7, y_coordinate, text=f"{y_value:.3f}", anchor="e", fill=MUTED, font=FONT_SMALL)
        canvas.create_rectangle(left, top, right, bottom, outline=TERTIARY)
        canvas.create_text((left + right) / 2, height - 10, text=self.x_label, fill=MUTED)
        canvas.create_text(5, (top + bottom) / 2, text=self.y_label, fill=MUTED, anchor="w")
        legend_x = left + 6
        for name, values, color, dashed in self.series:
            coordinates: list[float] = []
            for x_value, y_value in values:
                coordinates.extend((x_pos(x_value), y_pos(y_value)))
            if len(coordinates) >= 4:
                canvas.create_line(
                    *coordinates,
                    fill=color,
                    width=2.1,
                    dash=(5, 3) if dashed else (),
                    capstyle=tk.ROUND,
                    joinstyle=tk.ROUND,
                )
            if self.show_markers:
                for x_value, y_value in values:
                    x_coordinate, y_coordinate = x_pos(x_value), y_pos(y_value)
                    canvas.create_oval(x_coordinate - 2.5, y_coordinate - 2.5, x_coordinate + 2.5, y_coordinate + 2.5, fill=color, outline="")
            canvas.create_line(legend_x, 29, legend_x + 20, 29, fill=color, width=2, dash=(5, 3) if dashed else ())
            canvas.create_text(legend_x + 24, 29, text=name, anchor="w", fill=INK, font=FONT_SMALL)
            legend_x += 28 + max(52, len(name) * 8)


class GammaWorkspace:
    RANGE_LABELS = {"自动识别": "auto", "全部灰阶（仍执行工程排除）": "all", "手动指定": "manual"}
    RGB_LABELS = {"RGB 联动（推荐）": "linked", "R/G/B 独立（高级）": "independent"}

    def __init__(
        self,
        root: tk.Misc,
        *,
        on_close: Optional[Callable[[], None]] = None,
        on_home: Optional[Callable[[], None]] = None,
        on_colorchecker: Optional[Callable[[], object]] = None,
        on_image_inspector: Optional[Callable[[], object]] = None,
        on_about: Optional[Callable[[], None]] = None,
    ) -> None:
        self.root = root
        self.on_close = on_close
        self.on_home = on_home
        self.on_colorchecker = on_colorchecker
        self.on_image_inspector = on_image_inspector
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
            self.window_placement = fit_window_to_screen(
                self.root,
                desired_width=1540,
                desired_height=980,
            )
            try:
                self.root.protocol("WM_DELETE_WINDOW", self.close)
            except tk.TclError:
                pass

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        self.file_menu = tk.Menu(menu, tearoff=False)
        self.file_menu.add_command(label="打开 Gamma CSV...", command=self.load_csv)
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
            self.functions_menu.add_command(label="CCM / ColorChecker 校正", command=self.on_close)
        if self.on_image_inspector is not None:
            self.functions_menu.add_command(label="图像分析器", command=self.on_image_inspector)
        menu.add_cascade(label="工具", menu=self.functions_menu)

        self.help_menu = tk.Menu(menu, tearoff=False)
        self.help_menu.add_command(label="TuneLab 使用说明", command=self._show_workbench_help)
        self.help_menu.add_separator()
        self.help_menu.add_command(label="Gamma 参数说明", command=self.show_help)
        self.help_menu.add_separator()
        self.help_menu.add_command(label="检查更新...", command=self._check_for_updates)
        self.help_menu.add_command(label="关于 TuneLab", command=self._show_about)
        menu.add_cascade(label="帮助", menu=self.help_menu)
        self.root.configure(menu=menu)

    def _show_about(self) -> None:
        if self.on_about is not None:
            self.on_about()
            return
        show_about_dialog(self.root, application_icon_path())

    def _show_workbench_help(self) -> None:
        show_workbench_help(self.root)

    def _check_for_updates(self) -> None:
        update_controller_for(self.root).check(manual=True)

    def _configure_styles(self) -> None:
        style = configure_macos_theme(self.root)
        style.configure("GammaRoot.TFrame", background=BG)
        style.configure(
            "GammaCard.TFrame",
            background=PANEL,
            relief="solid",
            borderwidth=1,
            bordercolor=SUBTLE_SEPARATOR,
            lightcolor=SUBTLE_SEPARATOR,
            darkcolor=SUBTLE_SEPARATOR,
        )
        style.configure("GammaSurface.TFrame", background=PANEL)
        style.configure("GammaCard.TLabel", background=PANEL, foreground=INK, font=FONT_BODY)
        style.configure("GammaTitle.TLabel", background=BG, foreground=INK, font=FONT_TITLE)
        style.configure("GammaMuted.TLabel", background=BG, foreground=MUTED, font=FONT_BODY)
        style.configure("GammaEyebrow.TLabel", background=BG, foreground=TERTIARY, font=FONT_NAV_SECTION)
        style.configure("GammaKpi.TLabel", background=PANEL, foreground=INK, font=FONT_KPI)
        style.configure(
            "Gamma.Treeview",
            rowheight=ROW_HEIGHT,
            background=PANEL,
            fieldbackground=PANEL,
            foreground=INK,
            font=FONT_SMALL,
        )
        style.configure(
            "Gamma.Treeview.Heading",
            background=TABLE_HEADING_BG,
            foreground=MUTED,
            font=FONT_SMALL_BOLD,
            relief="flat",
            padding=(7, 6),
        )

    def _build_ui(self) -> None:
        self.root.title("TuneLab · Qualcomm Gamma 1.5 LUT 优化")
        outer = ttk.Frame(self.root, padding=(16, 10), style="GammaRoot.TFrame")
        self.outer = outer
        outer.pack(fill="both", expand=True)
        title = ttk.Frame(outer, style="GammaRoot.TFrame")
        title.pack(fill="x", pady=(0, 6))
        ttk.Label(title, text="Gamma 优化", style="GammaTitle.TLabel").pack(side="left")
        ttk.Label(
            title,
            text="Imatest Stepchart · Qualcomm Gamma LUT，点数与位宽由 XML 自动识别",
            style="GammaMuted.TLabel",
        ).pack(side="left", padx=(14, 0), pady=(5, 0))
        if self.on_home is not None:
            ttk.Button(title, text="返回首页", command=self.on_home, style="Quiet.TButton").pack(side="right")

        toolbar = ttk.Frame(outer, padding=(10, 8), style="GammaCard.TFrame")
        self.toolbar_panel = toolbar
        toolbar.pack(fill="x", pady=(0, 8))
        toolbar.columnconfigure(5, weight=1)
        ttk.Button(toolbar, text="1  打开 Gamma CSV", command=self.load_csv).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(toolbar, text="2  打开 Gamma XML", command=self.load_xml).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Label(toolbar, text="CCT", style="GammaCard.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.cct_var = tk.StringVar(value="6500")
        ttk.Entry(toolbar, textvariable=self.cct_var, width=8).grid(row=0, column=3, sticky="w", padx=(0, 6))
        self.region_match_button = ttk.Button(
            toolbar,
            text="自动匹配 Region",
            command=self.auto_match_region,
            style="RegionMatch.TButton",
        )
        self.region_match_button.grid(row=0, column=4, sticky="ew", padx=(0, 6))
        self.region_var = tk.StringVar()
        self.region_combo = ttk.Combobox(toolbar, textvariable=self.region_var, state="readonly", width=10)
        self.region_combo.grid(row=0, column=5, sticky="ew", padx=(0, 6))
        self.region_combo.bind("<<ComboboxSelected>>", self._on_region_selected)
        self.optimize_button = ttk.Button(toolbar, text="3  自动优化", command=self.run_optimization, style="Primary.TButton")
        self.optimize_button.grid(row=0, column=6, sticky="ew", padx=(0, 6))
        self.save_button = ttk.Button(toolbar, text="保存 XML", command=self.save_xml, state="disabled")
        self.save_button.grid(row=0, column=7, sticky="ew")

        settings = ttk.Frame(outer, padding=(10, 8), style="GammaCard.TFrame")
        self.settings_panel = settings
        settings.pack(fill="x", pady=(0, 8))
        config = self.settings
        ttk.Label(settings, text="Gamma", style="GammaCard.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.target_gamma_var = tk.StringVar(value=f"{config.target_gamma:g}")
        ttk.Entry(settings, textvariable=self.target_gamma_var, width=8).grid(row=0, column=1, sticky="w", padx=(0, 10))
        ttk.Label(settings, text="目标阶数", style="GammaCard.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 5))
        self.target_steps_var = tk.StringVar(value="自动" if config.target_step_count is None else str(config.target_step_count))
        ttk.Entry(settings, textvariable=self.target_steps_var, width=8).grid(row=0, column=3, sticky="w", padx=(0, 10))
        ttk.Label(settings, text="调整强度", style="GammaCard.TLabel").grid(row=0, column=4, sticky="w", padx=(0, 5))
        self.strength_var = tk.DoubleVar(value=config.maximum_adjustment * 100.0)
        ttk.Scale(settings, from_=0, to=100, variable=self.strength_var, orient="horizontal", length=105).grid(row=0, column=5, sticky="w", padx=(0, 10))
        ttk.Label(settings, text="高光保护", style="GammaCard.TLabel").grid(row=0, column=6, sticky="w", padx=(0, 5))
        self.highlight_var = tk.DoubleVar(value=config.highlight_protection * 100.0)
        ttk.Scale(settings, from_=0, to=100, variable=self.highlight_var, orient="horizontal", length=90).grid(row=0, column=7, sticky="w", padx=(0, 10))
        ttk.Label(settings, text="暗部保护", style="GammaCard.TLabel").grid(row=0, column=8, sticky="w", padx=(0, 5))
        self.shadow_var = tk.DoubleVar(value=config.shadow_protection * 100.0)
        ttk.Scale(settings, from_=0, to=100, variable=self.shadow_var, orient="horizontal", length=90).grid(row=0, column=9, sticky="w")
        secondary = ttk.Frame(settings, style="GammaSurface.TFrame")
        secondary.grid(row=1, column=0, columnspan=10, sticky="ew", pady=(5, 0))
        rgb_label = next((label for label, value in self.RGB_LABELS.items() if value == config.rgb_mode), next(iter(self.RGB_LABELS)))
        ttk.Label(secondary, text="RGB", style="GammaCard.TLabel").pack(side="left", padx=(0, 5))
        self.rgb_mode_var = tk.StringVar(value=rgb_label)
        ttk.Combobox(secondary, textvariable=self.rgb_mode_var, values=list(self.RGB_LABELS), state="readonly", width=17).pack(side="left", padx=(0, 10))
        range_label = next((label for label, value in self.RANGE_LABELS.items() if value == config.range_mode), next(iter(self.RANGE_LABELS)))
        ttk.Label(secondary, text="灰阶范围", style="GammaCard.TLabel").pack(side="left", padx=(0, 5))
        self.range_mode_var = tk.StringVar(value=range_label)
        range_combo = ttk.Combobox(secondary, textvariable=self.range_mode_var, values=list(self.RANGE_LABELS), state="readonly", width=21)
        range_combo.pack(side="left", padx=(0, 10))
        range_combo.bind("<<ComboboxSelected>>", lambda _event: self._range_changed())
        ttk.Label(secondary, text="阈值 / Pixel", style="GammaCard.TLabel").pack(side="left", padx=(0, 5))
        self.threshold_var = tk.StringVar(value=f"{config.threshold:g}")
        threshold_entry = ttk.Entry(secondary, textvariable=self.threshold_var, width=9)
        threshold_entry.pack(side="left", padx=(0, 10))
        threshold_entry.bind("<Return>", lambda _event: self.reanalyze())
        threshold_entry.bind("<FocusOut>", lambda _event: self.reanalyze(quiet=True))
        ttk.Label(secondary, text="手动 Zone", style="GammaCard.TLabel").pack(side="left", padx=(0, 5))
        manual = ttk.Frame(secondary, style="GammaSurface.TFrame")
        manual.pack(side="left")
        self.manual_start_var = tk.StringVar(value=str(config.manual_start_zone or 1))
        self.manual_end_var = tk.StringVar(value=str(config.manual_end_zone or 12))
        self.manual_start_entry = ttk.Entry(manual, textvariable=self.manual_start_var, width=5, state="disabled")
        self.manual_start_entry.pack(side="left")
        ttk.Label(manual, text=" – ", style="GammaCard.TLabel").pack(side="left")
        self.manual_end_entry = ttk.Entry(manual, textvariable=self.manual_end_var, width=5, state="disabled")
        self.manual_end_entry.pack(side="left")
        settings.columnconfigure(9, weight=1)

        self.info_var = tk.StringVar(value="请打开 Gamma CSV 与 Gamma XML。")
        self.info_label = ttk.Label(outer, textvariable=self.info_var, style="GammaMuted.TLabel")
        self.info_label.pack(fill="x", pady=(0, 3))
        bind_responsive_wrap(self.info_label)
        self.status_var = tk.StringVar(value="等待数据")
        self.status_label = ttk.Label(
            outer,
            textvariable=self.status_var,
            foreground=BLUE,
            background=INFO_BG,
            padding=(10, 6),
        )
        self.status_label.pack(fill="x", pady=(0, 8))
        bind_responsive_wrap(self.status_label)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.curve_tab = ttk.Frame(self.notebook, padding=8, style="GammaRoot.TFrame")
        self.engineering_tab = ttk.Frame(self.notebook, padding=8, style="GammaRoot.TFrame")
        self.diagnosis_tab = ttk.Frame(self.notebook, padding=8, style="GammaRoot.TFrame")
        self.history_tab = ttk.Frame(self.notebook, padding=8, style="GammaRoot.TFrame")
        self.notebook.add(self.curve_tab, text="曲线对比")
        self.notebook.add(self.engineering_tab, text="工程统计")
        self.notebook.add(self.diagnosis_tab, text="诊断与解释")
        self.notebook.add(self.history_tab, text="History / XML Diff")

        self.curve_tab.columnconfigure(0, weight=1)
        self.curve_tab.rowconfigure(1, weight=5, minsize=230)
        self.curve_tab.rowconfigure(2, weight=4, minsize=250)
        kpis = ttk.Frame(self.curve_tab, style="GammaRoot.TFrame")
        kpis.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        for column in range(8):
            kpis.columnconfigure(column, weight=1, uniform="gamma-kpi")
        captions = ("原始 Zone", "连续阶数", "目标阶数", "Global Gamma", "RMSE", "RGB 偏差", "LUT 格式", "曲线健康")
        self.kpi_vars: list[tk.StringVar] = []
        for column, caption in enumerate(captions):
            card = ttk.Frame(kpis, padding=(6, 4), style="GammaCard.TFrame")
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 5 if column < 7 else 0))
            value = tk.StringVar(value="—")
            self.kpi_vars.append(value)
            ttk.Label(card, textvariable=value, style="GammaKpi.TLabel").pack(anchor="w")
            ttk.Label(card, text=caption, style="GammaCard.TLabel", foreground=MUTED).pack(anchor="w")

        charts = ttk.Frame(self.curve_tab, style="GammaRoot.TFrame")
        self.charts_panel = charts
        charts.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        for column in range(3):
            charts.columnconfigure(column, weight=1, uniform="gamma-chart")
        charts.rowconfigure(0, weight=1)
        self.response_plot = CurvePlot(charts, "灰阶响应：Before / Target / After", "-Log Exposure", "Density")
        self.response_plot.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.local_plot = CurvePlot(charts, "Local Gamma", "Zone", "Gamma")
        self.local_plot.grid(row=0, column=1, sticky="nsew", padx=5)
        self.lut_plot = CurvePlot(
            charts,
            "Gamma LUT",
            "LUT Index",
            "XML Value",
            show_markers=False,
        )
        self.lut_plot.grid(row=0, column=2, sticky="nsew", padx=(5, 0))

        details = ttk.Panedwindow(self.curve_tab, orient="horizontal")
        self.details_panel = details
        details.grid(row=2, column=0, sticky="nsew")
        pair_panel = ttk.Frame(details, padding=6, style="GammaCard.TFrame", width=410)
        zone_panel = ttk.Frame(details, padding=6, style="GammaCard.TFrame")
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
        zone_frame = ttk.Frame(zone_panel, style="GammaSurface.TFrame")
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
        self.zone_tree.tag_configure("fit", background="#EDF8F0")
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
            background="#1C1C1E",
            foreground="#F5F5F7",
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
            title="打开 Gamma CSV",
            initialdir=str(default_sources_directory()),
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            self.dataset = parse_gray_csv(selected)
        except (OSError, GrayCSVError) as exc:
            messagebox.showerror("Gamma CSV 读取失败", str(exc), parent=self.root)
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
            initialdir=str(default_sources_directory()),
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
            f"CCT {cct:g}K {'精确命中' if mode == 'exact' else '选择最近'} Gamma Region #{region.index}。"
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
            messagebox.showinfo("资料未齐", "请打开 Gamma CSV、Gamma XML 并选择 Region。", parent=self.root)
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
        self.save_button.configure(state="normal" if self._result_is_writable() else "disabled")
        self._render_result()
        self._render_history()

    def _render_raw_charts(self) -> None:
        if self.dataset is not None:
            response = [(-zone.log_exposure, zone.density) for zone in self.dataset.zones]
            local = [(zone.zone, zone.slope) for zone in self.dataset.zones if zone.slope is not None]
            self.response_plot.set_series((("Before", response, BLUE, False),))
            self.local_plot.set_series((("Before", local, BLUE, False),))
        if self.selected_region is not None:
            before = list(enumerate(self.selected_region.channel_g))
            self._update_lut_plot_title((self.selected_region.channel_g,), "Before")
            self.lut_plot.set_series((("Before G", before, BLUE, False),))

    def _update_lut_plot_title(
        self,
        curves: Sequence[Sequence[float]],
        label: str,
        *,
        shape_status: Optional[str] = None,
        natural_status: Optional[str] = None,
    ) -> None:
        steps = [
            following - current
            for curve in curves
            for current, following in zip(curve, curve[1:])
        ]
        if not steps:
            self.lut_plot.title = "Gamma LUT"
            return
        plateaus = sum(step <= 0 for step in steps)
        quality = ""
        if shape_status is not None:
            quality += f" · 形状保持 {shape_status}"
        if natural_status is not None:
            quality += f" · 自然平滑 {natural_status}"
        self.lut_plot.title = (
            f"Gamma LUT · {label} Δ {min(steps):g}–{max(steps):g} · "
            f"平台 {plateaus}{quality}"
        )

    def _render_result(self) -> None:
        assert self.result is not None and self.dataset is not None and self.analysis is not None
        result = self.result
        metrics = result.metrics
        lut_changed = any(
            before != after
            for before_curve, after_curve in (
                (result.before_r, result.after_r),
                (result.before_g, result.after_g),
                (result.before_b, result.after_b),
            )
            for before, after in zip(before_curve, after_curve)
        )
        safe_after_generated = result.applied_strength > 0.0 and lut_changed
        self.kpi_vars[1].set(f"{metrics.distinguishable_before} → {metrics.distinguishable_after}")
        self.kpi_vars[2].set(
            str(metrics.distinguishable_target)
            if result.requested_step_count == metrics.distinguishable_target
            else f"{result.requested_step_count} → 安全 {metrics.distinguishable_target}"
        )
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
        response_series = [
            ("Before", response_before, BLUE, False),
            ("Target", response_target, AMBER, True),
        ]
        if safe_after_generated:
            response_series.append(("After", response_after, GREEN, False))
        self.response_plot.set_series(tuple(response_series))
        local_before = [(item.zone, item.local_gamma_before) for item in result.zone_results if item.local_gamma_before is not None]
        local_target = [(item.zone, item.local_gamma_target) for item in result.zone_results if item.local_gamma_target is not None]
        local_after = [(item.zone, item.local_gamma_after) for item in result.zone_results if item.local_gamma_after is not None]
        local_series = [
            ("Before", local_before, BLUE, False),
            ("Target", local_target, AMBER, True),
        ]
        if safe_after_generated:
            local_series.append(("After", local_after, GREEN, False))
        self.local_plot.set_series(tuple(local_series))
        indexes = range(len(result.before_g))
        shape_check = next(
            (
                check
                for check in result.health.checks
                if check.name == "LUT Shape Preservation"
            ),
            None,
        )
        natural_check = next(
            (
                check
                for check in result.health.checks
                if check.name == "LUT Natural Smoothness"
            ),
            None,
        )
        if safe_after_generated:
            self._update_lut_plot_title(
                (result.after_r, result.after_g, result.after_b),
                "After RGB",
                shape_status=(None if shape_check is None else shape_check.status),
                natural_status=(None if natural_check is None else natural_check.status),
            )
            self.lut_plot.set_series(
                (
                    ("Before G", [(index, result.before_g[index]) for index in indexes], BLUE, False),
                    ("After G", [(index, result.after_g[index]) for index in indexes], GREEN, False),
                    ("After R/B", [(index, (result.after_r[index] + result.after_b[index]) / 2.0) for index in indexes], PURPLE, True),
                )
            )
        else:
            self._update_lut_plot_title(
                (result.before_r, result.before_g, result.before_b),
                "未生成安全 After · 已保留原 LUT",
            )
            self.lut_plot.set_series(
                (("Before G", [(index, result.before_g[index]) for index in indexes], BLUE, False),)
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
        if safe_after_generated:
            target_summary = (
                f"目标 {metrics.distinguishable_target}"
                if result.requested_step_count == metrics.distinguishable_target
                else (
                    f"请求 {result.requested_step_count}，"
                    f"最高安全 {metrics.distinguishable_target}"
                )
            )
            self.status_var.set(
                f"Gamma 优化完成：连续可识别 {metrics.distinguishable_before}→{metrics.distinguishable_after} 阶，{target_summary}；"
                f"RMSE {metrics.rmse_before:.5f}→{metrics.rmse_after:.5f}；Curve Health={result.health.status}；"
                f"提亮系数={result.target_gamma_factor:.3f}，实际强度={result.applied_strength:.0%}，LUT={result.lut_length}点/0–{result.maximum_value}。"
            )
        else:
            self.status_var.set(
                f"目标 {metrics.distinguishable_target} 阶未通过形状/平滑门禁；未生成或显示不安全 After，"
                f"已保留原 LUT 和 {metrics.distinguishable_before} 阶 Before，写回已禁用。"
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
        required_pairs = [pair for pair in self.result.pair_results if pair.target_required]
        required_gap = self._minimum_continuity_gap()
        continuity_ok = (
            len(required_pairs) == self.result.metrics.distinguishable_target
            and all(pair.delta_after >= required_gap for pair in required_pairs)
            and self.result.metrics.distinguishable_after == self.result.metrics.distinguishable_target
        )
        minimum_gap = min((pair.delta_after for pair in required_pairs), default=0.0)
        self.engineering_tree.insert(
            "",
            "end",
            iid="gamma-check-continuity",
            values=(
                "Gray Step Continuity",
                "PASS" if continuity_ok else "FAIL",
                f"continuous={self.result.metrics.distinguishable_after}; min ΔPixel={minimum_gap:.2f}",
                f"{self.result.metrics.distinguishable_target} contiguous; every ΔPixel≥{required_gap:g}",
                "目标灰阶必须属于同一连续区间，不累计断点后的孤立灰阶。",
            ),
            tags=("PASS" if continuity_ok else "FAIL",),
        )
        metrics = self.result.metrics
        loss_before = self.result.loss_before
        loss_after = self.result.loss_after
        lines = [
            f"Curve Health: {self.result.health.status}",
            f"LUT: {self.result.lut_length} points · integer 0–{self.result.maximum_value}",
            f"Gamma lift factor: {self.result.target_gamma_factor:.3f} (1.0 nominal; larger is brighter)",
            f"Recognizable steps: Before {metrics.distinguishable_before} · Requested {self.result.requested_step_count} · Safe target {metrics.distinguishable_target} · After {metrics.distinguishable_after}",
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
        if not self._result_is_writable():
            messagebox.showerror(
                "Gamma 工程门禁未通过",
                "目标连续灰阶、Curve Health 或有效 LUT 变更未通过，禁止写回 Gamma XML。",
                parent=self.root,
            )
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

    def _result_is_writable(self) -> bool:
        if self.result is None or self.result.health.status == "FAIL":
            return False
        required_pairs = [pair for pair in self.result.pair_results if pair.target_required]
        required_gap = self._minimum_continuity_gap()
        natural_check = next(
            (
                check
                for check in self.result.health.checks
                if check.name == "LUT Natural Smoothness"
            ),
            None,
        )
        shape_check = next(
            (
                check
                for check in self.result.health.checks
                if check.name == "LUT Shape Preservation"
            ),
            None,
        )
        return (
            self.result.applied_strength > 0.0
            and shape_check is not None
            and shape_check.status == "PASS"
            and natural_check is not None
            and natural_check.status == "PASS"
            and self.result.metrics.distinguishable_after == self.result.metrics.distinguishable_target
            and len(required_pairs) == self.result.metrics.distinguishable_target
            and all(pair.delta_after >= required_gap for pair in required_pairs)
            and any(
                before != after
                for before_curve, after_curve in (
                    (self.result.before_r, self.result.after_r),
                    (self.result.before_g, self.result.after_g),
                    (self.result.before_b, self.result.after_b),
                )
                for before, after in zip(before_curve, after_curve)
            )
        )

    def _minimum_continuity_gap(self) -> float:
        return minimum_continuity_gap(self.settings.threshold)


def open_gamma_window(master: tk.Misc) -> GammaWorkspace:
    window = tk.Toplevel(master)
    application = GammaWorkspace(window)
    setattr(window, "_tunelab_gamma_app", application)
    return application


def main() -> None:
    root = tk.Tk()
    GammaWorkspace(root)
    update_controller_for(root).schedule_startup_check()
    root.mainloop()


if __name__ == "__main__":
    main()
