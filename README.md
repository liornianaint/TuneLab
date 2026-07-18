# TuneLab

TuneLab 是一个面向 Qualcomm Camera 的跨平台调试工作台。目前包含统一的 CCM / ColorChecker 校正、Gamma15 和图像分析器三个工作区。CCM 页面既可读取 Imatest CSV，也可直接从色卡图片自动取样；两种输入共用同一套 ΔE/ΔC/Δh 优化、回退保护、报告、History 和安全 XML 回写。

作者联系邮箱：<kaiyi.jiang@thundersoft.com>

当前版本包含：

- Windows / macOS 共用一套原生 macOS 风格 Tk 桌面界面；macOS 使用系统 San Francisco，Windows 优先使用 Segoe UI Variable，并为矩阵与 Diff 使用平台等宽字体；工具默认按当前屏幕尺寸展开
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
- TuneLab 内置图像分析器工作区：文件夹地址栏与连续分组、1–4 图像素取样、ROI 匹配、置信度门禁与 CSV 导出
- 统一 CCM / ColorChecker 工作区：CSV 或测试图输入、内置标准/自定义目标、24 色块自动识别、CC Region 选择、整图 CCM 仿真与 XML 覆盖回写
- JPG、PNG、TIFF、BMP 以及 HEIC/HEIF 解码（HEIC/HEIF 由默认依赖 `pillow-heif` 提供）
- 只替换目标 `<c_tab><c>` 的 XML 定点回写及回读校验
- CLI 批处理和 UTF-8 BOM 分析 CSV 导出

## 快速运行

需要 Python 3.9.6 或更高版本。源码工程推荐直接运行启动器：

```bash
python3 run_tunelab.py
```

启动器会在工程根目录创建或复用 `.venv`，使用该虚拟环境的 Python 启动 TuneLab。首次运行以及 `pyproject.toml` 依赖变化时，会自动执行一次可编辑安装；普通启动不会重复安装或访问网络。Windows 使用 `py run_tunelab.py`。

也可以手动管理环境并安装主命令入口：

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python3 -m pip install -e .
tunelab
```

NumPy、Pillow、OpenCV 与 ReportLab 都是 TuneLab 默认工程依赖，安装工程时会一并安装，无需再指定额外依赖组。若 OpenCV 因本机运行库问题无法导入，像素、ROI、直方图和 CSV 仍可使用，自动匹配会明确提示并临时采用较慢的 NumPy FFT 归一化相关后备路径。Tkinter 由 Python/操作系统提供，不是可通过 pip 安装的第三方包。

工程素材统一放在根目录 `sources/`。CCM、Gamma 与图像分析器的原生文件选择窗口都会优先从该目录打开；旧的单数目录名不再使用。

macOS 应用构建：

```bash
.venv/bin/python -m pip install pyinstaller
.venv/bin/python scripts/build.py
```

Windows EXE 构建必须在 Windows 上执行：

```bat
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python scripts\build_windows.py
```

生成文件位于 `dist\TuneLab.exe`。

## 推荐工作流

1. 先把 AWB、曝光、LSC 和 Gamma 调稳定。Qualcomm 文档明确要求 Gamma 在 CCM 前准确，Gamma 改动后需要重调 CC。
2. 在目标光源和 CCT 下拍摄 ColorChecker。可以导出 Imatest summary CSV，也可以直接使用包含完整 24 色块的实拍图。
3. 在“CCM / ColorChecker 校正”中选择 CSV 或图片输入，再打开 CC XML。工具会从测试图或 CSV 记录的 `D65_normal.jpg` 一类名称推断 6500 K，并选中 `5800-6500 K` region；目标图文件名不会参与 CCT 推断。
4. 核对完整 trigger path。若 CCT 位于两个 region 之间，运行时通常会插值；工具会选最近端点并明确报警，保存前必须人工确认端点。
5. 设置 Strategy、Regularization、饱和度系数、重点 Patch、权重与系数边界；参数会自动写入内部 `settings.json`。配置菜单可用于显式导入/导出备份，标准 JSON 不使用注释。
6. 比较两张独立的优化前/优化后 a\*b\* 色块图。两图始终共用坐标范围和 1:1 比例；勾选 Show Motion 查看 Measured→Optimized 轨迹，点击 Patch 与表格可双向联动。图中只允许滚轮缩放，双击或“恢复 a*b* 视图”会按全部 Target/Measured/Optimized 点重新自动适应。
7. 检查“工程统计”和“诊断与解释”：Matrix Health 不得为 FAIL，Pass Rate 不得下降，重点 Patch 不得明显退化。
8. 在 History / XML Diff 确认只修改目标 region 的 9 个 `c_tab/c` 数值，再点击主工具栏“保存 XML”；确认后会覆盖当前加载的原 XML。
9. 导出 HTML/PDF/Excel/CSV 报告，编译/烧录后重新拍摄 ColorChecker 验证。a\*b\* 优化后结果是色块级预测，不能替代上机复测。

## CLI 示例

```bash
python3 -m tunelab.ccm.cli \
  --csv sources/D65_normal_summary.csv \
  --xml sources/cc13_ipe_v2_改前.xml \
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

