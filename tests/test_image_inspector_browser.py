from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"Pillow unavailable: {exc}")

from tunelab.image_inspector.browser import (
    ImageFolderError,
    THUMBNAIL_SIZE,
    discover_images,
    load_thumbnail,
    selected_paths_in_folder_order,
)


class ImageFolderBrowserTests(unittest.TestCase):
    def test_discovery_filters_supported_files_and_uses_natural_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("scene10.JPG", "scene2.png", "scene1.tiff"):
                Image.new("RGB", (20, 10), "red").save(folder / name)
            (folder / "readme.txt").write_text("ignore", encoding="utf-8")
            (folder / "nested").mkdir()
            Image.new("RGB", (20, 10), "blue").save(folder / "nested" / "hidden.png")
            discovered = discover_images(folder)
        self.assertEqual([path.name for path in discovered], ["scene1.tiff", "scene2.png", "scene10.JPG"])

    def test_thumbnail_is_exif_safe_fixed_size_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wide.png"
            Image.new("RGB", (200, 50), "green").save(path)
            thumbnail = load_thumbnail(path)
        self.assertEqual(thumbnail.source_size, (200, 50))
        self.assertEqual(thumbnail.image.size, THUMBNAIL_SIZE)

    def test_selected_paths_keep_folder_order(self) -> None:
        paths = [Path("a.png"), Path("b.png"), Path("c.png")]
        selected = selected_paths_in_folder_order(paths, [paths[2], paths[0]])
        self.assertEqual(selected, [paths[0], paths[2]])

    def test_missing_folder_is_friendly_error(self) -> None:
        with self.assertRaisesRegex(ImageFolderError, "文件夹不存在"):
            discover_images(Path("/definitely/missing/tunelab-folder"))


if __name__ == "__main__":
    unittest.main()
