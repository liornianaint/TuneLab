from __future__ import annotations

import unittest

from matrixcorrect.engineering import matrix_rank, project_row_sum_and_bounds


class EngineeringTests(unittest.TestCase):
    def test_projection_enforces_bounds_and_exact_row_sum(self) -> None:
        matrix = ((4.2, -2.8, -0.4), (-4.1, 2.0, 3.1), (0.2, 0.3, 0.4))
        projected = project_row_sum_and_bounds(matrix, -3.0, 3.0)
        for row in projected:
            self.assertAlmostEqual(sum(row), 1.0, places=9)
            self.assertGreaterEqual(min(row), -3.0)
            self.assertLessEqual(max(row), 3.0)

    def test_rank_detects_singular_matrix(self) -> None:
        singular = ((1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 1.0))
        self.assertEqual(matrix_rank(singular), 2)
