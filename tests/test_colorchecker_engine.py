from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tunelab.ccm.color_science import srgb_to_linear
from tunelab.colorchecker.engine import (
    ColorCheckerError,
    LUMA_WEIGHTS,
    RESTORATION_PROFILES,
    build_calibrated_restoration_plan,
    build_comparison_dataset,
    detect_colorchecker,
    image_optimization_config,
    restoration_evaluation_config,
    sample_patch_means,
    simulate_correction,
    simulate_restoration_response,
)
from tunelab.ccm.optimizer import evaluate_ccm_correction
from tunelab.image_inspector.types import ImageData


CLASSIC_RGB = (
    (115, 82, 68), (194, 150, 130), (98, 122, 157), (87, 108, 67), (133, 128, 177), (103, 189, 170),
    (214, 126, 44), (80, 91, 166), (193, 90, 99), (94, 60, 108), (157, 188, 64), (224, 163, 46),
    (56, 61, 150), (70, 148, 73), (175, 54, 60), (231, 199, 31), (187, 86, 149), (8, 133, 161),
    (243, 243, 242), (200, 200, 200), (160, 160, 160), (122, 122, 121), (85, 85, 85), (52, 52, 52),
)


def synthetic_chart(name: str = "4000K_test.png", *, colour_scale: float = 1.0) -> ImageData:
    rgb = np.full((520, 720, 3), 230, dtype=np.uint8)
    left, top, width, height = 60, 60, 600, 400
    rgb[top : top + height, left : left + width] = 18
    for index, colour in enumerate(CLASSIC_RGB):
        row, column = divmod(index, 6)
        x0 = left + column * 100 + 20
        y0 = top + row * 100 + 20
        values = np.rint(np.clip(np.asarray(colour) * colour_scale, 0, 255)).astype(np.uint8)
        rgb[y0 : y0 + 60, x0 : x0 + 60] = values
    return ImageData(
        path=Path(name),
        width=rgb.shape[1],
        height=rgb.shape[0],
        bit_depth=8,
        source_mode="RGB",
        rgb=rgb,
        display_rgb=rgb,
    )


