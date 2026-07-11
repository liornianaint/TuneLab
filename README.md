# MatrixCorrect

MatrixCorrect 是一个面向 Qualcomm Camera CC13 XML 的跨平台色彩还原工具。它可以读取 Imatest `*_summary.csv`，自动定位 XML 中的 CCT region，分析 24 色块，生成改前/改后 CCM、a\*b\* 色差图、逐色块 ΔE00 与改善百分比，并把选中 region 的新矩阵安全写回 XML。

当前版本包含：

- Windows / macOS 原生 Tk 桌面界面，兼容 Python 3.9.6+
- Imatest ColorChecker RGB 段自动识别
- D65、D50、D55、D75、A、TL84、CWF 和显式 CCT 文件名推断
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
- 只替换目标 `<c_tab><c>` 的 XML 定点回写及回读校验
- CLI 批处理和 UTF-8 BOM 分析 CSV 导出

## 快速运行

需要 Python 3.9.6 或更高版本。你的 `python3` 如果是 3.9.6 可以直接使用，不要求 Python 3.10。

```bash
python3 -m matrixcorrect
```

也可以直接运行 `python3 run_matrixcorrect.py`。

如果希望安装命令行入口：

```bash
python3 -m pip install -e .
matrixcorrect
```

PDF 导出依赖 ReportLab，执行 `python3 -m pip install -e .` 时会自动安装。若只运行源码且尚未安装，可执行 `python3 -m pip install reportlab`。

## 推荐工作流

1. 先把 AWB、曝光、LSC 和 Gamma 调稳定。Qualcomm 文档明确要求 Gamma 在 CCM 前准确，Gamma 改动后需要重调 CC。
2. 在目标光源和 CCT 下拍摄 ColorChecker，导出 Imatest summary CSV。
3. 打开 CSV 与 CC XML。工具会从 `D65_normal.jpg` 一类名称推断 6500 K，并选中 `5800-6500 K` region。
4. 核对完整 trigger path。若 CCT 位于两个 region 之间，运行时通常会插值；工具会选最近端点并明确报警，保存前必须人工确认端点。
5. 设置 Strategy、Regularization、饱和度系数、重点 Patch、权重与系数边界；参数会自动写入内部 `settings.json`，无需手动保存；点击“自动优化”。
6. 比较两张独立的改前/改后 a\*b\* 图。两图始终共用坐标范围和 1:1 比例；勾选 Show Motion 查看 Before→After 轨迹，点击 Patch 查看详细 Lab、ΔE00、ΔL、ΔC、Δh。可用滚轮缩放、拖动平移，并用“恢复 a*b* 视图”回到完整数据范围。
7. 检查“工程统计”和“诊断与解释”：Matrix Health 不得为 FAIL，Pass Rate 不得下降，重点 Patch 不得明显退化。
8. 在 History / XML Diff 确认只修改目标 region 的 9 个 `c_tab/c` 数值，再点击主工具栏“保存 XML”，通过“另存为”保存新的 XML；默认文件名为 `原文件名_optimized.xml`，不会默认覆盖原文件。
9. 导出 HTML/PDF/Excel/CSV 报告，编译/烧录后重新拍摄 ColorChecker 验证。界面中的改后图是模型模拟，不能替代上机复测。

## CLI 示例

