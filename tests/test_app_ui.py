from __future__ import annotations

import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

from matrixcorrect.app import LabViewState, MatrixCorrectApp, calculate_lab_bounds
from matrixcorrect.qualcomm_xml import QualcommCCDocument

from .test_settings_history import ROOT


class LabViewTests(unittest.TestCase):
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

    def test_zoom_pan_and_one_click_reset_keep_square_shared_view(self) -> None:
        view = LabViewState()
        view.fit([(-30.0, -52.0), (44.0, 32.0)])
        original = view.bounds
        view.zoom(0.5, 0.0, -10.0)
        self.assertAlmostEqual(view.bounds[1] - view.bounds[0], view.bounds[3] - view.bounds[2])
        view.pan_to(12.0, -8.0)
        self.assertAlmostEqual((view.bounds[0] + view.bounds[1]) / 2.0, 12.0)
        self.assertAlmostEqual((view.bounds[2] + view.bounds[3]) / 2.0, -8.0)
        view.reset()
        self.assertEqual(view.bounds, original)


class DesktopUISmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.withdraw()
        self.app = MatrixCorrectApp(self.root)

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
                "打开 Imatest CSV...",
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

    def test_save_as_defaults_to_optimized_xml_without_writing(self) -> None:
        document = QualcommCCDocument.load(ROOT / "Source" / "cc13_ipe_v2.xml")
        self.app.document = document
        self.app.selected_region = document.regions[0]
        self.app.result = SimpleNamespace(matrix_health=SimpleNamespace(status="PASS"))
        with mock.patch("matrixcorrect.app.filedialog.asksaveasfilename", return_value="") as dialog:
            self.app.save_xml()
        kwargs = dialog.call_args.kwargs
        self.assertEqual(kwargs["initialfile"], "cc13_ipe_v2_optimized.xml")
        self.assertEqual(kwargs["defaultextension"], ".xml")
        self.assertEqual(kwargs["filetypes"], [("XML 文件", "*.xml")])
        self.assertTrue(kwargs["confirmoverwrite"])

    def test_selected_region_is_always_visible(self) -> None:
        document = QualcommCCDocument.load(ROOT / "Source" / "cc13_ipe_v2.xml")
        self.app.document = document
        self.app._select_region(0)
        self.assertIn("当前 Region：#0", self.app.active_region_var.get())


if __name__ == "__main__":
    unittest.main()
