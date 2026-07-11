from __future__ import annotations

import argparse
import json
from pathlib import Path

from .imatest import parse_imatest_csv
from .optimizer import optimize_ccm
from .qualcomm_xml import QualcommCCDocument
from .report import save_analysis_csv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qualcomm CC13 CCM optimizer")
    parser.add_argument("--csv", required=True, type=Path, help="Imatest summary CSV")
    parser.add_argument("--xml", required=True, type=Path, help="Qualcomm CC XML")
    parser.add_argument("--cct", type=float, help="Target CCT in Kelvin; inferred from CSV when omitted")
    parser.add_argument("--region-index", type=int, help="Explicit zero-based CC region index")
    parser.add_argument("--out", required=True, type=Path, help="Destination XML")
    parser.add_argument("--report", type=Path, help="Destination analysis CSV")
    parser.add_argument(
        "--composition",
        choices=("pre", "post_transposed"),
        default="pre",
        help="pre: A@M (Qualcomm row-major); post_transposed: M@A.T (legacy C7/Excel convention)",
    )
    parser.add_argument("--regularization", type=float, help="Fixed positive ridge weight; default is automatic search")
    parser.add_argument("--strength", type=float, default=1.0, help="Maximum optimization strength in (0,1]")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dataset = parse_imatest_csv(args.csv)
    document = QualcommCCDocument.load(args.xml)
    match_mode = "explicit"
    if args.region_index is not None:
        try:
            region = document.regions[args.region_index]
        except IndexError as exc:
            raise SystemExit(f"region-index 超出范围：XML 共有 {len(document.regions)} 个 region") from exc
    else:
        cct = args.cct if args.cct is not None else dataset.inferred_cct
        if cct is None:
            raise SystemExit("无法推断 CCT；请提供 --cct 或 --region-index。")
        region, match_mode = document.find_region_for_cct(cct)
    result = optimize_ccm(
        dataset,
        region.matrix,
        composition=args.composition,
        regularization=args.regularization,
        max_blend=args.strength,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    document.save_with_matrix(args.out, region.index, result.optimized_matrix)
    report_path = args.report or args.out.with_name(f"{args.out.stem}_analysis.csv")
    save_analysis_csv(report_path, dataset, result, region_label=region.path_label())
    summary = {
        "output_xml": str(args.out),
        "report_csv": str(report_path),
        "region_index": region.index,
        "region": region.path_label(),
        "match_mode": match_mode,
        "mean_delta_e00_before": round(result.mean_before, 4),
        "mean_delta_e00_after": round(result.mean_after, 4),
        "mean_improvement_percent": round(result.mean_improvement_percent, 2),
        "improved_patches": result.improved_count,
        "regressed_patches": result.regressed_count,
        "regularization": result.regularization,
        "blend": result.blend,
        "warnings": result.warnings,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"CC region: {summary['region']} ({match_mode})")
        print(
            f"Mean ΔE00: {result.mean_before:.3f} -> {result.mean_after:.3f} "
            f"({result.mean_improvement_percent:+.1f}%)"
        )
        print(f"XML: {args.out}")
        print(f"Report: {report_path}")
        for warning in result.warnings:
            print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
