from __future__ import annotations

import csv
import html
import math
import os
import platform
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

from .models import ImatestDataset, OptimizationHistoryRecord, OptimizationResult


PathLike = Union[str, Path]


def save_analysis_csv(
    destination: PathLike,
    dataset: ImatestDataset,
    result: OptimizationResult,
    *,
    region_label: str = "",
    xml_diff: str = "",
    history: Sequence[OptimizationHistoryRecord] = (),
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["MatrixCorrect 工程分析报告"])
        writer.writerow(["源 CSV", str(dataset.source_path)])
        writer.writerow(["图像", dataset.image_name])
        writer.writerow(["测试日期", dataset.run_date])
        writer.writerow(["色彩空间", dataset.color_space])
        writer.writerow(["CC region", region_label])
        writer.writerow(["Optimization Strategy", result.strategy])
        writer.writerow(["Search Method", result.search_method])
        writer.writerow(["矩阵组合", result.composition])
        writer.writerow(["Regularization", f"{result.regularization:.7g}"])
        writer.writerow(["优化强度", "N/A (engineering-boundary)" if result.search_method == "engineering-boundary" else f"{result.blend:.1%}"])
        writer.writerow(["饱和度系数", f"{result.saturation_factor:.3f}"])
        writer.writerow(["Matrix Health", result.matrix_health.status])
        writer.writerow(["彩色色块平均 ΔE00（改前）", f"{result.mean_before:.4f}"])
        writer.writerow(["彩色色块平均 ΔE00（改后）", f"{result.mean_after:.4f}"])
        writer.writerow(["平均改善", f"{result.mean_improvement_percent:.2f}%"])
        writer.writerow(["改善/回退色块数", result.improved_count, result.regressed_count])
        writer.writerow(["Chroma ratio", f"{result.saturation_ratio_before:.4f}", f"{result.saturation_ratio_after:.4f}"])
        writer.writerow([])
        writer.writerow(["Pass Rate"])
        writer.writerow(["阈值", "Before", "After", "Before %", "After %"])
        for index, threshold in enumerate(result.pass_rates.thresholds):
            writer.writerow(
                [
                    f"ΔE<{threshold:g}",
                    result.pass_rates.before_counts[index],
                    result.pass_rates.after_counts[index],
                    f"{result.pass_rates.before_rate(index):.2%}",
                    f"{result.pass_rates.after_rate(index):.2%}",
                ]
            )
        writer.writerow([])
        writer.writerow(["改前 CC 矩阵"])
        writer.writerows([[f"{value:.7f}" for value in row] for row in result.original_matrix])
        writer.writerow(["Delta correction 矩阵"])
        writer.writerows([[f"{value:.7f}" for value in row] for row in result.correction_matrix])
        writer.writerow(["改后 CC 矩阵"])
        writer.writerows([[f"{value:.7f}" for value in row] for row in result.optimized_matrix])
        writer.writerow([])
        writer.writerow(["Matrix Engineering Checks"])
        writer.writerow(["Check", "Status", "Value", "Limit", "Meaning"])
        for check in result.matrix_health.checks:
            writer.writerow([check.name, check.status, check.value, check.limit, check.message])
        writer.writerow([])
        writer.writerow(["Patch 分类统计"])
        writer.writerow(["Category", "Count", "Mean Before", "Mean After", "Improve", "Regression", "Pass<3 Before", "Pass<3 After"])
        for category in result.category_statistics:
            writer.writerow(
                [
                    category.category,
                    category.count,
                    f"{category.mean_before:.4f}",
                    f"{category.mean_after:.4f}",
                    category.improved,
                    category.regressed,
                    f"{category.pass_rate_before_3:.2%}",
                    f"{category.pass_rate_after_3:.2%}",
                ]
            )
        writer.writerow([])
        writer.writerow(
            [
                "Zone", "色块", "Category", "Weight", "ΔE00 改前", "ΔE00 改后", "改善百分比",
                "ΔL* 改前", "ΔL* 改后", "ΔC* 改前", "ΔC* 改后", "Δh° 改前", "Δh° 改后",
                "Regression", "Regression Status", "建议模块",
                "R-meas", "G-meas", "B-meas", "R-sim", "G-sim", "B-sim", "R-ideal", "G-ideal", "B-ideal",
            ]
        )
        for patch in result.patch_results:
            writer.writerow(
                [
                    patch.zone,
                    patch.name,
                    patch.category,
                    f"{patch.priority_weight:.2f}",
                    f"{patch.delta_e_before:.4f}",
                    f"{patch.delta_e_after:.4f}",
                    f"{patch.improvement_percent:.2f}%",
                    f"{patch.delta_l_before:.4f}",
                    f"{patch.delta_l_after:.4f}",
                    f"{patch.delta_c_before:.4f}",
                    f"{patch.delta_c_after:.4f}",
                    f"{patch.delta_h_before:.4f}",
                    f"{patch.delta_h_after:.4f}",
                    f"{patch.regression:.4f}",
                    patch.regression_status,
                    patch.module_hint,
                    *[f"{value:.6f}" for value in patch.before_srgb],
                    *[f"{value:.6f}" for value in patch.after_srgb],
                    *[f"{value:.6f}" for value in patch.ideal_srgb],
                ]
            )
        writer.writerow([])
        writer.writerow(["模块诊断"])
        writer.writerow(["Module", "Confidence", "Severity", "Root Cause", "Evidence", "Action"])
        for diagnosis in result.diagnostics:
            writer.writerow(
                [
                    diagnosis.module,
                    f"{diagnosis.confidence:.1%}",
                    diagnosis.severity,
                    diagnosis.root_cause,
                    " | ".join(diagnosis.evidence),
                    diagnosis.action,
                ]
            )
        writer.writerow([])
        writer.writerow(["Explainable Optimization"])
        for line in result.explainability:
            writer.writerow([line])
        if result.warnings:
            writer.writerow([])
            writer.writerow(["警告"])
            for warning in result.warnings:
                writer.writerow([warning])
        if xml_diff:
            writer.writerow([])
            writer.writerow(["XML Diff"])
            for line in xml_diff.splitlines():
                writer.writerow([line])
        if history:
            writer.writerow([])
            writer.writerow(["Matrix History"])
            writer.writerow(["Timestamp", "Dataset", "Region", "Strategy", "Method", "Mean Before", "Mean After", "Pass<3 Before", "Pass<3 After", "Matrix Status"])
            for record in history:
                writer.writerow(
                    [
                        record.timestamp,
                        record.dataset_name,
                        record.region_label,
                        record.strategy,
                        record.search_method,
                        f"{record.mean_before:.4f}",
                        f"{record.mean_after:.4f}",
                        f"{record.pass_rate_before_3:.2%}",
                        f"{record.pass_rate_after_3:.2%}",
                        record.matrix_status,
                    ]
                )
    return path


