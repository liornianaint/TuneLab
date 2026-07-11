from __future__ import annotations

import csv
import re
from pathlib import Path

from .models import ColorPatch, ImatestDataset, PATCH_NAMES_ZH


class ImatestCSVError(ValueError):
    pass


_ILLUMINANT_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"(?:^|[_\-\s])D65(?:[_\-\s.]|$)", 6500),
    (r"(?:^|[_\-\s])D75(?:[_\-\s.]|$)", 7500),
    (r"(?:^|[_\-\s])D55(?:[_\-\s.]|$)", 5500),
    (r"(?:^|[_\-\s])D50(?:[_\-\s.]|$)", 5000),
    (r"(?:^|[_\-\s])TL84(?:[_\-\s.]|$)", 4000),
    (r"(?:^|[_\-\s])CWF(?:[_\-\s.]|$)", 4150),
    (r"(?:^|[_\-\s])A(?:[_\-\s.]|$)", 2856),
)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImatestCSVError(f"无法识别 CSV 编码: {path}")


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def _float(value: str, *, field: str, row_number: int) -> float:
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ImatestCSVError(f"第 {row_number} 行的 {field} 不是有效数字: {value!r}") from exc


def infer_cct(*texts: str) -> int | None:
    joined = " ".join(texts).upper()
    explicit = re.search(r"(?:CCT|K)[_\-\s:=]*([2-9]\d{3})\s*K?", joined)
    if explicit:
        return int(explicit.group(1))
    trailing_k = re.search(r"(?:^|[_\-\s])([2-9]\d{3})K(?:[_\-\s.]|$)", joined)
    if trailing_k:
        return int(trailing_k.group(1))
    for pattern, value in _ILLUMINANT_PATTERNS:
        if re.search(pattern, joined, flags=re.IGNORECASE):
            return value
    return None


def parse_imatest_csv(path: str | Path) -> ImatestDataset:
    source_path = Path(path)
    rows = list(csv.reader(_read_text(source_path).splitlines()))
    image_name = ""
    run_date = ""
    color_space = "sRGB"
    warnings: list[str] = []

    for row in rows:
        if not row:
            continue
        label = row[0].strip().lower()
        if label == "file" and len(row) > 1:
            image_name = row[1].strip()
        elif label == "run date" and len(row) > 1:
            run_date = row[1].strip()
        elif label == "color space" and len(row) > 1:
            color_space = row[1].strip() or "sRGB"

    header_index = None
    column_map: dict[str, int] = {}
    required = ("zone", "rmeas", "gmeas", "bmeas", "rideal", "gideal", "bideal")
    for index, row in enumerate(rows):
        normalized = [_normalize_header(value) for value in row]
        candidate = {name: normalized.index(name) for name in required if name in normalized}
        if len(candidate) == len(required):
            header_index = index
            column_map = candidate
            break
    if header_index is None:
        raise ImatestCSVError(
            "未找到 Imatest ColorChecker RGB 数据段；需要包含 Zone、R/G/B-meas、R/G/B-ideal 列。"
        )

    patches: list[ColorPatch] = []
    seen_zones: set[int] = set()
    max_column = max(column_map.values())
    for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) <= max_column:
            if patches:
                break
            continue
        try:
            zone = int(float(row[column_map["zone"]].strip()))
        except ValueError:
            if patches:
                break
            continue
        if zone in seen_zones:
            break
        measured = tuple(
            _float(row[column_map[key]], field=key, row_number=row_index)
            for key in ("rmeas", "gmeas", "bmeas")
        )
        ideal = tuple(
            _float(row[column_map[key]], field=key, row_number=row_index)
            for key in ("rideal", "gideal", "bideal")
        )
        if any(not (0.0 <= value <= 1.5) for value in (*measured, *ideal)):
            raise ImatestCSVError(f"第 {row_index} 行 RGB 值超出预期的 0~1 范围。")
        name = PATCH_NAMES_ZH[zone - 1] if 1 <= zone <= len(PATCH_NAMES_ZH) else f"色块 {zone}"
        patches.append(ColorPatch(zone=zone, measured_srgb=measured, ideal_srgb=ideal, name=name))  # type: ignore[arg-type]
        seen_zones.add(zone)

    if len(patches) < 18:
        raise ImatestCSVError(f"ColorChecker 数据不足：仅解析到 {len(patches)} 个色块，至少需要 18 个。")
    patches.sort(key=lambda patch: patch.zone)
    if len(patches) != 24:
        warnings.append(f"解析到 {len(patches)} 个色块；标准 ColorChecker 为 24 个。")
    if color_space.strip().lower() != "srgb":
        warnings.append(f"CSV 标注色彩空间为 {color_space}；当前版本按 sRGB/D65 计算。")
    inferred = infer_cct(image_name, source_path.name)
    if inferred is None:
        warnings.append("未能从文件名推断色温，请在界面中手动输入 CCT。")

    return ImatestDataset(
        source_path=source_path,
        patches=patches,
        image_name=image_name,
        run_date=run_date,
        color_space=color_space,
        inferred_cct=inferred,
        warnings=warnings,
    )

