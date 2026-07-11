from __future__ import annotations

import argparse
import html
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Iterable, Optional, Sequence, Union

from .imatest import ImatestCSVError, parse_imatest_csv
from .models import OptimizationConfig
from .optimizer import optimize_ccm
from .qualcomm_xml import QualcommCCDocument, QualcommXMLError


@dataclass(frozen=True)
class GoldenCaseResult:
    case_id: str
    csv_path: str
    xml_path: str
    cct: int
    region_index: int
    region_label: str
    status: str
    reasons: tuple[str, ...]
    matrix_status: str
    mean_before: float
    mean_after: float
    improvement_percent: float
    pass_before: tuple[int, ...]
    pass_after: tuple[int, ...]
    regressed_patches: int
    saturation_before: float
    saturation_after: float
    coefficient_min: float
    coefficient_max: float
    condition_number: float
    determinant: float


@dataclass(frozen=True)
class GoldenSuiteResult:
    status: str
    cases: tuple[GoldenCaseResult, ...]
    csv_count: int
    xml_count: int

    @property
    def passed_count(self) -> int:
        return sum(case.status == "PASS" for case in self.cases)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "csv_count": self.csv_count,
            "xml_count": self.xml_count,
            "passed_count": self.passed_count,
            "case_count": len(self.cases),
            "cases": [asdict(case) for case in self.cases],
        }


def _unique_files(directories: Iterable[Union[str, Path]], suffix: str) -> list[Path]:
    files: dict[tuple[int, int], Path] = {}
    fallback: dict[str, Path] = {}
    for directory in directories:
        root = Path(directory)
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() != suffix.lower():
                continue
            try:
                stat = path.stat()
                files.setdefault((stat.st_dev, stat.st_ino), path)
            except OSError:
                fallback.setdefault(str(path.resolve()), path)
    output = list(files.values()) + list(fallback.values())
    return sorted(output, key=lambda path: (path.name.lower(), str(path).lower()))


def discover_golden_inputs(source_directories: Sequence[Union[str, Path]]) -> tuple[list[Path], list[Path]]:
    # Source folders can also contain Stepchart/Gamma CSVs or XMLs for other
    # ISP modules.  Golden CCM regression still traverses the whole tree, but
    # only compatible ColorChecker and Qualcomm CC inputs become test cases.
    csv_files: list[Path] = []
    for path in _unique_files(source_directories, ".csv"):
        try:
            parse_imatest_csv(path)
        except (OSError, ImatestCSVError):
            continue
        csv_files.append(path)

    xml_files: list[Path] = []
    for path in _unique_files(source_directories, ".xml"):
        try:
            QualcommCCDocument.load(path)
        except (OSError, QualcommXMLError):
            continue
        xml_files.append(path)
    return csv_files, xml_files


def _validate_result(result: object, config: OptimizationConfig) -> tuple[str, ...]:
    reasons: list[str] = []
    optimization = result
    if optimization.matrix_health.status == "FAIL":
        reasons.append("Matrix Health=FAIL")
    failed_checks = [check.name for check in optimization.matrix_health.checks if check.status == "FAIL"]
    if failed_checks:
        reasons.append("Engineering FAIL: " + ", ".join(failed_checks))
    if optimization.mean_after >= optimization.mean_before - 1e-6:
        reasons.append("Average ΔE 未改善")
    if any(after < before for before, after in zip(optimization.pass_rates.before_counts, optimization.pass_rates.after_counts)):
        reasons.append("至少一个 Pass Rate 下降")
    if not any(after > before for before, after in zip(optimization.pass_rates.before_counts, optimization.pass_rates.after_counts)):
        reasons.append("Pass Rate 未提升")
    failed_patches = [patch.zone for patch in optimization.patch_results if patch.regression_status == "FAIL"]
    if failed_patches:
        reasons.append("明显退化 Patch: " + ",".join(str(zone) for zone in failed_patches))
    before_saturation_error = abs(optimization.saturation_ratio_before - config.saturation_factor)
    after_saturation_error = abs(optimization.saturation_ratio_after - config.saturation_factor)
    if after_saturation_error > before_saturation_error + 0.008:
        reasons.append("整体 Saturation 偏差扩大")
    if optimization.matrix_health.coefficient_min < config.coefficient_min - 1e-7 or optimization.matrix_health.coefficient_max > config.coefficient_max + 1e-7:
        reasons.append("Matrix 系数越界")
    if max(abs(value - 1.0) for value in optimization.matrix_health.row_sums) > 1e-5:
        reasons.append("Matrix Row Sum 不等于 1")
    focus = [patch for patch in optimization.patch_results if patch.zone in config.focus_patches]
    if focus:
        if mean(patch.delta_e_after for patch in focus) >= mean(patch.delta_e_before for patch in focus) - 1e-6:
            reasons.append("重点 Patch 平均 ΔE 未改善")
        if mean(abs(patch.delta_c_after) for patch in focus) > mean(abs(patch.delta_c_before) for patch in focus) + 0.25:
            reasons.append("重点 Patch 平均 abs(ΔC) 退化")
        if mean(abs(patch.delta_h_after) for patch in focus) > mean(abs(patch.delta_h_before) for patch in focus) + 0.5:
            reasons.append("重点 Patch 平均 abs(Δh) 退化")
    return tuple(reasons)