def _matrix_html(title: str, matrix: tuple[tuple[float, float, float], ...]) -> str:
    rows = "".join("<tr>" + "".join(f"<td>{value:.7f}</td>" for value in row) + "</tr>" for row in matrix)
    return f"<section class='matrix'><h3>{html.escape(title)}</h3><table>{rows}</table></section>"


def _lab_plot_html(result: OptimizationResult, mode: str, title: str) -> str:
    low, high, size, pad = -100.0, 100.0, 360.0, 28.0

    def x(value: float) -> float:
        return pad + (max(low, min(high, value)) - low) / (high - low) * size

    def y(value: float) -> float:
        return pad + (high - max(low, min(high, value))) / (high - low) * size

    grid = []
    for value in range(-100, 101, 25):
        grid.append(f"<line x1='{x(value):.1f}' y1='{pad}' x2='{x(value):.1f}' y2='{pad + size}' class='grid'/>")
        grid.append(f"<line x1='{pad}' y1='{y(value):.1f}' x2='{pad + size}' y2='{y(value):.1f}' class='grid'/>")
    marks = []
    for patch in result.patch_results:
        actual = patch.before_lab if mode == "before" else patch.after_lab
        ideal = patch.ideal_lab
        marks.append(f"<line x1='{x(actual[1]):.1f}' y1='{y(actual[2]):.1f}' x2='{x(ideal[1]):.1f}' y2='{y(ideal[2]):.1f}' class='motion'/>")
        marks.append(f"<rect x='{x(ideal[1]) - 3:.1f}' y='{y(ideal[2]) - 3:.1f}' width='6' height='6' class='ideal'/>")
        marks.append(f"<circle cx='{x(actual[1]):.1f}' cy='{y(actual[2]):.1f}' r='4' class='camera'/>")
        marks.append(f"<text x='{x(actual[1]) + 6:.1f}' y='{y(actual[2]) + 3:.1f}'>{patch.zone}</text>")
    return (
        f"<figure><figcaption>{html.escape(title)}</figcaption><svg viewBox='0 0 {size + 2 * pad:.0f} {size + 2 * pad:.0f}' role='img'>"
        + "".join(grid)
        + f"<rect x='{pad}' y='{pad}' width='{size}' height='{size}' class='frame'/><text x='{pad + size / 2}' y='{pad + size + 22}' class='axis'>a*</text><text x='5' y='{pad + size / 2}' class='axis'>b*</text>"
        + "".join(marks)
        + "</svg></figure>"
    )


