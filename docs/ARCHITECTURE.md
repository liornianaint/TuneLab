# TuneLab 架构设计

## 1. 数据流

```text
Imatest CSV ───────> CSV Adapter ───┐
                                    ├─> Unified 24-patch Dataset ─> Protected Optimizer
ColorChecker Image ─> MCC24 Adapter ┘                                  │
                                                                        │
Qualcomm XML ───────> Trigger Tree ─> Selected CCT Region ─> Old CCM ───┤
                                                                        ↓
                                      a*b* / Image Simulation / Reports / History
                                                                        │
                                                                        ↓
                                        Surgical XML Writer + Read-back Validation
```

`tunelab/` 按产品壳层和调试领域分层：

- `app.py`、`branding.py`：TuneLab 桌面壳层、统一 CCM/ColorChecker 页面、首页、模块切换和品牌资源
- `ui_foundation.py`：跨 macOS / Windows 的 macOS 风格颜色、字体选择、控件状态、表格、标签页、窗口尺寸与 `sources/` 文件选择入口
- `ccm/imatest.py`：ColorChecker CSV、RGB measured/ideal 段与光源/CCT 推断
- `ccm/qualcomm_xml.py`：CC control variables、trigger tree、CCT region 与定点回写
- `ccm/color_science.py`：sRGB EOTF/OETF、XYZ/Lab、CIEDE2000 与矩阵运算
- `ccm/optimizer.py`、`ccm/engineering.py`、`ccm/diagnostics.py`：CCM 优化、工程检查与模块诊断
- `ccm/reporting.py`、`ccm/settings.py`、`ccm/history.py`：CCM 报告、配置与历史记录
- `ccm/cli.py`：CCM 批处理入口
- `gamma/`：Gamma 数据模型、Imatest 解析、Qualcomm XML、优化、报告、配置、历史与页面
- `colorchecker/engine.py`：图片输入适配器，包括 MCC24/几何后备检测、标准/自定义目标取样、统一 dataset 构造与改后整图近似模拟
- `colorchecker/ui.py`：统一页面复用的原图/改后模拟图/目标图预览组件；不拥有独立工作区、优化状态或 XML 写回
- `image_inspector/`：普通场景 1–4 图直接打开/文件夹浏览、画布内信息浮层、联动缩放、像素/ROI 分析、匹配与 CSV 导出
- `regression.py`：跨 CCM/Gamma 的 Golden Dataset 发现、验收和汇总

## 2. 为什么不直接用 CSV 求一个新绝对 CCM

Imatest CSV 来自完整 ISP 输出。它没有 AWB 后、CC 前的 camera-linear RGB，因此无法仅凭 JPEG 反推严格的绝对 Camera RGB → sRGB 矩阵。

本工具使用 Delta CCM：

```text
y_current = current ISP output in linear sRGB
y_target  = ColorChecker reference in linear sRGB
y_sim     = A × y_current
```

再把 `A` 与 XML 中的旧矩阵组合：

```text
M_new = A × M_old               # CC13 行主序/列向量
```

这使工具能在没有 RAW 和 sensor spectral response 的情况下给出工程上可用的首轮方向，同时明确保留“必须上机复测”的边界。

图片输入与 CSV 输入不再拥有两套页面或两套优化生命周期。MCC24 自动识别测试图的 24 个中心色块；目标默认由引擎生成 ColorChecker Classic 24 标准 8-bit sRGB 色块及确定几何，也可切换为自动识别的自定义目标图。目标色块的 linear-sRGB 亮度逐块匹配到测试图，再构造与 Imatest 完全相同的 measured/ideal dataset，交给同一个 `optimize_ccm()`。内置目标没有 CCT 语义；自定义目标文件名也不参与 Region 推断，CCT 只来自测试图或用户输入。

CSV 固定调用统一的 ΔE/ΔC/Δh `optimize_ccm()` 路径。图片模式在 2800K–4500K 内先以 3000K/4000K 实拍 Profile 的 Delta CCM 作为候选方向，再通过 `evaluate_protected_ccm_correction()` 执行与通用优化一致的 Patch Regression、Neutral、Pass Rate 和 Matrix 门禁；候选可确定性回退强度，完全不适用或无安全点时才调用 `optimize_ccm()`。两条路径都固定使用 `M_new = A × M_old`。

