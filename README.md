# MatrixCorrect

MatrixCorrect 是一个面向 Qualcomm Camera CC13 XML 的跨平台色彩还原工具。它可以读取 Imatest `*_summary.csv`，自动定位 XML 中的 CCT region，分析 24 色块，生成改前/改后 CCM、a\*b\* 色差图、逐色块 ΔE00 与改善百分比，并把选中 region 的新矩阵安全写回 XML。

当前版本包含：

- Windows / macOS 原生 Tk 桌面界面，无运行时第三方依赖
- Imatest ColorChecker RGB 段自动识别
- D65、D50、D55、D75、A、TL84、CWF 和显式 CCT 文件名推断
- Qualcomm CC13 多层 trigger 解析（DRC / AEC / LED / Lux / CCT）
- CCT 精确命中与 transition/gap 警告
- 线性 sRGB 域、行和为 1 的约束 CCM 优化
- CIE Lab / CIEDE2000 改前改后模拟
- 24 色块逐项改善、回退和模块建议
- 只替换目标 `<c_tab><c>` 的 XML 定点回写及回读校验
- CLI 批处理和 UTF-8 BOM 分析 CSV 导出

## 快速运行

需要 Python 3.10 或更高版本。

```bash
python -m matrixcorrect
```

也可以直接运行 `python run_matrixcorrect.py`。

如果希望安装命令行入口：

```bash
python -m pip install -e .
matrixcorrect
```

macOS 自带的 Xcode Command Line Tools Python 可能仍是 3.9；请使用 python.org、Homebrew 或你现有的 Python 3.10+。

## 推荐工作流

1. 先把 AWB、曝光、LSC 和 Gamma 调稳定。Qualcomm 文档明确要求 Gamma 在 CCM 前准确，Gamma 改动后需要重调 CC。
2. 在目标光源和 CCT 下拍摄 ColorChecker，导出 Imatest summary CSV。
3. 打开 CSV 与 CC XML。工具会从 `D65_normal.jpg` 一类名称推断 6500 K，并选中 `5800-6500 K` region。
4. 核对完整 trigger path。若 CCT 位于两个 region 之间，运行时通常会插值；工具会选最近端点并明确报警，保存前必须人工确认端点。
5. 点击“自动优化”，比较改前/改后 a\*b\* 图、平均/最大 ΔE00、逐色块改善与回退。
6. 检查“模块建议与警告”。亮度主导问题应回到 Gamma/TMC/曝光；局部肤色或单色问题应使用 SCE/2D LUT；全局 Chroma 可交给 CV/饱和度。
7. 保存为新的 XML。工具不会覆盖原文件，且只修改所选 region 的 9 个 `c_tab/c` 数值。
8. 编译/烧录后重新拍摄 ColorChecker，再次导入 CSV 验证。界面中的改后图是模型模拟，不能替代上机复测。

## CLI 示例

```bash
python -m matrixcorrect.cli \
  --csv Source/D65_normal_summary.csv \
  --xml Source/cc13_ipe_v2.xml \
  --cct 6500 \
  --out outputs/cc13_ipe_v2_D65_optimized.xml \
  --report outputs/D65_ccm_analysis.csv \
  --json
```

可选矩阵组合约定：

- `--composition pre`（默认）：列向量/CC13 行主序，`M_new = A × M_old`
- `--composition post_transposed`：旧 Excel/C7 行向量等价形式，`M_new = M_old × Aᵀ`

请以平台实际寄存器/Chromatix 约定为准。两种方式的模型预测一致，但写入矩阵的排列不同。

## 优化算法

1. 将 Imatest 的 measured / ideal sRGB 去 Gamma，转换为线性 RGB。
2. 将 ideal 色块的线性亮度缩放到 measured 亮度。这样 CC 只承担色度修正，曝光和 Gamma 的 L\* 误差不会被错误塞进 CCM。
3. 对 1-18 彩色色块做带正则化的加权最小二乘；肤色和 RGB/CMY 主色块有轻量权重。
4. 每个输出通道都通过 KKT 方程强制 `C0 + C1 + C2 = 1`，保持中性轴。
5. 自动搜索正则化与强度，再在 6 个独立自由度上用 ΔE00 做小步细化；目标函数同时惩罚 P90、色块回退、越界和过大的 Delta CCM。
6. 将 Delta CCM 与 XML 原矩阵组合，模拟新输出并计算 24 色块 ΔE00。

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
python -m pip install pyinstaller
python scripts/build.py
```

产物位于 `dist/`：

- Windows：`dist/MatrixCorrect/MatrixCorrect.exe`
- macOS：`dist/MatrixCorrect.app`

PyInstaller 不能跨系统生成原生包，所以 Windows 和 macOS 需要各自构建一次。

## 测试

```bash
python -m unittest discover -v
```

上传的 D65 样本是固定回归用例，覆盖 CSV 解析、CIEDE2000、五层 trigger、CCT 匹配、矩阵行和、XML 定点回写和报告导出。

## 资料依据

实现参考了工程内资料与本地 Qualcomm 文档，重点包括：

- `Source/20210817-CCM色彩矩阵调试分享-韦启宝.pptx`：CCM 行和、调试方向和 13-15 色块方法
- `Source/常用工具集-20210824.xlsx`：转置约定、C7/C6 输出与手动 fine tune
- `Documents/Codex/Qualcomm/80-PT841-101_REV_AB_XR_Camera_Tuning_Guide.pdf` 第 59-61 页：CCT region、Gamma 前置、Optimize/Simulate 和逐 region 调整
- `Documents/Codex/Qualcomm/80-35348-60_REV_AD_Qualcomm_Spectra_7XX_Deep_Dive.pdf` 第 82-83 页：3×3 CCM、范围 `[-15.99, 15.99]` 和行和为 1
- `Documents/Codex/Qualcomm/80-74889-81_REV_AH_Qualcomm_Spectra_1080_Deep_Dive.pdf` 第 414-415 页：CC1.3/CC1.4 color-format trigger 映射

更完整的模块划分、数据流和扩展路线见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
