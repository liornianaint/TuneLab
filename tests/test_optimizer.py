from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from matrixcorrect.color import row_sums
from matrixcorrect.imatest import parse_imatest_csv
from matrixcorrect.optimizer import optimize_ccm
from matrixcorrect.qualcomm_xml import QualcommCCDocument
from matrixcorrect.report import save_analysis_csv


ROOT = Path(__file__).resolve().parents[1]


class OptimizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = parse_imatest_csv(ROOT / "Source" / "D65_normal_summary.csv")
        cls.document = QualcommCCDocument.load(ROOT / "Source" / "cc13_ipe_v2.xml")
        cls.region, _ = cls.document.find_region_for_cct(6500)

    def test_sample_improves_and_preserves_neutral_axis(self) -> None:
        result = optimize_ccm(self.dataset, self.region.matrix)
        self.assertLess(result.mean_after, result.mean_before)
        self.assertGreater(result.mean_improvement_percent, 5.0)
        self.assertGreaterEqual(result.improved_count, 10)
        for total in row_sums(result.correction_matrix):
            self.assertAlmostEqual(total, 1.0, places=8)
        for total in row_sums(result.optimized_matrix):
            self.assertAlmostEqual(total, 1.0, places=5)

    def test_legacy_composition_is_available(self) -> None:
        result = optimize_ccm(self.dataset, self.region.matrix, composition="post_transposed", max_blend=0.6)
        self.assertEqual(result.composition, "post_transposed")
        self.assertLess(result.mean_after, result.mean_before)

    def test_report_and_xml_round_trip(self) -> None:
        result = optimize_ccm(self.dataset, self.region.matrix)
        with tempfile.TemporaryDirectory() as temp_dir:
            xml_path = Path(temp_dir) / "cc13_optimized.xml"
            report_path = Path(temp_dir) / "analysis.csv"
            self.document.save_with_matrix(xml_path, self.region.index, result.optimized_matrix)
            save_analysis_csv(report_path, self.dataset, result, region_label=self.region.path_label())
            self.assertTrue(xml_path.exists())
            self.assertIn("改善百分比", report_path.read_text(encoding="utf-8-sig"))
            reloaded = QualcommCCDocument.load(xml_path)
            for actual_row, expected_row in zip(reloaded.regions[self.region.index].matrix, result.optimized_matrix):
                for actual, expected in zip(actual_row, expected_row):
                    self.assertAlmostEqual(actual, expected, places=6)


if __name__ == "__main__":
    unittest.main()
