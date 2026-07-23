"""Public constants shared by TuneLab's macOS build and release tools."""

from __future__ import annotations


GITHUB_REPOSITORY = "liornianaint/TuneLab"
SPARKLE_VERSION = "2.9.2"
SPARKLE_ARCHIVE_NAME = f"Sparkle-{SPARKLE_VERSION}.tar.xz"
SPARKLE_DOWNLOAD_URL = (
    "https://github.com/sparkle-project/Sparkle/releases/download/"
    f"{SPARKLE_VERSION}/{SPARKLE_ARCHIVE_NAME}"
)
SPARKLE_ARCHIVE_SHA256 = (
    "1cb340cbbef04c6c0d162078610c25e2221031d794a3449d89f2f56f4df77c95"
)
SPARKLE_ARCHIVE_SIZE = 15_564_036
SPARKLE_APPCAST_URL = (
    f"https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/appcast.xml"
)
SPARKLE_PUBLIC_ED_KEY = "JIX9IjYbBw85cg+wjpinVPwwzDbb61axSF0cOsjoEqE="
SPARKLE_KEY_ACCOUNT = "com.tunelab.app"
