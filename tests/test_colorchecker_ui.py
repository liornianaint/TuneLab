from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from tunelab.ccm.qualcomm_xml import QualcommCCDocument
from tunelab.colorchecker.engine import RESTORATION_PROFILES
from tunelab.colorchecker.ui import ColorCheckerWorkspace, WINDOW_TITLE
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


CALIBRATED_CC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<cc13_ipe_v2>
  <control_method><control_var_type>6</control_var_type></control_method>
  <chromatix_cc13_core>
    <mod_cc13_trigger_data>
      <start>3800</start><end>4500</end>
      <region>
        <c_tab><c>1.958841 -0.713342 -0.245498 -0.118669 0.739305 0.379364 0.411650 -1.956748 2.545098</c></c_tab>
        <k_tab><k>0 0 0</k></k_tab>
      </region>
    </mod_cc13_trigger_data>
  </chromatix_cc13_core>
</cc13_ipe_v2>
"""


class ColorCheckerUISmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.withdraw()
        self.app = ColorCheckerWorkspace(self.root)

    def tearDown(self) -> None:
        if hasattr(self, "app"):
            self.app.shutdown()
        if hasattr(self, "root"):
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def test_empty_workspace_uses_shared_typography_and_manual_region_selector(self) -> None:
        self.assertEqual(self.root.title(), WINDOW_TITLE)
        self.assertEqual(str(self.app.optimize_button.cget("style")), "Primary.TButton")
        self.assertEqual(str(self.app.region_match_button.cget("style")), "RegionMatch.TButton")
        self.assertEqual(str(self.app.region_combo.cget("state")), "readonly")
        style = self.app.region_combo.winfo_toplevel().tk.call("ttk::style", "lookup", "CheckerTitle.TLabel", "-font")
        self.assertEqual(str(style), FONT_TITLE)
        body = self.app.region_combo.winfo_toplevel().tk.call("ttk::style", "lookup", "CheckerCard.TLabel", "-font")
        self.assertEqual(str(body), FONT_BODY)
        self.assertEqual(
            [self.app.notebook.tab(tab, "text") for tab in self.app.notebook.tabs()],
            ["图像对比", "24 色块", "矩阵与工程检查", "XML Diff"],
        )
        self.assertEqual(self.app.strategy_var.get(), "色彩还原（资料标定）")
        self.assertEqual(self.app.strength_var.get(), "100")
        file_labels = [
            self.app.file_menu.entrycget(index, "label")
            for index in range(self.app.file_menu.index("end") + 1)
            if self.app.file_menu.type(index) == "command"
        ]
        self.assertIn("打开目标对比图...", file_labels)
        self.assertNotIn("D65", " ".join(file_labels))
        self.assertEqual(self.app.reference_preview.title_var.get(), "目标对比图")

    def test_synthetic_end_to_end_detection_region_fit_and_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            measured_path = root / "4000K_Before.png"
            reference_path = root / "4000K_Target.png"
            xml_path = root / "cc13.xml"
            Image.fromarray(synthetic_chart().display_rgb, mode="RGB").save(measured_path)
            Image.fromarray(synthetic_chart(colour_scale=0.80).display_rgb, mode="RGB").save(reference_path)
            xml_path.write_text(SIMPLE_CC_XML, encoding="utf-8")
            self.app.load_test_image(str(measured_path))
            self.app.load_reference_image(str(reference_path))
            self.app.load_xml(str(xml_path))
            self.app.strategy_var.set("图像拟合·平衡")
            self.app.run_optimization()

        self.assertIsNotNone(self.app.test_detection)
        self.assertIsNotNone(self.app.reference_detection)
        self.assertEqual(self.app.cct_var.get(), "4000")
        self.assertIsNotNone(self.app.selected_region)
        self.assertIsNotNone(self.app.result)
        self.assertIsNotNone(self.app.simulation)
        self.assertEqual(len(self.app.patch_tree.get_children()), 24)
        self.assertIsNotNone(self.app.simulation_preview.image_data)
        self.assertIn("M_new = A × M_old", self.app.engineering_text.get("1.0", "end"))

    def test_4000k_profile_reproduces_and_overwrites_the_validated_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            measured_path = root / "4000K_Before.png"
            reference_path = root / "D65.png"
            xml_path = root / "cc13.xml"
            Image.fromarray(synthetic_chart().display_rgb, mode="RGB").save(measured_path)
            Image.fromarray(synthetic_chart(colour_scale=0.80).display_rgb, mode="RGB").save(reference_path)
            xml_path.write_text(CALIBRATED_CC_XML, encoding="utf-8")
            self.app.load_test_image(str(measured_path))
            self.app.load_reference_image(str(reference_path))
            self.app.load_xml(str(xml_path))
            self.app.run_optimization()

            self.assertIsNotNone(self.app.result)
            assert self.app.result is not None
            target = RESTORATION_PROFILES[1].target_matrix
            np.testing.assert_allclose(self.app.result.optimized_matrix, target, atol=1e-12)
            self.assertEqual(self.app.result.matrix_health.status, "PASS")
            self.assertEqual(self.app.simulation.domain, "real-shot-response")
            self.assertEqual(str(self.app.save_button.cget("state")), "normal")
            with mock.patch("tunelab.colorchecker.ui.messagebox.askyesno", return_value=True):
                self.app.save_xml()
            reloaded = QualcommCCDocument.load(xml_path)
            np.testing.assert_allclose(reloaded.regions[0].matrix, target, atol=1e-7)
            np.testing.assert_allclose(self.app.document.regions[0].matrix, target, atol=1e-7)

        self.assertIn("已覆盖并回读校验", self.app.status_var.get())


if __name__ == "__main__":
    unittest.main()
