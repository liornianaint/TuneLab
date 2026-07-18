from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
import unittest
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from unittest import mock

from tunelab.branding import WORKBENCH_HELP_TEXT
from tunelab.app import (
    APP_TITLE,
    FONT_BODY,
    FONT_KPI,
    FONT_MONO,
    FONT_SMALL,
    FONT_SMALL_BOLD,
    FONT_TITLE,
    LAB_PLACEHOLDER_BOUNDS,
    LabViewState,
    TuneLabApp,
    calculate_lab_bounds,
    lab_plane_hex,
)
from tunelab.ui_foundation import ACTION_BLUE, ACTION_BLUE_HOVER, calculate_window_placement, default_sources_directory, fit_window_to_screen, select_font_families
from tunelab.ui_foundation import FONT_BODY_BOLD
from tunelab.ccm.optimizer import optimize_ccm
from tunelab.ccm.qualcomm_xml import QualcommCCDocument

from .test_ccm_persistence import ROOT
from .materials import CC_XML, d65_dataset


class LabViewTests(unittest.TestCase):
    def test_lab_plane_uses_the_same_imatest_like_lightness(self) -> None:
        self.assertEqual(lab_plane_hex(-6.1, -3.5), "#cadee0")

    def test_auto_bounds_include_all_ideal_before_after_extremes(self) -> None:
        points = [(-42.0, -63.0), (50.0, 30.0), (4.0, -45.0)]
        a_min, a_max, b_min, b_max = calculate_lab_bounds(points)
        self.assertAlmostEqual(a_max - a_min, b_max - b_min)
        self.assertLess(b_min, -63.0)
        for a_value, b_value in points:
            self.assertLess(a_min, a_value)
            self.assertGreater(a_max, a_value)
            self.assertLess(b_min, b_value)
            self.assertGreater(b_max, b_value)

    def test_empty_plots_use_the_colorchecker_gamut_placeholder(self) -> None:
        self.assertEqual(calculate_lab_bounds([]), LAB_PLACEHOLDER_BOUNDS)
        self.assertEqual(LabViewState().bounds, LAB_PLACEHOLDER_BOUNDS)
        self.assertEqual(LAB_PLACEHOLDER_BOUNDS, (-70.0, 70.0, -70.0, 70.0))

    def test_zoom_limits_and_reset_keep_square_shared_view(self) -> None:
        view = LabViewState()
        view.fit([(-30.0, -52.0), (44.0, 32.0)])
        original = view.bounds
        auto_span = original[1] - original[0]
        for _ in range(30):
            view.zoom(0.5, 0.0, -10.0)
        self.assertAlmostEqual(view.bounds[1] - view.bounds[0], view.bounds[3] - view.bounds[2])
        self.assertAlmostEqual(view.bounds[1] - view.bounds[0], auto_span * 0.20)
        for _ in range(30):
            view.zoom(2.0, original[1], original[3])
        self.assertAlmostEqual(view.bounds[1] - view.bounds[0], auto_span * 2.0)
        auto_a_center = (original[0] + original[1]) / 2.0
        auto_b_center = (original[2] + original[3]) / 2.0
        current_a_center = (view.bounds[0] + view.bounds[1]) / 2.0
        current_b_center = (view.bounds[2] + view.bounds[3]) / 2.0
        self.assertLessEqual(abs(current_a_center - auto_a_center), auto_span / 2.0 + 1e-9)
        self.assertLessEqual(abs(current_b_center - auto_b_center), auto_span / 2.0 + 1e-9)
        view.reset()
        self.assertEqual(view.bounds, original)


class WindowPlacementTests(unittest.TestCase):
    def test_window_fills_large_and_small_screens_without_outer_margins(self) -> None:
        desktop = calculate_window_placement(
            1512,
            982,
            desired_width=1520,
            desired_height=1080,
        )
        self.assertEqual((desktop.width, desktop.height, desktop.x, desktop.y), (1512, 982, 0, 0))
        compact = calculate_window_placement(
            1024,
            640,
            desired_width=1520,
            desired_height=980,
        )
        self.assertLessEqual(compact.width, 1024)
        self.assertLessEqual(compact.height, 640)
        self.assertEqual(compact.x * 2 + compact.width, 1024)
        self.assertEqual(compact.y * 2 + compact.height, 640)

    def test_fit_uses_the_full_available_screen(self) -> None:
        window = mock.Mock()
        window.winfo_screenwidth.return_value = 1512
        window.winfo_screenheight.return_value = 982
        window.winfo_vrootx.return_value = 0
        window.winfo_vrooty.return_value = 0
        placement = fit_window_to_screen(window)
        self.assertEqual(placement.geometry, "1512x982+0+0")
        window.geometry.assert_called_once_with("1512x982+0+0")
        window.state.assert_called_once_with("zoomed")


class DesktopUISmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.withdraw()
        self.app = TuneLabApp(self.root)

    def tearDown(self) -> None:
        if hasattr(self, "root"):
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def test_file_menu_and_primary_actions_are_not_duplicated(self) -> None:
        labels = [
            self.app.file_menu.entrycget(index, "label")
            for index in range(self.app.file_menu.index("end") + 1)
        ]
        self.assertEqual(
            labels,
            [
                "打开 CC CSV...",
                "打开测试 ColorChecker...",
                "使用标准 ColorChecker 目标",
                "打开自定义目标对比图...",
                "打开 Qualcomm CC XML...",
                "保存 XML...",
                "导出工程报告...",
                "退出",
            ],
        )

        button_labels: list[str] = []

        def visit(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if child.winfo_class() == "TButton":
                    button_labels.append(str(child.cget("text")))
                visit(child)

        visit(self.root)
        self.assertEqual(button_labels.count("保存 XML"), 1)
        self.assertNotIn("保存参数", button_labels)
        self.assertEqual(str(self.app.region_match_button.cget("text")), "自动匹配 Region")
        self.assertEqual(str(self.app.region_match_button.cget("style")), "RegionMatch.TButton")
        self.assertEqual(str(self.app.optimize_button.cget("text")), "3  自动优化")
        style = ttk.Style(self.root)
        self.assertEqual(style.lookup("Primary.TButton", "background"), ACTION_BLUE)
        self.assertEqual(
            style.lookup("Primary.TButton", "background", ("active",)),
            ACTION_BLUE_HOVER,
        )
        self.assertEqual(
            style.lookup("Primary.TButton", "foreground", ("active",)),
            "white",
        )
        self.assertEqual(
            style.lookup("RegionMatch.TButton", "foreground", ("active",)),
            "#1D1D1F",
        )
        config_labels = [
            self.app.config_menu.entrycget(index, "label")
            for index in range(self.app.config_menu.index("end") + 1)
        ]
        self.assertEqual(config_labels, ["导入配置...", "导出配置..."])
        self.assertEqual(
            [self.app.tools_menu.entrycget(index, "label") for index in range(self.app.tools_menu.index("end") + 1)],
            ["首页", "CCM / ColorChecker 校正", "Gamma 优化", "图像分析器..."],
        )

    def test_save_xml_defaults_to_confirmed_source_overwrite(self) -> None:
        document = QualcommCCDocument.load(CC_XML)
        self.app.document = document
        self.app.selected_region = document.regions[0]
        self.app.result = SimpleNamespace(matrix_health=SimpleNamespace(status="PASS"))
        with mock.patch("tunelab.app.messagebox.askyesno", return_value=False) as confirmation, mock.patch.object(document, "save_with_matrix") as save:
            self.app.save_xml()
        self.assertIn(str(document.source_path), confirmation.call_args.args[1])
        save.assert_not_called()

    def test_home_header_has_help_without_unused_settings(self) -> None:
        button_labels: list[str] = []
        button_styles: list[str] = []

        def visit(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if child.winfo_class() == "TButton":
                    button_labels.append(str(child.cget("text")))
                    button_styles.append(str(child.cget("style")))
                visit(child)

        visit(self.app.home_view)
        self.assertIn("帮助", button_labels)
        self.assertNotIn("开始色彩校正", button_labels)
        self.assertNotIn("Primary.TButton", button_styles)
        self.assertNotIn("设置", button_labels)
        self.assertFalse(hasattr(self.app, "home_settings_button"))
        with mock.patch("tunelab.app.show_workbench_help") as help_dialog:
            self.app.home_help_button.invoke()
        help_dialog.assert_called_once_with(self.root)

    def test_native_file_dialogs_start_in_plural_sources(self) -> None:
        self.assertEqual(default_sources_directory().resolve(), (ROOT / "sources").resolve())
        with mock.patch("tunelab.app.filedialog.askopenfilename", return_value="") as chooser:
            self.app.load_csv()
        self.assertEqual(Path(chooser.call_args.kwargs["initialdir"]).resolve(), (ROOT / "sources").resolve())

    def test_workbench_help_is_module_neutral_and_extensible(self) -> None:
        self.assertIn("当前可用模块以首页和“工具”菜单为准", WORKBENCH_HELP_TEXT)
        self.assertIn("后续新增模块", WORKBENCH_HELP_TEXT)
        self.assertIn("每个模块拥有独立的输入要求、算法边界和结果解释", WORKBENCH_HELP_TEXT)
        self.assertNotIn("CSV 需要 R/G/B-meas", WORKBENCH_HELP_TEXT)

    def test_selected_region_is_always_visible(self) -> None:
        document = QualcommCCDocument.load(CC_XML)
        self.app.document = document
        self.app._select_region(0)
        self.assertIn("当前 Region：#0", self.app.active_region_var.get())

    def test_comparison_tab_contains_plots_and_complete_scrollable_patch_table(self) -> None:
        tabs = [self.app.notebook.tab(tab_id, "text").strip() for tab_id in self.app.notebook.tabs()]
        self.assertEqual(tabs[0], "色差对比")
        self.assertEqual(tabs[1], "ColorChecker 输入")
        self.assertNotIn("色块明细", tabs)
        self.assertEqual(int(str(self.app.patch_table_panel.cget("width"))), 420)
        self.assertEqual(self.root.title(), APP_TITLE)
        self.assertIsNotNone(self.app._app_icon)
        self.assertEqual(self.app.app_icon_source_path.resolve(), (ROOT / "tunelab" / "assets" / "tunelab.png").resolve())
        self.assertTrue(self.app.app_icon_path.exists())
        self.assertGreaterEqual(self.app._app_icon.width(), 512)
        from PIL import Image

        with Image.open(self.app.app_icon_path) as icon:
            bounds = icon.getchannel("A").getbbox()
            self.assertIsNotNone(bounds)
            fill_ratio = (bounds[2] - bounds[0]) / icon.width
            self.assertGreaterEqual(fill_ratio, 0.80)
            self.assertLessEqual(fill_ratio, 0.84)
        self.assertEqual(
            self.app.tree.cget("columns"),
            ("zone", "name", "category", "weight", "before", "after", "change", "dl", "dc", "dh", "regression", "status", "module"),
        )
        self.assertTrue(self.app.tree.cget("xscrollcommand"))
        self.assertTrue(self.app.tree.cget("yscrollcommand"))
        self.assertTrue(self.app.before_plot.canvas.bind("<MouseWheel>"))
        if tk.TkVersion >= 9.0:
            self.assertTrue(self.app.before_plot.canvas.bind("<TouchpadScroll>"))
        self.assertTrue(self.app.before_plot.canvas.bind("<Double-Button-1>"))
        self.assertFalse(self.app.before_plot.canvas.bind("<B1-Motion>"))
        self.assertFalse(self.app.before_plot.canvas.bind("<B2-Motion>"))
        self.assertFalse(self.app.before_plot.canvas.bind("<B3-Motion>"))
        original_bounds = self.app.lab_view.bounds
        left, top, side = self.app.before_plot._geometry
        self.app.before_plot._on_mousewheel(
            SimpleNamespace(delta=120, x=left + side / 2.0, y=top + side / 2.0)
        )
        self.assertNotEqual(self.app.lab_view.bounds, original_bounds)
        self.assertIs(self.app.before_plot.view_state, self.app.after_plot.view_state)
        self.app.before_plot._reset_view(SimpleNamespace())
        self.assertEqual(self.app.lab_view.bounds, original_bounds)
        if tk.TkVersion >= 9.0:
            self.app.before_plot._on_touchpad_scroll(
                SimpleNamespace(delta=120, x=left + side / 2.0, y=top + side / 2.0)
            )
            self.assertNotEqual(self.app.lab_view.bounds, original_bounds)
            self.app.before_plot._reset_view(SimpleNamespace())
        visible_text = [
            str(child.cget("text"))
            for child in self.app.patch_table_panel.winfo_children()
            if "text" in child.keys()
        ]
        self.assertFalse(any("CIEDE2000" in text for text in visible_text))

    def test_comparison_fonts_and_plot_colours_are_consistent(self) -> None:
        style = ttk.Style(self.root)
        self.assertEqual(style.lookup("Kpi.TLabel", "font"), FONT_KPI)
        self.assertEqual(style.lookup("KpiCompact.TLabel", "font"), FONT_KPI)
        self.assertEqual(str(self.app.tree.tag_configure("focus", "font")), FONT_SMALL_BOLD)
        self.assertEqual(style.lookup("TButton", "font"), FONT_BODY)
        self.assertEqual(style.lookup("TCombobox", "font"), FONT_BODY)
        self.assertEqual(style.lookup("TNotebook.Tab", "font"), FONT_BODY)
        self.assertEqual(style.lookup("ActiveNav.TButton", "font"), FONT_BODY_BOLD)
        self.assertEqual(tkfont.Font(root=self.root, name=FONT_BODY, exists=True).actual("size"), 11)
        self.assertEqual(tkfont.Font(root=self.root, name=FONT_SMALL, exists=True).actual("size"), 9)
        self.assertEqual(tkfont.Font(root=self.root, name=FONT_TITLE, exists=True).actual("size"), 20)
        body_family = tkfont.Font(root=self.root, name=FONT_BODY, exists=True).actual("family")
        for font_name in ("TkDefaultFont", "TkHeadingFont", "TkMenuFont"):
            with self.subTest(font_name=font_name):
                self.assertEqual(
                    body_family,
                    tkfont.Font(root=self.root, name=font_name, exists=True).actual("family"),
                )
        mono_family = tkfont.Font(root=self.root, name=FONT_MONO, exists=True).actual("family")
        self.assertEqual(mono_family, tkfont.Font(root=self.root, name="TkFixedFont", exists=True).actual("family"))
        before_items = self.app.before_plot.canvas.find_withtag("plot-background")
        after_items = self.app.after_plot.canvas.find_withtag("plot-background")
        self.assertTrue(before_items)
        self.assertEqual(len(before_items), len(after_items))
        before_colours = [self.app.before_plot.canvas.itemcget(item, "fill") for item in before_items]
        after_colours = [self.app.after_plot.canvas.itemcget(item, "fill") for item in after_items]
        self.assertEqual(before_colours, after_colours)

    def test_windows_font_stack_keeps_the_same_ui_hierarchy(self) -> None:
        available = (
            "Segoe UI Variable Text",
            "Segoe UI Variable Display",
            "Cascadia Mono",
        )
        with mock.patch("tunelab.ui_foundation.tkfont.families", return_value=available):
            families = select_font_families(self.root, system="Windows")
        self.assertEqual(families.body, "Segoe UI Variable Text")
        self.assertEqual(families.display, "Segoe UI Variable Display")
        self.assertEqual(families.mono, "Cascadia Mono")

    def test_plot_and_table_patch_selection_are_bidirectionally_linked(self) -> None:
        dataset = d65_dataset()
        document = QualcommCCDocument.load(CC_XML)
        region, _mode = document.find_region_for_cct(6500)
        self.app.dataset = dataset
        self.app.document = document
        self.app.selected_region = region
        self.app.result = optimize_ccm(dataset, region.matrix)
        self.app._render_result()

        for plot in (self.app.before_plot, self.app.after_plot):
            left, top, side = plot._geometry
            for zone in range(1, 25):
                patch_box = plot.canvas.bbox(f"patch-{zone}")
                self.assertIsNotNone(patch_box)
                assert patch_box is not None
                self.assertGreaterEqual(patch_box[0], left - 2)
                self.assertGreaterEqual(patch_box[1], top - 2)
                self.assertLessEqual(patch_box[2], left + side + 2)
                self.assertLessEqual(patch_box[3], top + side + 2)

    def test_show_motion_hides_only_motion_artists_without_resetting_view(self) -> None:
        dataset = d65_dataset()
        document = QualcommCCDocument.load(CC_XML)
        region, _mode = document.find_region_for_cct(6500)
        self.app.dataset = dataset
        self.app.document = document
        self.app.selected_region = region
        self.app.result = optimize_ccm(dataset, region.matrix)
        self.app.show_motion_var.set(True)
        self.app._render_result()
        self.app._show_patch_detail(13)
        bounds = self.app.lab_view.bounds
        selected = self.app.before_plot.selected_zone

        baseline = [len(plot.canvas.find_withtag("motion")) for plot in (self.app.before_plot, self.app.after_plot)]
        self.assertTrue(all(count > 0 for count in baseline))
        for _ in range(20):
            self.app.show_motion_var.set(False)
            self.app._on_show_motion_changed()
            for plot in (self.app.before_plot, self.app.after_plot):
                self.assertFalse(plot.canvas.find_withtag("motion"))
                self.assertTrue(plot.canvas.find_withtag("trajectory"))
                self.assertTrue(plot.canvas.find_withtag("patch-13"))
            self.app.show_motion_var.set(True)
            self.app._on_show_motion_changed()
            self.assertEqual(
                [len(plot.canvas.find_withtag("motion")) for plot in (self.app.before_plot, self.app.after_plot)],
                baseline,
            )
        self.assertEqual(self.app.lab_view.bounds, bounds)
        self.assertEqual(self.app.before_plot.selected_zone, selected)
        self.assertEqual(self.app.after_plot.selected_zone, selected)

        self.app.tree.selection_set("patch-13")
        self.app._on_patch_table_selected()
        self.assertEqual(self.app.before_plot.selected_zone, 13)
        self.assertEqual(self.app.after_plot.selected_zone, 13)
        self.assertIn("focus", self.app.tree.item("patch-13", "tags"))

        self.app._show_patch_detail(14)
        self.assertEqual(self.app.tree.selection(), ("patch-14",))
        self.assertEqual(self.app.before_plot.selected_zone, 14)
        self.assertEqual(self.app.after_plot.selected_zone, 14)

        self.app.focus_patches_var.set("1,13,14,15")
        self.app._redraw_plots()
        self.assertIn(1, self.app.before_plot.focus_zones)
        self.assertIn(1, self.app.after_plot.focus_zones)

        for _ in range(10):
            self.app.lab_view.zoom(0.5, 0.0, 0.0)
        self.app._redraw_plots()
        for plot in (self.app.before_plot, self.app.after_plot):
            left, top, side = plot._geometry
            for zone in range(1, 25):
                patch_box = plot.canvas.bbox(f"patch-{zone}")
                if patch_box is None:
                    continue
                self.assertGreaterEqual(patch_box[0], left - 2)
                self.assertGreaterEqual(patch_box[1], top - 2)
                self.assertLessEqual(patch_box[2], left + side + 2)
                self.assertLessEqual(patch_box[3], top + side + 2)

    def test_patch_table_sorting_tooltip_and_embedded_gamma_switch(self) -> None:
        dataset = d65_dataset()
        document = QualcommCCDocument.load(CC_XML)
        region, _mode = document.find_region_for_cct(6500)
        self.app.dataset = dataset
        self.app.document = document
        self.app.selected_region = region
        self.app.result = optimize_ccm(dataset, region.matrix)
        self.app._render_result()

        self.app._sort_patch_table("before")
        ascending = [float(self.app.tree.item(item, "values")[4]) for item in self.app.tree.get_children()]
        self.assertEqual(ascending, sorted(ascending))
        self.app._sort_patch_table("before")
        descending = [float(self.app.tree.item(item, "values")[4]) for item in self.app.tree.get_children()]
        self.assertEqual(descending, sorted(descending, reverse=True))
        self.assertIn("▼", self.app.tree.heading("before", "text"))

        self.app.before_plot._show_tooltip(SimpleNamespace(x=40, y=40), self.app.result.patch_results[0])
        self.assertTrue(self.app.before_plot.canvas.find_withtag("tooltip"))
        self.app.before_plot._hide_tooltip()
        self.assertFalse(self.app.before_plot.canvas.find_withtag("tooltip"))

        close_callback = self.root.protocol("WM_DELETE_WINDOW")
        with mock.patch("tunelab.gamma.ui.fit_window_to_screen") as refit:
            gamma = self.app.open_gamma_optimizer()
        refit.assert_not_called()
        self.root.update_idletasks()
        self.assertIs(self.root.nametowidget(gamma.outer.winfo_parent()), self.root)
        self.assertFalse(self.app.cc_view.winfo_manager())
        self.assertEqual(self.root.protocol("WM_DELETE_WINDOW"), close_callback)
        self.app.show_cc_workspace()
        self.assertTrue(self.app.cc_view.winfo_manager())
        self.assertEqual(self.root.protocol("WM_DELETE_WINDOW"), close_callback)
        gamma.outer.destroy()
        reopened = self.app.open_gamma_optimizer()
        self.assertTrue(reopened.outer.winfo_exists())

    def test_home_is_default_and_module_switches_do_not_create_toplevels(self) -> None:
        self.assertTrue(self.app.home_view.winfo_manager())
        self.assertFalse(self.app.cc_view.winfo_manager())
        visible_text: list[str] = []

        def visit(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if "text" in child.keys():
                    visible_text.append(str(child.cget("text")))
                visit(child)

        visit(self.app.home_view)
        self.assertNotIn("最近优化", visible_text)
        self.assertNotIn("快捷操作", visible_text)
        self.assertNotIn("Qualcomm CC13", visible_text)
        self.assertNotIn("把调校工作，收进一个窗口。", visible_text)
        self.assertIn("TuneLab 相机调校工程工作台", visible_text)
        self.app.show_cc_workspace()
        self.root.update_idletasks()
        self.assertFalse(self.app.home_view.winfo_manager())
        self.assertTrue(self.app.cc_view.winfo_manager())
        self.assertLessEqual(self.app.controls_panel.winfo_reqwidth(), 1140)
        self.assertLessEqual(self.app.controls_panel.winfo_reqheight(), 130)
        self.assertLessEqual(self.app.parameters_panel.winfo_reqwidth(), 1020)
        self.assertLessEqual(self.app.parameters_panel.winfo_reqheight(), 105)
        gamma = self.app.open_gamma_optimizer()
        self.root.update_idletasks()
        self.assertTrue(gamma.outer.winfo_manager())
        self.assertLessEqual(gamma.toolbar_panel.winfo_reqwidth(), 1140)
        self.assertLessEqual(gamma.toolbar_panel.winfo_reqheight(), 65)
        self.assertLessEqual(gamma.settings_panel.winfo_reqwidth(), 1100)
        self.assertLessEqual(gamma.settings_panel.winfo_reqheight(), 105)
        self.assertGreaterEqual(gamma.pair_tree.winfo_reqheight(), 250)
        style = ttk.Style(self.root)
        self.assertEqual(style.lookup("GammaTitle.TLabel", "font"), FONT_TITLE)
        self.assertEqual(style.lookup("GammaCard.TLabel", "font"), FONT_BODY)
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in self.root.winfo_children()))
        self.app.show_home_workspace()
        self.assertTrue(self.app.home_view.winfo_manager())

    def test_former_colorchecker_route_reuses_the_unified_ccm_workspace(self) -> None:
        workspace = self.app.open_colorchecker_optimizer()
        self.root.update_idletasks()
        self.assertIs(workspace, self.app)
        self.assertTrue(self.app.cc_view.winfo_manager())
        self.assertEqual(self.app.dataset_source, "image")
        self.assertEqual(self.app.notebook.select(), str(self.app.image_tab))
        self.assertTrue(self.app.reference_is_standard)
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in self.root.winfo_children()))

    def test_about_menu_uses_one_tunelab_owned_dialog_on_all_pages(self) -> None:
        self.app.help_menu.invoke("end")
        self.root.update_idletasks()
        dialog = getattr(self.root, "_tunelab_about_dialog", None)
        self.assertIsInstance(dialog, tk.Toplevel)
        self.assertEqual(dialog.title(), "关于 TuneLab")
        self.assertTrue(hasattr(dialog, "_tunelab_icon"))

        text: list[str] = []

        def visit(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if "text" in child.keys():
                    text.append(str(child.cget("text")))
                visit(child)

        visit(dialog)
        self.assertIn("TuneLab", text)
        self.assertIn("版本", text)
        self.assertIn("0.2.0", text)
        self.assertIn("联系", text)
        self.assertIn("kaiyi.jiang@thundersoft.com", text)
        self.assertIn("所有计算均在本地完成", text)
        self.assertNotIn("CC 校正 · Gamma 优化", text)
        self.assertFalse(any("Python Software Foundation" in value for value in text))

        self.app.show_cc_workspace()
        self.app.help_menu.invoke("end")
        self.assertIs(getattr(self.root, "_tunelab_about_dialog", None), dialog)
        gamma = self.app.open_gamma_optimizer()
        gamma.help_menu.invoke("end")
        self.assertIs(getattr(self.root, "_tunelab_about_dialog", None), dialog)


if __name__ == "__main__":
    unittest.main()
