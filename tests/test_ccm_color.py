from __future__ import annotations

import unittest

from tunelab.ccm.color_science import delta_e_2000, lab_to_srgb, srgb_to_lab


class ColorScienceTests(unittest.TestCase):
    def test_ciede2000_reference_pairs(self) -> None:
        pairs = (
            ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
            ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
            ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
        )
        for first, second, expected in pairs:
            with self.subTest(first=first):
                self.assertAlmostEqual(delta_e_2000(first, second), expected, places=4)

    def test_srgb_lab_round_trip(self) -> None:
        original = (0.362, 0.192, 0.187)
        restored = lab_to_srgb(srgb_to_lab(original))
        for actual, expected in zip(restored, original):
            self.assertAlmostEqual(actual, expected, places=5)


if __name__ == "__main__":
    unittest.main()