class ColorCheckerEngineTests(unittest.TestCase):
    def test_calibrated_profiles_reproduce_the_accepted_3000k_and_4000k_matrices(self) -> None:
        for profile in RESTORATION_PROFILES:
            with self.subTest(cct=profile.cct):
                plan = build_calibrated_restoration_plan(
                    profile.source_matrix,
                    profile.cct,
                    strength=1.0,
                )
                np.testing.assert_allclose(plan.optimized_matrix, profile.target_matrix, atol=1e-12)
                recomposed = np.asarray(plan.correction_matrix) @ np.asarray(profile.source_matrix)
                np.testing.assert_allclose(recomposed, profile.target_matrix, atol=1e-12)

        plan_4000 = build_calibrated_restoration_plan(
            RESTORATION_PROFILES[1].source_matrix,
            4000.0,
        )
        row_sums = np.sum(np.asarray(plan_4000.optimized_matrix), axis=1)
        self.assertTrue(np.allclose(row_sums, row_sums.mean(), atol=1.1e-6))
        self.assertAlmostEqual(float(row_sums.mean()), 0.7576353333, places=7)
        self.assertNotAlmostEqual(float(row_sums.mean()), 1.0, places=2)

    def test_calibrated_profile_does_not_double_apply_an_accepted_matrix(self) -> None:
        profile = RESTORATION_PROFILES[0]
        plan = build_calibrated_restoration_plan(profile.target_matrix, profile.cct)
        self.assertTrue(plan.already_calibrated)
        np.testing.assert_allclose(plan.optimized_matrix, profile.target_matrix, atol=1e-12)
        np.testing.assert_allclose(plan.correction_matrix, np.identity(3), atol=1e-12)

        with self.assertRaisesRegex(ColorCheckerError, "仅覆盖 2800K–4500K"):
            build_calibrated_restoration_plan(profile.source_matrix, 6500.0)

    def test_mired_interpolation_and_strength_keep_a_common_neutral_scale(self) -> None:
        plan = build_calibrated_restoration_plan(
            RESTORATION_PROFILES[0].source_matrix,
            3500.0,
            strength=0.5,
        )
        correction_row_sums = np.sum(np.asarray(plan.correction_matrix), axis=1)
        self.assertTrue(np.allclose(correction_row_sums, correction_row_sums.mean(), atol=1e-6))
        self.assertGreater(float(correction_row_sums.mean()), 0.75)
        self.assertLess(float(correction_row_sums.mean()), 1.0)

    def test_validated_common_scale_matrix_passes_restoration_health(self) -> None:
        profile = RESTORATION_PROFILES[1]
        plan = build_calibrated_restoration_plan(profile.source_matrix, profile.cct)
        with mock.patch("tunelab.colorchecker.engine._mcc_polygons", return_value=None):
            measured = detect_colorchecker(synthetic_chart("4000K_Before.png"))
            reference = detect_colorchecker(synthetic_chart("D65.png", colour_scale=0.80))
        dataset = build_comparison_dataset(measured, reference)
        result = evaluate_ccm_correction(
            dataset,
            profile.source_matrix,
            plan.correction_matrix,
            config=restoration_evaluation_config(profile.source_matrix, plan.optimized_matrix),
            search_method="calibrated-restoration",
        )
        self.assertEqual(result.matrix_health.status, "PASS")
        self.assertEqual(result.search_method, "calibrated-restoration")
        np.testing.assert_allclose(result.optimized_matrix, profile.target_matrix, atol=1e-12)

    def test_geometric_fallback_detects_and_orients_all_24_patches(self) -> None:
        image = synthetic_chart()
        with mock.patch("tunelab.colorchecker.engine._mcc_polygons", return_value=None):
            detection = detect_colorchecker(image)
        self.assertEqual(detection.method, "几何网格后备")
        self.assertEqual([patch.zone for patch in detection.patches], list(range(1, 25)))
        np.testing.assert_allclose(detection.patches[0].mean_rgb, CLASSIC_RGB[0], atol=2.0)
        np.testing.assert_allclose(detection.patches[18].mean_rgb, CLASSIC_RGB[18], atol=2.0)
        np.testing.assert_allclose(detection.patches[23].mean_rgb, CLASSIC_RGB[23], atol=2.0)
        self.assertGreaterEqual(detection.confidence, 0.45)

    def test_paired_dataset_matches_reference_luminance_and_infers_cct(self) -> None:
        with mock.patch("tunelab.colorchecker.engine._mcc_polygons", return_value=None):
            measured = detect_colorchecker(synthetic_chart("3000K_Before.png"))
            reference = detect_colorchecker(synthetic_chart("target.png", colour_scale=0.72))
        dataset = build_comparison_dataset(measured, reference)
        self.assertEqual(dataset.inferred_cct, 3000)
        self.assertEqual(len(dataset.patches), 24)
        self.assertIn("逐色块匹配测试图亮度", dataset.warnings[0])
        for patch in dataset.patches:
            measured_luma = float(np.asarray(srgb_to_linear(patch.measured_srgb)) @ LUMA_WEIGHTS)
            target_luma = float(np.asarray(srgb_to_linear(patch.ideal_srgb)) @ LUMA_WEIGHTS)
            self.assertAlmostEqual(measured_luma, target_luma, places=5)

    def test_simulation_identity_is_exact_and_neutral_axis_is_preserved(self) -> None:
        pixels = np.asarray(
            [
                [[0, 0, 0], [64, 128, 220]],
                [[128, 128, 128], [255, 255, 255]],
            ],
            dtype=np.uint8,
        )
        identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        same = simulate_correction(pixels, identity, chunk_rows=1)
        np.testing.assert_array_equal(same.rgb, pixels)
        self.assertEqual(same.clipped_pixel_ratio, 0.0)
        same_encoded = simulate_correction(pixels, identity, chunk_rows=1, domain="encoded")
        np.testing.assert_array_equal(same_encoded.rgb, pixels)
        self.assertEqual(same_encoded.domain, "encoded")

        neutral_safe = ((1.1, -0.1, 0.0), (-0.05, 1.1, -0.05), (0.0, -0.1, 1.1))
        simulated = simulate_correction(pixels, neutral_safe, chunk_rows=1)
        self.assertEqual(tuple(simulated.rgb[1, 0]), (128, 128, 128))
        self.assertEqual(tuple(simulated.rgb[1, 1]), (255, 255, 255))

    def test_real_shot_response_reproduces_key_after_patch_colours(self) -> None:
        samples = (
            (
                RESTORATION_PROFILES[0],
                np.asarray([[[228, 96, 127], [176, 172, 165]]], dtype=np.uint8),
                np.asarray([[[187, 27, 40], [148, 150, 140]]], dtype=np.uint8),
            ),
            (
                RESTORATION_PROFILES[1],
                np.asarray([[[216, 119, 94], [187, 186, 177]]], dtype=np.uint8),
                np.asarray([[[196, 50, 51], [158, 160, 150]]], dtype=np.uint8),
            ),
        )
        for profile, before, expected_after in samples:
            with self.subTest(cct=profile.cct):
                plan = build_calibrated_restoration_plan(profile.source_matrix, profile.cct)
                simulation = simulate_restoration_response(before, plan, chunk_rows=1)
                self.assertEqual(simulation.domain, "real-shot-response")
                np.testing.assert_allclose(simulation.rgb, expected_after, atol=8)

                half_plan = build_calibrated_restoration_plan(
                    profile.source_matrix,
                    profile.cct,
                    strength=0.5,
                )
                half = simulate_restoration_response(before, half_plan, chunk_rows=1)
                expected_half = np.rint((before.astype(np.float64) + simulation.rgb) / 2.0)
                np.testing.assert_allclose(half.rgb, expected_half, atol=1)

    def test_real_shot_response_matches_supplied_after_captures_when_available(self) -> None:
        sources = Path(__file__).resolve().parents[1] / "sources"
        paths = [sources / f"{cct}K_{state}.jpg" for cct in (3000, 4000) for state in ("Before", "After")]
        if not all(path.exists() for path in paths):
            self.skipTest("Supplied real-shot ColorChecker captures are not present.")

        limits = {3000: 5.5, 4000: 2.0}
        for cct, profile in zip((3000, 4000), RESTORATION_PROFILES):
            with self.subTest(cct=cct):
                before = detect_colorchecker(sources / f"{cct}K_Before.jpg")
                after = detect_colorchecker(sources / f"{cct}K_After.jpg")
                plan = build_calibrated_restoration_plan(profile.source_matrix, cct)
                simulation = simulate_restoration_response(before.image, plan)
                simulated_means = np.asarray(sample_patch_means(simulation.rgb, before))
                target_means = np.asarray([patch.mean_rgb for patch in after.patches])
                rmse = float(np.sqrt(np.mean((simulated_means - target_means) ** 2)))
                self.assertLess(rmse, limits[cct])

                simulated_red = simulated_means[14]
                target_red = target_means[14]
                self.assertLess(abs(simulated_red[1] / simulated_red[0] - target_red[1] / target_red[0]), 0.03)
                self.assertLess(abs(simulated_red[2] / simulated_red[0] - target_red[2] / target_red[0]), 0.04)

    def test_already_calibrated_profile_does_not_apply_real_shot_response_twice(self) -> None:
        profile = RESTORATION_PROFILES[1]
        plan = build_calibrated_restoration_plan(profile.target_matrix, profile.cct)
        pixels = np.asarray([[[12, 34, 56], [220, 180, 90]]], dtype=np.uint8)
        simulation = simulate_restoration_response(pixels, plan)
        np.testing.assert_array_equal(simulation.rgb, pixels)
        self.assertEqual(simulation.clipped_pixel_ratio, 0.0)

    def test_image_config_never_rejects_a_loaded_source_coefficient_by_default(self) -> None:
        matrix = ((2.1, -1.4, 0.3), (-0.3, 0.7, 0.6), (0.75, -3.01, 3.26))
        config = image_optimization_config(matrix, strategy="aggressive", maximum_strength=0.95)
        self.assertEqual(config.strategy, "aggressive")
        self.assertEqual(config.max_blend, 0.95)
        self.assertLessEqual(config.coefficient_min, -3.01)
        self.assertGreaterEqual(config.coefficient_max, 3.26)
        self.assertEqual(config.focus_patches, (1, 2, 9, 15))


if __name__ == "__main__":
    unittest.main()
