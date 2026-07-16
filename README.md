# TuneLab

TuneLab 是一个面向 Qualcomm Camera 的跨平台调试工作台。目前包含 CC13 色彩矩阵与 Gamma15 两套独立工作流：读取 Imatest 数据、自动匹配 XML region、执行优化与工程校验，并安全回写目标配置。

作者联系邮箱：<kaiyi.jiang@thundersoft.com>

当前版本包含：

- Windows / macOS 原生 Tk 桌面界面，兼容 Python 3.9.6+
- Imatest ColorChecker RGB 段自动识别
- D65、D50、CWF、TL84、A、H 和显式 CCT 文件名推断
- Qualcomm CC13 多层 trigger 解析（DRC / AEC / LED / Lux / CCT）
- CCT 精确命中与 transition/gap 警告
- 带 Regularization 的多目标 Loss（ΔE / ΔC / Δh / P90 / Regression / Saturation / Smoothness）
- 默认重点优化并保护 13、14、15 号 Patch；重点 Patch 与权重可编辑
- 最终系数范围默认 `[-3,3]`、Row Sum=1、Condition Number、Determinant、Rank、Fixed Point 检查
- Regression Protection、Saturation Penalty 与可调“饱和度系数”（默认 1.0）
- CIE Lab / CIEDE2000 改前改后模拟
- 24 色块逐项 ΔE/ΔL/ΔC/Δh、Improve%、Regression 与分类统计
- Gamma、AWB、CC、SCE、2D LUT、CV/Saturation 的 Confidence / Root Cause / Action 诊断
- Matrix History、多轮优化记录、XML Unified Diff 与参数持久化
- CSV、HTML、PDF、Excel 四种工程报告
- A / CWF / D65 / TL84 全量 Golden Dataset 回归
- 独立 Qualcomm Gamma 模块：Imatest Stepchart、连续有效灰阶识别、动态点数/位宽 R/G/B LUT 优化与安全 XML 回写
- 只替换目标 `<c_tab><c>` 的 XML 定点回写及回读校验
- CLI 批处理和 UTF-8 BOM 分析 CSV 导出

## 快速运行

需要 Python 3.9.6 或更高版本。你的 `python3` 如果是 3.9.6 可以直接使用，不要求 Python 3.10。

```bash
python3 -m tunelab
```

也可以直接运行 `python3 run_tunelab.py`。

如果希望安装命令行入口：

```bash
python3 -m pip install -e .
tunelab
```

ReportLab 是可选依赖；未安装时 GUI、CC/Gamma 优化以及 HTML/Excel/CSV 导出均可正常运行，只有 PDF 导出会提示缺少依赖。需要 PDF 时执行 `python3 -m pip install -e '.[pdf]'` 或 `python3 -m pip install reportlab`。

macOS 应用构建：

```bash
python3 scripts/build.py
```

Windows EXE 构建必须在 Windows 上执行：

```bat
py -m pip install pyinstaller reportlab pillow
py scripts\build_windows.py
```

生成文件位于 `dist\TuneLab.exe`。

## 推荐工作流

1. 先把 AWB、曝光、LSC 和 Gamma 调稳定。Qualcomm 文档明确要求 Gamma 在 CCM 前准确，Gamma 改动后需要重调 CC。
2. 在目标光源和 CCT 下拍摄 ColorChecker，导出 Imatest summary CSV。
3. 打开 CSV 与 CC XML。工具会从 `D65_normal.jpg` 一类名称推断 6500 K，并选中 `5800-6500 K` region。
4. 核对完整 trigger path。若 CCT 位于两个 region 之间，运行时通常会插值；工具会选最近端点并明确报警，保存前必须人工确认端点。
5. 设置 Strategy、Regularization、饱和度系数、重点 Patch、权重与系数边界；参数会自动写入内部 `settings.json`。配置菜单可用于显式导入/导出备份，标准 JSON 不使用注释。
6. 比较两张独立的改前/改后 a\*b\* 图。两图始终共用坐标范围和 1:1 比例；勾选 Show Motion 查看 Before→After 轨迹，点击 Patch 与表格可双向联动。图中只允许滚轮缩放，双击或“恢复 a*b* 视图”会按全部 Ideal/Before/After 点重新自动适应。
7. 检查“工程统计”和“诊断与解释”：Matrix Health 不得为 FAIL，Pass Rate 不得下降，重点 Patch 不得明显退化。
8. 在 History / XML Diff 确认只修改目标 region 的 9 个 `c_tab/c` 数值，再点击主工具栏“保存 XML”；确认后会覆盖当前加载的原 XML。
9. 导出 HTML/PDF/Excel/CSV 报告，编译/烧录后重新拍摄 ColorChecker 验证。界面中的改后图是模型模拟，不能替代上机复测。