```bash
python3 -m matrixcorrect.cli \
  --csv Source/D65_normal_summary.csv \
  --xml Source/cc13_ipe_v2.xml \
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

界面中的“色差 ΔE00”就是 **CIEDE2000 色差**。`00` 表示 2000 年定义的 CIEDE2000 公式，不是 Patch 编号，也不是百分比；数值越小代表视觉色差越小。MatrixCorrect 没有改变计算公式，只将色块明细名称统一为“色差 ΔE00”，并在界面中注明 CIEDE2000，避免与 ΔE76 等其他公式混淆。

## 优化算法

1. 将 Imatest 的 measured / ideal sRGB 去 Gamma，转换为线性 RGB。
2. 将 ideal 色块的线性亮度缩放到 measured 亮度。这样 CC 只承担色度修正，曝光和 Gamma 的 L\* 误差不会被错误塞进 CCM。
3. 对 1-18 彩色色块做带正则化的加权最小二乘；13/14/15 默认获得额外权重，并独立保护 ΔE、ΔC、Δh。
4. 每个输出通道都通过 KKT 方程强制 `C0 + C1 + C2 = 1`，保持中性轴。
5. 自动搜索 Regularization 与强度，并沿 3 组行内系数对做边界搜索；即使某个系数贴住 `[-3,3]`，仍能沿边界保持 Row Sum=1。
6. 多目标 Loss 同时包含 ΔE、ΔC、Δh、ΔL、P90、Patch Regression、Saturation、Matrix Regularization、Smoothness 与 Engineering Penalty。
7. 硬保护拒绝 Matrix FAIL、Pass Rate 下降、重点 Patch 明显退化、整体/局部过饱和和异常系数候选。
8. 对最终 Matrix 执行系数范围、Row Sum、Condition、Determinant、Rank、Smoothness、Max Delta 和 Q12 Fixed Point Simulation。

## 模块边界

一个 3×3 CCM 是全局线性变换，不可能独立修正所有局部色相。MatrixCorrect 的职责边界是：

| 误差类型 | 首选模块 |
| --- | --- |
| 全局 RGB 通道串扰、多个色相同方向偏移 | CC / CCM |
| 全局饱和度偏高或偏低、色相基本正确 | CV / Saturation |
| 肤色或单个色域局部偏移 | SCE / 2D LUT |
| 中性色偏 | 先 AWB，再检查 CC |
| L\*、灰阶、暗部/高光亮度误差 | 曝光 / Gamma / TMC |

CSV 是完整 ISP 输出 JPEG，因此 MatrixCorrect 得到的是“对现有输出做 Delta CCM”的工程近似。若 SCE、CV、2D LUT、Gamma 或 TMC 已经大幅改变颜色，应先关闭/固定这些模块，或在每次修改后重新采集 CSV。

## 打包 Windows / macOS

在目标系统上安装 PyInstaller，然后运行：

```bash
python3 -m pip install pyinstaller
python3 scripts/build.py
```

产物位于 `dist/`：

- Windows：`dist/MatrixCorrect/MatrixCorrect.exe`
- macOS：`dist/MatrixCorrect.app`

PyInstaller 不能跨系统生成原生包，所以 Windows 和 macOS 需要各自构建一次。

## 测试

```bash
python3 -m unittest discover -v
python3 -m matrixcorrect.golden --source Source \
  --json outputs/golden_regression.json \
  --html outputs/golden_regression.html
```

Golden Regression 会去重遍历 `Source` / `source` 中的全部 CSV 与 XML。当前 8 个光源/照度 Case（A、CWF、D65、TL84）必须全部满足：Average ΔE 改善、至少一个 Pass Rate 提升、无 FAIL Patch、无工程 FAIL、无越界 Matrix、Row Sum=1 且饱和度偏差不扩大。

## 资料依据（Reference）

算法设计、工程约束与 Qualcomm XML/trigger 解释仅依据本地 `Documents/Codex/Qualcomm` 仓中的 Qualcomm 官方原生 Camera ISP / Chromatix 文档：

- Qualcomm `80-PT841-101_REV_AB_XR_Camera_Tuning_Guide.pdf` 第 59-61 页：CCT region、Gamma 前置、Optimize/Simulate 和逐 region 调整。
- Qualcomm `80-35348-60_REV_AD_Qualcomm_Spectra_7XX_Deep_Dive.pdf` 第 82-83 页：3×3 CCM、硬件系数范围和行和约束。
- Qualcomm `80-74889-81_REV_AH_Qualcomm_Spectra_1080_Deep_Dive.pdf` 第 414-415 页：CC1.3 / CC1.4 color-format trigger 映射。

更完整的模块划分、数据流和扩展路线见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
