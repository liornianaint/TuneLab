"""Small bounded caches used by the desktop image browser."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .types import ImageData


DEFAULT_IMAGE_CACHE_ITEMS = 8
DEFAULT_IMAGE_CACHE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class _CacheEntry:
    signature: tuple[int, int]
    image: ImageData
    byte_size: int


def image_data_byte_size(image: ImageData) -> int:
    """Estimate resident array bytes without double-counting shared objects."""

    total = 0
    seen: set[int] = set()
    for value in (
        image.rgb,
        image.display_rgb,
        image.alpha,
        image.histogram,
        image.luminance_histogram,
    ):
        if value is None or id(value) in seen:
            continue
        seen.add(id(value))
        try:
            total += max(0, int(value.nbytes))
        except (AttributeError, TypeError, ValueError):
            continue
    return total


class ImageDataCache:
    """File-signature-aware LRU for decoded images.

    Cached objects are shared with active canvases, so a cache hit does not
    duplicate pixel arrays. Entries are discarded when their file size or
    modification timestamp changes.
    """

    def __init__(
        self,
        *,
        max_items: int = DEFAULT_IMAGE_CACHE_ITEMS,
        max_bytes: int = DEFAULT_IMAGE_CACHE_BYTES,
    ) -> None:
        self.max_items = max(1, int(max_items))
        self.max_bytes = max(1, int(max_bytes))
        self._entries: "OrderedDict[Path, _CacheEntry]" = OrderedDict()
        self._byte_size = 0

    @staticmethod
    def _path(path: Union[str, Path]) -> Path:
        return Path(path).expanduser().resolve()

    @staticmethod
    def _signature(path: Path) -> Optional[tuple[int, int]]:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    @property
    def item_count(self) -> int:
        return len(self._entries)

    @property
    def byte_size(self) -> int:
        return self._byte_size

    def get(self, path: Union[str, Path]) -> Optional[ImageData]:
        source = self._path(path)
        entry = self._entries.get(source)
        if entry is None:
            return None
        if self._signature(source) != entry.signature:
            self._remove(source)
            return None
        self._entries.move_to_end(source)
        return entry.image

    def put(self, image: ImageData) -> None:
        source = self._path(image.path)
        signature = self._signature(source)
        if signature is None:
            return
        byte_size = image_data_byte_size(image)
        self._remove(source)
        if byte_size > self.max_bytes:
            return
        self._entries[source] = _CacheEntry(signature, image, byte_size)
        self._byte_size += byte_size
        while len(self._entries) > self.max_items or self._byte_size > self.max_bytes:
            oldest = next(iter(self._entries))
            self._remove(oldest)

    def _remove(self, path: Path) -> None:
        entry = self._entries.pop(path, None)
        if entry is not None:
            self._byte_size = max(0, self._byte_size - entry.byte_size)

    def discard(self, path: Union[str, Path]) -> None:
        """Drop one cached decode while leaving active users untouched."""

        self._remove(self._path(path))

    def retain(self, paths: list[Union[str, Path]]) -> None:
        """Keep only the named entries, useful when a workspace is hidden."""

        retained = {self._path(path) for path in paths}
        for path in tuple(self._entries):
            if path not in retained:
                self._remove(path)

    def clear(self) -> None:
        self._entries.clear()
        self._byte_size = 0
