from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from matrixcorrect.gamma_models import GammaOptimizationConfig
from matrixcorrect.gamma_app import GammaOptimizationApp
from matrixcorrect.gamma_optimizer import optimize_gamma_lut
from matrixcorrect.gray_imatest import analyze_gray_range, parse_gray_csv, select_fit_zones
from matrixcorrect.qualcomm_gamma_xml import QualcommGammaDocument


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"


class GrayStepchartParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = parse_gray_csv(SOURCE / "gray_summary.csv")

    def test_uploaded_csv_parses_all_engineering_columns(self) -> None:
        self.assertEqual(len(self.dataset.zones), 19)
        first = self.dataset.zones[0]
        self.assertEqual(first.zone, 1)
        self.assertAlmostEqual(first.pixel, 227.0)
        self.assertAlmostEqual(first.log_exposure, -0.05)
        self.assertAlmostEqual(first.density, 0.0505)
        self.assertAlmostEqual(first.slope, 0.310)
        self.assertAlmostEqual(first.noise, 2.041)
        self.assertAlmostEqual(first.mean_r, 232.0)

    def test_threshold_recognition_matches_b9_to_b20(self) -> None:
        expected = {6: (16, 1, 16), 8: (12, 1, 12), 10: (8, 1, 8)}
        for threshold, (count, start, end) in expected.items():
            with self.subTest(threshold=threshold):
                analysis = analyze_gray_range(self.dataset, threshold)
                self.assertEqual(analysis.effective_count, count)
                self.assertEqual((analysis.start_zone, analysis.end_zone), (start, end))
        default = analyze_gray_range(self.dataset)
        self.assertEqual(default.selected_zones, tuple(range(1, 13)))
        self.assertTrue(all(pair.distinguishable for pair in default.pairs[:12]))
        self.assertTrue(all(not pair.distinguishable for pair in default.pairs[12:]))

    def test_manual_range_cannot_cross_an_indistinguishable_break(self) -> None:
        analysis = analyze_gray_range(self.dataset, 8)
        self.assertEqual(
            select_fit_zones(self.dataset, analysis, mode="manual", manual_start=2, manual_end=7),
            (2, 3, 4, 5, 6, 7),
        )
        with self.assertRaisesRegex(ValueError, "不可区分"):
            select_fit_zones(self.dataset, analysis, mode="manual", manual_start=10, manual_end=14)


class QualcommGammaXMLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = SOURCE / "gamma15_ipe_v2.xml"
        cls.document = QualcommGammaDocument.load(cls.path)

    def test_uploaded_gamma_xml_is_257_point_10_bit(self) -> None:
        self.assertEqual(len(self.document.regions), 1)
        region = self.document.regions[0]
        self.assertEqual(region.length, 257)
        self.assertEqual(region.maximum, 1023)
        self.assertEqual(region.channel_r[0], 0)
        self.assertEqual(region.channel_r[-1], 1023)
        self.assertTrue(all(following >= current for current, following in zip(region.channel_g, region.channel_g[1:])))
        selected, mode = self.document.find_region_for_cct(6500)
        self.assertEqual(selected.index, 0)
        self.assertEqual(mode, "exact")

    def test_surgical_save_never_modifies_source_xml(self) -> None:
        dataset = parse_gray_csv(SOURCE / "gray_summary.csv")
        analysis = analyze_gray_range(dataset, 8)
        region = self.document.regions[0]
        result = optimize_gamma_lut(dataset, region, analysis)
        source_before = self.path.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "gamma15_ipe_v2_optimized.xml"
            self.document.save_with_luts(
                destination,
                region.index,
                result.after_r,
                result.after_g,
                result.after_b,
            )
            reloaded = QualcommGammaDocument.load(destination)
            self.assertEqual(reloaded.regions[0].channel_r, result.after_r)
            self.assertEqual(reloaded.regions[0].channel_g, result.after_g)
            self.assertEqual(reloaded.regions[0].channel_b, result.after_b)
            self.assertIn("channel_r", self.document.diff_with_luts(0, result.after_r, result.after_g, result.after_b))
        self.assertEqual(self.path.read_bytes(), source_before)


class GammaOptimizerRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = parse_gray_csv(SOURCE / "gray_summary.csv")
        cls.document = QualcommGammaDocument.load(SOURCE / "gamma15_ipe_v2.xml")
        cls.region = cls.document.regions[0]

    def test_default_gamma_golden_case_improves_without_curve_anomaly(self) -> None:
        analysis = analyze_gray_range(self.dataset, 8)
        result = optimize_gamma_lut(self.dataset, self.region, analysis)
        self.assertEqual(result.selected_zones, tuple(range(1, 13)))
        self.assertGreater(result.applied_strength, 0.0)
        self.assertEqual(result.health.status, "PASS")
        self.assertTrue(result.health.monotonic)
        self.assertEqual(result.health.reversal_count, 0)
        self.assertLess(result.metrics.rmse_after, result.metrics.rmse_before)
        self.assertLess(result.metrics.local_gamma_error_after, result.metrics.local_gamma_error_before)
        self.assertLessEqual(
            result.metrics.rgb_gray_deviation_after,
            result.metrics.rgb_gray_deviation_before + 0.002,
        )
        for before, after in ((result.before_r, result.after_r), (result.before_g, result.after_g), (result.before_b, result.after_b)):
            self.assertEqual((after[0], after[-1]), (before[0], before[-1]))
            self.assertEqual(len(after), 257)
            self.assertTrue(all(0 <= value <= 1023 for value in after))
            self.assertTrue(all(following >= current for current, following in zip(after, after[1:])))
        for zone in (item for item in result.zone_results if item.used):
            self.assertLessEqual(abs(zone.error_after), abs(zone.error_before) + 0.005)

    def test_threshold_manual_and_independent_modes(self) -> None:
        for threshold, expected_count in ((6, 16), (10, 8)):
            analysis = analyze_gray_range(self.dataset, threshold)
            result = optimize_gamma_lut(
                self.dataset,
                self.region,
                analysis,
                config=GammaOptimizationConfig(threshold=threshold),
            )
            self.assertEqual(len(result.selected_zones), expected_count)
            self.assertNotEqual(result.health.status, "FAIL")
        analysis = analyze_gray_range(self.dataset, 8)
        manual = optimize_gamma_lut(
            self.dataset,
            self.region,
            analysis,
            config=GammaOptimizationConfig(
                threshold=8,
                range_mode="manual",
                manual_start_zone=2,
                manual_end_zone=7,
                rgb_mode="independent",
            ),
        )
        self.assertEqual(manual.selected_zones, (2, 3, 4, 5, 6, 7))
        self.assertEqual(manual.rgb_mode, "independent")
        self.assertEqual(manual.health.status, "PASS")


class GammaDesktopSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.withdraw()
        self.app = GammaOptimizationApp(self.root)

    def tearDown(self) -> None:
        if hasattr(self, "root"):
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def test_complete_gamma_workflow_and_save_as_default(self) -> None:
        self.app.load_csv(str(SOURCE / "gray_summary.csv"))
        self.app.load_xml(str(SOURCE / "gamma15_ipe_v2.xml"))
        self.assertEqual(self.app.analysis.effective_count if self.app.analysis else None, 12)
        self.assertEqual(self.app.selected_region.length if self.app.selected_region else None, 257)
        self.app.run_optimization()
        self.assertIsNotNone(self.app.result)
        assert self.app.result is not None
        self.assertEqual(self.app.result.health.status, "PASS")
        self.assertEqual(len(self.app.zone_tree.get_children()), 19)
        with mock.patch("matrixcorrect.gamma_app.filedialog.asksaveasfilename", return_value="") as dialog:
            self.app.save_xml()
        kwargs = dialog.call_args.kwargs
        self.assertEqual(kwargs["initialfile"], "gamma15_ipe_v2_optimized.xml")
        self.assertEqual(kwargs["defaultextension"], ".xml")
        self.assertEqual(kwargs["filetypes"], [("XML 文件", "*.xml")])


if __name__ == "__main__":
    unittest.main()
