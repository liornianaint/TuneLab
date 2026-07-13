from __future__ import annotations

import unittest
from pathlib import Path

from tunelab.regression import discover_golden_inputs, run_golden_suite


ROOT = Path(__file__).resolve().parents[1]


class TuneLabRegressionTests(unittest.TestCase):
    def test_all_uploaded_csv_and_xml_cases_pass(self) -> None:
        csv_files, xml_files = discover_golden_inputs([ROOT / "source"])
        self.assertGreaterEqual(len(csv_files), 8)
        self.assertGreaterEqual(len(xml_files), 1)
        self.assertNotIn("gray_summary.csv", {path.name.lower() for path in csv_files})
        suite = run_golden_suite([ROOT / "source"])
        self.assertEqual(
            suite.status,
            "PASS",
            [(case.case_id, case.reasons) for case in (*suite.cases, *suite.gamma_cases)],
        )
        self.assertEqual(len(suite.cases), len(csv_files) * len(xml_files))
        for case in suite.cases:
            self.assertEqual(case.status, "PASS", case.reasons)
            self.assertLess(case.mean_after, case.mean_before)
            self.assertTrue(any(after > before for before, after in zip(case.pass_before, case.pass_after)))
            self.assertNotEqual(case.matrix_status, "FAIL")
            self.assertGreaterEqual(case.coefficient_min, -3.000001)
            self.assertLessEqual(case.coefficient_max, 3.000001)
        self.assertGreaterEqual(len(suite.gamma_cases), 1)
        for case in suite.gamma_cases:
            self.assertEqual(case.status, "PASS", case.reasons)
            self.assertGreaterEqual(case.after_steps, case.target_steps)
            self.assertLess(case.rmse_after, case.rmse_before)
            self.assertNotEqual(case.curve_status, "FAIL")
