from __future__ import annotations

import unittest

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - optional test environment
    raise unittest.SkipTest(f"NumPy unavailable: {exc}")

from tunelab.image_inspector.color import rgb_to_hsv, rgb_to_lab, relative_luminance, srgb_to_linear


class ImageInspectorColorTests(unittest.TestCase):
    def assert_triplet_close(self, actual, expected, places: int = 2) -> None:
        for value, reference in zip(actual, expected):
            self.assertAlmostEqual(float(value), reference, places=places)

    def test_black_and_white_lab(self) -> None:
        self.assert_triplet_close(rgb_to_lab((0, 0, 0)), (0.0, 0.0, 0.0), places=3)
        self.assert_triplet_close(rgb_to_lab((255, 255, 255)), (100.0, 0.0, 0.0), places=3)

    def test_srgb_primary_lab_known_values(self) -> None:
        self.assert_triplet_close(rgb_to_lab((255, 0, 0)), (53.2408, 80.0925, 67.2032), places=2)
        self.assert_triplet_close(rgb_to_lab((0, 255, 0)), (87.7347, -86.1827, 83.1793), places=2)
        self.assert_triplet_close(rgb_to_lab((0, 0, 255)), (32.2970, 79.1875, -107.8602), places=2)

    def test_neutral_gray_has_near_zero_ab(self) -> None:
        lab = rgb_to_lab((128, 128, 128))
        self.assertAlmostEqual(float(lab[1]), 0.0, places=3)
        self.assertAlmostEqual(float(lab[2]), 0.0, places=3)
        self.assertAlmostEqual(float(lab[0]), 53.585, places=2)

    def test_srgb_transfer_function_is_not_a_simple_gamma(self) -> None:
        decoded = srgb_to_linear(np.array(((0, 10, 255), (128, 128, 128))))
        self.assertAlmostEqual(float(decoded[0, 1]), (10.0 / 255.0) / 12.92, places=8)
        self.assertAlmostEqual(float(decoded[1, 0]), 0.2158605, places=6)

    def test_relative_luminance_known_values(self) -> None:
        self.assertAlmostEqual(float(relative_luminance((0, 0, 0))), 0.0, places=8)
        self.assertAlmostEqual(float(relative_luminance((255, 255, 255))), 1.0, places=8)
        self.assertAlmostEqual(float(relative_luminance((255, 0, 0))), 0.2126, places=6)
        self.assertAlmostEqual(float(relative_luminance((0, 255, 0))), 0.7152, places=6)
        self.assertAlmostEqual(float(relative_luminance((0, 0, 255))), 0.0722, places=6)

    def test_hsv_primaries_and_gray(self) -> None:
        self.assert_triplet_close(rgb_to_hsv((255, 0, 0)), (0.0, 1.0, 1.0), places=5)
        self.assert_triplet_close(rgb_to_hsv((0, 255, 0)), (120.0, 1.0, 1.0), places=5)
        self.assert_triplet_close(rgb_to_hsv((0, 0, 255)), (240.0, 1.0, 1.0), places=5)
        self.assert_triplet_close(rgb_to_hsv((128, 128, 128)), (0.0, 0.0, 128 / 255.0), places=5)


if __name__ == "__main__":
    unittest.main()
