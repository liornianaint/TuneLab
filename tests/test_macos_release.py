from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest import mock

from scripts.prepare_macos_release import remove_existing_appcast_version
from scripts.build import (
    SPARKLE_ARCHIVE_SHA256,
    SPARKLE_ARCHIVE_SIZE,
    macos_minimum_version,
)
from tunelab.update_config import (
    SPARKLE_APPCAST_URL,
    SPARKLE_PUBLIC_ED_KEY,
)


class _Completed:
    def __init__(self, output: str) -> None:
        self.returncode = 0
        self.stdout = output


class MacOSReleaseTests(unittest.TestCase):
    def test_bundle_minimum_version_uses_newest_embedded_binary(self) -> None:
        results = [
            _Completed("    minos 11.0\n"),
            _Completed("    minos 26.0\n"),
            _Completed("    minos 11.0\n"),
        ]
        with mock.patch(
            "scripts.build.subprocess.run",
            side_effect=results,
        ):
            version = macos_minimum_version(
                [Path(__file__), Path(__file__), Path(__file__)]
            )
        self.assertEqual(version, "26.0")

    def test_sparkle_supply_chain_values_are_pinned_and_public(self) -> None:
        self.assertEqual(len(SPARKLE_ARCHIVE_SHA256), 64)
        self.assertEqual(SPARKLE_ARCHIVE_SIZE, 15_564_036)
        self.assertTrue(SPARKLE_APPCAST_URL.startswith("https://github.com/"))
        self.assertTrue(SPARKLE_APPCAST_URL.endswith("/appcast.xml"))
        self.assertEqual(len(SPARKLE_PUBLIC_ED_KEY), 44)

    def test_rebuilding_release_replaces_only_matching_appcast_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appcast = Path(directory) / "appcast.xml"
            appcast.write_text(
                '<?xml version="1.0"?>'
                '<rss xmlns:sparkle="http://www.andymatuschak.org/'
                'xml-namespaces/sparkle"><channel>'
                "<item><sparkle:version>1.0.0</sparkle:version></item>"
                "<item><sparkle:version>1.1.0</sparkle:version></item>"
                "</channel></rss>",
                encoding="utf-8",
            )

            remove_existing_appcast_version(appcast, "1.1.0")

            updated = appcast.read_text(encoding="utf-8")
            self.assertIn("1.0.0", updated)
            self.assertNotIn("1.1.0", updated)

    def test_empty_preserved_feed_keeps_sparkle_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appcast = Path(directory) / "appcast.xml"
            appcast.write_text(
                '<?xml version="1.0"?>'
                '<rss xmlns:sparkle="http://www.andymatuschak.org/'
                'xml-namespaces/sparkle"><channel>'
                "<item><sparkle:version>1.1.0</sparkle:version></item>"
                "</channel></rss>",
                encoding="utf-8",
            )

            remove_existing_appcast_version(appcast, "1.1.0")

            updated = appcast.read_text(encoding="utf-8")
            self.assertIn("xmlns:sparkle=", updated)


if __name__ == "__main__":
    unittest.main()