ColorChecker 页签只在图片输入时存在，并显示原图、改后仿真和目标图；CSV 输入只显示色差对比页签。接受实拍 Profile 时，整图预览使用由 3000K/4000K Before/After 样本拟合并按 mired 插值的响应模型；通用优化使用 linear-sRGB Delta CCM 近似。预览从不参与求解或安全门禁。

## 3. 约束与稳健性

- 通用自动拟合仍要求 Delta CCM 与最终 CCM 每行和严格等于 1。
- ideal 线性亮度先匹配 measured 亮度，防止用 CC 追逐曝光/Gamma 误差。
- Ridge 正则化把 `A` 拉向单位阵，避免 18 个色块上的过拟合。
- 自动搜索多个正则化与 blend 候选，并直接搜索最终 Matrix 的三组行内系数对。
- Loss 同时约束 ΔE/ΔC/Δh/P90、Regression、Saturation、矩阵幅度与 Smoothness。
- 默认保护 13/14/15；所有候选必须通过 Pass Rate、局部/整体饱和度和 Patch Regression 硬门槛。
- 最终 Matrix 默认限制在 `[-3,3]`，并输出 PASS/WARNING/FAIL 工程检查。
- Qualcomm `c_tab` 范围在保存前再次校验为 `[-15.99, 15.99]`。

## 4. XML 安全策略

ElementTree 只负责理解结构和定位 region，不直接序列化原文。保存阶段按文档顺序定位 `<c_tab>...<c>...</c>...</c_tab>`，仅替换目标 region 的文本：

- XML 注释不丢失
- 原换行和缩进不变
- `k_tab`、其他 CCT、AEC/LED/Lux trigger 不变
- 保存后重新解析并逐值校验目标矩阵
- region 数和原文 `c_tab` 数不一致时拒绝写入

## 5. CCT transition

样本 XML 的 CCT core ranges 为 `2000-2600`、`2800-3500` 等，区间之间是过渡带。处在过渡带的实际 ISP 输出可能由相邻矩阵插值得到，因此“2700 K 对应唯一矩阵”并不成立。

工具的处理是：

1. 精确落入 core range 时直接选中。
2. 落入 gap 时选最近端点，但显示强警告。
3. 保存只更新用户确认的端点 region，不猜测或同时修改相邻矩阵。

后续版本可以增加“相邻端点联合优化”，对多个 CCT 样本共同求解，并对过渡区连续性施加约束。

## 6. Golden Regression

`python3 -m tunelab.regression --source sources` 会遍历所有 CSV × XML，并对每个 Case 验证：

- Average ΔE 必须改善，至少一个 ΔE Pass Rate 必须提升，任何阈值不得下降
- 重点 Patch 的平均 ΔE/ΔC/Δh 受保护，任何 Patch 不得为 Regression FAIL
- Saturation 误差不得扩大
- Matrix 不得有工程 FAIL，系数范围、Row Sum、Rank 和 Fixed Point 必须合格

## 7. 扩展路线

### 多光源批量标定

一次加载 A/TL84/CWF/D50/D65 多份 CSV，按 CCT 自动分组，对每个 region 分别求解，并增加相邻矩阵平滑项。

### RAW 绝对 CCM

若能提供 AWB 后、CC 前的 RAW patch RGB、参考光谱和目标色彩空间，可新增绝对 CCM 模式，直接拟合 Camera RGB → XYZ/目标 RGB，不再依赖 Delta 近似。

### Color-format trigger

CC1.4/IPE 可按 SDR/HLG/PQ 与 sRGB/BT709/Display-P3/BT2020 选择不同 trigger。未来需要为 CSV/测试图增加目标色彩空间和 transfer function 元数据，不能把所有格式都按 sRGB/D65 处理。

### 局部色彩模块

当前版本只给出 SCE/CV/2D LUT 路由建议。后续可分别增加：

- CV：基于平均 Chroma ratio 的全局饱和度建议
- SCE：肤色 hue/chroma 局部目标
- 2D LUT：按 hue sector 拟合局部位移并做连续性约束
- Gamma/TMC：使用灰阶 19-24 的 L\* 曲线诊断，而不修改 CCM

这些模块必须独立建模和回写，不能把 24 色块误差全部压进一个 3×3 矩阵。
