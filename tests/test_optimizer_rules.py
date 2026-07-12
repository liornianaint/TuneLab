from __future__ import annotations

import builtins
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from matrixcorrect.imatest import parse_imatest_csv
from matrixcorrect.models import OptimizationConfig, safe_improvement_percent
from matrixcorrect.optimizer import (
    NEUTRAL_PATCHES,
    NEUTRAL_PATCH_REGRESSION_LIMIT,
    _fit_target,
    compose_correction_matrix,
    optimize_ccm,
    pass_rate_counts,
    saturation_target_chroma,
)
from matrixcorrect.qualcomm_xml import QualcommCCDocument
from matrixcorrect.report import save_analysis_pdf


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"


class OptimizerRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = QualcommCCDocument.load(SOURCE / "cc13_ipe_v2.xml")

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
        dataset = parse_imatest_csv(SOURCE / "D65_normal_summary.csv")
        region, _ = self.document.find_region_for_cct(6500)
        for composition in ("pre", "post_transposed"):
            result = optimize_ccm(dataset, region.matrix, composition=composition, max_blend=0.6)
            reconstructed = compose_correction_matrix(result.correction_matrix, result.original_matrix, composition)
            for actual_row, expected_row in zip(reconstructed, result.optimized_matrix):
                for actual, expected in zip(actual_row, expected_row):
                    self.assertAlmostEqual(actual, expected, places=8)

    def test_neutral_19_to_24_are_protected_in_every_colorchecker_case(self) -> None:
        for path in sorted(SOURCE.glob("*_summary.csv")):
            if path.name == "gray_summary.csv":
                continue
            with self.subTest(path=path.name):
                dataset = parse_imatest_csv(path)
                region, _ = self.document.find_region_for_cct(dataset.inferred_cct or 6500)
                result = optimize_ccm(dataset, region.matrix)
                neutral = [patch for patch in result.patch_results if patch.zone in NEUTRAL_PATCHES]
                self.assertEqual({patch.zone for patch in neutral}, set(range(19, 25)))
                self.assertLessEqual(max(patch.regression for patch in neutral), NEUTRAL_PATCH_REGRESSION_LIMIT + 1e-9)

    def test_all_source_colorchecker_csv_files_are_openable(self) -> None:
        names = []
        for path in sorted(SOURCE.glob("*_summary.csv")):
            if path.name == "gray_summary.csv":
                continue
            dataset = parse_imatest_csv(path)
            self.assertEqual(len(dataset.patches), 24, path.name)
            self.assertIsNotNone(dataset.inferred_cct, path.name)
            names.append(path.name)
        self.assertEqual(len(names), 8)

    def test_a_light_focus_13_14_finds_a_protected_engineering_candidate(self) -> None:
        dataset = parse_imatest_csv(SOURCE / "A_summary.csv")
        region, _ = self.document.find_region_for_cct(dataset.inferred_cct or 2850)
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
                self.assertTrue(all(patch.delta_e_after < patch.delta_e_before for patch in focus.values()))
                self.assertTrue(all(patch.regression_status != "FAIL" for patch in result.patch_results))
                self.assertTrue(
                    all(after >= before for before, after in zip(result.pass_rates.before_counts, result.pass_rates.after_counts))
                )
                for row in result.optimized_matrix:
                    self.assertAlmostEqual(sum(row), 1.0, places=8)
                    self.assertGreaterEqual(min(row), -3.0)
                    self.assertLessEqual(max(row), 3.0)

    def test_reportlab_is_optional_until_pdf_export(self) -> None:
        dataset = parse_imatest_csv(SOURCE / "D65_normal_summary.csv")
        region, _ = self.document.find_region_for_cct(6500)
        result = optimize_ccm(dataset, region.matrix)
        real_import = builtins.__import__

        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "reportlab" or name.startswith("reportlab."):
                raise ImportError("simulated missing optional dependency")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("builtins.__import__", side_effect=blocked_import):
                with self.assertRaisesRegex(RuntimeError, "导出 PDF 需要 reportlab"):
                    save_analysis_pdf(Path(temporary) / "analysis.pdf", dataset, result)


if __name__ == "__main__":
    unittest.main()
