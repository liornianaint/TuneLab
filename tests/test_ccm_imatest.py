from __future__ import annotations

import unittest

from tunelab.ccm.imatest import infer_cct, parse_imatest_csv

from .materials import D65_CSV


class ImatestParserTests(unittest.TestCase):
    def test_parse_uploaded_summary(self) -> None:
        dataset = parse_imatest_csv(D65_CSV)
        self.assertEqual(len(dataset.patches), 24)
        self.assertEqual(dataset.image_name, "D65_normal.jpg")
        self.assertEqual(dataset.inferred_cct, 6500)
        self.assertEqual(dataset.patches[0].zone, 1)
        self.assertEqual(dataset.patches[0].measured_srgb, (0.362, 0.192, 0.187))
        self.assertEqual(dataset.patches[14].ideal_srgb, (0.681, 0.199, 0.223))

    def test_infer_common_illuminants(self) -> None:
        self.assertEqual(infer_cct("capture_TL84_01.jpg"), 4000)
        self.assertEqual(infer_cct("A_normal.jpg"), 2856)
        self.assertEqual(infer_cct("scene_CCT_5200K.csv"), 5200)


if __name__ == "__main__":
    unittest.main()