def run_golden_suite(
    source_directories: Sequence[Union[str, Path]],
    *,
    config: Optional[OptimizationConfig] = None,
) -> GoldenSuiteResult:
    selected_config = config or OptimizationConfig()
    selected_config.validate()
    csv_files, xml_files = discover_golden_inputs(source_directories)
    if not csv_files:
        raise ValueError("Golden Dataset 未发现 CSV。")
    if not xml_files:
        raise ValueError("Golden Dataset 未发现 Qualcomm XML。")
    cases: list[GoldenCaseResult] = []
    for xml_path in xml_files:
        document = QualcommCCDocument.load(xml_path)
        for csv_path in csv_files:
            dataset = parse_imatest_csv(csv_path)
            if dataset.inferred_cct is None:
                reasons = ("无法推断 CCT",)
                cases.append(
                    GoldenCaseResult(
                        f"{xml_path.stem}::{csv_path.stem}", str(csv_path), str(xml_path), 0, -1, "", "FAIL", reasons,
                        "FAIL", 0.0, 0.0, 0.0, (), (), 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                    )
                )
                continue
            region, _ = document.find_region_for_cct(dataset.inferred_cct)
            try:
                result = optimize_ccm(dataset, region.matrix, config=selected_config)
                reasons = _validate_result(result, selected_config)
                status = "PASS" if not reasons else "FAIL"
                health = result.matrix_health
                cases.append(
                    GoldenCaseResult(
                        case_id=f"{xml_path.stem}::{csv_path.stem}",
                        csv_path=str(csv_path),
                        xml_path=str(xml_path),
                        cct=dataset.inferred_cct,
                        region_index=region.index,
                        region_label=region.path_label(),
                        status=status,
                        reasons=reasons,
                        matrix_status=health.status,
                        mean_before=result.mean_before,
                        mean_after=result.mean_after,
                        improvement_percent=result.mean_improvement_percent,
                        pass_before=result.pass_rates.before_counts,
                        pass_after=result.pass_rates.after_counts,
                        regressed_patches=result.regressed_count,
                        saturation_before=result.saturation_ratio_before,
                        saturation_after=result.saturation_ratio_after,
                        coefficient_min=health.coefficient_min,
                        coefficient_max=health.coefficient_max,
                        condition_number=health.condition_number,
                        determinant=health.determinant,
                    )
                )
            except Exception as exc:  # The suite must record every case, not stop at the first failure.
                cases.append(
                    GoldenCaseResult(
                        f"{xml_path.stem}::{csv_path.stem}", str(csv_path), str(xml_path), dataset.inferred_cct,
                        region.index, region.path_label(), "FAIL", (f"{type(exc).__name__}: {exc}",), "FAIL",
                        0.0, 0.0, 0.0, (), (), 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                    )
                )
    suite_status = "PASS" if cases and all(case.status == "PASS" for case in cases) else "FAIL"
    return GoldenSuiteResult(suite_status, tuple(cases), len(csv_files), len(xml_files))


def save_golden_json(destination: Union[str, Path], suite: GoldenSuiteResult) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(suite.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_golden_html(destination: Union[str, Path], suite: GoldenSuiteResult) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(
        f"<tr class='{case.status.lower()}'><td>{html.escape(case.case_id)}</td><td>{case.cct}</td><td>{case.mean_before:.3f} → {case.mean_after:.3f}</td><td>{case.improvement_percent:+.1f}%</td><td>{case.pass_before} → {case.pass_after}</td><td>{case.saturation_before:.3f} → {case.saturation_after:.3f}</td><td>{case.matrix_status}</td><td>[{case.coefficient_min:.3f}, {case.coefficient_max:.3f}]</td><td>{html.escape('; '.join(case.reasons) or 'All acceptance checks passed')}</td></tr>"
        for case in suite.cases
    )
    document = f"""<!doctype html><meta charset='utf-8'><title>MatrixCorrect Golden Regression</title><style>body{{font:14px system-ui;margin:28px;color:#172033}}h1{{margin-bottom:4px}}.pass{{color:#087a55}}.fail{{color:#b42318}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d8e0eb;padding:8px;text-align:left}}th{{background:#173b74;color:white}}tr.fail td{{background:#fff0ee}}</style><h1>MatrixCorrect Golden Regression</h1><h2 class='{suite.status.lower()}'>{suite.status} · {suite.passed_count}/{len(suite.cases)} cases</h2><p>{suite.csv_count} CSV × {suite.xml_count} XML</p><table><thead><tr><th>Case</th><th>CCT</th><th>Average ΔE</th><th>Improve</th><th>Pass counts</th><th>Saturation</th><th>Matrix</th><th>Range</th><th>Acceptance</th></tr></thead><tbody>{rows}</tbody></table>"""
    path.write_text(document, encoding="utf-8")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MatrixCorrect Golden Dataset regression")
    parser.add_argument("--source", type=Path, action="append", help="Source directory; may be repeated")
    parser.add_argument("--json", type=Path, help="Write JSON result")
    parser.add_argument("--html", type=Path, help="Write HTML result")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    directories = args.source or [Path("Source"), Path("source")]
    suite = run_golden_suite(directories)
    if args.json:
        save_golden_json(args.json, suite)
    if args.html:
        save_golden_html(args.html, suite)
    print(f"Golden Regression: {suite.status} ({suite.passed_count}/{len(suite.cases)})")
    for case in suite.cases:
        print(
            f"[{case.status}] {case.case_id}: ΔE {case.mean_before:.3f}->{case.mean_after:.3f} "
            f"({case.improvement_percent:+.1f}%), Matrix={case.matrix_status}, "
            f"Pass {case.pass_before}->{case.pass_after}"
        )
        for reason in case.reasons:
            print(f"  - {reason}")
    return 0 if suite.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
