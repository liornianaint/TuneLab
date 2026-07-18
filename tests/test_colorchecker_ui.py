from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from tunelab.app import CCM_WORKSPACE_TITLE, TuneLabApp
from tunelab.ccm.qualcomm_xml import QualcommCCDocument
from tunelab.colorchecker.engine import COLORCHECKER_CLASSIC_SRGB_8BIT
from tunelab.ui_foundation import FONT_BODY, FONT_TITLE

from .test_colorchecker_engine import synthetic_chart


SIMPLE_CC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<cc13_ipe_v2>
  <control_method><control_var_type>6</control_var_type></control_method>
  <chromatix_cc13_core>
    <mod_cc13_trigger_data>
      <start>3000</start><end>5000</end>
      <region>
        <c_tab><c>1 0 0 0 1 0 0 0 1</c></c_tab>
        <k_tab><k>0 0 0</k></k_tab>
      </region>
    </mod_cc13_trigger_data>
  </chromatix_cc13_core>
</cc13_ipe_v2>
"""


def write_identity_csv(path: Path) -> None:
    rows = ["File,D65_capture.png", "Color space,sRGB", "Zone,R-meas,G-meas,B-meas,R-ideal,G-ideal,B-ideal"]
    for zone, colour in enumerate(COLORCHECKER_CLASSIC_SRGB_8BIT, start=1):
        values = [value / 255.0 for value in colour]
        rows.append(
            ",".join(
                [str(zone), *(f"{value:.8f}" for value in values), *(f"{value:.8f}" for value in values)]
            )
        )
    path.write_text("\n".join(rows), encoding="utf-8")


class UnifiedColorCheckerUISmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.withdraw()
        self.app = TuneLabApp(self.root)
        self.app.show_cc_workspace()

    def tearDown(self) -> None:
        if hasattr(self, "root"):
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def test_one_workspace_contains_csv_image_and_original_cc_features(self) -> None:
        self.assertEqual(self.root.title(), CCM_WORKSPACE_TITLE)
        self.assertEqual(self.app.dataset_source, "csv")
        self.assertEqual(str(self.app.optimize_button.cget("style")), "Primary.TButton")
        self.assertEqual(str(self.app.region_match_button.cget("style")), "RegionMatch.TButton")
        self.assertEqual(str(self.app.region_combo.cget("state")), "readonly")
        style = self.app.region_combo.winfo_toplevel().tk.call("ttk::style", "lookup", "Title.TLabel", "-font")
        self.assertEqual(str(style), FONT_TITLE)
        body = self.app.region_combo.winfo_toplevel().tk.call("ttk::style", "lookup", "Card.TLabel", "-font")
        self.assertEqual(str(body), FONT_BODY)
        self.assertEqual(
            [self.app.notebook.tab(tab, "text").strip() for tab in self.app.notebook.tabs()],
            ["色差对比", "ColorChecker 输入", "工程统计", "诊断与解释", "History / XML Diff"],
        )
        self.assertTrue(self.app.reference_is_standard)
        self.assertEqual(self.app.reference_preview.title_var.get(), "标准 ColorChecker 目标")
        self.assertFalse(hasattr(self.app, "image_solver_var"))
        self.assertFalse(hasattr(self.app, "composition_var"))
        self.assertFalse(hasattr(self.app, "simulation_preview"))
        tool_labels = [
            self.app.tools_menu.entrycget(index, "label")
            for index in range(self.app.tools_menu.index("end") + 1)
        ]
        self.assertIn("CCM / ColorChecker 校正", tool_labels)
        self.assertNotIn("ColorChecker 图像校正...", tool_labels)

    def test_csv_input_remains_supported_and_switches_without_losing_image_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "D65_measurement.csv"
            write_identity_csv(csv_path)
            with mock.patch("tunelab.app.filedialog.askopenfilename", return_value=str(csv_path)):
                self.app.load_csv()
        self.assertEqual(self.app.dataset_source, "csv")
        self.assertIs(self.app.dataset, self.app.csv_dataset)
        self.assertEqual(len(self.app.dataset.patches), 24)
        self.assertEqual(self.app.cct_var.get(), "6500")

        self.assertIs(self.app.open_colorchecker_optimizer(), self.app)
        self.assertEqual(self.app.dataset_source, "image")
        self.assertIsNone(self.app.dataset)
        self.app._set_input_mode("csv")
        self.assertIs(self.app.dataset, self.app.csv_dataset)

    def test_standard_target_is_default_and_custom_target_never_changes_cct(self) -> None:
        self.app.cct_var.set("4000")
        with tempfile.TemporaryDirectory() as directory:
            target_path = Path(directory) / "D65_custom_target.png"
            Image.fromarray(synthetic_chart(colour_scale=0.8).display_rgb, mode="RGB").save(target_path)
            self.app.load_reference_image(str(target_path))
        self.assertFalse(self.app.reference_is_standard)
        self.assertEqual(self.app.reference_preview.title_var.get(), "自定义目标对比图")
        self.assertEqual(self.app.cct_var.get(), "4000")
        self.app.use_standard_reference()
        self.assertTrue(self.app.reference_is_standard)
        self.assertEqual(self.app.cct_var.get(), "4000")

    def test_image_end_to_end_uses_shared_optimizer_without_full_image_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            measured_path = root / "4000K_Before.png"
            xml_path = root / "cc13.xml"
            Image.fromarray(synthetic_chart().display_rgb, mode="RGB").save(measured_path)
            xml_path.write_text(SIMPLE_CC_XML, encoding="utf-8")
            self.app.load_test_image(str(measured_path))
            self.app.load_xml(str(xml_path))
            self.app.run_optimization()

        self.assertEqual(self.app.dataset_source, "image")
        self.assertEqual(self.app.cct_var.get(), "4000")
        self.assertIsNotNone(self.app.selected_region)
        self.assertIsNotNone(self.app.result)
        self.assertEqual(len(self.app.tree.get_children()), 24)
        self.assertIsNotNone(self.app.test_preview.image_data)
        self.assertIsNotNone(self.app.reference_preview.image_data)
        self.assertIn("色块级优化", self.app.image_metrics_var.get())
        self.assertIn("ΔC", self.app.statistics_text.get("1.0", "end"))
        self.assertFalse(any(patch.regression_status == "FAIL" for patch in self.app.result.patch_results))

    def test_safe_image_result_overwrites_only_the_selected_xml_region(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            measured_path = root / "4000K_warm.png"
            xml_path = root / "cc13.xml"
            chart = synthetic_chart()
            warm = np.rint(
                np.clip(chart.display_rgb.astype(np.float64) * np.asarray((1.15, 1.0, 0.8)), 0, 255)
            ).astype(np.uint8)
            Image.fromarray(warm, mode="RGB").save(measured_path)
            xml_path.write_text(SIMPLE_CC_XML, encoding="utf-8")
            self.app.load_test_image(str(measured_path))
            self.app.load_xml(str(xml_path))
            self.app.run_optimization()
            self.assertEqual(str(self.app.save_xml_button.cget("state")), "normal")
            expected = self.app.result.optimized_matrix
            with mock.patch("tunelab.app.messagebox.askyesno", return_value=True):
                self.app.save_xml()
            reloaded = QualcommCCDocument.load(xml_path)
            np.testing.assert_allclose(reloaded.regions[0].matrix, expected, atol=1e-7)
            np.testing.assert_allclose(self.app.document.regions[0].matrix, expected, atol=1e-7)

        self.assertIn("已覆盖并回读校验", self.app.status_var.get())


if __name__ == "__main__":
    unittest.main()
