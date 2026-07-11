from __future__ import annotations

from statistics import mean

from .models import MatrixHealth, ModuleDiagnosis, PatchResult


def _bounded_confidence(value: float) -> float:
    return max(0.0, min(0.99, value))


def build_module_diagnostics(
    patches: list[PatchResult],
    saturation_ratio_before: float,
    saturation_ratio_after: float,
    matrix_health: MatrixHealth,
    saturation_target: float = 1.0,
) -> tuple[ModuleDiagnosis, ...]:
    neutral = [patch for patch in patches if patch.category == "Neutral"]
    skin = [patch for patch in patches if patch.category == "Skin"]
    primaries = [patch for patch in patches if patch.category in {"Primary", "Secondary"}]
    chromatic = [patch for patch in patches if patch.category != "Neutral"]

    neutral_l = mean(abs(patch.delta_l_before) for patch in neutral) if neutral else 0.0
    neutral_c = mean(abs(patch.delta_c_before) for patch in neutral) if neutral else 0.0
    skin_h = mean(abs(patch.delta_h_after) for patch in skin) if skin else 0.0
    skin_de = mean(patch.delta_e_after for patch in skin) if skin else 0.0
    primary_h = mean(abs(patch.delta_h_after) for patch in primaries) if primaries else 0.0
    primary_de = mean(patch.delta_e_after for patch in primaries) if primaries else 0.0
    localized = [patch for patch in chromatic if abs(patch.delta_h_after) > 6.0 and patch.delta_e_after > 3.0]
    regressions = [patch for patch in chromatic if patch.regression > 0.05]

    gamma_conf = _bounded_confidence(neutral_l / 12.0)
    awb_conf = _bounded_confidence(neutral_c / 6.0)
    cc_conf = _bounded_confidence((primary_h / 12.0 + primary_de / 10.0) / 2.0)
    sce_conf = _bounded_confidence((skin_h / 10.0 + skin_de / 8.0) / 2.0)
    lut_conf = _bounded_confidence((len(localized) / max(len(chromatic), 1)) * 2.5)
    sat_error = abs(saturation_ratio_after - saturation_target)
    cv_conf = _bounded_confidence(sat_error / 0.12)

    def severity(confidence: float) -> str:
        return "HIGH" if confidence >= 0.70 else "MEDIUM" if confidence >= 0.40 else "LOW"

    return (
        ModuleDiagnosis(
            "Gamma/TMC",
            gamma_conf,
            severity(gamma_conf),
            f"中性色平均 |dL*|={neutral_l:.2f}，亮度误差不应由 CC 矩阵承担。",
            tuple(f"Patch {patch.zone}: dL*={patch.delta_l_before:+.2f}" for patch in neutral[:6]),
            "先校正曝光、Gamma 和 tone mapping，再重新拍摄 ColorChecker。",
        ),
        ModuleDiagnosis(
            "AWB",
            awb_conf,
            severity(awb_conf),
            f"中性色平均 |dC*|={neutral_c:.2f}，反映白平衡或中性轴残余色偏。",
            tuple(f"Patch {patch.zone}: dC*={patch.delta_c_before:+.2f}" for patch in neutral[:6]),
            "检查 R/G/B gain、光源稳定性和灰块 ROI；AWB 稳定后再接受 CCM。",
        ),
        ModuleDiagnosis(
            "CC",
            cc_conf,
            severity(cc_conf),
            f"RGB/CMY 平均 After dE00={primary_de:.2f}、|dh|={primary_h:.2f} deg。",
            tuple(f"Patch {patch.zone}: dE {patch.delta_e_before:.2f}->{patch.delta_e_after:.2f}" for patch in primaries),
            f"当前矩阵健康度 {matrix_health.status}；CC 只处理跨多个色相的一致性通道串扰。",
        ),
        ModuleDiagnosis(
            "SCE",
            sce_conf,
            severity(sce_conf),
            f"肤色 After 平均 dE00={skin_de:.2f}、|dh|={skin_h:.2f} deg。",
            tuple(f"Patch {patch.zone}: dE {patch.delta_e_after:.2f}, dh={patch.delta_h_after:+.1f} deg" for patch in skin),
            "若肤色仍异常而 Primary/Secondary 已稳定，使用 SCE 做局部肤色修正。",
        ),
        ModuleDiagnosis(
            "2D LUT",
            lut_conf,
            severity(lut_conf),
            f"仍有 {len(localized)} 个局部 hue 异常 Patch；全局 CCM 继续追逐会造成其它 Patch regression。",
            tuple(f"Patch {patch.zone}: dE={patch.delta_e_after:.2f}, dh={patch.delta_h_after:+.1f} deg" for patch in localized[:8]),
            "把孤立色域交给 2D LUT，并保持 LUT 邻域连续和肤色保护。",
        ),
        ModuleDiagnosis(
            "CV/Saturation",
            cv_conf,
            severity(cv_conf),
            f"全局 Chroma ratio {saturation_ratio_before:.3f}->{saturation_ratio_after:.3f}，目标为 {saturation_target:.3f}。",
            tuple(f"Patch {patch.zone}: C* {patch.chroma_before:.1f}->{patch.chroma_after:.1f}/{patch.chroma_ideal:.1f}" for patch in chromatic if abs(patch.delta_c_after) > 4.0)[:8],
            "色相正确但整体 Chroma 偏离时使用 CV；不要用激进 CCM 伪造饱和度改善。",
        ),
        ModuleDiagnosis(
            "Regression Protection",
            _bounded_confidence(len(regressions) / max(len(chromatic), 1)),
            "HIGH" if any(patch.regression_status == "FAIL" for patch in regressions) else "LOW",
            f"After 有 {len(regressions)} 个 Patch 回退，其中 FAIL={sum(patch.regression_status == 'FAIL' for patch in regressions)}。",
            tuple(f"Patch {patch.zone}: regression +{patch.regression:.3f}" for patch in regressions),
            "重点 Patch 与低 dE Patch 受硬门禁保护；仅允许不影响 Pass Rate 的轻微 trade-off。",
        ),
    )
