"""Collision-safe batch renaming for Image Inspector files."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Union


class BatchRenameError(ValueError):
    """Raised when a rename preview is invalid or cannot be committed safely."""


@dataclass(frozen=True)
class RenameItem:
    source: Path
    destination: Path

    @property
    def changed(self) -> bool:
        return self.source != self.destination


_INVALID_COMPONENT = re.compile(r"[\x00-\x1f/:\\]")


def _normalise_paths(paths: Iterable[Union[str, Path]]) -> list[Path]:
    values: list[Path] = []
    seen: set[Path] = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        values.append(path)
    return values


def _matches_batch_source(destination: Path, sources: Iterable[Path]) -> bool:
    """Treat case-only aliases as the same source on Finder-style volumes."""

    for source in sources:
        if destination == source:
            return True
        if not destination.exists() or not source.exists():
            continue
        try:
            if os.path.samefile(destination, source):
                return True
        except OSError:
            continue
    return False


def _render_stem(template: str, source: Path, number: int, digits: int) -> str:
    rendered = template.replace("{name}", source.stem).replace("{n}", f"{number:0{digits}d}")
    rendered = rendered.strip()
    if not rendered:
        raise BatchRenameError("命名格式生成了空文件名。")
    if _INVALID_COMPONENT.search(rendered):
        raise BatchRenameError("文件名不能包含 /、\\、: 或控制字符。")
    if rendered in {".", ".."}:
        raise BatchRenameError("文件名不能是 . 或 ..。")
    # Finder silently trims these characters in several editing paths.  Reject
    # them here so the preview always matches the committed filename exactly.
    if rendered.endswith((".", " ")):
        raise BatchRenameError("文件名不能以句点或空格结尾。")
    return rendered


def build_rename_plan(
    paths: Iterable[Union[str, Path]],
    template: str,
    *,
    start: int = 1,
    digits: int = 3,
) -> list[RenameItem]:
    """Build and validate a Finder-style rename preview.

    ``{name}`` expands to the original stem and ``{n}`` to a zero-padded
    sequence.  Original extensions are always preserved.
    """

    sources = _normalise_paths(paths)
    if not sources:
        raise BatchRenameError("没有可重命名的图片。")
    if start < 0:
        raise BatchRenameError("起始序号不能小于 0。")
    if not 1 <= digits <= 9:
        raise BatchRenameError("序号位数必须在 1–9 之间。")
    if "{" in template.replace("{name}", "").replace("{n}", "") or "}" in template.replace(
        "{name}", ""
    ).replace("{n}", ""):
        raise BatchRenameError("命名格式只支持 {name} 和 {n} 两个占位符。")

    items: list[RenameItem] = []
    destination_keys: set[tuple[Path, str]] = set()
    source_paths = set(sources)
    for offset, source in enumerate(sources):
        if not source.is_file():
            raise BatchRenameError(f"图片不存在：{source}")
        stem = _render_stem(template, source, start + offset, digits)
        destination = source.with_name(stem + source.suffix)
        # macOS and Windows commonly use case-insensitive filesystems.  Detect
        # collisions with the same rule even when tests run on a case-sensitive
        # volume.
        key = (destination.parent, destination.name.casefold())
        if key in destination_keys:
            raise BatchRenameError(f"多个图片会得到同一文件名：{destination.name}")
        destination_keys.add(key)
        if destination.exists() and not _matches_batch_source(destination, source_paths):
            raise BatchRenameError(f"目标文件已存在：{destination.name}")
        items.append(RenameItem(source, destination))
    return items


def execute_rename_plan(plan: Sequence[RenameItem]) -> dict[Path, Path]:
    """Commit a plan with a two-phase move and best-effort rollback.

    Every source first moves to a unique temporary name in the same directory.
    This makes swaps and sequence shifts safe and avoids overwriting files that
    are also part of the batch.
    """

    items = [item for item in plan if item.changed]
    if not items:
        return {item.source: item.destination for item in plan}

    # Revalidate immediately before touching the filesystem.
    sources = {item.source for item in items}
    for item in items:
        if not item.source.is_file():
            raise BatchRenameError(f"图片不存在：{item.source}")
        if item.destination.exists() and not _matches_batch_source(item.destination, sources):
            raise BatchRenameError(f"目标文件已存在：{item.destination.name}")

    token = uuid.uuid4().hex
    temporary: dict[Path, Path] = {}
    completed: list[RenameItem] = []
    try:
        for index, item in enumerate(items):
            candidate = item.source.with_name(f".tunelab-rename-{token}-{index}{item.source.suffix}")
            if candidate.exists():
                raise BatchRenameError(f"无法创建安全临时文件：{candidate.name}")
            os.replace(item.source, candidate)
            temporary[item.source] = candidate
        for item in items:
            if item.destination.exists():
                raise BatchRenameError(f"目标文件在重命名过程中被占用：{item.destination.name}")
            os.replace(temporary[item.source], item.destination)
            completed.append(item)
    except (OSError, BatchRenameError) as exc:
        # Restore destinations already committed, then untouched temporary
        # files.  Never overwrite an unrelated file during rollback.
        for item in reversed(completed):
            try:
                if item.destination.exists() and not item.source.exists():
                    os.replace(item.destination, item.source)
            except OSError:
                pass
        for source, temp in temporary.items():
            try:
                if temp.exists() and not source.exists():
                    os.replace(temp, source)
            except OSError:
                pass
        if isinstance(exc, BatchRenameError):
            raise
        raise BatchRenameError(f"批量重命名失败：{exc}") from exc

    mapping = {item.source: item.destination for item in plan}
    return mapping
