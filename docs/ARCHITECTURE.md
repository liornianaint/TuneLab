# MatrixCorrect 架构设计

## 1. 数据流

```text
Imatest CSV ──> CSV Parser ──> 24 Patches ──> Color Science ──> Optimizer
                                                               │
Qualcomm XML ─> Trigger Tree ─> Selected CCT Region ─> Old CCM ─┤
                                                               ↓
                                   Before/After Simulation + Patch Report
                                                               │
                                                               ↓
                                  Surgical XML Writer + Read-back Validation
```

`matrixcorrect/` 中各模块职责：

- `imatest.py`：编码兼容、元数据、RGB measured/ideal 段、光源/CCT 推断
- `qualcomm_xml.py`：control variables、任意深度 trigger tree、CCT region、原文定点回写
- `color.py`：sRGB EOTF/OETF、XYZ/Lab、CIEDE2000、矩阵运算
- `optimizer.py`：约束拟合、自动正则化、ΔE00 细化、矩阵组合、模块路由
- `report.py`：可审计的 CSV 报告
- `app.py`：Windows/macOS Tk GUI 和双 a\*b\* 图
- `cli.py`：批处理接口

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
M_new = M_old × Aᵀ              # 旧 Excel/C7 行向量等价形式
```

这使工具能在没有 RAW 和 sensor spectral response 的情况下给出工程上可用的首轮方向，同时明确保留“必须上机复测”的边界。

## 3. 约束与稳健性

- `A` 每行和严格等于 1，避免把中性 RGB 轴染色。
- ideal 线性亮度先匹配 measured 亮度，防止用 CC 追逐曝光/Gamma 误差。
- Ridge 正则化把 `A` 拉向单位阵，避免 18 个色块上的过拟合。
- 自动搜索多个正则化与 blend 候选。
- ΔE00 坐标细化保留行和，同时惩罚 P90、明显回退、越界和大矩阵。
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

## 6. 扩展路线

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