def save_analysis_html(
    destination: PathLike,
    dataset: ImatestDataset,
    result: OptimizationResult,
    *,
    region_label: str = "",
    xml_diff: str = "",
    history: Sequence[OptimizationHistoryRecord] = (),
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    pass_rows = "".join(
        f"<tr><td>ΔE&lt;{threshold:g}</td><td>{result.pass_rates.before_counts[index]} ({result.pass_rates.before_rate(index):.1%})</td><td>{result.pass_rates.after_counts[index]} ({result.pass_rates.after_rate(index):.1%})</td></tr>"
        for index, threshold in enumerate(result.pass_rates.thresholds)
    )
    engineering_rows = "".join(
        f"<tr><td>{html.escape(check.name)}</td><td><span class='pill {check.status.lower()}'>{check.status}</span></td><td>{html.escape(check.value)}</td><td>{html.escape(check.limit)}</td><td>{html.escape(check.message)}</td></tr>"
        for check in result.matrix_health.checks
    )
    category_rows = "".join(
        f"<tr><td>{html.escape(item.category)}</td><td>{item.count}</td><td>{item.mean_before:.3f}</td><td>{item.mean_after:.3f}</td><td>{item.improved}</td><td>{item.regressed}</td><td>{item.pass_rate_before_3:.1%}</td><td>{item.pass_rate_after_3:.1%}</td></tr>"
        for item in result.category_statistics
    )
    patch_rows = "".join(
        f"<tr class='{patch.regression_status.lower()}'><td>{patch.zone}</td><td>{html.escape(patch.name)}</td><td>{html.escape(patch.category)}</td><td>{patch.priority_weight:.2f}</td><td>{patch.delta_e_before:.3f}</td><td>{patch.delta_e_after:.3f}</td><td>{patch.improvement_percent:+.1f}%</td><td>{patch.delta_l_before:+.2f} → {patch.delta_l_after:+.2f}</td><td>{patch.delta_c_before:+.2f} → {patch.delta_c_after:+.2f}</td><td>{patch.delta_h_before:+.1f} → {patch.delta_h_after:+.1f}</td><td>{patch.regression:.3f}</td><td>{patch.regression_status}</td><td>{html.escape(patch.module_hint)}</td></tr>"
        for patch in result.patch_results
    )
    diagnosis_rows = "".join(
        f"<tr><td>{html.escape(item.module)}</td><td>{item.confidence:.0%}</td><td>{html.escape(item.severity)}</td><td>{html.escape(item.root_cause)}</td><td>{html.escape(' · '.join(item.evidence))}</td><td>{html.escape(item.action)}</td></tr>"
        for item in result.diagnostics
    )
    history_rows = "".join(
        f"<tr><td>{html.escape(item.timestamp)}</td><td>{html.escape(item.dataset_name)}</td><td>{html.escape(item.strategy)} / {html.escape(item.search_method)}</td><td>{item.mean_before:.3f} → {item.mean_after:.3f}</td><td>{item.pass_rate_before_3:.1%} → {item.pass_rate_after_3:.1%}</td><td>{html.escape(item.matrix_status)}</td></tr>"
        for item in history
    )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MatrixCorrect · {html.escape(dataset.source_path.stem)}</title>
<style>
:root{{--ink:#172033;--muted:#667085;--blue:#2563eb;--panel:#fff;--bg:#f3f5f8;--line:#dfe5ee;--green:#087a55;--red:#b42318;--amber:#a15c00}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}main{{max-width:1320px;margin:auto;padding:28px}}header{{background:linear-gradient(120deg,#14274d,#2563eb);color:white;padding:28px;border-radius:16px}}h1{{margin:0 0 4px}}h2{{margin-top:28px}}h3{{margin:4px 0 10px}}.meta{{opacity:.9}}.kpis,.matrices,.plots{{display:grid;gap:12px}}.kpis{{grid-template-columns:repeat(5,1fr);margin-top:16px}}.kpi,.matrix,section.card,figure{{background:white;border:1px solid var(--line);border-radius:12px;padding:14px}}.kpi strong{{font-size:22px;display:block}}.matrices{{grid-template-columns:repeat(3,1fr)}}.plots{{grid-template-columns:repeat(2,1fr)}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}}th{{background:#eaf0ff}}.scroll{{overflow:auto;border:1px solid var(--line);border-radius:12px}}.pill{{font-weight:700}}.pass{{color:var(--green)}}.warning{{color:var(--amber)}}.fail{{color:var(--red)}}tr.fail td{{background:#fff2f0}}pre{{white-space:pre-wrap;background:#101828;color:#f2f4f7;padding:14px;border-radius:10px;overflow:auto}}figure{{margin:0}}figcaption{{font-weight:700}}svg{{width:100%;max-width:480px;display:block;margin:auto;background:#fbfcff}}svg .grid{{stroke:#d9e2f0;stroke-width:.6}}svg .frame{{fill:none;stroke:#344054}}svg .motion{{stroke:#98a2b3;stroke-width:.8}}svg .ideal{{fill:white;stroke:#172033}}svg .camera{{fill:#2563eb;stroke:white}}svg text{{font-size:7px;fill:#344054}}svg .axis{{font-size:11px;font-weight:700}}ul{{padding-left:20px}}@media(max-width:900px){{.kpis,.matrices,.plots{{grid-template-columns:1fr}}}}@media print{{body{{background:white}}main{{max-width:none;padding:0}}header{{border-radius:0}}}}
</style></head><body><main>
<header><h1>MatrixCorrect 工程分析报告</h1><div class="meta">{html.escape(dataset.source_path.name)} · {html.escape(region_label)} · Strategy {html.escape(result.strategy)} · {html.escape(result.search_method)}</div></header>
<div class="kpis"><div class="kpi"><span>Average ΔE00</span><strong>{result.mean_before:.2f} → {result.mean_after:.2f}</strong></div><div class="kpi"><span>Improve</span><strong>{result.mean_improvement_percent:+.1f}%</strong></div><div class="kpi"><span>Pass ΔE&lt;3</span><strong>{result.pass_rates.before_rate(1):.0%} → {result.pass_rates.after_rate(1):.0%}</strong></div><div class="kpi"><span>Chroma Ratio</span><strong>{result.saturation_ratio_before:.3f} → {result.saturation_ratio_after:.3f}</strong></div><div class="kpi"><span>Matrix</span><strong class="{result.matrix_health.status.lower()}">{result.matrix_health.status}</strong></div></div>
<h2>Before / After a*b*</h2><div class="plots">{_lab_plot_html(result, 'before', 'Before：Camera → Ideal')}{_lab_plot_html(result, 'after', 'After：Camera → Ideal')}</div>
<h2>CC Matrix</h2><div class="matrices">{_matrix_html('Before CC Matrix', result.original_matrix)}{_matrix_html('Delta Correction', result.correction_matrix)}{_matrix_html('After CC Matrix', result.optimized_matrix)}</div>
<h2>Pass Rate</h2><div class="scroll"><table><thead><tr><th>Threshold</th><th>Before</th><th>After</th></tr></thead><tbody>{pass_rows}</tbody></table></div>
<h2>Matrix Engineering Checks</h2><div class="scroll"><table><thead><tr><th>Check</th><th>Status</th><th>Value</th><th>Limit</th><th>Meaning</th></tr></thead><tbody>{engineering_rows}</tbody></table></div>
<h2>Patch 分类统计</h2><div class="scroll"><table><thead><tr><th>Category</th><th>Count</th><th>Mean Before</th><th>Mean After</th><th>Improve</th><th>Regression</th><th>Pass&lt;3 Before</th><th>Pass&lt;3 After</th></tr></thead><tbody>{category_rows}</tbody></table></div>
<h2>Patch Before / After</h2><div class="scroll"><table><thead><tr><th>Zone</th><th>Name</th><th>Category</th><th>Weight</th><th>ΔE Before</th><th>ΔE After</th><th>Improve%</th><th>ΔL</th><th>ΔC</th><th>Δh</th><th>Regression</th><th>Status</th><th>Module</th></tr></thead><tbody>{patch_rows}</tbody></table></div>
<h2>Module Diagnosis</h2><div class="scroll"><table><thead><tr><th>Module</th><th>Confidence</th><th>Severity</th><th>Root Cause</th><th>Evidence</th><th>Action</th></tr></thead><tbody>{diagnosis_rows}</tbody></table></div>
<h2>Explainable Optimization</h2><section class="card"><ul>{''.join(f'<li>{html.escape(line)}</li>' for line in result.explainability)}</ul></section>
<h2>Warnings</h2><section class="card"><ul>{''.join(f'<li>{html.escape(line)}</li>' for line in result.warnings) or '<li>无额外警告。</li>'}</ul></section>
{f'<h2>Matrix History</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Dataset</th><th>Strategy / Method</th><th>Average ΔE</th><th>Pass&lt;3</th><th>Matrix</th></tr></thead><tbody>{history_rows}</tbody></table></div>' if history else ''}
{f'<h2>XML Diff</h2><pre>{html.escape(xml_diff)}</pre>' if xml_diff else ''}
</main></body></html>"""
    path.write_text(document, encoding="utf-8")
    return path


def save_analysis_pdf(
    destination: PathLike,
    dataset: ImatestDataset,
    result: OptimizationResult,
    *,
    region_label: str = "",
    xml_diff: str = "",
    history: Sequence[OptimizationHistoryRecord] = (),
) -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import KeepTogether, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise RuntimeError("导出 PDF 需要 reportlab：python -m pip install reportlab") from exc

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    font_candidates = [Path(__file__).with_name("assets") / "NotoSansCJKsc-Regular.otf"]
    system = platform.system()
    if system == "Darwin":
        font_candidates.extend(
            [
                Path("/System/Library/Fonts/STHeiti Light.ttc"),
                Path("/System/Library/Fonts/PingFang.ttc"),
                Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
            ]
        )
    elif system == "Windows":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        font_candidates.extend([fonts / "msyh.ttc", fonts / "simhei.ttf", fonts / "simsun.ttc"])
    else:
        font_candidates.extend(
            [
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
                Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            ]
        )
    font_name = "MatrixCorrectUnicode"
    for candidate in font_candidates:
        if not candidate.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(font_name, str(candidate), subfontIndex=0))
            break
        except Exception:
            continue
    else:
        raise RuntimeError("未找到可嵌入的中文字体；请安装 Noto Sans CJK、微软雅黑或文泉驿正黑后重试 PDF 导出。")
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("MC-Title", parent=styles["Title"], fontName=font_name, fontSize=21, leading=26, textColor=colors.HexColor("#173B74"), spaceAfter=10)
    h2 = ParagraphStyle("MC-H2", parent=styles["Heading2"], fontName=font_name, fontSize=13, leading=16, textColor=colors.HexColor("#173B74"), spaceBefore=10, spaceAfter=6)
    body = ParagraphStyle("MC-Body", parent=styles["BodyText"], fontName=font_name, fontSize=8.5, leading=11, alignment=TA_LEFT)
    small = ParagraphStyle("MC-Small", parent=body, fontSize=7, leading=9)
    code_style = ParagraphStyle("MC-Code", parent=small, fontName=font_name, fontSize=6, leading=8, backColor=colors.HexColor("#F8FAFD"), borderPadding=5)

    def paragraph(value: Any, style: Any = body) -> Any:
        return Paragraph(html.escape(str(value)).replace("\n", "<br/>"), style)

    def styled_table(rows: list[list[Any]], widths: Optional[list[float]] = None, header: bool = True) -> Any:
        table = Table(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
        commands = [
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 7.2),
            ("LEADING", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D8E0EB")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, colors.HexColor("#F8FAFD")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        if header:
            commands.extend(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#173B74")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ]
            )
        table.setStyle(TableStyle(commands))
        return table

    def footer(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont(font_name, 7)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(14 * mm, 8 * mm, "MatrixCorrect · 工程模拟结果必须上机复测")
        canvas.drawRightString(landscape(A4)[0] - 14 * mm, 8 * mm, f"Page {document.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        str(path),
        pagesize=landscape(A4),
        rightMargin=13 * mm,
        leftMargin=13 * mm,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title="MatrixCorrect Engineering Report",
        author="MatrixCorrect",
    )
    story: list[Any] = [
        Paragraph("MatrixCorrect 工程分析报告", title_style),
        paragraph(f"CSV: {dataset.source_path.name}　Region: {region_label}　Strategy: {result.strategy}　Method: {result.search_method}"),
        Spacer(1, 5),
    ]
    summary = [
        ["Metric", "Before", "After", "Result"],
        ["Average ΔE00 (1-18)", f"{result.mean_before:.3f}", f"{result.mean_after:.3f}", f"{result.mean_improvement_percent:+.1f}%"],
        ["Max ΔE00 (1-18)", f"{result.max_before:.3f}", f"{result.max_after:.3f}", f"Improve {result.improved_count} / Regression {result.regressed_count}"],
        ["Chroma Ratio", f"{result.saturation_ratio_before:.3f}", f"{result.saturation_ratio_after:.3f}", f"Target {result.saturation_factor:.3f}"],
        ["Matrix Health", "—", result.matrix_health.status, f"method={result.search_method}; λ={result.regularization:g}"],
    ]
    story.extend([styled_table(summary, [48 * mm, 38 * mm, 38 * mm, 100 * mm]), Paragraph("CC Matrix", h2)])
    matrix_rows = [["", "C0", "C1", "C2"], *[["Before R" + str(index), *[f"{value:.7f}" for value in row]] for index, row in enumerate(result.original_matrix)], *[["After R" + str(index), *[f"{value:.7f}" for value in row]] for index, row in enumerate(result.optimized_matrix)]]
    story.append(styled_table(matrix_rows, [35 * mm, 42 * mm, 42 * mm, 42 * mm]))
    pass_rows = [["Threshold", "Before", "After"]] + [
        [f"ΔE<{threshold:g}", f"{result.pass_rates.before_counts[index]} ({result.pass_rates.before_rate(index):.1%})", f"{result.pass_rates.after_counts[index]} ({result.pass_rates.after_rate(index):.1%})"]
        for index, threshold in enumerate(result.pass_rates.thresholds)
    ]
    engineering = [["Check", "Status", "Value", "Limit"]] + [[check.name, check.status, check.value, check.limit] for check in result.matrix_health.checks]
    story.extend(
        [
            PageBreak(),
            KeepTogether(
                [
                    Paragraph("Pass Rate & Matrix Engineering", h2),
                    styled_table(pass_rows, [32 * mm, 36 * mm, 36 * mm]),
                    Spacer(1, 5),
                    styled_table(engineering, [48 * mm, 24 * mm, 62 * mm, 86 * mm]),
                ]
            ),
        ]
    )
    story.extend([PageBreak(), Paragraph("Patch Before / After", h2)])
    patch_table: list[list[Any]] = [["Zone", "Name", "Category", "Weight", "ΔE Before", "ΔE After", "Improve", "ΔL After", "ΔC After", "Δh After", "Regression", "Status"]]
    for patch in result.patch_results:
        patch_table.append(
            [
                patch.zone, paragraph(patch.name, small), patch.category, f"{patch.priority_weight:.2f}",
                f"{patch.delta_e_before:.3f}", f"{patch.delta_e_after:.3f}", f"{patch.improvement_percent:+.1f}%",
                f"{patch.delta_l_after:+.2f}", f"{patch.delta_c_after:+.2f}", f"{patch.delta_h_after:+.1f}",
                f"{patch.regression:.3f}", patch.regression_status,
            ]
        )
    story.append(styled_table(patch_table, [12 * mm, 24 * mm, 20 * mm, 15 * mm, 20 * mm, 20 * mm, 18 * mm, 19 * mm, 19 * mm, 19 * mm, 20 * mm, 20 * mm]))
    diagnosis_table = [["Module", "Confidence", "Severity", "Root Cause", "Evidence", "Action"]]
    for item in result.diagnostics:
        diagnosis_table.append([item.module, f"{item.confidence:.0%}", item.severity, paragraph(item.root_cause, small), paragraph(" · ".join(item.evidence), small), paragraph(item.action, small)])
    story.append(
        KeepTogether(
            [
                Paragraph("Module Diagnosis", h2),
                styled_table(diagnosis_table, [28 * mm, 22 * mm, 20 * mm, 55 * mm, 75 * mm, 55 * mm]),
            ]
        )
    )
    story.append(Paragraph("Explainable Optimization", h2))
    story.extend(paragraph("• " + line) for line in result.explainability)
    if xml_diff:
        story.extend([PageBreak(), Paragraph("XML Diff", h2), Preformatted(xml_diff, code_style)])
    if history:
        story.append(Paragraph("Matrix History", h2))
        history_table = [["Time", "Dataset", "Strategy", "Method", "Mean ΔE", "Pass<3", "Matrix"]]
        history_table.extend(
            [
                record.timestamp,
                record.dataset_name,
                record.strategy,
                record.search_method,
                f"{record.mean_before:.3f} → {record.mean_after:.3f}",
                f"{record.pass_rate_before_3:.1%} → {record.pass_rate_after_3:.1%}",
                record.matrix_status,
            ]
            for record in history
        )
        story.append(styled_table(history_table, [42 * mm, 42 * mm, 24 * mm, 34 * mm, 38 * mm, 38 * mm, 22 * mm]))
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


@dataclass(frozen=True)
class _Formula:
    expression: str
    cached: float


def _column_name(index: int) -> str:
    value = index + 1
    name = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_cell(reference: str, value: Any, style: int = 0) -> str:
    style_attribute = f' s="{style}"' if style else ""
    if isinstance(value, _Formula):
        return f'<c r="{reference}"{style_attribute}><f>{html.escape(value.expression)}</f><v>{value.cached:.12g}</v></c>'
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{style_attribute}><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return f'<c r="{reference}"{style_attribute}><v>{value}</v></c>'
    escaped = html.escape("" if value is None else str(value))
    return f'<c r="{reference}" t="inlineStr"{style_attribute}><is><t xml:space="preserve">{escaped}</t></is></c>'


def _sheet_xml(
    rows: Sequence[Sequence[Any]],
    *,
    styles: Optional[Sequence[Sequence[int]]] = None,
    widths: Sequence[float] = (),
    freeze_rows: int = 1,
    autofilter: bool = False,
) -> str:
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row):
            style = 0
            if styles is not None and row_index - 1 < len(styles) and column_index < len(styles[row_index - 1]):
                style = styles[row_index - 1][column_index]
            cells.append(_xlsx_cell(f"{_column_name(column_index)}{row_index}", value, style))
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    column_xml = ""
    if widths:
        column_xml = "<cols>" + "".join(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>' for index, width in enumerate(widths, start=1)) + "</cols>"
    pane = ""
    if freeze_rows:
        pane = f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="{freeze_rows}" topLeftCell="A{freeze_rows + 1}" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
    else:
        pane = '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
    max_columns = max((len(row) for row in rows), default=1)
    max_rows = max(len(rows), 1)
    filter_xml = f'<autoFilter ref="A1:{_column_name(max_columns - 1)}{max_rows}"/>' if autofilter and rows else ""
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:{_column_name(max_columns - 1)}{max_rows}"/>{pane}<sheetFormatPr defaultRowHeight="15"/>{column_xml}<sheetData>{"".join(row_xml)}</sheetData>{filter_xml}<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/></worksheet>'''


def save_analysis_xlsx(
    destination: PathLike,
    dataset: ImatestDataset,
    result: OptimizationResult,
    *,
    region_label: str = "",
    xml_diff: str = "",
    history: Sequence[OptimizationHistoryRecord] = (),
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary_rows: list[list[Any]] = [
        ["MatrixCorrect Engineering Report", "Value", "Before", "After"],
        ["Source CSV", dataset.source_path.name, "", ""],
        ["Region", region_label, "", ""],
        ["Strategy", result.strategy, "", ""],
        ["Search Method", result.search_method, "", ""],
        ["Regularization", result.regularization, "", ""],
        ["Blend", "N/A" if result.search_method == "engineering-boundary" else result.blend, "", ""],
        ["Saturation Factor", result.saturation_factor, "", ""],
        [
            "Average ΔE00",
            _Formula("D9", result.mean_after),
            _Formula("AVERAGE('Patches'!E2:E19)", result.mean_before),
            _Formula("AVERAGE('Patches'!F2:F19)", result.mean_after),
        ],
        ["Mean Improve", _Formula("IF(C9=0,0,(C9-D9)/C9)", result.mean_improvement_percent / 100.0), "", ""],
        ["Chroma Ratio", "", result.saturation_ratio_before, result.saturation_ratio_after],
        ["Matrix Health", result.matrix_health.status, "", ""],
        [],
        ["Pass Rate", "Threshold", "Before", "After"],
    ]
    for index, threshold in enumerate(result.pass_rates.thresholds):
        row_number = 15 + index
        summary_rows.append(
            [
                f"ΔE<{threshold:g}",
                threshold,
                _Formula(f"COUNTIF('Patches'!E$2:E$25,\"<\"&B{row_number})/24", result.pass_rates.before_rate(index)),
                _Formula(f"COUNTIF('Patches'!F$2:F$25,\"<\"&B{row_number})/24", result.pass_rates.after_rate(index)),
            ]
        )
    summary_rows.extend([[], ["Before Matrix", "C0", "C1", "C2"]])
    summary_rows.extend([[f"R{index}", *row] for index, row in enumerate(result.original_matrix)])
    summary_rows.append([])
    summary_rows.append(["Delta Correction", "C0", "C1", "C2"])
    summary_rows.extend([[f"R{index}", *row] for index, row in enumerate(result.correction_matrix)])
    summary_rows.append([])
    summary_rows.append(["After Matrix", "C0", "C1", "C2"])
    summary_rows.extend([[f"R{index}", *row] for index, row in enumerate(result.optimized_matrix)])
    summary_styles = [[0] * 4 for _ in summary_rows]
    for row_index in (0, 13, 19, 24, 29):
        if row_index < len(summary_styles):
            summary_styles[row_index] = [1] * 4 if row_index == 0 else [3] * 4
    summary_styles[9][1] = 4
    for row_index in range(14, 18):
        summary_styles[row_index][2] = 4
        summary_styles[row_index][3] = 4
    summary_styles[11][1] = {"PASS": 6, "WARNING": 7, "FAIL": 8}.get(result.matrix_health.status, 0)

    patch_rows: list[list[Any]] = [["Zone", "Name", "Category", "Weight", "ΔE Before", "ΔE After", "Improve %", "ΔL Before", "ΔL After", "ΔC Before", "ΔC After", "Δh Before", "Δh After", "Regression", "Status", "Module"]]
    patch_styles: list[list[int]] = [[2] * 16]
    for index, patch in enumerate(result.patch_results, start=2):
        patch_rows.append(
            [
                patch.zone,
                patch.name,
                patch.category,
                patch.priority_weight,
                patch.delta_e_before,
                patch.delta_e_after,
                _Formula(f"IF(E{index}=0,0,(E{index}-F{index})/E{index})", patch.improvement_percent / 100.0),
                patch.delta_l_before,
                patch.delta_l_after,
                patch.delta_c_before,
                patch.delta_c_after,
                patch.delta_h_before,
                patch.delta_h_after,
                patch.regression,
                patch.regression_status,
                patch.module_hint,
            ]
        )
        status_style = {"PASS": 6, "WARNING": 7, "FAIL": 8}.get(patch.regression_status, 0)
        patch_styles.append([0, 0, 0, 5, 5, 5, 4, 5, 5, 5, 5, 5, 5, 5, status_style, 0])

    engineering_rows = [["Check", "Status", "Value", "Limit", "Meaning"]]
    engineering_styles = [[2] * 5]
    for check in result.matrix_health.checks:
        engineering_rows.append([check.name, check.status, check.value, check.limit, check.message])
        engineering_styles.append([0, {"PASS": 6, "WARNING": 7, "FAIL": 8}.get(check.status, 0), 0, 0, 0])
    engineering_rows.extend([[], ["Category", "Count", "Mean Before", "Mean After", "Improve", "Regression", "Pass<3 Before", "Pass<3 After"]])
    engineering_styles.extend([[0] * 8, [2] * 8])
    for item in result.category_statistics:
        engineering_rows.append([item.category, item.count, item.mean_before, item.mean_after, item.improved, item.regressed, item.pass_rate_before_3, item.pass_rate_after_3])
        engineering_styles.append([0, 0, 5, 5, 0, 0, 4, 4])

    diagnosis_rows = [["Module", "Confidence", "Severity", "Root Cause", "Evidence", "Action"]]
    diagnosis_styles = [[2] * 6]
    for item in result.diagnostics:
        diagnosis_rows.append([item.module, item.confidence, item.severity, item.root_cause, " · ".join(item.evidence), item.action])
        diagnosis_styles.append([0, 4, 0, 0, 0, 0])
    diagnosis_rows.extend([[], ["Explainable Optimization"]])
    diagnosis_styles.extend([[0] * 6, [3] * 6])
    for line in result.explainability:
        diagnosis_rows.append([line])
        diagnosis_styles.append([0])

    sheet_specs: list[tuple[str, str]] = [
        ("Summary", _sheet_xml(summary_rows, styles=summary_styles, widths=[28, 58, 18, 18], freeze_rows=1)),
        ("Patches", _sheet_xml(patch_rows, styles=patch_styles, widths=[8, 17, 13, 10, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 13, 30], freeze_rows=1, autofilter=True)),
        ("Engineering", _sheet_xml(engineering_rows, styles=engineering_styles, widths=[28, 13, 28, 35, 54, 14, 16, 16], freeze_rows=1)),
        ("Diagnostics", _sheet_xml(diagnosis_rows, styles=diagnosis_styles, widths=[22, 14, 14, 48, 72, 60], freeze_rows=1)),
    ]
    if history:
        rows: list[list[Any]] = [["Timestamp", "Dataset", "Region", "Strategy", "Method", "Mean Before", "Mean After", "Pass<3 Before", "Pass<3 After", "Matrix Status"]]
        styles = [[2] * 10]
        for record in history:
            rows.append([record.timestamp, record.dataset_name, record.region_label, record.strategy, record.search_method, record.mean_before, record.mean_after, record.pass_rate_before_3, record.pass_rate_after_3, record.matrix_status])
            styles.append([0, 0, 0, 0, 0, 5, 5, 4, 4, {"PASS": 6, "WARNING": 7, "FAIL": 8}.get(record.matrix_status, 0)])
        sheet_specs.append(("History", _sheet_xml(rows, styles=styles, widths=[24, 26, 60, 16, 24, 14, 14, 16, 16, 14], freeze_rows=1, autofilter=True)))
    if xml_diff:
        rows = [["XML Diff"], *[[line] for line in xml_diff.splitlines()]]
        styles = [[1], *[[0] for _ in xml_diff.splitlines()]]
        sheet_specs.append(("XML Diff", _sheet_xml(rows, styles=styles, widths=[140], freeze_rows=1)))

    content_types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>']
    content_types.extend(f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for index in range(1, len(sheet_specs) + 1))
    content_types.append("</Types>")
    workbook_sheets = "".join(f'<sheet name="{html.escape(name)}" sheetId="{index}" r:id="rId{index}"/>' for index, (name, _) in enumerate(sheet_specs, start=1))
    workbook_rels = "".join(f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>' for index in range(1, len(sheet_specs) + 1))
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="3"><font><sz val="10"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos Display"/></font><font><b/><color rgb="FF173B74"/><sz val="10"/><name val="Aptos"/></font></fonts><fills count="8"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF173B74"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFEAF0FF"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE8F7F0"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFDE9E7"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF8FAFC"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="FFD8E0EB"/></left><right style="thin"><color rgb="FFD8E0EB"/></right><top style="thin"><color rgb="FFD8E0EB"/></top><bottom style="thin"><color rgb="FFD8E0EB"/></bottom><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="9"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf><xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/><xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="0" fontId="2" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/><xf numFmtId="0" fontId="2" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/><xf numFmtId="0" fontId="2" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "".join(content_types))
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>')
        archive.writestr("docProps/core.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>MatrixCorrect Engineering Report</dc:title><dc:creator>MatrixCorrect</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')
        archive.writestr("docProps/app.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>MatrixCorrect</Application></Properties>')
        archive.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView/></bookViews><sheets>{workbook_sheets}</sheets><calcPr calcId="191029" fullCalcOnLoad="1" forceFullCalc="1" calcMode="auto"/></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{workbook_rels}<Relationship Id="rId{len(sheet_specs) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr("xl/styles.xml", styles_xml)
        for index, (_, sheet) in enumerate(sheet_specs, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet)
    return path


def save_analysis_report(
    destination: PathLike,
    dataset: ImatestDataset,
    result: OptimizationResult,
    *,
    region_label: str = "",
    xml_diff: str = "",
    history: Sequence[OptimizationHistoryRecord] = (),
) -> Path:
    suffix = Path(destination).suffix.lower()
    options = {"region_label": region_label, "xml_diff": xml_diff, "history": history}
    if suffix == ".csv":
        return save_analysis_csv(destination, dataset, result, **options)
    if suffix in {".html", ".htm"}:
        return save_analysis_html(destination, dataset, result, **options)
    if suffix == ".pdf":
        return save_analysis_pdf(destination, dataset, result, **options)
    if suffix == ".xlsx":
        return save_analysis_xlsx(destination, dataset, result, **options)
    raise ValueError("报告扩展名必须是 .csv、.html、.pdf 或 .xlsx。")