桌面工作流固定采用 Qualcomm CC13 行主序前乘约定：`M_new = A × M_old`。界面不再提供旧 Excel/C7 后乘选项。

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
2. 默认以相邻 `delta_pixel = Pixel(i) - Pixel(i+1) ≥ 8` 识别最长连续区间；灰阶 CSV 统一从 `sources/` 选择。阈值 6/8/10 可编辑，断点之后不会继续累计。
3. 仅连续可区分的灰阶参加 Gamma/Local Gamma 拟合；用户要求提高阶数时，后续灰阶只作为 `ΔPixel` 工程间隔约束，不混入原始有效区间的 Gamma 回归。
4. “Gamma 提亮系数”默认 `1.0`：`1.0` 保持标称亮度，数值越大目标 LUT 中间调越亮。它不是 Imatest Density/Exposure 表中约 `0.43` 的 Global Gamma 斜率；两者会分别显示。
5. “目标可识别阶数”可设为自动或手动数值。自动模式禁止从当前 12 阶退化；使用样例、阈值 8、目标 14 时，Golden Test 要求 After 至少达到连续 14 阶。
6. LUT 点数和整数范围按当前 XML 动态解析。样例 `gamma15_ipe_v2.xml` 为 257 点、0–1023，工具也测试 65 点/0–255 等格式，不再硬编码 257 点。
7. 多目标 Loss 包含灰阶目标、Local Gamma、阶间隔、LUT 平滑、原 LUT 变化、高光/暗部保护与 RGB 灰阶偏差。结果必须单调不下降、无局部反转、无异常突变并保持首尾点及 XML 最大亮度。
8. 页面包含曲线对比、工程统计、诊断与解释、History/XML Diff；菜单提供文件、配置、工具、帮助。保存前会确认，并默认覆盖当前加载的原 XML，只替换当前 Region 的三个 LUT。

## 统一 CCM / ColorChecker 校正

从首页或“工具 → CCM / ColorChecker 校正”进入。原 CC 校正的 CSV、参数配置、a\*b\* 图、完整色块表、工程统计、诊断、报告、History/XML Diff 和原 XML 覆盖回写全部保留；顶部“输入”可在两种数据源之间切换：

1. **CC CSV（Imatest）**：沿用原工作流，读取 measured/ideal RGB；矩阵固定使用 CC13 前乘约定。
2. **ColorChecker 图片**：打开测试图后，OpenCV MCC24 自动定位、排序并取样 24 色块；MCC 不可用时使用几何网格后备并提示核对覆盖框。目标默认是内置 ColorChecker Classic 24 标准 sRGB，也可导入任意自定义目标图。
3. 图片色块会转换成与 CSV 相同的 dataset：目标在线性 sRGB 中逐块匹配测试亮度，避免把曝光、Gamma 或 Tone Mapping 差异写进 CCM。之后两种输入都调用同一个多目标优化器，同时考虑 ΔE、ΔC、Δh、P90、回退数量、Neutral 19–24、Pass Rate、饱和度与 Matrix Health。
4. 测试图文件名包含显式 CCT 或常见光源标签时，会自动填写 CCT 并匹配 XML Region；也可手动输入 CCT、自动匹配或直接从下拉框选择 Region。标准/自定义目标只定义期望颜色，其文件名不会改变测试图 CCT。
5. 主结果页按输入隔离：CSV 模式只显示“色差对比”，图片模式只显示“ColorChecker 输入”；工程统计、诊断与 History/XML Diff 为两种模式共用。
6. 图片模式在 2800K–4500K 内会自动优先验证 3000K/4000K 实拍 Profile 候选；候选仍必须通过统一的 ΔE/ΔC/Δh、Neutral、Pass Rate、Patch Regression 与 Matrix 门禁，可自动降低强度，没有安全候选时回落到通用优化。
7. “ColorChecker 输入”并排等宽显示原始测试图、改后仿真图和标准/自定义目标。所选 Region 与 3000K/4000K 实拍起始矩阵一致时，默认启用“完整实拍还原（仅预览）”，以 100% Before→After 响应显示色彩还原；界面同时标明 XML 实际通过门禁的安全强度，关闭该开关即可查看与 XML 一致的保守预览。Profile 不适用时使用 linear-sRGB Delta CCM 近似。两类预览均可导出，但完整预览不会绕过安全门禁、改变优化矩阵或写入 XML，也不能替代烧录后的上机重拍。
8. “保存 XML”再次确认后覆盖当前加载的原文件，仅替换所选 Region 的 9 个 `<c_tab><c>` 数值，并立即回读校验。Matrix FAIL、Patch FAIL 或没有有效矩阵变化时按钮不可用。

