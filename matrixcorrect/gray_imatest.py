from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Optional, Sequence, Union

from .gamma_models import GrayDataset, GrayPair, GrayRangeAnalysis, GrayZone


class GrayCSVError(ValueError):
    pass


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise GrayCSVError(f"无法识别 CSV 编码: {path}")


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def _number(value: str, row_number: int, field: str) -> float:
    try:
        return float(value.strip())
    except (TypeError, ValueError) as exc:
        raise GrayCSVError(f"第 {row_number} 行的 {field} 不是有效数字: {value!r}") from exc


def _optional(row: list[str], index: Optional[int], row_number: int, field: str) -> Optional[float]:
    if index is None or index >= len(row) or not row[index].strip():
        return None
    return _number(row[index], row_number, field)


def _table(rows: list[list[str]], required: tuple[str, ...], *, start: int = 0) -> tuple[int, dict[str, int]]:
    for row_index in range(start, len(rows)):
        normalized = [_normal(value) for value in rows[row_index]]
        mapping = {name: normalized.index(name) for name in required if name in normalized}
        if len(mapping) == len(required):
            return row_index, mapping
    raise GrayCSVError("未找到 Imatest Gray/Stepchart 数据段。")


def _rows_by_zone(
    rows: list[list[str]],
    header_index: int,
    mapping: dict[str, int],
) -> dict[int, tuple[list[str], int]]:
    output: dict[int, tuple[list[str], int]] = {}
    maximum = max(mapping.values())
    for zero_index in range(header_index + 1, len(rows)):
        row = rows[zero_index]
        if len(row) <= maximum:
            if output:
                break
            continue
        try:
            zone = int(float(row[mapping["zone"]].strip()))
        except (TypeError, ValueError):
            if output:
                break
            continue
        if zone in output:
            break
        output[zone] = (row, zero_index + 1)
    return output


def parse_gray_csv(path: Union[str, Path]) -> GrayDataset:
    source_path = Path(path)
    rows = list(csv.reader(_read_text(source_path).splitlines()))
    image_name = ""
    run_date = ""
    reported_gamma: Optional[float] = None
    for row in rows:
        if not row:
            continue
        label = row[0].strip().lower()
        if label == "file" and len(row) > 1:
            image_name = row[1].strip()
        elif label == "run date" and len(row) > 1:
            run_date = row[1].strip()
        elif label == "gamma" and len(row) > 1:
            try:
                reported_gamma = float(row[1].strip())
            except ValueError:
                pass

    primary_index, primary_map = _table(rows, ("zone", "pixel", "pixel255", "logexp", "logpx255"))
    primary = _rows_by_zone(rows, primary_index, primary_map)
    if len(primary) < 3:
        raise GrayCSVError(f"灰阶数据不足：只解析到 {len(primary)} 个 Zone。")

    density_index, density_map = _table(
        rows,
        ("zone", "ydensity", "rdensity", "gdensity", "bdensity", "rmean", "gmean", "bmean"),
        start=primary_index + 1,
    )
    density = _rows_by_zone(rows, density_index, density_map)
    slope_index, slope_map = _table(
        rows,
        ("zone", "yslope", "rslope", "gslope", "bslope"),
        start=density_index + 1,
    )
    slopes = _rows_by_zone(rows, slope_index, slope_map)
    noise_index, noise_map = _table(
        rows,
        ("zone", "ynoise", "rnoise", "gnoise", "bnoise"),
        start=slope_index + 1,
    )
    noise = _rows_by_zone(rows, noise_index, noise_map)

    zones: list[GrayZone] = []
    warnings: list[str] = []
    for zone in sorted(primary):
        row, row_number = primary[zone]
        density_row, density_number = density.get(zone, ([], 0))
        slope_row, slope_number = slopes.get(zone, ([], 0))
        noise_row, noise_number = noise.get(zone, ([], 0))
        zones.append(
            GrayZone(
                zone=zone,
                pixel=_number(row[primary_map["pixel"]], row_number, "Pixel"),
                pixel_normalized=_number(row[primary_map["pixel255"]], row_number, "Pixel/255"),
                log_exposure=_number(row[primary_map["logexp"]], row_number, "Log Exposure"),
                density=_number(row[primary_map["logpx255"]], row_number, "Density") * -1.0,
                density_r=_optional(density_row, density_map.get("rdensity"), density_number, "R Density"),
                density_g=_optional(density_row, density_map.get("gdensity"), density_number, "G Density"),
                density_b=_optional(density_row, density_map.get("bdensity"), density_number, "B Density"),
                mean_r=_optional(density_row, density_map.get("rmean"), density_number, "R Mean"),
                mean_g=_optional(density_row, density_map.get("gmean"), density_number, "G Mean"),
                mean_b=_optional(density_row, density_map.get("bmean"), density_number, "B Mean"),
                slope=_optional(slope_row, slope_map.get("yslope"), slope_number, "Y Slope"),
                slope_r=_optional(slope_row, slope_map.get("rslope"), slope_number, "R Slope"),
                slope_g=_optional(slope_row, slope_map.get("gslope"), slope_number, "G Slope"),
                slope_b=_optional(slope_row, slope_map.get("bslope"), slope_number, "B Slope"),
                noise=_optional(noise_row, noise_map.get("ynoise"), noise_number, "Y Noise"),
                noise_r=_optional(noise_row, noise_map.get("rnoise"), noise_number, "R Noise"),
                noise_g=_optional(noise_row, noise_map.get("gnoise"), noise_number, "G Noise"),
                noise_b=_optional(noise_row, noise_map.get("bnoise"), noise_number, "B Noise"),
            )
        )
    if len(density) != len(zones):
        warnings.append("部分 Zone 缺少 Density/RGB Mean 数据。")
    return GrayDataset(
        source_path=source_path,
        zones=tuple(zones),
        image_name=image_name,
        run_date=run_date,
        reported_gamma=reported_gamma,
        warnings=tuple(warnings),
    )


