from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .models import CCRegion, Matrix3, TriggerRange


class QualcommXMLError(ValueError):
    pass


CONTROL_VAR_NAMES = {
    0: "Lux Index",
    1: "Gain",
    2: "DRC Gain",
    3: "Exposure Time",
    4: "Exposure Gain Ratio",
    5: "AEC Sensitivity Ratio",
    6: "CCT",
    100: "LED Index",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _direct_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _required_number(element: ET.Element, name: str) -> float:
    child = _direct_child(element, name)
    if child is None or child.text is None:
        raise QualcommXMLError(f"触发节点缺少 <{name}>。")
    try:
        return float(child.text.strip())
    except ValueError as exc:
        raise QualcommXMLError(f"<{name}> 不是有效数字: {child.text!r}") from exc


def _parse_matrix(text: str | None) -> Matrix3:
    if not text:
        raise QualcommXMLError("<c_tab><c> 为空。")
    try:
        values = [float(value) for value in text.split()]
    except ValueError as exc:
        raise QualcommXMLError("<c_tab><c> 含非数字内容。") from exc
    if len(values) != 9:
        raise QualcommXMLError(f"CC 矩阵应有 9 个数，实际为 {len(values)} 个。")
    return tuple(tuple(values[row * 3 + col] for col in range(3)) for row in range(3))  # type: ignore[return-value]


def _parse_offsets(text: str | None) -> tuple[int, int, int]:
    if not text:
        return (0, 0, 0)
    try:
        values = tuple(int(value) for value in text.split())
    except ValueError as exc:
        raise QualcommXMLError("<k_tab><k> 含非整数内容。") from exc
    if len(values) != 3:
        raise QualcommXMLError(f"CC offset 应有 3 个数，实际为 {len(values)} 个。")
    return values  # type: ignore[return-value]


@dataclass
class QualcommCCDocument:
    source_path: Path
    source_text: str
    encoding: str
    control_variables: tuple[str, ...]
    regions: list[CCRegion]

    @classmethod
    def load(cls, path: str | Path) -> "QualcommCCDocument":
        source_path = Path(path)
        raw = source_path.read_bytes()
        encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
        try:
            source_text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise QualcommXMLError("当前仅支持 UTF-8 编码的 Qualcomm XML。") from exc
        try:
            root = ET.fromstring(source_text)
        except ET.ParseError as exc:
            raise QualcommXMLError(f"XML 解析失败: {exc}") from exc

        if _local_name(root.tag) not in {"cc13_ipe_v2", "cc14_ipe_v2", "cc12_ipe_v2"}:
            raise QualcommXMLError(f"不支持的 CC 根节点: {_local_name(root.tag)}")

        control_types: list[int] = []
        for element in root.iter():
            if _local_name(element.tag) == "control_var_type" and element.text:
                try:
                    control_types.append(int(element.text.strip()))
                except ValueError as exc:
                    raise QualcommXMLError(f"无效 control_var_type: {element.text!r}") from exc
        control_variables = tuple(CONTROL_VAR_NAMES.get(value, f"Trigger {value}") for value in control_types)

        core = next((element for element in root.iter() if _local_name(element.tag) == "chromatix_cc13_core"), None)
        if core is None:
            core = next((element for element in root.iter() if _local_name(element.tag).startswith("chromatix_cc") and _local_name(element.tag).endswith("_core")), None)
        if core is None:
            raise QualcommXMLError("未找到 chromatix CC core。")
        trigger_root = next((child for child in core if _local_name(child.tag).startswith("mod_cc") and _local_name(child.tag).endswith("trigger_data")), None)
        if trigger_root is None:
            raise QualcommXMLError("未找到 mod_cc trigger_data。")

        regions: list[CCRegion] = []

        def walk(node: ET.Element, depth: int, path_items: tuple[TriggerRange, ...]) -> None:
            start = _required_number(node, "start")
            end = _required_number(node, "end")
            name = control_variables[depth] if depth < len(control_variables) else f"Trigger {depth + 1}"
            current_path = (*path_items, TriggerRange(name=name, start=start, end=end))
            for region_element in _direct_children(node, "region"):
                c_tab = _direct_child(region_element, "c_tab")
                c_element = _direct_child(c_tab, "c") if c_tab is not None else None
                k_tab = _direct_child(region_element, "k_tab")
                k_element = _direct_child(k_tab, "k") if k_tab is not None else None
                if c_element is None:
                    raise QualcommXMLError("region 缺少 <c_tab><c>。")
                regions.append(
                    CCRegion(
                        index=len(regions),
                        trigger_path=current_path,
                        matrix=_parse_matrix(c_element.text),
                        offsets=_parse_offsets(k_element.text if k_element is not None else None),
                    )
                )
            for child_trigger in _direct_children(node, "trigger"):
                walk(child_trigger, depth + 1, current_path)

        walk(trigger_root, 0, ())
        if not regions:
            raise QualcommXMLError("XML 中没有可编辑的 CC region。")
        return cls(
            source_path=source_path,
            source_text=source_text,
            encoding=encoding,
            control_variables=control_variables,
            regions=regions,
        )

    def find_region_for_cct(self, cct: float) -> tuple[CCRegion, str]:
        candidates = [region for region in self.regions if region.cct_range is not None]
        exact = [region for region in candidates if region.cct_range and region.cct_range.contains(cct)]
        if exact:
            return exact[0], "exact"
        if not candidates:
            raise QualcommXMLError("XML 没有 CCT trigger。")
        nearest = min(
            candidates,
            key=lambda region: min(abs(cct - region.cct_range.start), abs(cct - region.cct_range.end)) if region.cct_range else float("inf"),
        )
        return nearest, "transition"

    def save_with_matrix(self, destination: str | Path, region_index: int, matrix: Matrix3) -> Path:
        if not (0 <= region_index < len(self.regions)):
            raise QualcommXMLError(f"无效 region index: {region_index}")
        flat = [value for row in matrix for value in row]
        if any(not (-15.99 <= value <= 15.99) for value in flat):
            raise QualcommXMLError("矩阵值超出 Qualcomm c_tab 范围 [-15.99, 15.99]。")
        replacement = " ".join(_format_float(value) for value in flat)
        pattern = re.compile(r"(<c_tab\b[^>]*>.*?<c>)(.*?)(</c>.*?</c_tab>)", flags=re.DOTALL)
        matches = list(pattern.finditer(self.source_text))
        if len(matches) != len(self.regions):
            raise QualcommXMLError(
                f"XML region 数量({len(self.regions)})与原文 c_tab 数量({len(matches)})不一致，已拒绝写入。"
            )
        target = matches[region_index]
        old_content = target.group(2)
        leading = old_content[: len(old_content) - len(old_content.lstrip())]
        trailing = old_content[len(old_content.rstrip()) :]
        new_content = f"{leading}{replacement}{trailing}"
        updated = self.source_text[: target.start(2)] + new_content + self.source_text[target.end(2) :]
        try:
            ET.fromstring(updated)
        except ET.ParseError as exc:
            raise QualcommXMLError(f"写入后的 XML 校验失败: {exc}") from exc
        destination_path = Path(destination)
        destination_path.write_bytes(updated.encode(self.encoding))
        reloaded = QualcommCCDocument.load(destination_path)
        expected = tuple(round(value, 7) for value in flat)
        actual = tuple(round(value, 7) for row in reloaded.regions[region_index].matrix for value in row)
        if actual != expected:
            raise QualcommXMLError("写入后的矩阵回读校验失败。")
        return destination_path


def _format_float(value: float) -> str:
    if abs(value) < 0.00000005:
        value = 0.0
    text = f"{value:.7f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text
