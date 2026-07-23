from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tunelab.macos_updater import (
    SparkleUnavailable,
    SparkleUpdater,
    bundled_sparkle_binary,
    can_use_sparkle,
)


class MacOSUpdaterTests(unittest.TestCase):
    def test_framework_path_is_resolved_from_app_executable(self) -> None:
        executable = Path(
            "/Applications/TuneLab.app/Contents/MacOS/TuneLab"
        )
        self.assertEqual(
            bundled_sparkle_binary(executable),
            Path(
                "/Applications/TuneLab.app/Contents/Frameworks/"
                "Sparkle.framework/Versions/B/Sparkle"
            ),
        )

    def test_sparkle_is_only_selected_for_frozen_macos_app(self) -> None:
        with mock.patch("tunelab.macos_updater.platform.system", return_value="Darwin"), mock.patch(
            "tunelab.macos_updater.sys.frozen",
            True,
            create=True,
        ):
            self.assertTrue(can_use_sparkle())
        with mock.patch("tunelab.macos_updater.platform.system", return_value="Windows"):
            self.assertFalse(can_use_sparkle())

    def test_missing_embedded_framework_fails_safely(self) -> None:
        with mock.patch("tunelab.macos_updater.platform.system", return_value="Darwin"):
            with self.assertRaises(SparkleUnavailable):
                SparkleUpdater(Path("/tmp/TuneLab-missing-Sparkle"))


if __name__ == "__main__":
    unittest.main()
