from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tunelab.image_inspector.settings import (
    ImageInspectorSettings,
    load_image_inspector_settings,
    save_image_inspector_settings,
)


class ImageInspectorSettingsTests(unittest.TestCase):
    def test_all_ui_preferences_round_trip_without_analysis_data(self) -> None:
        settings = ImageInspectorSettings(
            last_directory="/tmp/中文图片",
            search_range=200,
            match_threshold=0.84,
            show_histogram=False,
            live_pixel=False,
            default_roi_name="灰墙",
            window_geometry="1280x800+20+30",
            panel_ratio=0.24,
            include_full_path=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image_settings.json"
            save_image_inspector_settings(settings, path)
            loaded = load_image_inspector_settings(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, settings)
        serialised = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("image_data", serialised)
        self.assertNotIn("roi_statistics", serialised)
        self.assertNotIn("analysis_result", serialised)

    def test_invalid_ranges_fall_back_or_are_clamped(self) -> None:
        validated = ImageInspectorSettings(
            search_range=999,
            match_threshold=1.4,
            panel_ratio=0.01,
            default_roi_name="   ",
        ).validated()
        self.assertEqual(validated.search_range, 100)
        self.assertEqual(validated.match_threshold, 1.0)
        self.assertEqual(validated.panel_ratio, 0.15)
        self.assertEqual(validated.default_roi_name, "ROI 1")

    def test_removed_neutral_mode_is_ignored_in_legacy_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy_image_settings.json"
            path.write_text(
                json.dumps({"version": 1, "values": {"last_directory": "/tmp/images", "neutral_mode": True}}),
                encoding="utf-8",
            )
            loaded = load_image_inspector_settings(path)

        self.assertEqual(loaded.last_directory, "/tmp/images")
        self.assertFalse(hasattr(loaded, "neutral_mode"))


if __name__ == "__main__":
    unittest.main()