## CLI 示例

```bash
python3 -m tunelab.ccm.cli \
  --csv source/D65_normal_summary.csv \
  --xml source/cc13_ipe_v2.xml \
  --cct 6500 \
  --out outputs/cc13_ipe_v2_D65_optimized.xml \
  --report outputs/D65_ccm_analysis.csv \
  --strategy balanced \
  --saturation-factor 1.0 \
  --focus-patches 13,14,15 \
  --focus-weight 4 \
  --coefficient-min -3 \
  --coefficient-max 3 \
  --json
```

可选矩阵组合约定：

- `--composition pre`（默认）：列向量/CC13 行主序，`M_new = A × M_old`
- `--composition post_transposed`：旧 Excel/C7 行向量等价形式，`M_new = M_old × Aᵀ`

请以平台实际寄存器/Chromatix 约定为准。两种方式的模型预测一致，但写入矩阵的排列不同。

## 指标命名

界面中的“色差 ΔE00”就是 **CIEDE2000 色差**。`00` 表示 2000 年定义的 CIEDE2000 公式，不是 Patch 编号，也不是百分比；数值越小代表视觉色差越小。TuneLab 没有改变计算公式，只将色块明细名称统一为“色差 ΔE00”，并在界面中注明 CIEDE2000，避免与 ΔE76 等其他公式混淆。

## 优化算法

1. 将 Imatest 的 measured / ideal sRGB 去 Gamma，转换为线性 RGB。
2. 将 ideal 色块的线性亮度缩放到 measured 亮度。这样 CC 只承担色度修正，曝光和 Gamma 的 L\* 误差不会被错误塞进 CCM。
3. 对 1-18 彩色色块做带正则化的加权最小二乘；13/14/15 默认获得额外权重，并独立保护 ΔE、ΔC、Δh。
4. 每个输出通道都通过 KKT 方程强制 `C0 + C1 + C2 = 1`，保持中性轴。
5. 自动搜索 Regularization 与强度，并沿 3 组行内系数对做边界搜索；即使某个系数贴住 `[-3,3]`，仍能沿边界保持 Row Sum=1。
6. 多目标 Loss 同时包含 ΔE、ΔC、Δh、ΔL、P90、Patch Regression、Saturation、Matrix Regularization、Smoothness 与 Engineering Penalty。
7. 硬保护拒绝 Matrix FAIL、Pass Rate 下降、重点 Patch 明显退化、整体/局部过饱和和异常系数候选。
8. 对最终 Matrix 执行系数范围、Row Sum、Condition、Determinant、Rank、Smoothness、Max Delta 和 Q12 Fixed Point Simulation。

Pass Rate 的统一规则为 **CIEDE2000 ΔE00 ≤ 2 / 3 / 5 / 10**，边界值计为通过。Optimizer、Golden Regression、GUI 以及 CSV/HTML/PDF/Excel 报告全部使用同一规则。Neutral Patch 19–24 参加独立 Regression Protection，但不加入彩色色块 CCM 拟合目标。

`saturation_factor` 的语义是“相对 Ideal Chroma 的单次目标缩放”：拟合目标应用一次，Loss 和硬门禁仅对同一目标做评估，不会再次乘方或重复增饱和。

## Gamma 优化

从“工具 → Gamma 优化...”打开独立页面，也可运行 `python3 -m tunelab.gamma.ui`。Gamma 模块与 CC 流程隔离：