def analyze_pixel_values(
    zone_pixels: Sequence[tuple[int, float]],
    threshold: float = 8.0,
    *,
    middle_gray_zone: int = 8,
) -> GrayRangeAnalysis:
    """Analyze any Before/Target/After pixel sequence with the CSV rule."""

    if threshold < 0.0:
        raise ValueError("灰阶识别阈值不能小于 0。")
    ordered = sorted(((int(zone), float(pixel)) for zone, pixel in zone_pixels), key=lambda item: item[0])
    pairs: list[GrayPair] = []
    for current, following in zip(ordered, ordered[1:]):
        delta = current[1] - following[1]
        pairs.append(GrayPair(current[0], following[0], delta, delta >= threshold))

    runs: list[tuple[int, ...]] = []
    current_run: list[int] = []
    previous_to: Optional[int] = None
    for pair in pairs:
        if pair.distinguishable:
            if current_run and previous_to != pair.from_zone:
                runs.append(tuple(current_run))
                current_run = []
            current_run.append(pair.from_zone)
            previous_to = pair.to_zone
        else:
            if current_run:
                runs.append(tuple(current_run))
                current_run = []
            previous_to = None
    if current_run:
        runs.append(tuple(current_run))

    middle = middle_gray_zone
    selected: tuple[int, ...] = ()
    if runs:
        def key(run: tuple[int, ...]) -> tuple[int, int, float, int]:
            contains_middle = 1 if run[0] <= middle <= run[-1] else 0
            distance = min(abs(zone - middle) for zone in run)
            return (len(run), contains_middle, -float(distance), -run[0])

        selected = max(runs, key=key)
    return GrayRangeAnalysis(
        threshold=threshold,
        pairs=tuple(pairs),
        runs=tuple(runs),
        selected_zones=selected,
        start_zone=selected[0] if selected else None,
        end_zone=selected[-1] if selected else None,
    )


def analyze_gray_range(dataset: GrayDataset, threshold: float = 8.0) -> GrayRangeAnalysis:
    return analyze_pixel_values(
        tuple((zone.zone, zone.pixel) for zone in dataset.zones),
        threshold,
        middle_gray_zone=dataset.middle_gray_zone,
    )


def select_fit_zones(
    dataset: GrayDataset,
    analysis: GrayRangeAnalysis,
    *,
    mode: str = "auto",
    manual_start: Optional[int] = None,
    manual_end: Optional[int] = None,
) -> tuple[int, ...]:
    if mode == "auto":
        selected = analysis.selected_zones
    elif mode == "all":
        # "All" requests the full measured range, while the engineering gate
        # still excludes non-distinguishable/non-contiguous stages.  Therefore
        # the longest valid contiguous run remains the maximum safe fit range.
        selected = analysis.selected_zones
    elif mode == "manual":
        if manual_start is None or manual_end is None or manual_start > manual_end:
            raise ValueError("手动灰阶范围需要有效的起止 Zone。")
        requested = tuple(range(manual_start, manual_end + 1))
        valid = set(analysis.selected_zones)
        if not requested or any(zone not in valid for zone in requested):
            raise ValueError("手动范围含不可区分、近黑、过曝或非连续灰阶，不能参与 Gamma 拟合。")
        selected = requested
    else:
        raise ValueError(f"未知灰阶范围模式: {mode}")
    if len(selected) < 3:
        raise ValueError("连续有效灰阶少于 3 阶，不能优化 Gamma LUT。")
    available = {zone.zone for zone in dataset.zones}
    if any(zone not in available for zone in selected):
        raise ValueError("灰阶选择包含 CSV 中不存在的 Zone。")
    return selected
