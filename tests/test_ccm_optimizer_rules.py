from __future__ import annotations

import builtins
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tunelab.ccm.models import OptimizationConfig, safe_improvement_percent
from tunelab.ccm.optimizer import (
    NEUTRAL_PATCHES,
    NEUTRAL_PATCH_REGRESSION_LIMIT,
    _fit_target,
    compose_correction_matrix,
    optimize_colorchecker_target_match,
    optimize_ccm,
    pass_rate_counts,
    saturation_target_chroma,
)
from tunelab.ccm.qualcomm_xml import QualcommCCDocument
from tunelab.ccm.reporting import save_analysis_pdf
from tunelab.colorchecker.engine import build_comparison_dataset, detect_colorchecker, standard_colorchecker_reference


from .materials import CC_XML, SOURCES, d65_dataset


SOURCE = SOURCES
CURRENT_COLORCHECKER_IMAGES = (
    "A.jpg",
    "CWF.jpg",
    "D65_300lux.jpg",
    "D65_30lux.jpg",
    "D65_normal.jpg",
    "TL84_700lux.jpg",
    "TL84_70lux.jpg",
    "TL84_normal.jpg",
)


class OptimizerRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = QualcommCCDocument.load(CC_XML)

    def test_pass_rate_includes_exact_2_3_5_10_boundaries(self) -> None:
        self.assertEqual(pass_rate_counts((2.0, 3.0, 5.0, 10.0)), (1, 2, 3, 4))
        self.assertEqual(pass_rate_counts((2.000001, 3.000001, 5.000001, 10.000001)), (0, 1, 2, 3))

    def test_tiny_before_delta_e_uses_na_instead_of_unstable_percent(self) -> None:
        self.assertIsNone(safe_improvement_percent(0.05, 0.01))
        self.assertEqual(safe_improvement_percent(0.10, 0.05), 50.0)

    def test_saturation_factor_is_applied_once(self) -> None:
        self.assertAlmostEqual(saturation_target_chroma(20.0, 0.97), 19.4)
        source = (0.20, 0.20, 0.20)
        ideal = (0.55, 0.18, 0.08)
        full = _fit_target(source, ideal, 1.0)
        half = _fit_target(source, ideal, 0.5)
        neutral = sum(value * weight for value, weight in zip(source, (0.2126729, 0.7151522, 0.0721750)))
        for full_value, half_value in zip(full, half):
            self.assertAlmostEqual(half_value - neutral, 0.5 * (full_value - neutral), places=12)

    def test_delta_correction_exactly_reconstructs_after_matrix(self) -> None:
        dataset = d65_dataset()
        region, _ = self.document.find_region_for_cct(6500)
        for composition in ("pre", "post_transposed"):
            result = optimize_ccm(dataset, region.matrix, composition=composition, max_blend=0.6)
            reconstructed = compose_correction_matrix(result.correction_matrix, result.original_matrix, composition)
            for actual_row, expected_row in zip(reconstructed, result.optimized_matrix):
                for actual, expected in zip(actual_row, expected_row):
                    self.assertAlmostEqual(actual, expected, places=8)

    def test_neutral_19_to_24_are_protected_in_current_colorchecker_cases(self) -> None:
        reference = standard_colorchecker_reference()
        for name in CURRENT_COLORCHECKER_IMAGES:
            path = SOURCE / name
            with self.subTest(path=path.name):
                dataset = build_comparison_dataset(detect_colorchecker(path), reference)
                region, _ = self.document.find_region_for_cct(dataset.inferred_cct or 6500)
                result = optimize_ccm(dataset, region.matrix)
                neutral = [patch for patch in result.patch_results if patch.zone in NEUTRAL_PATCHES]
                self.assertEqual({patch.zone for patch in neutral}, set(range(19, 25)))
                self.assertLessEqual(max(patch.regression for patch in neutral), NEUTRAL_PATCH_REGRESSION_LIMIT + 1e-9)

    def test_all_current_source_colorchecker_images_are_openable(self) -> None:
        names = []
        for name in CURRENT_COLORCHECKER_IMAGES:
            path = SOURCE / name
            detection = detect_colorchecker(path)
            self.assertEqual(len(detection.patches), 24, path.name)
            names.append(path.name)
        self.assertEqual(names, list(CURRENT_COLORCHECKER_IMAGES))

    def test_d65_focus_13_14_finds_a_protected_engineering_candidate(self) -> None:
        dataset = d65_dataset()
        region, _ = self.document.find_region_for_cct(dataset.inferred_cct or 6500)
        for maximum_strength in (0.8, 1.0):
            with self.subTest(maximum_strength=maximum_strength):
                result = optimize_ccm(
                    dataset,
                    region.matrix,
                    config=OptimizationConfig(
                        focus_patches=(13, 14),
                        max_blend=maximum_strength,
                    ),
                )
                focus = {patch.zone: patch for patch in result.patch_results if patch.zone in (13, 14)}
                self.assertEqual(result.matrix_health.status, "PASS")
                self.assertLess(result.mean_after, result.mean_before)
                self.assertLess(
                    sum(patch.delta_e_after for patch in focus.values()),
                    sum(patch.delta_e_before for patch in focus.values()),
                )
                self.assertTrue(all(patch.regression_status != "FAIL" for patch in result.patch_results))
                self.assertTrue(
                    all(after >= before for before, after in zip(result.pass_rates.before_counts, result.pass_rates.after_counts))
                )
                for row in result.optimized_matrix:
                    self.assertAlmostEqual(sum(row), 1.0, places=8)
                    self.assertGreaterEqual(min(row), -3.0)
                    self.assertLessEqual(max(row), 3.0)

    def test_image_target_match_uses_only_equal_weight_24_patch_delta_e(self) -> None:
        dataset = d65_dataset()
        identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        baseline_config = OptimizationConfig()
        unrelated_controls_changed = OptimizationConfig(
            strategy="aggressive",
            regularization=100.0,
            max_blend=0.20,
            saturation_factor=0.50,
            focus_patches=(1, 2, 3, 4),
            focus_weight=50.0,
            max_patch_regression=0.01,
            max_regressed_patches=0,
        )

        baseline = optimize_colorchecker_target_match(
            dataset,
            identity,
            config=baseline_config,
        )
        changed = optimize_colorchecker_target_match(
            dataset,
            identity,
            config=unrelated_controls_changed,
        )

        self.assertTrue(baseline.search_method.startswith("colorchecker-target-match"))
        self.assertLess(baseline.mean_after, baseline.mean_before)
        self.assertEqual(len(baseline.patch_results), 24)
        self.assertTrue(all(patch.priority_weight == 1.0 for patch in baseline.patch_results))
        self.assertFalse(any(patch.regression_status == "FAIL" for patch in baseline.patch_results))
        self.assertEqual(baseline.optimized_matrix, changed.optimized_matrix)
        self.assertAlmostEqual(baseline.mean_after, changed.mean_after, places=12)
        for row in baseline.optimized_matrix:
            self.assertAlmostEqual(sum(row), 1.0, places=8)

    def test_missing_default_reportlab_dependency_has_actionable_error(self) -> None:
        dataset = d65_dataset()
        region, _ = self.document.find_region_for_cct(6500)
        result = optimize_ccm(dataset, region.matrix)
        real_import = builtins.__import__

        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "reportlab" or name.startswith("reportlab."):
                raise ImportError("simulated missing optional dependency")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("builtins.__import__", side_effect=blocked_import):
                with self.assertRaisesRegex(RuntimeError, "默认 PDF 依赖 reportlab 未安装"):
                    save_analysis_pdf(Path(temporary) / "analysis.pdf", dataset, result)


if __name__ == "__main__":
    unittest.main()
