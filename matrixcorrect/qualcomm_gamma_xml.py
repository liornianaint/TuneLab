from __future__ import annotations

import difflib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from .gamma_models import GammaLUT, GammaRegion
from .models import TriggerRange
from .qualcomm_xml import CONTROL_VAR_NAMES


class QualcommGammaXMLError(ValueError):
    pass


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_child(element: ET.Element, name: str) -> Optional[ET.Element]:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _direct_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _required_number(element: ET.Element, name: str) -> float:
    child = _direct_child(element, name)
    if child is None or child.text is None:
        raise QualcommGammaXMLError(f"触发节点缺少 <{name}>。")
    try:
        return float(child.text.strip())
    except ValueError as exc:
        raise QualcommGammaXMLError(f"<{name}> 不是有效数字: {child.text!r}") from exc


def _parse_lut(region: ET.Element, channel: str) -> tuple[GammaLUT, int]:
    tab = _direct_child(region, f"channel_{channel}_tab")
    value_element = _direct_child(tab, f"channel_{channel}") if tab is not None else None
    if tab is None or value_element is None or not value_element.text:
        raise QualcommGammaXMLError(f"region 缺少 channel_{channel}_tab/channel_{channel}。")
    try:
        values = tuple(int(value) for value in value_element.text.split())
    except ValueError as exc:
        raise QualcommGammaXMLError(f"channel_{channel} LUT 含非整数。") from exc
    try:
        declared_length = int(tab.attrib.get("length", str(len(values))))
    except ValueError as exc:
        raise QualcommGammaXMLError(f"channel_{channel}_tab length 无效。") from exc
    if len(values) != declared_length:
        raise QualcommGammaXMLError(
            f"channel_{channel} LUT 长度应为 {declared_length}，实际为 {len(values)}。"
        )
    if declared_length < 2:
        raise QualcommGammaXMLError(f"channel_{channel} LUT 至少需要 2 点。")
    ranges = re.findall(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", tab.attrib.get("range", ""))
    candidate_maxima = sorted({int(upper) for _lower, upper in ranges if int(upper) > 0})
    value_maximum = max(values)
    # Qualcomm range metadata may list several targets (8/10/12-bit) in one
    # attribute.  The actual table endpoint identifies the active integer
    # format; otherwise choose the narrowest declared range that contains it.
    if value_maximum in candidate_maxima:
        maximum = value_maximum
    else:
        covering = [candidate for candidate in candidate_maxima if candidate >= value_maximum]
        maximum = min(covering) if covering else value_maximum
    if maximum <= 0 or any(value < 0 or value > maximum for value in values):
        raise QualcommGammaXMLError(f"channel_{channel} LUT 超出 0~{maximum}。")
    return values, maximum


def _validate_lut(values: GammaLUT, *, length: int, maximum: int, channel: str) -> None:
    if len(values) != length:
        raise QualcommGammaXMLError(f"{channel} LUT 必须保持 {length} 点，实际为 {len(values)}。")
    if any(not isinstance(value, int) or value < 0 or value > maximum for value in values):
        raise QualcommGammaXMLError(f"{channel} LUT 必须是 0~{maximum} 的整数。")
    if any(following < current for current, following in zip(values, values[1:])):
        raise QualcommGammaXMLError(f"{channel} LUT 存在局部反转，拒绝写入。")


@dataclass
class QualcommGammaDocument:
    source_path: Path
    source_text: str
    encoding: str
    control_variables: tuple[str, ...]
    regions: list[GammaRegion]

    @classmethod
    def load(cls, path: Union[str, Path]) -> "QualcommGammaDocument":
        source_path = Path(path)
        raw = source_path.read_bytes()
        encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
        try:
            source_text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise QualcommGammaXMLError("当前仅支持 UTF-8 编码的 Qualcomm Gamma XML。") from exc
        try:
            root = ET.fromstring(source_text)
        except ET.ParseError as exc:
            raise QualcommGammaXMLError(f"Gamma XML 解析失败: {exc}") from exc
        root_name = _local_name(root.tag).lower()
        if "gamma" not in root_name:
            raise QualcommGammaXMLError(f"不支持的 Gamma 根节点: {_local_name(root.tag)}")

        control_types: list[int] = []
        for element in root.iter():
            if _local_name(element.tag) == "control_var_type" and element.text:
                try:
                    control_types.append(int(element.text.strip()))
                except ValueError as exc:
                    raise QualcommGammaXMLError(f"无效 control_var_type: {element.text!r}") from exc
        control_variables = tuple(CONTROL_VAR_NAMES.get(value, f"Trigger {value}") for value in control_types)

        core = next(
            (
                element
                for element in root.iter()
                if _local_name(element.tag).startswith("chromatix_gamma")
                and _local_name(element.tag).endswith("_core")
            ),
            None,
        )
        if core is None:
            raise QualcommGammaXMLError("未找到 chromatix Gamma core。")
        trigger_root = next(
            (
                child
                for child in core
                if _local_name(child.tag).startswith("mod_gamma")
                and _local_name(child.tag).endswith("trigger_data")
            ),
            None,
        )
        if trigger_root is None:
            raise QualcommGammaXMLError("未找到 mod_gamma trigger_data。")

        regions: list[GammaRegion] = []

        def walk(node: ET.Element, depth: int, path_items: tuple[TriggerRange, ...]) -> None:
            start = _required_number(node, "start")
            end = _required_number(node, "end")
            name = control_variables[depth] if depth < len(control_variables) else f"Trigger {depth + 1}"
            current_path = (*path_items, TriggerRange(name=name, start=start, end=end))
            for region_element in _direct_children(node, "region"):
                channel_r, maximum_r = _parse_lut(region_element, "r")
                channel_g, maximum_g = _parse_lut(region_element, "g")
                channel_b, maximum_b = _parse_lut(region_element, "b")
                if len({len(channel_r), len(channel_g), len(channel_b)}) != 1:
                    raise QualcommGammaXMLError("R/G/B Gamma LUT 点数不一致。")
                if len({maximum_r, maximum_g, maximum_b}) != 1:
                    raise QualcommGammaXMLError("R/G/B Gamma LUT 数值范围不一致。")
                regions.append(
                    GammaRegion(
                        index=len(regions),
                        trigger_path=current_path,
                        channel_r=channel_r,
                        channel_g=channel_g,
                        channel_b=channel_b,
                        maximum=maximum_r,
                    )
                )
            for child_trigger in _direct_children(node, "trigger"):
                walk(child_trigger, depth + 1, current_path)

        walk(trigger_root, 0, ())
        if not regions:
            raise QualcommGammaXMLError("XML 中没有可编辑的 Gamma region。")
        return cls(source_path, source_text, encoding, control_variables, regions)

    def find_region_for_cct(self, cct: float) -> tuple[GammaRegion, str]:
        candidates = [region for region in self.regions if region.cct_range is not None]
        exact = [region for region in candidates if region.cct_range and region.cct_range.contains(cct)]
        if exact:
            return exact[0], "exact"
        if not candidates:
            raise QualcommGammaXMLError("Gamma XML 没有 CCT trigger。")
        nearest = min(
            candidates,
            key=lambda region: min(
                abs(cct - region.cct_range.start),
                abs(cct - region.cct_range.end),
            ) if region.cct_range else float("inf"),
        )
        return nearest, "transition"

    def render_with_luts(
        self,
        region_index: int,
        channel_r: GammaLUT,
        channel_g: GammaLUT,
        channel_b: GammaLUT,
    ) -> str:
        if not 0 <= region_index < len(self.regions):
            raise QualcommGammaXMLError(f"无效 Gamma region index: {region_index}")
        region = self.regions[region_index]
        for name, values in (("R", channel_r), ("G", channel_g), ("B", channel_b)):
            _validate_lut(values, length=region.length, maximum=region.maximum, channel=name)

        updated = self.source_text
        for channel, values in (("r", channel_r), ("g", channel_g), ("b", channel_b)):
            pattern = re.compile(
                rf"(<channel_{channel}\b[^>]*>)(.*?)(</channel_{channel}>)",
                flags=re.DOTALL,
            )
            matches = list(pattern.finditer(updated))
            if len(matches) != len(self.regions):
                raise QualcommGammaXMLError(
                    f"XML region 数量({len(self.regions)})与 channel_{channel} 数量({len(matches)})不一致。"
                )
            target = matches[region_index]
            old_content = target.group(2)
            leading = old_content[: len(old_content) - len(old_content.lstrip())]
            trailing = old_content[len(old_content.rstrip()) :]
            replacement = " ".join(str(value) for value in values)
            new_content = f"{leading}{replacement}{trailing}"
            updated = updated[: target.start(2)] + new_content + updated[target.end(2) :]
        try:
            ET.fromstring(updated)
        except ET.ParseError as exc:
            raise QualcommGammaXMLError(f"写入后的 Gamma XML 校验失败: {exc}") from exc
        return updated

    def diff_with_luts(
        self,
        region_index: int,
        channel_r: GammaLUT,
        channel_g: GammaLUT,
        channel_b: GammaLUT,
    ) -> str:
        updated = self.render_with_luts(region_index, channel_r, channel_g, channel_b)
        return "".join(
            difflib.unified_diff(
                self.source_text.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=self.source_path.name,
                tofile=f"{self.source_path.stem}_optimized{self.source_path.suffix}",
                n=3,
            )
        )

    def save_with_luts(
        self,
        destination: Union[str, Path],
        region_index: int,
        channel_r: GammaLUT,
        channel_g: GammaLUT,
        channel_b: GammaLUT,
    ) -> Path:
        updated = self.render_with_luts(region_index, channel_r, channel_g, channel_b)
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(updated.encode(self.encoding))
        reloaded = QualcommGammaDocument.load(destination_path)
        expected = (channel_r, channel_g, channel_b)
        actual_region = reloaded.regions[region_index]
        actual = (actual_region.channel_r, actual_region.channel_g, actual_region.channel_b)
        if actual != expected:
            raise QualcommGammaXMLError("Gamma LUT 写入后回读校验失败。")
        for index, original_region in enumerate(self.regions):
            if index == region_index:
                continue
            reloaded_region = reloaded.regions[index]
            if (
                reloaded_region.channel_r != original_region.channel_r
                or reloaded_region.channel_g != original_region.channel_g
                or reloaded_region.channel_b != original_region.channel_b
            ):
                raise QualcommGammaXMLError(f"非目标 region #{index} 被修改，已拒绝保存。")
        return destination_path
