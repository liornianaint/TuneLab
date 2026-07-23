from __future__ import annotations

import json
import unittest
from io import BytesIO
from urllib.error import HTTPError
from unittest import mock

from tunelab.updates import (
    NoPublishedRelease,
    RELEASES_PAGE_URL,
    ReleaseInfo,
    UpdateCheckResult,
    UpdateController,
    check_for_updates,
    fetch_latest_release,
    is_newer_version,
    parse_version,
)


class _Response(BytesIO):
    pass


class UpdateModelTests(unittest.TestCase):
    def test_release_versions_compare_stable_and_prerelease_tags(self) -> None:
        self.assertEqual(parse_version("v1.0.0"), parse_version("1"))
        self.assertTrue(is_newer_version("v1.0.1", "1.0.0"))
        self.assertTrue(is_newer_version("1.1.0-rc.1", "1.0.0"))
        self.assertTrue(is_newer_version("1.1.0", "1.1.0-rc.2"))
        self.assertFalse(is_newer_version("1.0.0-beta.2", "1.0.0"))
        with self.assertRaises(ValueError):
            parse_version("release-final")

    def test_latest_release_uses_github_api_metadata_and_safe_page(self) -> None:
        captured = {}
        payload = {
            "tag_name": "v1.2.0",
            "name": "TuneLab 1.2",
            "html_url": "https://github.com/liornianaint/TuneLab/releases/tag/v1.2.0",
            "body": "Release notes",
            "published_at": "2026-07-19T00:00:00Z",
        }

        def opener(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response(json.dumps(payload).encode("utf-8"))

        release = fetch_latest_release(opener=opener, timeout=3.0)
        self.assertEqual(release.version, "1.2.0")
        self.assertEqual(release.name, "TuneLab 1.2")
        self.assertEqual(captured["timeout"], 3.0)
        self.assertIn("TuneLab/1.1.0", captured["request"].get_header("User-agent"))

        payload["html_url"] = "https://example.com/untrusted-download"
        release = fetch_latest_release(
            opener=lambda _request, **_kwargs: _Response(json.dumps(payload).encode("utf-8"))
        )
        self.assertEqual(release.page_url, RELEASES_PAGE_URL)

    def test_no_release_is_distinct_from_network_failure(self) -> None:
        def opener(request, **_kwargs):
            raise HTTPError(request.full_url, 404, "Not Found", None, None)

        with self.assertRaises(NoPublishedRelease):
            fetch_latest_release(opener=opener)

    def test_check_result_uses_the_formal_local_version(self) -> None:
        release = ReleaseInfo("1.1.1", "v1.1.1", "TuneLab 1.1.1", RELEASES_PAGE_URL)
        with mock.patch("tunelab.updates.fetch_latest_release", return_value=release):
            result = check_for_updates()
        self.assertEqual(result.current_version, "1.1.0")
        self.assertTrue(result.update_available)


class UpdateControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = mock.Mock()

    def test_manual_current_version_reports_result(self) -> None:
        result = UpdateCheckResult(
            "1.0.0",
            ReleaseInfo("1.0.0", "v1.0.0", "TuneLab 1.0", RELEASES_PAGE_URL),
            False,
        )
        controller = UpdateController(self.root)
        with mock.patch("tunelab.updates.messagebox.showinfo") as info:
            controller._present(result, manual=True)
        self.assertIn("当前已是最新版本", info.call_args.args[1])

    def test_available_release_opens_only_after_confirmation(self) -> None:
        release_url = "https://github.com/liornianaint/TuneLab/releases/tag/v1.1.0"
        result = UpdateCheckResult(
            "1.0.0",
            ReleaseInfo("1.1.0", "v1.1.0", "TuneLab 1.1", release_url),
            True,
        )
        controller = UpdateController(self.root)
        with mock.patch("tunelab.updates.messagebox.askyesno", return_value=True), mock.patch(
            "tunelab.updates.webbrowser.open_new_tab", return_value=True
        ) as browser:
            controller._present(result, manual=False)
        browser.assert_called_once_with(release_url)

    def test_automatic_failures_are_silent_but_manual_failures_are_visible(self) -> None:
        controller = UpdateController(self.root)
        missing = NoPublishedRelease("no release")
        with mock.patch("tunelab.updates.messagebox.showinfo") as info:
            controller._present(missing, manual=False)
            info.assert_not_called()
            controller._present(missing, manual=True)
            info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