1. 打开 Imatest Gray/Stepchart CSV 与 Qualcomm Gamma15 XML，选择或按 CCT 自动匹配 Region。
2. 默认以相邻 `delta_pixel = Pixel(i) - Pixel(i+1) ≥ 8` 识别最长连续区间；`source/gray_summary.csv` 对应 B9:B20，即 Zone 1–12 共 12 阶。阈值 6/8/10 可编辑，断点之后不会继续累计。
3. 仅连续可区分的灰阶参加 Gamma/Local Gamma 拟合；用户要求提高阶数时，后续灰阶只作为 `ΔPixel` 工程间隔约束，不混入原始有效区间的 Gamma 回归。
4. “Gamma 提亮系数”默认 `1.0`：`1.0` 保持标称亮度，数值越大目标 LUT 中间调越亮。它不是 Imatest Density/Exposure 表中约 `0.43` 的 Global Gamma 斜率；两者会分别显示。
5. “目标可识别阶数”可设为自动或手动数值。自动模式禁止从当前 12 阶退化；使用样例、阈值 8、目标 14 时，Golden Test 要求 After 至少达到连续 14 阶。
6. LUT 点数和整数范围按当前 XML 动态解析。样例 `gamma15_ipe_v2.xml` 为 257 点、0–1023，工具也测试 65 点/0–255 等格式，不再硬编码 257 点。
7. 多目标 Loss 包含灰阶目标、Local Gamma、阶间隔、LUT 平滑、原 LUT 变化、高光/暗部保护与 RGB 灰阶偏差。结果必须单调不下降、无局部反转、无异常突变并保持首尾点及 XML 最大亮度。
8. 页面包含曲线对比、工程统计、诊断与解释、History/XML Diff；菜单提供文件、配置、工具、帮助。保存前会确认，并默认覆盖当前加载的原 XML，只替换当前 Region 的三个 LUT。

## 模块边界

一个 3×3 CCM 是全局线性变换，不可能独立修正所有局部色相。TuneLab CCM 模块的职责边界是：

| 误差类型 | 首选模块 |
| --- | --- |
| 全局 RGB 通道串扰、多个色相同方向偏移 | CC / CCM |
| 全局饱和度偏高或偏低、色相基本正确 | CV / Saturation |
| 肤色或单个色域局部偏移 | SCE / 2D LUT |
| 中性色偏 | 先 AWB，再检查 CC |
| L\*、灰阶、暗部/高光亮度误差 | 曝光 / Gamma / TMC |

CSV 是完整 ISP 输出 JPEG，因此 TuneLab CCM 模块得到的是“对现有输出做 Delta CCM”的工程近似。若 SCE、CV、2D LUT、Gamma 或 TMC 已经大幅改变颜色，应先关闭/固定这些模块，或在每次修改后重新采集 CSV。

## 打包 Windows / macOS

在目标系统上安装 PyInstaller，然后运行：

```bash
python3 -m pip install pyinstaller
python3 scripts/build.py
```

产物位于 `dist/`：

- Windows：`dist/TuneLab/TuneLab.exe`
- macOS：`dist/TuneLab.app`

PyInstaller 不能跨系统生成原生包，所以 Windows 和 macOS 需要各自构建一次。

## 测试

```bash
python3 -m unittest discover -v
python3 -m tunelab.regression --source source \
  --json outputs/golden_regression.json \
  --html outputs/golden_regression.html
```

Golden Regression 会遍历 `source` 中的全部文件，并按格式路由：CCM Golden 使用兼容的 ColorChecker CSV/CC XML；Gamma Regression 使用 Stepchart CSV/Gamma XML。当前 8 个 CCM 光源/照度 Case（A、CWF、D65、TL84）必须全部满足：Average ΔE 改善、至少一个 `ΔE00≤2/3/5/10` Pass Rate 提升、Neutral 19–24 无明显退化、无 FAIL Patch、无工程 FAIL、无越界 Matrix、Row Sum=1 且饱和度偏差不扩大。

## 资料依据（Reference）

算法设计、工程约束与 Qualcomm XML/trigger 解释仅依据 Qualcomm 官方原生 Camera ISP / Chromatix 文档：

- Qualcomm `80-PT841-101_REV_AB_XR_Camera_Tuning_Guide.pdf` 第 59-62 页：CCT region、Gamma 前置、Gamma 曲线/Region 与逐 region 调整。
- Qualcomm `80-35348-60_REV_AD_Qualcomm_Spectra_7XX_Deep_Dive.pdf` 第 82-83 页：3×3 CCM、硬件系数范围和行和约束。
- Qualcomm `80-35348-60_REV_AD_Qualcomm_Spectra_7XX_Deep_Dive.pdf` 第 377 页：平台 Gamma table 为 65 点、0–255 的实例。
- Qualcomm `80-74889-81_REV_AH_Qualcomm_Spectra_1080_Deep_Dive.pdf` 第 414-421 页：CC1.3 / CC1.4、Gamma1.5 trigger/color-format 以及 257-entry 10-bit Gamma 实例。

更完整的模块划分、数据流和扩展路线见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