## 图像分析器

图像分析器用于检查普通场景最终输出图片，不识别 ColorChecker，不做人脸/场景语义分析，也不会自动推断或修改 ISP 参数。图像分析器只作为 TuneLab 主程序内的工作区提供，不提供独立命令或模块启动入口。推荐通过工程启动器启动 TuneLab：

```bash
python3 run_tunelab.py
```

安装项目入口后也可运行：

```bash
tunelab
```

随后从首页“图像分析器”卡片或“工具 → 图像分析器...”进入。

### 文件夹浏览与 1–4 图模式

- 支持 JPG、JPEG、PNG、BMP、TIF、TIFF、HEIC、HEIF；Pillow 无法解析、文件损坏或超过安全像素上限时会在 GUI 中报错。
- 可像桌面图片查看器一样使用“打开图片…”或 `Ctrl/⌘+O` 直接载入 1–4 张图；也可“打开文件夹”。文件夹路径显示在可编辑地址栏中，不再展开或预览目录内的全部图片。
- 打开文件夹后按自然文件名顺序自动载入当前 1–4 张；“上一组 / 下一组”或 `Alt+← / Alt+→` 可连续浏览后续分组，地址栏旁始终显示当前组及文件范围。“查看多图对比”在当前组包含至少两张图片时可用，因此文件夹打开和直接打开拥有相同的对比入口。
- 界面只使用中性的“图像 1–4”和实际文件名，不根据打开顺序预设文件语义。1 张铺满画布，2 张左右排列，3 张以图像 1 大画布加两张小画布排列，4 张使用 2×2 布局。
- EXIF Orientation 在建立分析坐标系前完成转正；灰度图转换为等值 RGB，RGBA 分析 RGB 并单独显示 Alpha，CMYK JPEG 由 Pillow 转为 RGB。
- 16-bit 灰度 PNG/TIFF 会保留原始整数精度后映射到 0–255 统计/显示；若某些多通道 16-bit 格式被 Pillow 解码为 8-bit RGB，界面会保留原始位深标识并注明精度限制。
- 鼠标移动实时显示原图坐标 RGB；点击固定取样点；左键拖动建立 ROI。中键、右键或 Shift+左键拖动画布时，现有画面立即跟随指针移动，并以约 60 Hz 合并高质量裁片刷新；滚轮、触控板及工具栏“− / +”会以同一归一化锚点联动缩放全部当前图片，“适应窗口”和“1:1”可快速恢复视图。
- 文件名、尺寸、位深、解码精度和缩放倍率作为画布内浮层显示，不占用图片布局高度；图片区域始终使用完整画布空间。
- 显示缓存只渲染当前可见裁片，并复用相同裁片的缩放结果；普通 8-bit 图片共享分析/显示数组。画布坐标通过当前缩放与平移量反算到 EXIF 转正后的原图坐标，读取像素始终来自分析数组。
- 完整图片使用按文件时间校验、受条目数和内存预算约束的 LRU 缓存。普通 8-bit 解码不建立全图 float64 副本，完整图片最多两路并发解码；返回刚查看过的分组通常无需重复解码，后台加载/统计/匹配不会阻塞 Tk 主线程。

单像素与 ROI 提供 RGB、归一化通道占比、通道差/比例、HSV、CIE Lab、相对亮度、饱和度、最大通道、剪切/暗部比例和区域稳定性。ROI 稳定性仅表示区域内部颜色一致性，不代表多图匹配准确度。RGB 直方图可在整图与活动 ROI 间切换。

所有普通 8-bit RGB 默认按标准 sRGB 处理：先执行 sRGB 传递函数解码到 Linear RGB，再转换到 D65 XYZ 与 CIE Lab；相对亮度也在线性 RGB 上计算。HSV 的 S/V 使用 0–1 范围，H 使用角度。接近中性的像素不会仅因某一通道略大就被描述为明显偏色；只有启用“将当前 ROI 视为中性区域”后，才会输出保守的暖/冷/红/绿方向判断，并避开过暗与可能剪切的区域。

### 多图 ROI 匹配

