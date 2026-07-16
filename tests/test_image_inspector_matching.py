from __future__ import annotations

import unittest
from unittest import mock

try:
    import numpy as np
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"Image dependencies unavailable: {exc}")

from tunelab.image_inspector.matching import MatchingError, confidence_for_score, match_roi
from tunelab.image_inspector.types import ROI


def textured_image(height: int = 180, width: int = 220) -> np.ndarray:
    rng = np.random.default_rng(12345)
    base = rng.normal(120, 35, (height, width, 3))
    yy, xx = np.mgrid[:height, :width]
    base[..., 0] += (xx % 31) * 1.8
    base[..., 1] += (yy % 23) * 1.4
    return np.clip(base, 0, 255).astype(np.float32)


def translate(image: np.ndarray, dx: int, dy: int) -> np.ndarray:
    output = np.zeros_like(image)
    source_x0 = max(0, -dx)
    source_y0 = max(0, -dy)
    target_x0 = max(0, dx)
    target_y0 = max(0, dy)
    width = image.shape[1] - abs(dx)
    height = image.shape[0] - abs(dy)
    output[target_y0 : target_y0 + height, target_x0 : target_x0 + width] = image[
        source_y0 : source_y0 + height,
        source_x0 : source_x0 + width,
    ]
    return output


class ImageInspectorMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.before = textured_image()
        self.roi = ROI(70, 60, 48, 42, "测试区域")

    def assert_match_offset(self, dx: int, dy: int, search_range: int) -> None:
        result = match_roi(self.before, translate(self.before, dx, dy), self.roi, search_range=search_range)
        self.assertEqual((result.after_roi.x, result.after_roi.y), (self.roi.x + dx, self.roi.y + dy))
        self.assertGreaterEqual(result.score, 0.99)
        self.assertTrue(result.reliable)

    def test_translation_10_pixels(self) -> None:
        self.assert_match_offset(10, -10, 30)

    def test_documented_confidence_bands_do_not_change_with_custom_gate(self) -> None:
        self.assertEqual(confidence_for_score(0.95, reliable_threshold=0.99), "高")
        self.assertEqual(confidence_for_score(0.85, reliable_threshold=0.70), "中")
        self.assertEqual(confidence_for_score(0.79, reliable_threshold=0.70), "低")

    def test_translation_50_pixels(self) -> None:
        self.assert_match_offset(50, 35, 60)

    def test_large_roi_path_downsamples_before_matching(self) -> None:
        with mock.patch("tunelab.image_inspector.matching.MAX_MATCH_TEMPLATE_PIXELS", 500):
            result = match_roi(self.before, translate(self.before, 10, 6), self.roi, search_range=20)
        self.assertLessEqual(abs(result.after_roi.x - (self.roi.x + 10)), 1)
        self.assertLessEqual(abs(result.after_roi.y - (self.roi.y + 6)), 1)
        self.assertIn("降采样", result.method)
        self.assertGreater(result.score, 0.90)

    def test_small_brightness_change(self) -> None:
        after = np.clip(translate(self.before, 14, 9) * 1.04 + 7.0, 0, 255)
        result = match_roi(self.before, after, self.roi, search_range=30)
        self.assertEqual((result.after_roi.x, result.after_roi.y), (84, 69))
        self.assertGreater(result.score, 0.95)

    def test_small_colour_change(self) -> None:
        after = translate(self.before, -12, 8)
        after = np.clip(after * np.array((1.05, 0.98, 0.92), dtype=np.float32), 0, 255)
        result = match_roi(self.before, after, self.roi, search_range=30)
        self.assertEqual((result.after_roi.x, result.after_roi.y), (58, 68))
        self.assertGreater(result.score, 0.92)

    def test_insufficient_search_range_does_not_find_true_translation(self) -> None:
        result = match_roi(self.before, translate(self.before, 50, 0), self.roi, search_range=10)
        self.assertNotEqual(result.after_roi.x, self.roi.x + 50)
        self.assertLess(result.score, 0.80)

    def test_textureless_roi_is_low_confidence(self) -> None:
        flat = np.full((100, 120, 3), 128, dtype=np.float32)
        result = match_roi(flat, flat, ROI(20, 20, 30, 30), search_range=20)
        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.reliable)
        self.assertIn("纹理", result.warning)

    def test_out_of_bounds_roi_is_rejected(self) -> None:
        with self.assertRaises(MatchingError):
            match_roi(self.before, self.before, ROI(-2, 4, 20, 20))

    def test_different_image_sizes_map_roi_by_width_and_height_ratio(self) -> None:
        resized = np.asarray(
            Image.fromarray(self.before.astype(np.uint8)).resize((440, 270), Image.Resampling.BILINEAR),
            dtype=np.float32,
        )
        result = match_roi(self.before, resized, self.roi, search_range=20)
        expected = (round(self.roi.x * 2.0), round(self.roi.y * 1.5))
        self.assertEqual((result.after_roi.x, result.after_roi.y), expected)
        self.assertEqual((result.after_roi.width, result.after_roi.height), (96, 63))
        self.assertGreater(result.score, 0.90)


if __name__ == "__main__":
    unittest.main()
