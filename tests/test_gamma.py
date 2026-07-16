from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
import re
import math
from pathlib import Path
from tkinter import ttk
from unittest import mock

from tunelab.gamma.models import (
    GammaOptimizationConfig,
    GrayDataset,
    GrayZone,
)
from tunelab.gamma.ui import GammaWorkspace
from tunelab.gamma.history import load_gamma_history, record_gamma_result, save_gamma_history
from tunelab.gamma.optimizer import (
    _curve_health,
    minimum_continuity_gap,
    optimize_gamma_lut,
)
from tunelab.gamma.reporting import save_gamma_html_report
from tunelab.gamma.settings import load_gamma_settings, save_gamma_settings
from tunelab.gamma.imatest import analyze_gray_range, parse_gray_csv, select_fit_zones
from tunelab.gamma.qualcomm_xml import QualcommGammaDocument


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


class CapturedGammaContinuityRegressionTests(unittest.TestCase):
    @staticmethod
    def _captured_dataset() -> GrayDataset:
        # The July 13 capture had one 12-stage run plus two isolated dark pairs.
        # Keep the measured pixel sequence here so this failure cannot regress
        # even when local source/ analysis files are unavailable.
        pixels = (
            242.3, 227.5, 212.0, 198.7, 183.6, 169.4, 161.1, 149.1,
            136.4, 124.9, 116.7, 106.1, 96.3, 89.7, 86.3, 83.6,
            79.3, 66.9, 50.0, 43.4,
        )
        normalized = (
            .9502, .8921, .8316, .7793, .7198, .6642, .6319, .5849,
            .5351, .4897, .4577, .4163, .3777, .3517, .3385, .3277,
            .3110, .2625, .1960, .1702,
        )
        densities = (
            .0222, .0496, .0801, .1083, .1428, .1777, .1993, .2329,
            .2716, .3100, .3394, .3806, .4229, .4538, .4705, .4845,
            .5072, .5808, .7077, .7689,
        )
        rgb_means = (
            (245.7, 241.2, 238.4), (231.0, 225.0, 225.0),
            (214.0, 209.6, 210.5), (200.3, 195.6, 198.5),
            (183.8, 180.4, 184.0), (169.4, 166.6, 170.8),
            (158.7, 157.1, 160.4), (147.3, 145.6, 149.6),
            (135.3, 133.9, 138.2), (124.1, 123.0, 127.8),
            (115.4, 114.8, 120.7), (104.0, 104.4, 110.7),
            (93.8, 94.0, 101.9), (85.4, 86.8, 95.0),
            (79.6, 80.9, 90.2), (73.1, 73.9, 83.8),
            (66.6, 66.3, 76.8), (56.4, 55.9, 65.5),
            (47.5, 46.6, 55.3), (43.0, 42.0, 50.0),
        )
        zones = tuple(
            GrayZone(
                zone=index,
                pixel=pixel,
                pixel_normalized=pixel_normalized,
                log_exposure=-0.05 - 0.10 * (index - 1),
                density=density,
                mean_r=rgb[0],
                mean_g=rgb[1],
                mean_b=rgb[2],
            )
            for index, (pixel, pixel_normalized, density, rgb) in enumerate(
                zip(pixels, normalized, densities, rgb_means),
                1,
            )
        )
        return GrayDataset(Path("gray_after_summary.csv"), zones)

    def test_disconnected_capture_reaches_even_contiguous_17_and_18_stage_runs(self) -> None:
        dataset = self._captured_dataset()
        analysis = analyze_gray_range(dataset, 8.0)
        region = QualcommGammaDocument.load(SOURCE / "gamma15_ipe_v2.xml").regions[0]
        self.assertEqual(analysis.effective_count, 12)
        self.assertEqual(analysis.runs, (tuple(range(1, 13)), (17, 18)))

        for target_steps in (17, 18):
            for maximum_adjustment in (0.70, 1.0):
                with self.subTest(target=target_steps, strength=maximum_adjustment):
                    result = optimize_gamma_lut(
                        dataset,
                        region,
                        analysis,
                        config=GammaOptimizationConfig(
                            target_step_count=target_steps,
                            maximum_adjustment=maximum_adjustment,
                            highlight_protection=1.0,
                            shadow_protection=1.0,
                        ),
                    )
                    required = [
                        pair for pair in result.pair_results if pair.target_required
                    ]
                    gaps = [pair.delta_after for pair in required]
                    self.assertEqual(result.requested_step_count, target_steps)
                    self.assertEqual(result.metrics.distinguishable_target, target_steps)
                    self.assertEqual(len(required), target_steps)
                    self.assertEqual(result.metrics.distinguishable_after, target_steps)
                    self.assertEqual(result.applied_strength, maximum_adjustment)
                    self.assertEqual(result.health.status, "PASS")
                    self.assertLessEqual(
                        result.health.maximum_jump,
                        18,
                        "17/18 阶候选不应重新形成 19-code 的局部增益峰。",
                    )
                    maximum_slope_change = max(
                        abs(
                            (curve[index + 1] - curve[index])
                            - (curve[index] - curve[index - 1])
                        )
                        for curve in (
                            result.after_r,
                            result.after_g,
                            result.after_b,
                        )
                        for index in range(1, len(curve) - 1)
                    )
                    self.assertLessEqual(maximum_slope_change, 3)
                    if target_steps == 17 or maximum_adjustment == 1.0:
                        self.assertLessEqual(maximum_slope_change, 2)
                    self.assertNotEqual(result.after_g, result.before_g)
                    self.assertGreaterEqual(
                        min(gaps),
                        minimum_continuity_gap(8.0),
                    )
                    self.assertLessEqual(max(gaps) - min(gaps), 3.0)
                    self.assertFalse(
                        any("最高安全结果" in warning for warning in result.warnings)
                    )
                    self.assertTrue(
                        any("高阶均匀化已启用" in line for line in result.explainability)
                    )
                    for check_name in (
                        "LUT Shape Preservation",
                        "LUT Natural Smoothness",
                        "LUT Slope Continuity",
                    ):
                        self.assertEqual(
                            next(
                                check.status
                                for check in result.health.checks
                                if check.name == check_name
                            ),
                            "PASS",
                        )
                    for curve in (result.after_r, result.after_g, result.after_b):
                        self.assertTrue(
                            all(
                                following > current
                                for current, following in zip(curve, curve[1:])
                            ),
                            "平滑 Gamma LUT 不应包含量化平台。",
                        )

    def test_curve_health_rejects_a_plateau_followed_by_a_jump(self) -> None:
        before = tuple(round(1023 * index / 256.0) for index in range(257))
        after = list(before)
        for index in range(40, 50):
            after[index] = after[39]
        health = _curve_health(
            (before, before, before),
            (tuple(after), tuple(after), tuple(after)),
            0.0,
            1023,
        )
        check = next(
            item for item in health.checks if item.name == "LUT Slope Continuity"
        )
        self.assertEqual(check.status, "FAIL")
        plateau_check = next(
            item for item in health.checks if item.name == "LUT Plateaus"
        )
        self.assertEqual(plateau_check.status, "FAIL")
        self.assertEqual(health.status, "FAIL")

    def test_curve_health_rejects_repeated_brightness_acceleration(self) -> None:
        before = tuple(round(1023 * index / 256.0) for index in range(257))
        steps = [3, 5] * 127 + [3, 4]
        after = [0]
        for step in steps:
            after.append(after[-1] + step)
        health = _curve_health(
            (before, before, before),
            (tuple(after), tuple(after), tuple(after)),
            0.0,
            1023,
            enforce_naturalness=True,
        )
        self.assertEqual(
            next(
                check.status
                for check in health.checks
                if check.name == "LUT Slope Continuity"
            ),
            "PASS",
        )
        self.assertEqual(
            next(
                check.status
                for check in health.checks
                if check.name == "LUT Natural Smoothness"
            ),
            "FAIL",
        )
        self.assertEqual(health.status, "FAIL")

    def test_curve_health_rejects_a_broad_s_curve_even_when_rms_is_smooth(self) -> None:
        before = tuple(1023.0 * index / 256.0 for index in range(257))
        after = list(before)
        for index in range(5, 76):
            after[index] -= 40.0 * math.sin(math.pi * (index - 5) / 70.0) ** 2
        health = _curve_health(
            (before, before, before),
            (tuple(after), tuple(after), tuple(after)),
            0.0,
            1023,
            enforce_naturalness=True,
        )
        self.assertTrue(
            all(following > current for current, following in zip(after, after[1:]))
        )
        self.assertEqual(
            next(
                check.status
                for check in health.checks
                if check.name == "LUT Natural Smoothness"
            ),
            "PASS",
        )
        self.assertEqual(
            next(
                check.status
                for check in health.checks
                if check.name == "LUT Shape Preservation"
            ),
            "FAIL",
        )
        self.assertEqual(health.status, "FAIL")

    def test_explicit_15_stage_target_is_exact_smooth_and_even(self) -> None:
        dataset = self._captured_dataset()
        document = QualcommGammaDocument.load(SOURCE / "gamma15_ipe_v2.xml")
        result = optimize_gamma_lut(
            dataset,
            document.regions[0],
            analyze_gray_range(dataset, 8.0),
            config=GammaOptimizationConfig(
                target_step_count=15,
                maximum_adjustment=1.0,
                highlight_protection=1.0,
                shadow_protection=1.0,
            ),
        )
        self.assertEqual(result.requested_step_count, 15)
        self.assertEqual(result.metrics.distinguishable_target, 15)
        self.assertEqual(result.metrics.distinguishable_after, 15)
        self.assertEqual(result.applied_strength, 1.0)
        self.assertNotEqual(result.after_g, result.before_g)
        required = [pair.delta_after for pair in result.pair_results if pair.target_required]
        self.assertGreaterEqual(min(required), minimum_continuity_gap(8.0))
        self.assertLessEqual(max(required) - min(required), 3.2)
        self.assertFalse(any("最高安全结果" in warning for warning in result.warnings))
        shape_check = next(
            check
            for check in result.health.checks
            if check.name == "LUT Shape Preservation"
        )
        self.assertEqual(shape_check.status, "PASS")
        natural_check = next(
            check
            for check in result.health.checks
            if check.name == "LUT Natural Smoothness"
        )
        self.assertEqual(natural_check.status, "PASS")
        for curve in (result.after_r, result.after_g, result.after_b):
            steps = [following - current for current, following in zip(curve, curve[1:])]
            self.assertGreaterEqual(min(steps), 1)


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
            "tunelab.gamma.ui.load_gamma_settings",
            return_value=GammaOptimizationConfig(),
        )
        self.history_patcher = mock.patch("tunelab.gamma.ui.load_gamma_history", return_value=[])
        self.save_settings_patcher = mock.patch("tunelab.gamma.ui.save_gamma_settings")
        self.save_history_patcher = mock.patch("tunelab.gamma.ui.save_gamma_history")
        self.settings_patcher.start()
        self.history_patcher.start()
        self.save_settings_patcher.start()
        self.save_history_patcher.start()
        self.app = GammaWorkspace(self.root)

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
        self.assertEqual(self.app.result.metrics.distinguishable_after, 14)
        self.assertEqual(str(self.app.save_button.cget("state")), "normal")
        self.assertFalse(self.app.lut_plot.show_markers)
        self.assertTrue(
            all(len(points) == self.app.result.lut_length for _name, points, _color, _dashed in self.app.lut_plot.series)
        )
        self.assertIn("平台 0", self.app.lut_plot.title)
        self.assertIn("自然平滑 PASS", self.app.lut_plot.title)
        self.assertFalse(
            any(name.startswith("Target") for name, _points, _color, _dashed in self.app.lut_plot.series)
        )
        self.assertEqual(self.app.engineering_tree.item("gamma-check-continuity", "values")[1], "PASS")
        self.assertEqual(len(self.app.zone_tree.get_children()), 19)
        self.assertEqual(len(self.app.pair_tree.get_children()), 18)
        with mock.patch("tunelab.gamma.ui.messagebox.askyesno", return_value=False) as confirmation, mock.patch.object(self.app.document, "save_with_luts") as save:
            self.app.save_xml()
        self.assertIn(str(self.app.document.source_path), confirmation.call_args.args[1])
        save.assert_not_called()

    def test_unmet_target_cannot_be_written_as_isolated_partial_stages(self) -> None:
        self.app.load_csv(str(SOURCE / "gray_summary.csv"))
        self.app.load_xml(str(SOURCE / "gamma15_ipe_v2.xml"))
        self.app.target_steps_var.set("18")
        self.app.strength_var.set(0.0)
        self.app.run_optimization()
        self.assertIsNotNone(self.app.result)
        assert self.app.result is not None
        self.assertLess(
            self.app.result.metrics.distinguishable_after,
            self.app.result.metrics.distinguishable_target,
        )
        self.assertEqual(str(self.app.save_button.cget("state")), "disabled")
        self.assertEqual(self.app.engineering_tree.item("gamma-check-continuity", "values")[1], "FAIL")
        with mock.patch("tunelab.gamma.ui.messagebox.showerror") as blocked, mock.patch.object(
            self.app.document,
            "save_with_luts",
        ) as save:
            self.app.save_xml()
        blocked.assert_called_once()
        save.assert_not_called()

    def test_requested_15_shows_and_allows_the_exact_smooth_lut(self) -> None:
        self.app.load_xml(str(SOURCE / "gamma15_ipe_v2.xml"))
        self.app.dataset = CapturedGammaContinuityRegressionTests._captured_dataset()
        self.app.analysis = analyze_gray_range(self.app.dataset, 8.0)
        self.app.target_steps_var.set("15")
        self.app.strength_var.set(100.0)
        self.app.highlight_var.set(100.0)
        self.app.shadow_var.set(100.0)
        self.app.run_optimization()

        self.assertIsNotNone(self.app.result)
        assert self.app.result is not None
        self.assertEqual(self.app.result.requested_step_count, 15)
        self.assertEqual(self.app.result.metrics.distinguishable_after, 15)
        self.assertGreater(self.app.result.applied_strength, 0.0)
        self.assertNotEqual(self.app.result.after_g, self.app.result.before_g)
        self.assertEqual(
            [name for name, _points, _color, _dashed in self.app.lut_plot.series],
            ["Before G", "After G", "After R/B"],
        )
        self.assertIn("After RGB", self.app.lut_plot.title)
        self.assertIn("目标 15", self.app.status_var.get())
        self.assertEqual(self.app.kpi_vars[2].get(), "15")
        self.assertEqual(str(self.app.save_button.cget("state")), "normal")

    def test_gamma_window_has_cc_style_menus_and_engineering_tabs(self) -> None:
        file_labels = [
            "" if self.app.file_menu.type(index) == "separator" else self.app.file_menu.entrycget(index, "label")
            for index in range(self.app.file_menu.index("end") + 1)
        ]
        self.assertEqual(
            file_labels,
            [
                "打开 Gamma CSV...",
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
        self.assertEqual(str(self.app.region_match_button.cget("text")), "自动匹配 Region")
        self.assertEqual(str(self.app.region_match_button.cget("style")), "RegionMatch.TButton")
        self.assertEqual(str(self.app.optimize_button.cget("text")), "3  自动优化")
        self.assertEqual(str(self.app.optimize_button.cget("style")), "Primary.TButton")
        style = ttk.Style(self.root)
        self.assertEqual(
            style.lookup("Primary.TButton", "background", ("active",)),
            "#1D4ED8",
        )
        self.assertEqual(
            style.lookup("Primary.TButton", "foreground", ("active",)),
            "white",
        )
        self.assertEqual(
            [self.app.notebook.tab(tab, "text").strip() for tab in self.app.notebook.tabs()],
            ["曲线对比", "工程统计", "诊断与解释", "History / XML Diff"],
        )
        self.assertTrue(bool(self.app.zone_tree.column("local_after", "stretch")))


if __name__ == "__main__":
    unittest.main()
