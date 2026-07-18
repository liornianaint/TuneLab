from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"NumPy unavailable: {exc}")

from tunelab.image_inspector.cache import ImageDataCache, image_data_byte_size
from tunelab.image_inspector.types import ImageData


def _image(path: Path, value: int = 0, side: int = 4) -> ImageData:
    pixels = np.full((side, side, 3), value, dtype=np.uint8)
    return ImageData(path, side, side, 8, "RGB", pixels, pixels)


class ImageDataCacheTests(unittest.TestCase):
    def test_shared_analysis_and_display_array_is_counted_once(self) -> None:
        pixels = np.zeros((4, 5, 3), dtype=np.uint8)
        image = ImageData(Path("memory.png"), 5, 4, 8, "RGB", pixels, pixels)
        self.assertEqual(image_data_byte_size(image), pixels.nbytes)

    def test_cache_is_lru_bounded_and_detects_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / f"{index}.png" for index in range(3)]
            for path in paths:
                path.write_bytes(b"image")
            cache = ImageDataCache(max_items=2, max_bytes=1024)
            images = [_image(path, index) for index, path in enumerate(paths)]
            cache.put(images[0])
            cache.put(images[1])
            self.assertIs(cache.get(paths[0]), images[0])
            cache.put(images[2])
            self.assertIsNone(cache.get(paths[1]))
            self.assertIs(cache.get(paths[0]), images[0])
            paths[0].write_bytes(b"changed and larger")
            self.assertIsNone(cache.get(paths[0]))

    def test_oversized_entry_is_not_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.png"
            path.write_bytes(b"image")
            cache = ImageDataCache(max_items=2, max_bytes=8)
            cache.put(_image(path, side=4))
            self.assertEqual(cache.item_count, 0)
            self.assertEqual(cache.byte_size, 0)

    def test_discard_and_retain_release_unneeded_decodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"{index}.png" for index in range(3)]
            for path in paths:
                path.write_bytes(b"image")
            cache = ImageDataCache(max_items=4, max_bytes=4096)
            for index, path in enumerate(paths):
                cache.put(_image(path, index))
            cache.discard(paths[0])
            self.assertIsNone(cache.get(paths[0]))
            cache.retain([paths[2]])
            self.assertIsNone(cache.get(paths[1]))
            self.assertIsNotNone(cache.get(paths[2]))
            self.assertEqual(cache.item_count, 1)


if __name__ == "__main__":
    unittest.main()
