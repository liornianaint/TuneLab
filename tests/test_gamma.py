from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
import re
from pathlib import Path
from unittest import mock

from matrixcorrect.gamma_models import GammaOptimizationConfig
from matrixcorrect.gamma_app import GammaOptimizationApp
from matrixcorrect.gamma_history import load_gamma_history, record_gamma_result, save_gamma_history
from matrixcorrect.gamma_optimizer import optimize_gamma_lut
from matrixcorrect.gamma_report import save_gamma_html_report
from matrixcorrect.gamma_settings import load_gamma_settings, save_gamma_settings
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

    def test_parser_and_optimizer_support_non_257_lut_formats(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        length = 65
        maximum = 255
        values = tuple(round(maximum * (index / (length - 1)) ** 0.45) for index in range(length))
        text = text.replace('length="257"', f'length="{length}"')
        for channel in ("r", "g", "b"):
            text = re.sub(
                rf"(<channel_{channel}>).*?(</channel_{channel}>)",
                rf"\g<1>{' '.join(map(str, values))}\g<2>",
                text,
                flags=re.DOTALL,
            )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gamma_65_8bit.xml"
            path.write_text(text, encoding="utf-8")
            document = QualcommGammaDocument.load(path)
            region = document.regions[0]
            self.assertEqual((region.length, region.maximum), (65, 255))
            result = optimize_gamma_lut(
                parse_gray_csv(SOURCE / "gray_summary.csv"),
                region,
                analyze_gray_range(parse_gray_csv(SOURCE / "gray_summary.csv"), 8),
                config=GammaOptimizationConfig(target_step_count=14),
            )
            self.assertEqual(len(result.after_g), 65)
            self.assertEqual((result.after_g[0], result.after_g[-1]), (0, 255))
            self.assertTrue(all(following >= current for current, following in zip(result.after_g, result.after_g[1:])))


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

    def test_manual_target_steps_increase_12_to_14_without_regression(self) -> None:
        analysis = analyze_gray_range(self.dataset, 8)
        result = optimize_gamma_lut(
            self.dataset,
            self.region,
            analysis,
            config=GammaOptimizationConfig(target_gamma=1.0, target_step_count=14),
        )
        self.assertEqual(result.metrics.distinguishable_before, 12)
        self.assertEqual(result.metrics.distinguishable_target, 14)
        self.assertGreaterEqual(result.metrics.distinguishable_after, 14)
        self.assertEqual(result.health.status, "PASS")
        self.assertTrue(result.health.monotonic)
        self.assertEqual(result.after_g[-1], 1023)

    def test_gamma_lift_factor_one_is_nominal_and_larger_target_is_brighter(self) -> None:
        analysis = analyze_gray_range(self.dataset, 8)
        nominal = optimize_gamma_lut(
            self.dataset,
            self.region,
            analysis,
            config=GammaOptimizationConfig(target_gamma=1.0, target_step_count=14),
        )
        brighter = optimize_gamma_lut(
            self.dataset,
            self.region,
            analysis,
            config=GammaOptimizationConfig(target_gamma=1.2, target_step_count=14),
        )
        midpoint = self.region.length // 2
        self.assertGreater(brighter.target_lut[midpoint], nominal.target_lut[midpoint])
        self.assertEqual(brighter.maximum_value, 1023)
        self.assertEqual(brighter.after_g[-1], 1023)
        self.assertGreaterEqual(brighter.metrics.distinguishable_after, brighter.metrics.distinguishable_before)


class GammaEngineeringExperienceTests(unittest.TestCase):
    def test_settings_history_and_html_report_round_trip(self) -> None:
        dataset = parse_gray_csv(SOURCE / "gray_summary.csv")
        document = QualcommGammaDocument.load(SOURCE / "gamma15_ipe_v2.xml")
        region = document.regions[0]
        result = optimize_gamma_lut(
            dataset,
            region,
            analyze_gray_range(dataset, 8),
            config=GammaOptimizationConfig(target_gamma=1.0, target_step_count=14),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings_path = root / "gamma_settings.json"
            history_path = root / "gamma_history.json"
            report_path = root / "gamma_report.html"
            config = GammaOptimizationConfig(target_gamma=1.2, target_step_count=14)
            save_gamma_settings(config, settings_path)
            self.assertEqual(load_gamma_settings(settings_path), config)
            record = record_gamma_result(
                result,
                dataset_name=dataset.source_path.name,
                xml_name=document.source_path.name,
                region_label=region.path_label(),
                xml_diff=document.diff_with_luts(region.index, result.after_r, result.after_g, result.after_b),
            )
            save_gamma_history([record], history_path)
            loaded = load_gamma_history(history_path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].after_steps, 14)
            save_gamma_html_report(report_path, dataset, region, result)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("Recognizable steps", report)
            self.assertIn("Diagnosis & Explainability", report)


class GammaDesktopSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.withdraw()
        self.settings_patcher = mock.patch(
            "matrixcorrect.gamma_app.load_gamma_settings",
            return_value=GammaOptimizationConfig(),
        )
        self.history_patcher = mock.patch("matrixcorrect.gamma_app.load_gamma_history", return_value=[])
        self.save_settings_patcher = mock.patch("matrixcorrect.gamma_app.save_gamma_settings")
        self.save_history_patcher = mock.patch("matrixcorrect.gamma_app.save_gamma_history")
        self.settings_patcher.start()
        self.history_patcher.start()
        self.save_settings_patcher.start()
        self.save_history_patcher.start()
        self.app = GammaOptimizationApp(self.root)

    def tearDown(self) -> None:
        if hasattr(self, "root"):
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        for patcher_name in (
            "settings_patcher",
            "history_patcher",
            "save_settings_patcher",
            "save_history_patcher",
        ):
            if hasattr(self, patcher_name):
                getattr(self, patcher_name).stop()

    def test_complete_gamma_workflow_and_source_overwrite(self) -> None:
        self.app.load_csv(str(SOURCE / "gray_summary.csv"))
        self.app.load_xml(str(SOURCE / "gamma15_ipe_v2.xml"))
        self.assertEqual(self.app.target_gamma_var.get(), "1")
        self.assertEqual(self.app.analysis.effective_count if self.app.analysis else None, 12)
        self.assertEqual(self.app.selected_region.length if self.app.selected_region else None, 257)
        self.assertEqual(self.app.kpi_vars[6].get(), "257 / 1023")
        self.app.target_steps_var.set("14")
        self.app.run_optimization()
        self.assertIsNotNone(self.app.result)
        assert self.app.result is not None
        self.assertEqual(self.app.result.health.status, "PASS")
        self.assertGreaterEqual(self.app.result.metrics.distinguishable_after, 14)
        self.assertEqual(len(self.app.zone_tree.get_children()), 19)
        self.assertEqual(len(self.app.pair_tree.get_children()), 18)
        with mock.patch("matrixcorrect.gamma_app.messagebox.askyesno", return_value=False) as confirmation, mock.patch.object(self.app.document, "save_with_luts") as save:
            self.app.save_xml()
        self.assertIn(str(self.app.document.source_path), confirmation.call_args.args[1])
        save.assert_not_called()

    def test_gamma_window_has_cc_style_menus_and_engineering_tabs(self) -> None:
        file_labels = [
            "" if self.app.file_menu.type(index) == "separator" else self.app.file_menu.entrycget(index, "label")
            for index in range(self.app.file_menu.index("end") + 1)
        ]
        self.assertEqual(
            file_labels,
            [
                "打开 Imatest Gray CSV...",
                "打开 Qualcomm Gamma XML...",
                "保存 Gamma XML...",
                "导出 Gamma 工程报告...",
                "",
                "关闭",
            ],
        )
        self.assertEqual(
            [self.app.config_menu.entrycget(index, "label") for index in range(self.app.config_menu.index("end") + 1)],
            ["导入 Gamma 配置...", "导出 Gamma 配置..."],
        )
        self.assertEqual(
            [self.app.notebook.tab(tab, "text").strip() for tab in self.app.notebook.tabs()],
            ["曲线对比", "工程统计", "诊断与解释", "History / XML Diff"],
        )
        self.assertTrue(bool(self.app.zone_tree.column("local_after", "stretch")))


if __name__ == "__main__":
    unittest.main()