1. 选择 2–4 张图片后，在图像 1 中框选至少 5×5 原图像素的 ROI。
2. 工具把图像 1 ROI 分别映射到图像 2–4；若尺寸不同，会按各图宽高比例映射中心与尺寸，不假设像素坐标完全一致。
3. 在映射中心附近按配置的 ±30、±60、±100 或 ±200 范围搜索。OpenCV 可用时使用轻微平滑后的灰度 `TM_CCOEFF_NORMED`；否则使用数学等价的 NumPy FFT 灰度归一化互相关。该输入会削弱整体曝光偏移的影响，颜色变化不会成为唯一匹配依据；超大 ROI 会先降采样搜索，再回到原图邻域精修。
4. 图像 2–4 分别独立显示最佳 ROI、匹配分数和置信度。用户可以统一接受自动结果，也可以直接在任一图像上重新框选并手动确认。

匹配分数是模板与候选灰度区域的归一化相关系数，显示为百分比：

| 分数 | 等级 | 处理 |
| --- | --- | --- |
| ≥ 0.92 | 高 | 允许在“两个 ROI 属于同一物体区域”的条件下给出保守解释 |
| 0.80–0.92 | 中 | 允许保守解释，但仍建议人工核对 |
| < 0.80 | 低 | 红色警告；只显示原始数值，禁止输出“颜色已改善”等确定性结论 |

低纹理 ROI 会直接标为低置信度。首版只针对轻微平移、很小裁切和轻微曝光/颜色变化；旋转、透视、大幅缩放、物体移动、遮挡和景深变化可能失败，未实现 SIFT/ORB、Homography 或整图自动配准。

多图对比页可在图像 2–4 之间切换，以 ROI 平均色色块和表格并排显示图像 1、当前图像、Delta 与上升/下降方向；RGB 同时给出百分比变化，匹配置信度用独立状态条显示。详细文本仍保留 ΔR/G/B、百分比变化、ΔR/G、ΔB/G、ΔHSV、ΔL\*/a\*/b\*、Δ亮度和 Δ饱和度。图像 1 的值为 0 时，百分比变化显示 N/A。每一组自然语言说明都单独经过当前图像的匹配置信度或用户手动确认门禁，并明确只描述最终输出像素。

### CSV、配置与数据边界

“导出当前分析 CSV”使用 UTF-8 BOM，按图像 1 和最多三张当前图像逐行写入图片尺寸/位深、ROI 坐标、匹配分数、RGB/HSV/Lab/亮度/剪切/暗部指标及相对图像 1 的 Delta 字段。默认只写文件名以降低路径隐私泄露；可在“导出”菜单显式启用完整路径。

图像分析器单独保存上次目录、搜索范围、匹配阈值、直方图/实时像素开关、默认 ROI 名称、中性模式和路径导出开关；窗口每次按当前屏幕尺寸展开，不会自动保存图片像素、ROI 结果或分析结论。

JPEG/PNG 等已经过曝光、AWB、Gamma、CCM、CV/Saturation、SCE、2D LUT、TMC、压缩等完整输出链处理。这里的 RGB、HSV、Lab 和亮度不能等同于 Sensor RAW、AWB Gain 或 ISP 中间节点，也不能单凭结果断言“AWB 已修复”“CCM 调整正确”或由某个具体模块负责。

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
python3 -m tunelab.regression --source sources \
  --json outputs/golden_regression.json \
  --html outputs/golden_regression.html
```

Golden Regression 会遍历 `sources` 中的全部文件，并按格式路由：CCM Golden 使用兼容的 ColorChecker CSV/CC XML；Gamma Regression 使用 Stepchart CSV/Gamma XML。图片工作流素材也统一放在这里，但不会被误当作 CSV Golden Case。每个 CCM Case 必须满足：Average ΔE 改善、至少一个 `ΔE00≤2/3/5/10` Pass Rate 提升、Neutral 19–24 无明显退化、无 FAIL Patch、无工程 FAIL、无越界 Matrix、Row Sum=1 且饱和度偏差不扩大。

## 资料依据（Reference）

算法设计、工程约束与 Qualcomm XML/trigger 解释仅依据 Qualcomm 官方原生 Camera ISP / Chromatix 文档：

- Qualcomm `80-PT841-101_REV_AB_XR_Camera_Tuning_Guide.pdf` 第 59-62 页：CCT region、Gamma 前置、Gamma 曲线/Region 与逐 region 调整。
- Qualcomm `80-35348-60_REV_AD_Qualcomm_Spectra_7XX_Deep_Dive.pdf` 第 82-83 页：3×3 CCM、硬件系数范围和行和约束。
- Qualcomm `80-35348-60_REV_AD_Qualcomm_Spectra_7XX_Deep_Dive.pdf` 第 377 页：平台 Gamma table 为 65 点、0–255 的实例。
- Qualcomm `80-74889-81_REV_AH_Qualcomm_Spectra_1080_Deep_Dive.pdf` 第 414-421 页：CC1.3 / CC1.4、Gamma1.5 trigger/color-format 以及 257-entry 10-bit Gamma 实例。

更完整的模块划分、数据流和扩展路线见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
