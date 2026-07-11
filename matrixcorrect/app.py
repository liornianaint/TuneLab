from __future__ import annotations

import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

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


class MatrixPanel(ttk.Frame):
    def __init__(self, master: tk.Misc, title: str) -> None:
        super().__init__(master, padding=12, style="Card.TFrame")
        self.title = title
        self.variables = [[tk.StringVar(value="—") for _ in range(3)] for _ in range(3)]
        header = ttk.Frame(self, style="Card.TFrame")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="复制", command=self.copy, style="Quiet.TButton").grid(row=0, column=1)
        for row in range(3):
            for col in range(3):
                ttk.Label(
                    self,
                    textvariable=self.variables[row][col],
                    width=12,
                    anchor="e",
                    style="Matrix.TLabel",
                ).grid(row=row + 1, column=col, padx=3, pady=3, sticky="ew")
                self.columnconfigure(col, weight=1)

    def set_matrix(self, matrix: Optional[Matrix3]) -> None:
        for row in range(3):
            for col in range(3):
                self.variables[row][col].set("—" if matrix is None else f"{matrix[row][col]: .7f}")

    def copy(self) -> None:
        text = "\n".join(" ".join(self.variables[row][col].get() for col in range(3)) for row in range(3))
        self.clipboard_clear()
        self.clipboard_append(text)


