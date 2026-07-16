from __future__ import annotations

import html
from pathlib import Path
from typing import Union

from .models import GammaOptimizationResult, GrayDataset, GammaRegion


def save_gamma_html_report(
    destination: Union[str, Path],
    dataset: GrayDataset,
    region: GammaRegion,
    result: GammaOptimizationResult,
) -> Path:
    path = Path(destination)
    metrics = result.metrics
    checks = "".join(
        f"<tr><td>{html.escape(check.name)}</td><td>{check.status}</td>"
        f"<td>{html.escape(check.value)}</td><td>{html.escape(check.limit)}</td>"
        f"<td>{html.escape(check.message)}</td></tr>"
        for check in result.health.checks
    )
    zones = "".join(
        f"<tr><td>{zone.zone}</td><td>{zone.status}</td><td>{zone.pixel_before:.2f}</td>"
        f"<td>{zone.pixel_target:.2f}</td><td>{zone.pixel_after:.2f}</td>"
        f"<td>{zone.error_before:+.5f}</td><td>{zone.error_after:+.5f}</td></tr>"
        for zone in result.zone_results
    )
    diagnoses = "".join(
        f"<section><h3>{html.escape(item.module)} · {item.severity} · {item.confidence:.0%}</h3>"
        f"<p>{html.escape(item.root_cause)}</p><ul>"
        + "".join(f"<li>{html.escape(value)}</li>" for value in item.evidence)
        + f"</ul><p><b>Action:</b> {html.escape(item.action)}</p></section>"
        for item in result.diagnostics
    )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>TuneLab Gamma Report</title>
<style>body{{font-family:system-ui,sans-serif;margin:28px;color:#172033}}table{{border-collapse:collapse;width:100%;margin:12px 0 24px}}th,td{{border:1px solid #d0d5dd;padding:6px 8px;text-align:left}}th{{background:#f2f4f7}}.pass{{color:#067647}}</style></head>
<body><h1>TuneLab · Qualcomm Gamma Engineering Report</h1>
<p>Dataset: {html.escape(dataset.source_path.name)} · Region #{region.index} · {region.length} points / 0–{region.maximum}</p>
<h2>Summary</h2><table><tr><th>Metric</th><th>Before</th><th>Target</th><th>After</th></tr>
<tr><td>Recognizable steps</td><td>{metrics.distinguishable_before}</td><td>requested {result.requested_step_count} / safe {metrics.distinguishable_target}</td><td>{metrics.distinguishable_after}</td></tr>
<tr><td>Global Gamma</td><td>{metrics.global_gamma_before:.5f}</td><td>{metrics.global_gamma_target:.5f}</td><td>{metrics.global_gamma_after:.5f}</td></tr>
<tr><td>RMSE</td><td>{metrics.rmse_before:.6f}</td><td>—</td><td>{metrics.rmse_after:.6f}</td></tr>
<tr><td>RGB gray deviation</td><td>{metrics.rgb_gray_deviation_before:.6f}</td><td>—</td><td>{metrics.rgb_gray_deviation_after:.6f}</td></tr></table>
<h2>Curve Health · {result.health.status}</h2><table><tr><th>Check</th><th>Status</th><th>Value</th><th>Limit</th><th>Meaning</th></tr>{checks}</table>
<h2>Zones</h2><table><tr><th>Zone</th><th>Role</th><th>Before Pixel</th><th>Target</th><th>After</th><th>Error Before</th><th>Error After</th></tr>{zones}</table>
<h2>Diagnosis & Explainability</h2>{diagnoses}
<h2>Optimization trace</h2><ul>{''.join(f'<li>{html.escape(line)}</li>' for line in result.explainability)}</ul>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path
