"""Folder discovery and thumbnail decoding for the Image Inspector browser."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Tuple, Union

from PIL import Image, ImageOps

from .constants import SUPPORTED_EXTENSIONS


THUMBNAIL_SIZE = (112, 84)
THUMBNAIL_PREFETCH_ROWS = 12


class ImageFolderError(ValueError):
    pass


@dataclass(frozen=True)
class ThumbnailData:
    path: Path
    image: Any
    source_size: Tuple[int, int]


def _natural_key(path: Path) -> tuple:
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name))


def discover_images(folder: Union[str, Path]) -> list[Path]:
    directory = Path(folder).expanduser()
    if not directory.exists():
        raise ImageFolderError(f"文件夹不存在：{directory}")
    if not directory.is_dir():
        raise ImageFolderError(f"所选路径不是文件夹：{directory}")
    try:
        images = [
            item
            for item in directory.iterdir()
            if item.is_file() and item.suffix.casefold() in SUPPORTED_EXTENSIONS
        ]
    except OSError as exc:
        raise ImageFolderError(f"无法读取文件夹：{directory}\n{exc}") from exc
    return sorted(images, key=_natural_key)


def load_thumbnail(path: Union[str, Path], size: Tuple[int, int] = THUMBNAIL_SIZE) -> ThumbnailData:
    source = Path(path)
    if source.suffix.lower() in {".heic", ".heif"}:
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError as exc:
            raise ImageFolderError("HEIC/HEIF 缩略图依赖 pillow-heif；请重新同步工程环境。") from exc
    try:
        with Image.open(source) as opened:
            original_size = opened.size
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail(size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, "#111827")
            left = (size[0] - image.width) // 2
            top = (size[1] - image.height) // 2
            canvas.paste(image, (left, top))
    except (OSError, ValueError) as exc:
        raise ImageFolderError(f"无法生成缩略图：{source.name}\n{exc}") from exc
    return ThumbnailData(source, canvas, original_size)


def selected_paths_in_folder_order(paths: Iterable[Path], selected: Iterable[Union[str, Path]]) -> list[Path]:
    selected_set = {Path(item) for item in selected}
    return [path for path in paths if path in selected_set]