class LabPlot(ttk.Frame):
    A_MIN = -100.0
    A_MAX = 100.0
    B_MIN = -100.0
    B_MAX = 100.0
    PLOT_WIDTH = 420
    PLOT_HEIGHT = 420
    LEFT = 52
    TOP = 38

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        background: tk.PhotoImage,
        patch_callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(master, style="Card.TFrame")
        self.title = title
        self.background = background
        self.patch_callback = patch_callback
        self.canvas = tk.Canvas(
            self,
            width=self.PLOT_WIDTH + self.LEFT + 22,
            height=self.PLOT_HEIGHT + self.TOP + 48,
            background=PANEL,
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.draw([], mode="before", show_motion=True)

    def _clamp_a(self, value: float) -> float:
        return max(self.A_MIN, min(self.A_MAX, value))

    def _clamp_b(self, value: float) -> float:
        return max(self.B_MIN, min(self.B_MAX, value))

    def _x(self, a_value: float) -> float:
        a_value = self._clamp_a(a_value)
        return self.LEFT + (a_value - self.A_MIN) / (self.A_MAX - self.A_MIN) * self.PLOT_WIDTH

    def _y(self, b_value: float) -> float:
        b_value = self._clamp_b(b_value)
        return self.TOP + (self.B_MAX - b_value) / (self.B_MAX - self.B_MIN) * self.PLOT_HEIGHT

    def draw(self, patch_results: list[PatchResult], *, mode: str, show_motion: bool) -> None:
        canvas = self.canvas
        canvas.delete("all")
        canvas.create_text(self.LEFT, 15, text=self.title, fill=INK, anchor="w", font=("TkDefaultFont", 12, "bold"))
        canvas.create_image(self.LEFT, self.TOP, image=self.background, anchor="nw")
        for value in range(-100, 101, 25):
            if self.A_MIN <= value <= self.A_MAX:
                x_pos = self._x(value)
                canvas.create_line(x_pos, self.TOP, x_pos, self.TOP + self.PLOT_HEIGHT, fill="#FFFFFF", stipple="gray50")
                canvas.create_text(x_pos, self.TOP + self.PLOT_HEIGHT + 18, text=str(value), fill=MUTED, font=("TkDefaultFont", 8))
            if self.B_MIN <= value <= self.B_MAX:
                y_pos = self._y(value)
                canvas.create_line(self.LEFT, y_pos, self.LEFT + self.PLOT_WIDTH, y_pos, fill="#FFFFFF", stipple="gray50")
                canvas.create_text(self.LEFT - 10, y_pos, text=str(value), fill=MUTED, anchor="e", font=("TkDefaultFont", 8))
        canvas.create_rectangle(
            self.LEFT,
            self.TOP,
            self.LEFT + self.PLOT_WIDTH,
            self.TOP + self.PLOT_HEIGHT,
            outline="#101828",
            width=1,
        )
        canvas.create_text(self.LEFT + self.PLOT_WIDTH / 2, self.TOP + self.PLOT_HEIGHT + 37, text="a*", fill=INK)
        # macOS system Python 3.9 can ship Tk 8.5, whose Canvas text item does
        # not support the newer -angle option.
        canvas.create_text(17, self.TOP + self.PLOT_HEIGHT / 2, text="b*", fill=INK)
        canvas.create_rectangle(self.LEFT + 9, self.TOP + 10, self.LEFT + 18, self.TOP + 19, fill="#FFFFFF", outline="#172033")
        canvas.create_text(self.LEFT + 25, self.TOP + 15, text="Ideal", fill=INK, anchor="w", font=("TkDefaultFont", 8))
        canvas.create_oval(self.LEFT + 75, self.TOP + 10, self.LEFT + 87, self.TOP + 22, fill="#FFFFFF", outline="#172033")
        canvas.create_text(self.LEFT + 94, self.TOP + 16, text="Camera", fill=INK, anchor="w", font=("TkDefaultFont", 8))
        for patch in patch_results:
            actual_lab = patch.before_lab if mode == "before" else patch.after_lab
            ideal_lab = patch.ideal_lab
            actual_x, actual_y = self._x(actual_lab[1]), self._y(actual_lab[2])
            ideal_x, ideal_y = self._x(ideal_lab[1]), self._y(ideal_lab[2])
            ideal_color = _rgb_hex(patch.ideal_srgb)
            actual_color = _rgb_hex(patch.before_srgb if mode == "before" else patch.after_srgb)
            if show_motion:
                if mode == "after":
                    before_x, before_y = self._x(patch.before_lab[1]), self._y(patch.before_lab[2])
                    canvas.create_line(
                        before_x, before_y, actual_x, actual_y,
                        fill=BLUE, width=1.4, arrow=tk.LAST, arrowshape=(7, 8, 3),
                    )
                canvas.create_line(ideal_x, ideal_y, actual_x, actual_y, fill="#667085", width=1)
            tag = f"patch-{patch.zone}"
            canvas.create_rectangle(
                ideal_x - 4, ideal_y - 4, ideal_x + 4, ideal_y + 4,
                fill=ideal_color, outline="#172033", tags=(tag,),
            )
            canvas.create_oval(
                actual_x - 6, actual_y - 6, actual_x + 6, actual_y + 6,
                fill=actual_color, outline="#172033", width=2 if patch.zone in (13, 14, 15) else 1,
                tags=(tag,),
            )
            label_x = min(self.LEFT + self.PLOT_WIDTH - 14, max(self.LEFT + 4, actual_x + 8))
            label_y = min(self.TOP + self.PLOT_HEIGHT - 5, max(self.TOP + 9, actual_y + 7))
            canvas.create_text(label_x, label_y, text=str(patch.zone), fill="#344054", anchor="w", font=("TkDefaultFont", 8), tags=(tag,))
            if self.patch_callback is not None:
                canvas.tag_bind(tag, "<Button-1>", lambda _event, zone=patch.zone: self.patch_callback(zone))
                canvas.tag_bind(tag, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
                canvas.tag_bind(tag, "<Leave>", lambda _event: canvas.configure(cursor=""))


def _rgb_hex(rgb: tuple[float, float, float]) -> str:
    values = [max(0, min(255, round(value * 255))) for value in rgb]
    return f"#{values[0]:02x}{values[1]:02x}{values[2]:02x}"


def _create_lab_background(master: tk.Misc) -> tk.PhotoImage:
    small_width = LabPlot.PLOT_WIDTH // 2
    small_height = LabPlot.PLOT_HEIGHT // 2
    image = tk.PhotoImage(master=master, width=small_width, height=small_height)
    for y_pos in range(small_height):
        b_value = LabPlot.B_MAX - y_pos / max(small_height - 1, 1) * (LabPlot.B_MAX - LabPlot.B_MIN)
        colors: list[str] = []
        for x_pos in range(small_width):
            a_value = LabPlot.A_MIN + x_pos / max(small_width - 1, 1) * (LabPlot.A_MAX - LabPlot.A_MIN)
            rgb = lab_to_srgb((70.0, a_value, b_value))
            colors.append(_rgb_hex(rgb))
        image.put("{" + " ".join(colors) + "}", to=(0, y_pos))
    return image.zoom(2, 2)


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

        root.title(APP_TITLE)
        root.geometry("1520x980")
        root.minsize(1180, 760)
        root.configure(background=BG)
        self._configure_styles()
        self._build_menu()
        self.lab_background = _create_lab_background(root)
        self._build_ui()
        self._set_status("请先打开 Imatest CSV 和 Qualcomm CC XML。")

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Root.TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, relief="flat")
        style.configure("Card.TLabel", background=PANEL, foreground=INK)
        style.configure("CardTitle.TLabel", background=PANEL, foreground=INK, font=("TkDefaultFont", 10, "bold"))
        style.configure("Title.TLabel", background=BG, foreground=INK, font=("TkDefaultFont", 20, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED)
        style.configure("Status.TLabel", background="#EAF0FF", foreground="#1D4ED8", padding=(10, 7))
        style.configure("Matrix.TLabel", background="#F8FAFC", foreground=INK, padding=(6, 5), font=("TkFixedFont", 10))
        style.configure("Primary.TButton", background=BLUE, foreground="white", padding=(14, 8), borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#1D4ED8"), ("disabled", "#98A2B3")])
        style.configure("Quiet.TButton", padding=(8, 4))
        style.configure("Kpi.TLabel", background=PANEL, foreground=INK, font=("TkDefaultFont", 13, "bold"))
        style.configure("KpiCaption.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Treeview", rowheight=27, fieldbackground=PANEL, background=PANEL, foreground=INK)
        style.configure("Treeview.Heading", background="#EAECF0", foreground=INK, font=("TkDefaultFont", 9, "bold"))

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="打开 Imatest CSV…", command=self.load_csv)
        file_menu.add_command(label="打开 Qualcomm CC XML…", command=self.load_xml)
        file_menu.add_separator()
        file_menu.add_command(label="保存改后 XML…", command=self.save_xml)
        file_menu.add_command(label="导出工程报告…", command=self.save_report)
        file_menu.add_command(label="保存当前参数", command=self.save_parameters)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.root.destroy)
        menu.add_cascade(label="文件", menu=file_menu)
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
        self.region_combo = ttk.Combobox(controls, textvariable=self.region_var, state="readonly", width=72)
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
            width=31,
        ).grid(row=1, column=6, sticky="w", padx=(0, 14))
        ttk.Label(controls, text="最大强度", style="Card.TLabel").grid(row=0, column=7, sticky="w")
        self.strength_var = tk.DoubleVar(value=self.settings.optimization.max_blend * 100.0)
        ttk.Scale(controls, from_=20, to=100, variable=self.strength_var, orient="horizontal", length=120).grid(row=1, column=7, sticky="w", padx=(0, 14))
        self.optimize_button = ttk.Button(controls, text="3  自动优化", command=self.run_optimization, style="Primary.TButton")
        self.optimize_button.grid(row=0, column=8, rowspan=2, sticky="ns")

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
        ttk.Checkbutton(parameters, text="Show Motion", variable=self.show_motion_var, command=self._redraw_plots).grid(row=1, column=6, sticky="w", padx=(0, 12))
        ttk.Button(parameters, text="保存参数", command=self.save_parameters, style="Quiet.TButton").grid(row=1, column=7, sticky="e")
        parameters.columnconfigure(7, weight=1)

        info = ttk.Frame(outer, style="Root.TFrame")
        info.pack(fill="x", pady=(0, 10))
        self.csv_label = ttk.Label(info, text="CSV：未加载", style="Subtitle.TLabel")
        self.csv_label.pack(side="left")
        self.xml_label = ttk.Label(info, text="XML：未加载", style="Subtitle.TLabel")
        self.xml_label.pack(side="left", padx=(22, 0))
        self.status_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel").pack(fill="x", pady=(0, 10))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        overview = ttk.Frame(self.notebook, padding=10, style="Root.TFrame")
        details = ttk.Frame(self.notebook, padding=10, style="Root.TFrame")
        engineering = ttk.Frame(self.notebook, padding=10, style="Root.TFrame")
        advice = ttk.Frame(self.notebook, padding=10, style="Root.TFrame")
        history_tab = ttk.Frame(self.notebook, padding=10, style="Root.TFrame")
        self.notebook.add(overview, text="  色差对比  ")
        self.notebook.add(details, text="  色块明细  ")
        self.notebook.add(engineering, text="  工程统计  ")
        self.notebook.add(advice, text="  诊断与解释  ")
        self.notebook.add(history_tab, text="  History / XML Diff  ")

        kpis = ttk.Frame(overview, style="Root.TFrame")
        kpis.pack(fill="x", pady=(0, 8))
        self.kpi_vars: list[tk.StringVar] = []
        for caption in ("平均 ΔE00（1-18）", "最大 ΔE00（1-18）", "平均改善", "改善 / 回退"):
            frame = ttk.Frame(kpis, padding=(16, 9), style="Card.TFrame")
            frame.pack(side="left", fill="x", expand=True, padx=(0, 8))
            value_var = tk.StringVar(value="—")
            self.kpi_vars.append(value_var)
            ttk.Label(frame, textvariable=value_var, style="Kpi.TLabel").pack(anchor="w")
            ttk.Label(frame, text=caption, style="KpiCaption.TLabel").pack(anchor="w")

        matrix_row = ttk.Frame(overview, style="Root.TFrame")
        matrix_row.pack(fill="x", pady=(0, 8))
        self.original_panel = MatrixPanel(matrix_row, "改前 CC 矩阵")
        self.original_panel.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.correction_panel = MatrixPanel(matrix_row, "Delta correction（行和=1）")
        self.correction_panel.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.optimized_panel = MatrixPanel(matrix_row, "改后 CC 矩阵")
        self.optimized_panel.pack(side="left", fill="x", expand=True)

        plot_container = ttk.Frame(overview, style="Root.TFrame")
        plot_container.pack(fill="both", expand=True)
        self.before_plot = LabPlot(plot_container, "改前：Camera → Ideal", self.lab_background, self._show_patch_detail)
        self.before_plot.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.after_plot = LabPlot(plot_container, "改后模拟：Camera → Ideal", self.lab_background, self._show_patch_detail)
        self.after_plot.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self.patch_detail_var = tk.StringVar(value="点击任一 Patch 可查看 Before → After → Ideal 的详细轨迹。")
        ttk.Label(overview, textvariable=self.patch_detail_var, style="Subtitle.TLabel").pack(fill="x", pady=(6, 0))

        detail_actions = ttk.Frame(details, style="Root.TFrame")
        detail_actions.pack(fill="x", pady=(0, 8))
        ttk.Label(detail_actions, text="改善百分比 = (改前 ΔE00 − 改后 ΔE00) / 改前 ΔE00", style="Subtitle.TLabel").pack(side="left")
        ttk.Button(detail_actions, text="导出工程报告", command=self.save_report).pack(side="right")
        columns = ("zone", "name", "category", "weight", "before", "after", "change", "dl", "dc", "dh", "regression", "status", "module")
        self.tree = ttk.Treeview(details, columns=columns, show="headings")
        headings = {
            "zone": "Zone",
            "name": "色块",
            "category": "分类",
            "weight": "权重",
            "before": "ΔE00 改前",
            "after": "ΔE00 改后",
            "change": "改善",
            "dl": "ΔL* Before→After",
            "dc": "ΔC* Before→After",
            "dh": "Δh° Before→After",
            "regression": "Regression",
            "status": "保护状态",
            "module": "建议模块",
        }
        widths = {"zone": 50, "name": 95, "category": 80, "weight": 60, "before": 85, "after": 85, "change": 75, "dl": 135, "dc": 135, "dh": 135, "regression": 85, "status": 85, "module": 210}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="center" if column != "module" else "w")
        self.tree.tag_configure("improved", foreground="#067647")
        self.tree.tag_configure("regressed", foreground="#B42318")
        self.tree.tag_configure("neutral", background="#F8FAFC")
        self.tree.tag_configure("focus", background="#EEF4FF")
        scrollbar = ttk.Scrollbar(details, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

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
            font=("TkDefaultFont", 10),
        )
        self.statistics_text.pack(fill="both", expand=True, pady=(8, 0))

        advice_header = ttk.Frame(advice, style="Root.TFrame")
        advice_header.pack(fill="x", pady=(0, 8))
        ttk.Label(advice_header, text="CCM 只能做全局线性修正；工具会把不适合交给 CC 的问题路由到对应模块。", style="Subtitle.TLabel").pack(side="left")
        ttk.Button(advice_header, text="保存改后 XML", command=self.save_xml, style="Primary.TButton").pack(side="right")
        self.advice_text = tk.Text(advice, wrap="word", background=PANEL, foreground=INK, relief="flat", padx=18, pady=16, font=("TkDefaultFont", 11))
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
            ("mean", "Average ΔE", 150), ("pass", "Pass<3", 150), ("matrix", "Matrix", 90),
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
            font=("TkFixedFont", 9),
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

    def save_parameters(self) -> None:
        try:
            config = self._config_from_controls()
            composition = self.COMPOSITION_LABELS[self.composition_var.get()]
            self.settings = AppSettings(
                optimization=config,
                composition=composition,
                show_motion=self.show_motion_var.get(),
                last_report_format=self.settings.last_report_format,
            )
            path = save_settings(self.settings)
        except (OSError, ValueError) as exc:
            messagebox.showerror("参数保存失败", str(exc))
            return
        self._set_status(f"参数已保存：{path}")

    def _redraw_plots(self) -> None:
        patches = self.result.patch_results if self.result is not None else []
        self.before_plot.draw(patches, mode="before", show_motion=self.show_motion_var.get())
        self.after_plot.draw(patches, mode="after", show_motion=self.show_motion_var.get())

    def _show_patch_detail(self, zone: int) -> None:
        if self.result is None:
            return
        patch = next((item for item in self.result.patch_results if item.zone == zone), None)
        if patch is None:
            return
        self.patch_detail_var.set(
            f"Patch {patch.zone} {patch.name} [{patch.category}] · "
            f"Lab Before ({patch.before_lab[0]:.2f}, {patch.before_lab[1]:+.2f}, {patch.before_lab[2]:+.2f}) → "
            f"After ({patch.after_lab[0]:.2f}, {patch.after_lab[1]:+.2f}, {patch.after_lab[2]:+.2f}) → "
            f"Ideal ({patch.ideal_lab[0]:.2f}, {patch.ideal_lab[1]:+.2f}, {patch.ideal_lab[2]:+.2f}); "
            f"ΔE {patch.delta_e_before:.3f}→{patch.delta_e_after:.3f}, "
            f"ΔL {patch.delta_l_before:+.2f}→{patch.delta_l_after:+.2f}, "
            f"ΔC {patch.delta_c_before:+.2f}→{patch.delta_c_after:+.2f}, "
            f"Δh {patch.delta_h_before:+.1f}°→{patch.delta_h_after:+.1f}°; {patch.regression_status}."
        )
        item = f"patch-{zone}"
        if self.tree.exists(item):
            self.tree.selection_set(item)
            self.tree.see(item)

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
        except (OSError, ImatestCSVError) as exc:
            messagebox.showerror("CSV 读取失败", str(exc))
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
        self.correction_panel.set_matrix(None)
        self.optimized_panel.set_matrix(None)
        for variable in self.kpi_vars:
            variable.set("—")
        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.engineering_tree.get_children():
            self.engineering_tree.delete(item)
        self.before_plot.draw([], mode="before", show_motion=self.show_motion_var.get())
        self.after_plot.draw([], mode="after", show_motion=self.show_motion_var.get())
        self.patch_detail_var.set("点击任一 Patch 可查看 Before → After → Ideal 的详细轨迹。")
        self._set_statistics("尚未运行优化。")
        self.xml_diff = ""
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        self.diff_text.configure(state="disabled")
        self._set_advice("尚未运行优化。")

    def _render_result(self) -> None:
        assert self.result is not None
        result = self.result
        self.original_panel.set_matrix(result.original_matrix)
        self.correction_panel.set_matrix(result.correction_matrix)
        self.optimized_panel.set_matrix(result.optimized_matrix)
        self.kpi_vars[0].set(f"{result.mean_before:.2f} → {result.mean_after:.2f}")
        self.kpi_vars[1].set(f"{result.max_before:.2f} → {result.max_after:.2f}")
        self.kpi_vars[2].set(f"{result.mean_improvement_percent:+.1f}%")
        self.kpi_vars[3].set(f"{result.improved_count} / {result.regressed_count}")
        self.before_plot.draw(result.patch_results, mode="before", show_motion=self.show_motion_var.get())
        self.after_plot.draw(result.patch_results, mode="after", show_motion=self.show_motion_var.get())
        for item in self.tree.get_children():
            self.tree.delete(item)
        for patch in result.patch_results:
            tags = ["neutral"] if patch.zone >= 19 else []
            if patch.zone in self.settings.optimization.focus_patches:
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
                    f"{patch.improvement_percent:+.1f}%",
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
            f"Fixed Point max ΔE={result.matrix_health.fixed_point_max_delta_e:.5f} · coefficient error={result.matrix_health.fixed_point_max_error:.7f}",
            "",
            "Pass Rate",
        ]
        for index, threshold in enumerate(result.pass_rates.thresholds):
            stats_lines.append(
                f"· ΔE<{threshold:g}: {result.pass_rates.before_counts[index]}/{result.pass_rates.sample_count} "
                f"({result.pass_rates.before_rate(index):.1%}) → {result.pass_rates.after_counts[index]}/{result.pass_rates.sample_count} "
                f"({result.pass_rates.after_rate(index):.1%})"
            )
        stats_lines.extend(["", "Patch 分类统计"])
        for category in result.category_statistics:
            stats_lines.append(
                f"· {category.category}: n={category.count}, mean {category.mean_before:.3f}→{category.mean_after:.3f}, "
                f"improve={category.improved}, regression={category.regressed}, Pass<3 {category.pass_rate_before_3:.1%}→{category.pass_rate_after_3:.1%}"
            )
        stats_lines.extend(
            [
                "",
                "Loss Breakdown (Before → After)",
                f"· Total {result.loss_before.total:.3f} → {result.loss_after.total:.3f}",
                f"· ΔE {result.loss_before.delta_e:.3f} → {result.loss_after.delta_e:.3f}; ΔC {result.loss_before.delta_c:.3f} → {result.loss_after.delta_c:.3f}; Δh {result.loss_before.delta_h:.3f} → {result.loss_after.delta_h:.3f}",
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
            title="保存改后 Qualcomm XML",
            defaultextension=".xml",
            initialfile=default_name,
            filetypes=[("XML", "*.xml")],
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
