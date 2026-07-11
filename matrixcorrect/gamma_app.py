from __future__ import annotations

import math
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable, Optional, Sequence

from .gamma_models import (
    GammaOptimizationConfig,
    GammaOptimizationResult,
    GammaRegion,
    GrayDataset,
    GrayRangeAnalysis,
)
from .gamma_optimizer import GammaOptimizationError, optimize_gamma_lut
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
        canvas.create_text(14, 15, text=self.title, anchor="w", fill=INK, font=("TkDefaultFont", 11, "bold"))
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
            canvas.create_text(x_coordinate, bottom + 16, text=f"{x_value:.2f}", fill=MUTED, font=("TkDefaultFont", 8))
            canvas.create_text(left - 7, y_coordinate, text=f"{y_value:.3f}", anchor="e", fill=MUTED, font=("TkDefaultFont", 8))
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
            canvas.create_text(legend_x + 24, 29, text=name, anchor="w", fill=INK, font=("TkDefaultFont", 8))
            legend_x += 28 + max(52, len(name) * 8)


class GammaOptimizationApp:
    RANGE_LABELS = {"自动识别": "auto", "全部灰阶（仍执行工程排除）": "all", "手动指定": "manual"}
    RGB_LABELS = {"RGB 联动（推荐）": "linked", "R/G/B 独立（高级）": "independent"}

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.dataset: Optional[GrayDataset] = None
        self.analysis: Optional[GrayRangeAnalysis] = None
        self.document: Optional[QualcommGammaDocument] = None
        self.selected_region: Optional[GammaRegion] = None
        self.result: Optional[GammaOptimizationResult] = None
        self.region_display_to_index: dict[str, int] = {}
        self._configure_styles()
        self._build_ui()

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            # Aqua follows macOS dark appearance and otherwise supplies white
            # Treeview text, which clashes with the explicit light engineering
            # cards/tags used by this cross-platform tool.
            style.theme_use("clam")
        style.configure("GammaRoot.TFrame", background=BG)
        style.configure("GammaCard.TFrame", background=PANEL)
        style.configure("GammaCard.TLabel", background=PANEL, foreground=INK)
        style.configure("GammaTitle.TLabel", background=BG, foreground=INK, font=("TkDefaultFont", 18, "bold"))
        style.configure("GammaMuted.TLabel", background=BG, foreground=MUTED)
        style.configure("GammaKpi.TLabel", background=PANEL, foreground=INK, font=("TkDefaultFont", 12, "bold"))
        style.configure("GammaPrimary.TButton", background=BLUE, foreground="white", padding=(14, 8))
        style.configure(
            "Gamma.Treeview",
            rowheight=25,
            background=PANEL,
            fieldbackground=PANEL,
            foreground=INK,
        )
        style.configure(
            "Gamma.Treeview.Heading",
            background="#EAECF0",
            foreground=INK,
            font=("TkDefaultFont", 9, "bold"),
        )

    def _build_ui(self) -> None:
        self.root.title("MatrixCorrect · Qualcomm Gamma 1.5 LUT 优化")
        try:
            self.root.geometry("1540x980")
            self.root.minsize(1180, 760)
        except tk.TclError:
            pass
        outer = ttk.Frame(self.root, padding=(18, 14), style="GammaRoot.TFrame")
        outer.pack(fill="both", expand=True)
        title = ttk.Frame(outer, style="GammaRoot.TFrame")
        title.pack(fill="x", pady=(0, 10))
        ttk.Label(title, text="Gamma 优化", style="GammaTitle.TLabel").pack(side="left")
        ttk.Label(title, text="Imatest Stepchart · Qualcomm Gamma15 257-point LUT", style="GammaMuted.TLabel").pack(side="left", padx=(12, 0), pady=(5, 0))

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
            ("目标 Gamma", 0), ("最大调整强度", 1), ("高光保护", 2), ("暗部保护", 3),
            ("RGB 模式", 4), ("灰阶范围", 5), ("识别阈值 / Pixel", 6), ("手动起止 Zone", 7),
        )
        for text, column in labels:
            ttk.Label(settings, text=text, style="GammaCard.TLabel").grid(row=0, column=column, sticky="w", padx=(0, 12))
        self.target_gamma_var = tk.StringVar(value="0.45")
        ttk.Entry(settings, textvariable=self.target_gamma_var, width=9).grid(row=1, column=0, sticky="w", padx=(0, 12))
        self.strength_var = tk.DoubleVar(value=70.0)
        ttk.Scale(settings, from_=0, to=100, variable=self.strength_var, orient="horizontal", length=115).grid(row=1, column=1, sticky="w", padx=(0, 12))
        self.highlight_var = tk.DoubleVar(value=75.0)
        ttk.Scale(settings, from_=0, to=100, variable=self.highlight_var, orient="horizontal", length=105).grid(row=1, column=2, sticky="w", padx=(0, 12))
        self.shadow_var = tk.DoubleVar(value=75.0)
        ttk.Scale(settings, from_=0, to=100, variable=self.shadow_var, orient="horizontal", length=105).grid(row=1, column=3, sticky="w", padx=(0, 12))
        self.rgb_mode_var = tk.StringVar(value=next(iter(self.RGB_LABELS)))
        ttk.Combobox(settings, textvariable=self.rgb_mode_var, values=list(self.RGB_LABELS), state="readonly", width=18).grid(row=1, column=4, sticky="w", padx=(0, 12))
        self.range_mode_var = tk.StringVar(value=next(iter(self.RANGE_LABELS)))
        range_combo = ttk.Combobox(settings, textvariable=self.range_mode_var, values=list(self.RANGE_LABELS), state="readonly", width=22)
        range_combo.grid(row=1, column=5, sticky="w", padx=(0, 12))
        range_combo.bind("<<ComboboxSelected>>", lambda _event: self._range_changed())
        self.threshold_var = tk.StringVar(value="8")
        threshold_entry = ttk.Entry(settings, textvariable=self.threshold_var, width=9)
        threshold_entry.grid(row=1, column=6, sticky="w", padx=(0, 12))
        threshold_entry.bind("<Return>", lambda _event: self.reanalyze())
        threshold_entry.bind("<FocusOut>", lambda _event: self.reanalyze(quiet=True))
        manual = ttk.Frame(settings, style="GammaCard.TFrame")
        manual.grid(row=1, column=7, sticky="w")
        self.manual_start_var = tk.StringVar(value="1")
        self.manual_end_var = tk.StringVar(value="12")
        self.manual_start_entry = ttk.Entry(manual, textvariable=self.manual_start_var, width=5, state="disabled")
        self.manual_start_entry.pack(side="left")
        ttk.Label(manual, text=" – ", style="GammaCard.TLabel").pack(side="left")
        self.manual_end_entry = ttk.Entry(manual, textvariable=self.manual_end_var, width=5, state="disabled")
        self.manual_end_entry.pack(side="left")
        settings.columnconfigure(8, weight=1)

        self.info_var = tk.StringVar(value="请打开 Gray CSV 与 Gamma XML。")
        ttk.Label(outer, textvariable=self.info_var, style="GammaMuted.TLabel").pack(fill="x", pady=(0, 6))
        self.status_var = tk.StringVar(value="等待数据")
        ttk.Label(outer, textvariable=self.status_var, foreground=BLUE, background="#EAF0FF", padding=(9, 6)).pack(fill="x", pady=(0, 8))

        kpis = ttk.Frame(outer, style="GammaRoot.TFrame")
        kpis.pack(fill="x", pady=(0, 8))
        for column in range(8):
            kpis.columnconfigure(column, weight=1, uniform="gamma-kpi")
        captions = ("原始 Zone", "连续有效", "有效范围", "Global Gamma", "RMSE", "Max Error", "RGB 偏差", "Curve Health")
        self.kpi_vars: list[tk.StringVar] = []
        for column, caption in enumerate(captions):
            card = ttk.Frame(kpis, padding=(9, 7), style="GammaCard.TFrame")
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 5 if column < 7 else 0))
            value = tk.StringVar(value="—")
            self.kpi_vars.append(value)
            ttk.Label(card, textvariable=value, style="GammaKpi.TLabel").pack(anchor="w")
            ttk.Label(card, text=caption, style="GammaCard.TLabel", foreground=MUTED).pack(anchor="w")

        charts = ttk.Frame(outer, style="GammaRoot.TFrame")
        charts.pack(fill="both", expand=True, pady=(0, 8))
        for column in range(3):
            charts.columnconfigure(column, weight=1, uniform="gamma-chart")
        charts.rowconfigure(0, weight=1)
        self.response_plot = CurvePlot(charts, "灰阶响应：Before / Target / After", "-Log Exposure", "Density")
        self.response_plot.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.local_plot = CurvePlot(charts, "Local Gamma", "Zone", "Gamma")
        self.local_plot.grid(row=0, column=1, sticky="nsew", padx=5)
        self.lut_plot = CurvePlot(charts, "257 点 Gamma LUT", "LUT Index", "XML Value")
        self.lut_plot.grid(row=0, column=2, sticky="nsew", padx=(5, 0))

        details = ttk.Panedwindow(outer, orient="horizontal")
        details.pack(fill="both", expand=True)
        pair_panel = ttk.Frame(details, padding=7, style="GammaCard.TFrame", width=410)
        zone_panel = ttk.Frame(details, padding=7, style="GammaCard.TFrame")
        details.add(pair_panel, weight=1)
        details.add(zone_panel, weight=3)
        ttk.Label(pair_panel, text="相邻灰阶可区分性", style="GammaCard.TLabel").pack(anchor="w", pady=(0, 5))
        self.pair_tree = ttk.Treeview(
            pair_panel,
            columns=("pair", "delta", "status"),
            show="headings",
            height=8,
            style="Gamma.Treeview",
        )
        for column, title, width in (("pair", "Zone i→i+1", 120), ("delta", "ΔPixel", 90), ("status", "识别", 120)):
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
            self.zone_tree.column(column, width=width, minwidth=width, stretch=False, anchor="center")
        zone_v = ttk.Scrollbar(zone_frame, orient="vertical", command=self.zone_tree.yview)
        zone_h = ttk.Scrollbar(zone_frame, orient="horizontal", command=self.zone_tree.xview)
        self.zone_tree.configure(yscrollcommand=zone_v.set, xscrollcommand=zone_h.set)
        self.zone_tree.grid(row=0, column=0, sticky="nsew")
        zone_v.grid(row=0, column=1, sticky="ns")
        zone_h.grid(row=1, column=0, sticky="ew")
        zone_frame.columnconfigure(0, weight=1)
        zone_frame.rowconfigure(0, weight=1)
        self.zone_tree.tag_configure("fit", background="#ECFDF3")
        self.zone_tree.tag_configure("excluded", foreground=MUTED)

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
                values=(f"{pair.from_zone} → {pair.to_zone}", f"{pair.delta_pixel:.1f}", "可区分" if pair.distinguishable else "不可区分"),
                tags=("valid" if pair.distinguishable else "invalid",),
            )
        self.kpi_vars[0].set(str(len(self.dataset.zones)))
        self.kpi_vars[1].set(str(analysis.effective_count))
        self.kpi_vars[2].set(f"Zone {analysis.start_zone}–{analysis.end_zone}" if analysis.selected_zones else "无")
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
        self._render_raw_charts()
        self.info_var.set(
            (self.info_var.get().split(" · XML：", 1)[0])
            + f" · XML：{self.document.source_path.name} · Region #{index} · "
            f"{self.selected_region.length} 点 / 0–{self.selected_region.maximum}"
        )

    def _config(self) -> GammaOptimizationConfig:
        mode = self.RANGE_LABELS[self.range_mode_var.get()]
        config = GammaOptimizationConfig(
            target_gamma=float(self.target_gamma_var.get()),
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
        except (ValueError, GammaOptimizationError) as exc:
            messagebox.showerror("Gamma 优化失败", str(exc), parent=self.root)
            return
        self.save_button.configure(state="normal" if self.result.health.status != "FAIL" else "disabled")
        self._render_result()

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
        self.kpi_vars[3].set(f"{metrics.global_gamma_before:.3f}→{metrics.global_gamma_after:.3f}")
        self.kpi_vars[4].set(f"{metrics.rmse_before:.4f}→{metrics.rmse_after:.4f}")
        self.kpi_vars[5].set(f"{metrics.maximum_error_before:.4f}→{metrics.maximum_error_after:.4f}")
        self.kpi_vars[6].set(f"{metrics.rgb_gray_deviation_before:.4f}→{metrics.rgb_gray_deviation_after:.4f}")
        self.kpi_vars[7].set(result.health.status)
        response_before = [(-next(zone.log_exposure for zone in self.dataset.zones if zone.zone == item.zone), item.density_before) for item in result.zone_results]
        response_target = [(-next(zone.log_exposure for zone in self.dataset.zones if zone.zone == item.zone), item.density_target) for item in result.zone_results]
        response_after = [(-next(zone.log_exposure for zone in self.dataset.zones if zone.zone == item.zone), item.density_after) for item in result.zone_results]
        self.response_plot.set_series(
            (("Before", response_before, BLUE, False), ("Target", response_target, AMBER, True), ("After", response_after, GREEN, False))
        )
        local_before = [(item.zone, item.local_gamma_before) for item in result.zone_results if item.local_gamma_before is not None]
        local_after = [(item.zone, item.local_gamma_after) for item in result.zone_results if item.local_gamma_after is not None]
        target = [(item.zone, float(self.target_gamma_var.get())) for item in result.zone_results if item.local_gamma_before is not None]
        self.local_plot.set_series(
            (("Before", local_before, BLUE, False), ("Target", target, AMBER, True), ("After", local_after, GREEN, False))
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
        for item in self.zone_tree.get_children():
            self.zone_tree.delete(item)
        for item in result.zone_results:
            improve = "N/A" if item.improvement_percent is None else f"{item.improvement_percent:+.1f}%"
            self.zone_tree.insert(
                "", "end",
                values=(
                    item.zone, "参与" if item.used else "排除", f"{item.pixel_before:.1f}", f"{item.pixel_target:.1f}",
                    f"{item.pixel_after:.1f}", f"{item.error_before:+.4f}", f"{item.error_after:+.4f}", improve,
                    "—" if item.local_gamma_before is None else f"{item.local_gamma_before:.3f}",
                    "—" if item.local_gamma_after is None else f"{item.local_gamma_after:.3f}",
                ),
                tags=("fit" if item.used else "excluded",),
            )
        self.status_var.set(
            f"Gamma 优化完成：连续有效 {len(result.selected_zones)} 阶 Zone {result.selected_zones[0]}–{result.selected_zones[-1]}；"
            f"RMSE {metrics.rmse_before:.5f}→{metrics.rmse_after:.5f}；Curve Health={result.health.status}；"
            f"实际强度={result.applied_strength:.0%}。"
        )

    def save_xml(self) -> None:
        if self.document is None or self.result is None or self.selected_region is None:
            messagebox.showinfo("尚无结果", "请先完成 Gamma 优化。", parent=self.root)
            return
        if self.result.health.status == "FAIL":
            messagebox.showerror("Curve Health 未通过", "Curve Health=FAIL，禁止写回 Gamma XML。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            title="保存优化后的 Qualcomm Gamma XML",
            defaultextension=".xml",
            initialdir=str(self.document.source_path.parent),
            initialfile=f"{self.document.source_path.stem}_optimized.xml",
            filetypes=[("XML 文件", "*.xml")],
            confirmoverwrite=True,
            parent=self.root,
        )
        if not path:
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
        self.status_var.set(f"已另存并回读校验：{path}；只修改 Gamma region #{self.selected_region.index}。")


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
