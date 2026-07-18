from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"Image dependencies unavailable: {exc}")

from tunelab.image_inspector.model import ROIError, analyse_roi, compare_statistics, load_image, pixel_metrics
from tunelab.image_inspector.types import ImageData, ROI


def image_data(values: np.ndarray, *, bit_depth: int = 8) -> ImageData:
    rgb = np.asarray(values, dtype=np.float32)
    if rgb.ndim == 2:
        rgb = np.repeat(rgb[..., None], 3, axis=-1)
    display = np.rint(np.clip(rgb, 0, 255)).astype(np.uint8)
    return ImageData(
        path=Path("synthetic.png"),
        width=rgb.shape[1],
        height=rgb.shape[0],
        bit_depth=bit_depth,
        source_mode="RGB",
        rgb=rgb,
        display_rgb=display,
    )


class ImageInspectorModelTests(unittest.TestCase):
    def test_constant_colour_roi_statistics(self) -> None:
        data = image_data(np.full((8, 9, 3), (20, 40, 80), dtype=np.float32))
        stats = analyse_roi(data, ROI(1, 2, 6, 5, "常量"))
        self.assertEqual(stats.pixel_count, 30)
        self.assertEqual(stats.mean_rgb, (20.0, 40.0, 80.0))
        self.assertEqual(stats.median_rgb, (20.0, 40.0, 80.0))
        self.assertEqual(stats.std_rgb, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(stats.r_over_g, 0.5)
        self.assertAlmostEqual(stats.b_over_g, 2.0)
        self.assertEqual(stats.stability, "高")
        expected_luminance = round(0.2126 * 20 + 0.7152 * 40 + 0.0722 * 80)
        self.assertEqual(int(stats.luminance_histogram[expected_luminance]), 30)
        self.assertEqual(int(np.sum(stats.luminance_histogram)), 30)

    def test_grayscale_roi_keeps_equal_channels(self) -> None:
        gray = np.arange(25, dtype=np.float32).reshape(5, 5)
        stats = analyse_roi(image_data(gray), ROI(0, 0, 5, 5))
        self.assertEqual(stats.mean_rgb[0], stats.mean_rgb[1])
        self.assertEqual(stats.mean_rgb[1], stats.mean_rgb[2])
        self.assertAlmostEqual(stats.hsv_mean[1], 0.0)
        self.assertAlmostEqual(stats.lab_mean[1], 0.0, places=3)
        self.assertAlmostEqual(stats.lab_mean[2], 0.0, places=3)

    def test_rgb_small_matrix_statistics_and_threshold_ratios(self) -> None:
        values = np.array(
            [
                [[0, 0, 0], [255, 1, 2]],
                [[10, 10, 10], [250, 100, 50]],
            ],
            dtype=np.float32,
        )
        stats = analyse_roi(image_data(values), ROI(0, 0, 2, 2))
        self.assertEqual(stats.min_rgb, (0.0, 0.0, 0.0))
        self.assertEqual(stats.max_rgb, (255.0, 100.0, 50.0))
        self.assertAlmostEqual(stats.clipped_ratio, 0.5)
        self.assertAlmostEqual(stats.dark_ratio, 0.5)

    def test_uint8_histogram_fast_path_matches_numpy_statistics(self) -> None:
        rng = np.random.default_rng(42)
        pixels = rng.integers(0, 256, size=(23, 19, 3), dtype=np.uint8)
        data = ImageData(
            path=Path("uint8.png"),
            width=19,
            height=23,
            bit_depth=8,
            source_mode="RGB",
            rgb=pixels,
            display_rgb=pixels,
        )
        stats = analyse_roi(data, ROI(0, 0, 19, 23))
        flat = pixels.reshape(-1, 3)
        np.testing.assert_allclose(stats.mean_rgb, np.mean(flat, axis=0), atol=1e-12)
        np.testing.assert_allclose(stats.median_rgb, np.median(flat, axis=0), atol=1e-12)
        np.testing.assert_allclose(stats.std_rgb, np.std(flat, axis=0), atol=1e-12)
        np.testing.assert_array_equal(stats.min_rgb, np.min(flat, axis=0))
        np.testing.assert_array_equal(stats.max_rgb, np.max(flat, axis=0))
        self.assertEqual(int(np.sum(stats.histogram[0])), flat.shape[0])
        self.assertEqual(int(np.sum(stats.luminance_histogram)), flat.shape[0])

    def test_darker_red_image_can_have_lower_mean_r_but_higher_redness_metrics(self) -> None:
        bright_neutral = analyse_roi(
            image_data(np.full((6, 7, 3), (180, 180, 180), dtype=np.float32)),
            ROI(0, 0, 7, 6),
        )
        darker_red = analyse_roi(
            image_data(np.full((6, 7, 3), (150, 50, 50), dtype=np.float32)),
            ROI(0, 0, 7, 6),
        )
        comparison = compare_statistics(bright_neutral, darker_red, reliable=True, match_score=1.0)

        self.assertLess(darker_red.mean_rgb[0], bright_neutral.mean_rgb[0])
        self.assertGreater(darker_red.normalized_rgb[0], bright_neutral.normalized_rgb[0])
        self.assertGreater(darker_red.r_over_g, bright_neutral.r_over_g)
        self.assertGreater(darker_red.lab_mean[1], bright_neutral.lab_mean[1])
        self.assertGreater(comparison.delta_normalized_rgb[0], 0.0)
        self.assertTrue(any("R 通道在 RGB 总量中的占比增加" in item for item in comparison.conclusions))

    def test_boundary_roi_is_clipped(self) -> None:
        stats = analyse_roi(image_data(np.ones((4, 4, 3)) * 100), ROI(-2, -1, 4, 3))
        self.assertEqual(stats.roi, ROI(0, 0, 2, 2))
        self.assertEqual(stats.pixel_count, 4)

    def test_empty_roi_raises_and_one_pixel_roi_is_supported_by_model(self) -> None:
        data = image_data(np.ones((3, 3, 3)) * 50)
        with self.assertRaises(ROIError):
            analyse_roi(data, ROI(5, 5, 2, 2))
        stats = analyse_roi(data, ROI(1, 1, 1, 1))
        self.assertEqual(stats.pixel_count, 1)
        self.assertEqual(stats.std_rgb, (0.0, 0.0, 0.0))

    def test_black_pixel_handles_zero_channel_sum_and_ratios(self) -> None:
        metrics = pixel_metrics(image_data(np.zeros((1, 1, 3))), 0, 0)
        self.assertEqual(metrics.normalized_rgb, (0.0, 0.0, 0.0))
        self.assertIsNone(metrics.r_over_g)
        self.assertIsNone(metrics.b_over_g)

    def test_near_neutral_warm_pixel_is_not_called_obviously_red(self) -> None:
        metrics = pixel_metrics(image_data(np.array([[[202, 198, 195]]], dtype=np.float32)), 0, 0)
        self.assertTrue(metrics.near_neutral)
        self.assertIn("接近中性", metrics.color_tendency)
        self.assertNotIn("明显偏红", metrics.color_tendency)

    def test_16_bit_grayscale_loading_preserves_fractional_normalisation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "灰度16.png"
            values = np.array([[0, 32768, 65535], [1000, 2000, 3000]], dtype=np.uint16)
            Image.fromarray(values).save(path)
            loaded = load_image(path)
        self.assertEqual(loaded.bit_depth, 16)
        self.assertTrue(loaded.precision_preserved)
        self.assertAlmostEqual(float(loaded.rgb[0, 1, 0]), 32768 / 65535 * 255, places=4)
        self.assertEqual(tuple(loaded.display_rgb[0, 2]), (255, 255, 255))

    def test_grayscale_rgba_and_cmyk_files_load_as_rgb(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gray_path = root / "gray.png"
            rgba_path = root / "alpha.png"
            cmyk_path = root / "cmyk.jpg"
            Image.new("L", (4, 3), 77).save(gray_path)
            Image.new("RGBA", (4, 3), (10, 20, 30, 128)).save(rgba_path)
            Image.new("CMYK", (4, 3), (0, 128, 255, 0)).save(cmyk_path)
            gray = load_image(gray_path)
            rgba = load_image(rgba_path)
            cmyk = load_image(cmyk_path)
        self.assertTrue(np.all(gray.display_rgb[..., 0] == gray.display_rgb[..., 1]))
        self.assertIs(gray.rgb, gray.display_rgb)
        self.assertEqual(gray.rgb.dtype, np.uint8)
        self.assertEqual(int(np.sum(gray.luminance_histogram)), gray.width * gray.height)
        self.assertEqual(int(gray.luminance_histogram[77]), gray.width * gray.height)
        self.assertIsNotNone(rgba.alpha)
        self.assertEqual(rgba.alpha.dtype, np.uint8)
        self.assertEqual(float(rgba.alpha[0, 0]), 128.0)
        self.assertEqual(cmyk.display_rgb.shape, (3, 4, 3))

    def test_palette_png_transparency_is_exposed_as_alpha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "palette-alpha.png"
            palette = Image.new("P", (2, 1))
            palette.putpalette([255, 0, 0, 0, 255, 0] + [0, 0, 0] * 254)
            palette.putdata([0, 1])
            palette.info["transparency"] = bytes((0, 255))
            palette.save(path)
            loaded = load_image(path)
        self.assertIsNotNone(loaded.alpha)
        self.assertEqual(tuple(float(value) for value in loaded.alpha[0]), (0.0, 255.0))

    def test_exif_orientation_is_applied_before_coordinates_are_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oriented.jpg"
            image = Image.new("RGB", (6, 4), (120, 80, 40))
            exif = Image.Exif()
            exif[274] = 6
            image.save(path, exif=exif)
            loaded = load_image(path)
        self.assertEqual((loaded.width, loaded.height), (4, 6))
        self.assertTrue(loaded.orientation_applied)

    def test_named_exif_metadata_is_retained_for_the_inspector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.jpg"
            image = Image.new("RGB", (12, 8), (90, 100, 110))
            exif = Image.Exif()
            exif[271] = "TuneLab Camera"
            exif[272] = "TL-1"
            exif[36867] = "2026:07:18 10:20:30"
            exif[33434] = (1, 125)
            image.save(path, exif=exif)
            loaded = load_image(path)

        metadata = dict(loaded.exif)
        self.assertEqual(metadata["Make"], "TuneLab Camera")
        self.assertEqual(metadata["Model"], "TL-1")
        self.assertEqual(metadata["DateTimeOriginal"], "2026:07:18 10:20:30")
        self.assertIn("ExposureTime", metadata)


if __name__ == "__main__":
    unittest.main()
