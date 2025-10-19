# 存储库指南（Agent 工作流）

## 目标与产出
- 输入：一份论文 PDF。
- 过程：用 `scripts/extract_pdf_assets.py` 提取正文与“附图与表格”（Figure x / Table x）。
- 输出：一份 1500–3000 字的 Markdown 摘要，支持中文或英文两种语言，默认是中文，如果用户主动提醒使用英文输出摘要，即改成英文；摘要文档中嵌入论文全部“图与表”的 PNG，并为每个图表按照标号给出精要解释；摘要面向的对象是同专业的高年级本科生，所以对于相对较难或者比较复杂的概念，适当给出专业术语的简要注释。
- 重要：生成摘要时，必须将 `text/<paper>.txt` 与 `images/*.png` 一并提供给大模型，再生成摘要；不要只给文本或只给图片。

## 目录与命名
- 输入 PDF：`<PDF_DIR>/<paper>.pdf`
- 脚本默认输出：
- 文本：`<PDF_DIR>/text/<paper>.txt`
- 图片：`<PDF_DIR>/images/*.png`（包含 Figure_* 与 Table_* ）
- 索引：`<PDF_DIR>/images/index.json`（统一清单，字段：type/id/page/caption/file/continued）
- 摘要文档：置于 PDF 同级，命名 `/<paper>_阅读摘要-yyyymmdd.md`；在 MD 中以 `images/...` 相对路径嵌图。

## 一次跑通（提取文本与图片）
 - 环境：Python 3.12+；依赖安装：`python3 -m pip install --user pymupdf pdfminer.six`
 - 基本执行：`python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf`

### 环境与命令差异（macOS/Linux vs Windows/PowerShell）
在执行任何命令前，请先确认当前运行环境；不同平台的常用命令如下（避免因命令差异导致报错）：

- macOS/Linux：`python3`、`mv`、`cp`、`pwd`、`date`
- Windows/PowerShell：`python`、`Move-Item`、`Copy-Item`、`Get-Location`、`Get-Date`

等价示例（已进入 PDF 所在目录 `<PDF_DIR>` 后执行）：

1) 运行提取脚本

```bash
# macOS/Linux
python3 scripts/extract_pdf_assets.py --pdf "./<paper>.pdf" --preset robust
```

```powershell
# Windows/PowerShell
python .\scripts\extract_pdf_assets.py --pdf ".\<paper>.pdf" --preset robust
```

2) 批量重命名图表文件

```bash
# macOS/Linux
cd images
mv "Figure_1_Overview.png" "Figure_1_Architecture_Overview.png"
mv "Table_1_Raw.png" "Table_1_Model_Performance_Metrics.png"
cd ..
```

```powershell
# Windows/PowerShell
Set-Location images
Move-Item "Figure_1_Overview.png" "Figure_1_Architecture_Overview.png"
Move-Item "Table_1_Raw.png" "Table_1_Model_Performance_Metrics.png"
Set-Location ..
```

3) 获取当天日期与当前路径（用于命名和路径确认）

```bash
# macOS/Linux
date +%Y%m%d
pwd
```

```powershell
# Windows/PowerShell
(Get-Date).ToString("yyyyMMdd")
Get-Location
```

### 一键稳健预设（推荐）
- 使用 `--preset robust` 自动启用稳健参数（A+B+D 精裁 + 验收 + 关键阈值），相当于：
  - `--dpi 300 --clip-height 520 --margin-x 26 --caption-gap 6`
  - A：`--text-trim --text-trim-width-ratio 0.5 --text-trim-font-min 7 --text-trim-font-max 16 --text-trim-gap 6 --adjacent-th 28`
  - B：`--object-pad 8 --object-min-area-ratio 0.015 --object-merge-gap 6`
  - D（图）：`--autocrop --autocrop-pad 30 --autocrop-white-th 250 --autocrop-mask-text --mask-font-max 14 --mask-width-ratio 0.5 --mask-top-frac 0.6`
  - 防过裁（图，已默认）：`--near-edge-pad-px 32`（靠近图注一侧回扩）+ `--protect-far-edge-px 18`（远端边保护，默认 14，robust=18）
  - 表格特化（自动启用）：`--include-tables --table-clip-height 520 --table-margin-x 26 --table-caption-gap 6 --table-object-min-area-ratio 0.005 --table-object-merge-gap 4 --table-autocrop --table-autocrop-pad 20 --no-table-mask-text`
  - 验收保护：高度≥0.6×、面积≥0.55×、对象覆盖率≥0.85×、墨迹密度≥0.9×，并保护多子图不被缩并。

### 方向与续页控制
- 强制方向：
  - `--above 4` 仅对图 4 强制从图注上方取图。
  - `--below 2,3` 对图 2 与 3 强制从图注下方取图。
  - 进阶：也可设置环境变量 `EXTRACT_FORCE_ABOVE="1,4"`（可选）。
  - 重要：当使用默认“锚点 V2”时，`--above/--below` 与 `EXTRACT_FORCE_ABOVE/EXTRACT_FORCE_TABLE_ABOVE` 不生效；如需按编号强制方向，请添加 `--anchor-mode v1`（或设置 `EXTRACT_ANCHOR_MODE=v1`）后再结合上述参数使用。
- 同号多页（continued）：
  - `--allow-continued` 允许输出同一图号的多页内容，命名为 `..._continued_p{page}.png`。
  - 表格同理：再次命中相同“表号”将输出 `Table_<id>_continued_p{page}.png`。
  - 环境变量：`EXTRACT_FORCE_TABLE_ABOVE="1,S1"` 可对表强制上方裁剪。

### 锚点 V2（默认）与"全局锚点一致性"
- 锚点 V2：围绕 caption 多尺度滑窗（默认高度：240,320,420,520,640,720,820），结合结构打分（墨迹/对象覆盖/段落占比/组件数量；表格再加"列对齐峰+线段密度"），并做边缘"吸附"。
- 中线护栏：扫描窗口不会跨越相邻两条图注的中线（`--caption-mid-guard 6`，建议 6–10pt）。
- 距离罚项：候选离 caption 越远得分越低（`--scan-dist-lambda 0.12`，建议 0.10–0.15）。
- 全局锚点一致性（默认开启）：
  - 图片：`--global-anchor auto` 预扫整篇后，若"下方总分"显著高于"上方总分"（或反之），本篇文档所有 Figure 统一采用该方向；阈值由 `--global-anchor-margin` 控制（默认 0.02）。可用 `--global-anchor off` 关闭。
  - **表格**（新增）：`--global-anchor-table auto` 对表格独立预扫，使用表格专用评分（含列对齐+线密度）；阈值更宽松（默认 0.03）以适应表格排版灵活性。可用 `--global-anchor-table off` 关闭。
- 模式切换与调试：可用 `--anchor-mode v1|v2` 显式指定锚点策略；扫描步长与高度可由 `--scan-step`、`--scan-heights` 调整；如需导出页面候选窗口用于调试，使用 `--dump-candidates`。

### 防“半幅/错截”的补救
- 远端外扩：若在精裁后远离图注的边仍被对象“贴边”，脚本会向该方向外扩（最多约 200pt）以补齐整幅；必要时可调大最高扫描高度（`--scan-heights`）或外扩上限（需要代码内改，默认 200pt）。

### 自适应行高（默认开启，v2.0新增）
**问题背景**：不同PDF文档的行高差异很大（单栏vs双栏，10pt vs 14pt正文），固定参数无法适配所有文档。

**核心功能**：
- 自动统计文档的典型行高、字号、行距（采样前5页）
- 基于行高**动态调整**裁切参数：
  - `adjacent_th` = 2.0 × 行高（约2行）
  - `far_text_th` = 10.0 × 行高（约10行）
  - `text_trim_gap` = 0.5 × 行高（约半行）
  - `far_side_min_dist` = 8.0 × 行高（约8行）
- **"两行检测"增强**：精确识别并移除"刚好两行文字"（如Abstract/Introduction顶部文字）

**使用示例**：
```bash
# 默认启用（推荐）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust

# 查看行高统计和参数调整（调试）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --debug-captions

# 禁用自适应（回退固定参数）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --no-adaptive-line-height
```

**效果示例**（KearnsNevmyvakaHFTRiskBooks.pdf）：
- 检测到典型行高：10.9pt
- 自适应参数：`adjacent_th=21.8pt` (原24pt), `far_text_th=109.0pt` (原300pt)
- Table 1：成功移除顶部120pt文字（约11行，包含两行正文+空白）

### 可选开关
- 对个别图禁用精裁：`--no-refine 2,3`（仅保留基线或 A）。
- 仅改靠近图注的一侧边界（默认开）：`--refine-near-edge-only`；如需禁用用于调试：`--no-refine-near-edge-only`。
- 调整自适应裁切的收缩保护：`--autocrop-shrink-limit 0.35`（最多收缩 35% 面积）、`--autocrop-min-height-px 80`（最小高度，随 DPI 换算）。
- 表格参数：`--table-*` 同名选项与图相近，但默认对表关闭文本掩膜、降低连通域面积阈值。
- 关闭表格提取：`--no-tables`（默认开启表格提取）。
- 导出 CSV 清单：`--manifest <path>` 可生成包含 `(type,id,page,caption,file,continued)` 的 CSV；与 `images/index.json` 字段一致。

### 智能 Caption 识别（默认开启）
**问题背景**：论文中的图表标号（如 Figure 1、Table 2）可能出现在三种位置：
1. **真实图注**：紧邻图表上方或下方，作为图注首次出现（期望的情况）
2. **前文引用**：在图表之前的正文中提前引用和说明
3. **混合情况**：图注、前文、后文都出现该标号

**智能识别机制**（默认启用，**图与表均已支持**）：
- **预扫描索引**：脚本会预先扫描全文，记录每个 Figure/Table 编号的所有出现位置。
- **四维评分**：为每个候选位置打分（总分 100），综合考虑：
  1. **位置特征**（40分）：与图像/绘图对象的距离（越近得分越高）
  2. **格式特征**（30分）：字体加粗、独立成段、后续标点（冒号、句点等）
  3. **结构特征**（20分）：下一行有描述文字、段落长度（长段落可能是正文引用）
  4. **上下文特征**（10分）：语义分析（是否包含"显示"、"展示"等图注关键词，或"如图所示"等引用关键词）
- **最佳选择**：自动选择得分最高的候选作为真实图注（阈值：25分）。
- **✨ 2025-10-11 更新**：表格智能Caption检测已启用，与图片使用相同的四维评分机制，成功解决"表格引用"与"真实表注"混淆问题。

**控制选项**：
- `--smart-caption-detection`（默认开启）：启用智能识别。
- `--no-smart-caption-detection`：关闭智能识别，使用简单模式（按顺序匹配第一个出现的标号）。
- `--debug-captions`：输出详细的候选项评分信息，用于调试和分析。

**使用示例**：
```bash
# 启用智能识别（默认）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust

# 查看候选项评分详情（调试模式）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --debug-captions

# 关闭智能识别（使用简单模式）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --no-smart-caption-detection
```

**适用场景**：
- ✅ 当图表标号在图片前文中提前出现时（如先讨论后列图）
- ✅ 当同一标号在多处出现时（前文+图注+后文引用）
- ✅ 复杂排版的论文（图注格式不规范、混合引用较多）
- ✅ 表格与图片均支持智能识别（支持罗马数字、附录表等复杂情况）

### 远距文字清除（Phase C，默认开启）
**问题背景**：某些论文PDF中，图表截取区域会包含距离图注较远的正文段落（如Abstract、Introduction等），导致图表上下方有多余文字。

**核心创新**（基于全局锚点方向）：
- **方向性检测**：利用全局锚点判定（ABOVE/BELOW），自动识别多余文字可能出现的方向
  - 图注在下方 → 多余文字通常在上方（far side = top）
  - 图注在上方 → 多余文字通常在下方（far side = bottom）
- **三阶段Trim策略**：
  - **Phase A**：移除紧邻图注的文字（<24pt，原有逻辑）
  - **Phase B**：移除near-side的远距文字（24-300pt，预留但通常不触发）
  - **Phase C**：移除far-side的大段正文（>100pt，覆盖率≥20%）★核心功能
- **安全保护**：最多trim 50%原始窗口高度，配合验收机制防止过度裁剪

**新增参数**：
- `--far-text-th 300.0`：远距文字检测范围（默认300pt）
- `--far-text-para-min-ratio 0.30`：触发trim的段落覆盖率阈值（默认0.30）
- `--far-text-trim-mode aggressive|conservative`：trim模式（默认aggressive）

**效果示例**（FunAudio-ASR.pdf实测）：
- Figure 1：上方Abstract移除，高度减少 138px (-13.4%)
- Figure 3：上方正文移除，高度减少 311px (-39.0%)
- Table 3：上方正文移除，高度减少 222px (-30.8%)

**使用示例**：
```bash
# 默认启用（随--preset robust自动启用）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust

# 调整远距检测阈值（更激进）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --far-text-th 400 --far-text-para-min-ratio 0.15

# 使用保守模式（仅当段落连续时才trim）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --far-text-trim-mode conservative
```

### 推荐参数备忘（遇到边沿轻微过裁时）
- 仅靠近图注一侧再放宽：`--near-edge-pad-px 34~36`
- 同时保护远端上/下边：`--protect-far-edge-px 20~24`
- 图注密集页防跨图：`--caption-mid-guard 8~12` + `--scan-dist-lambda 0.18`

### 质量校验
- 确认生成 `text/<paper>.txt`，且 `images/` 中附图数量与原文一致或接近。
- 对多子图页，检查 (a)/(b) 是否完整保留。
 - 终端会输出 QC 汇总与弱对齐统计（从 txt 统计 Figure/Table/图/表 出现次数，供参考）。

- 按修改时间检查最新导出的 PNG（确认时间戳为最近一次运行产生）：

```bash
# macOS/Linux（取最近 10 张）
ls -lt images/*.png | head -10
```

```powershell
# Windows/PowerShell（取最近 10 张）
Get-ChildItem images -Filter *.png |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 10 |
  Format-Table Name, LastWriteTime, Length
```

### 关于"基线→精裁"的融合策略
- 基线：按"图注为锚点"的上/下候选窗口与评分挑选（row 级聚合，避免子图丢失）。
- 精裁：顺序执行 A（单边裁头）→ B（连通域近侧对齐 + 主/横轴并集）→ D（文本掩膜 autocrop，带收缩保护）。
- 验收：若触发保护门槛，自动回退到 A-only 或基线，避免"半幅/过裁"。

### 可视化调试模式（Visual Debug Mode）
**问题背景**：当提取结果不理想时（图片截不完整、包含多余内容），需要直观了解各阶段的裁剪过程发生了什么。

**调试功能**：启用 `--debug-visual` 后，脚本会在 `images/debug/` 目录下生成可视化图片和图例文件（**图与表均支持**）：
- `Figure_N_pX_debug_stages.png` / `Table_N_pX_debug_stages.png`：在完整页面上叠加多色边界框，标注各阶段裁剪范围
- `Figure_N_pX_legend.txt` / `Table_N_pX_legend.txt`：文字说明各阶段的尺寸和描述

**边界框颜色方案**：
| 阶段 | 颜色 | 说明 |
|------|------|------|
| Baseline (Anchor Selection) | 🔵 蓝色 | 锚点选择阶段的原始窗口 |
| Phase A (Text Trimming) | 🟢 绿色 | 文本裁切后的窗口（如果启用） |
| Phase B (Object Alignment) | 🟠 橙色 | 对象对齐后的窗口（如果启用） |
| Phase D (Autocrop) | 🔴 红色 | 自动裁剪后的最终窗口（如果成功） |
| Fallback (Reverted) | 🟡 黄色 | 验收失败，回退到基线（如果发生） |
| Caption | 🟣 紫色 | 图注位置 |

**使用示例**：
```bash
# macOS/Linux
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --debug-visual

# Windows/PowerShell
python .\scripts\extract_pdf_assets.py --pdf .\paper.pdf --preset robust --debug-visual
```

**输出示例**：
```
[DEBUG] Saved visualization: images/debug/Figure_3_p5_debug_stages.png
[DEBUG] Saved legend: images/debug/Figure_3_p5_legend.txt
[DEBUG] Saved visualization: images/debug/Table_1_p8_debug_stages.png
[DEBUG] Saved legend: images/debug/Table_1_p8_legend.txt
```

**图例文件内容示例**（`Figure_3_p5_legend.txt`）：
```
=== Figure 3 Debug Legend (Page 5) ===

Caption: 72.0,450.2 -> 540.0,465.8 (468.0×15.6pt)

Baseline (Anchor Selection):
  Position: 46.0,150.0 -> 566.0,444.2
  Size: 520.0×294.2pt (5.30 sq.in)
  Color: RGB(0, 102, 255)
  Description: Initial window from anchor above selection

Phase A (Text Trimming):
  Position: 46.0,180.5 -> 566.0,444.2
  Size: 520.0×263.7pt (4.78 sq.in)
  Color: RGB(0, 200, 0)
  Description: After removing adjacent text (Phase A+B+C)

Phase D (Final - Autocrop):
  Position: 58.3,185.2 -> 553.7,438.9
  Size: 495.4×253.7pt (4.39 sq.in)
  Color: RGB(255, 0, 0)
  Description: Final result after A+B+D refinement
```

**适用场景**：
- ✅ 诊断图片/表格截不完整的问题（查看哪个阶段过度收缩）
- ✅ 诊断包含多余内容的问题（查看文本裁切是否生效）
- ✅ 对比 Baseline 和最终结果，评估精炼效果
- ✅ 验收失败时查看 Fallback 的回退范围
- ✅ **图与表均支持**：所有裁剪阶段的可视化调试（Baseline → Phase A → Phase B → Phase D → Fallback）

## 生成带图摘要（大模型提示词模板）
请务必同时提供 `text/<paper>.txt` 与 `images/*.png` 的完整集合。建议将 txt 的要点（或全文）与图片清单（图号+文件名）一并喂给模型。

### 📋 必做任务清单
生成摘要时，大模型必须完成以下两个任务：

#### 任务1：图表文件重命名（必做）
**背景说明**：脚本默认生成的文件名（如 `Figure_1_Overview_of_the_proposed_deep_learning.png`）是基于原始图注的**临时命名**。大模型需要基于论文完整内容与图表实际含义，为每个图表PNG文件生成**最终命名**。

**重命名规则**：
- 📏 **单词数量**：5-15个单词（不含 `Figure_N_` 或 `Table_N_` 前缀）
- 🎯 **命名原则**：
  - 准确反映图表的核心内容或贡献
  - 使用专业但简洁的描述性术语
  - 避免冗长的句式，突出关键概念
  - 保持与论文术语的一致性
- 📁 **命名格式**：`Figure_N_<新描述>.png` 或 `Table_N_<新描述>.png`
- ⚠️ **注意事项**：
  - 重命名时必须保留原有的 `Figure_N_` 或 `Table_N_` 前缀
  - 使用下划线 `_` 连接单词，不使用空格
  - 避免使用特殊字符（仅允许字母、数字、下划线、连字符）

**重命名工作流**：
1. 阅读论文全文与图表内容
2. 理解每个图表的核心贡献和含义
3. 使用 `mv` 命令（macOS/Linux）或等效命令批量重命名所有图表文件
4. 在摘要文档中使用**新的文件名**嵌入图表

**示例**：
```bash
# 原始临时命名（脚本生成）
Figure_1_Overview_of_the_proposed_deep_learning.png

# 最终命名（大模型重命名）
mv "images/Figure_1_Overview_of_the_proposed_deep_learning.png" \
   "images/Figure_1_Architecture_Overview.png"

# 或者更具体的命名
mv "images/Figure_1_Overview_of_the_proposed_deep_learning.png" \
   "images/Figure_1_Multimodal_Transformer_Architecture.png"
```

#### 任务2：生成带图摘要（必做）
请基于给定的 txt 与全部 PNG 附图与表格，生成一份1500–3000字的中文Markdown摘要：
- 结构包含：研究动机/方法/训练与后训练/评测与效率/局限与展望/结论。
- 按编号将所有"图与表"嵌入文档（使用**重命名后**的相对路径，如 `images/Figure_1_Architecture_Overview.png`），每个元素配1–2句精要解释。
- 语言准确、精炼，量化关键点（复杂度、算量、关键超参）。

### 完整工作流示例

**步骤1**：脚本提取（自动执行）
```bash
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --allow-continued
# 输出临时命名: Figure_1_Overview_of_the_proposed_deep_learning.png, Figure_2_Experimental_results_on_benchmark_datasets.png, ...
```

**步骤2**：阅读并理解论文内容
- 读取 `text/paper.txt` 了解论文整体内容
- 查看 `images/*.png` 理解每个图表的实际含义
- 参考 `images/index.json` 获取图表清单

**步骤3**：图表重命名（必做）
```bash
# 根据论文内容重命名所有图表
cd images/
mv "Figure_1_Overview_of_the_proposed_deep_learning.png" "Figure_1_Multimodal_Architecture_Overview.png"
mv "Figure_2_Experimental_results_on_benchmark_datasets.png" "Figure_2_Benchmark_Performance_Comparison.png"
mv "Figure_3_Ablation_study_results_showing_the_impact.png" "Figure_3_Ablation_Study_Results.png"
mv "Table_1_Comparison_of_model_performance_across_different.png" "Table_1_Model_Performance_Metrics.png"
mv "Table_2_Hyperparameter_settings_used_in_our_experiments.png" "Table_2_Training_Hyperparameters.png"
# ... 重命名所有图表
cd ..
```

**步骤4**：生成摘要文档（使用新文件名）
```markdown
# 论文标题_阅读摘要-20250114.md

## 研究动机
...

## 方法
本文提出了一种多模态架构...

![Figure 1: 架构概览](images/Figure_1_Multimodal_Architecture_Overview.png)
**图1** 展示了提出的多模态Transformer架构，包含...

## 实验结果
...

![Figure 2: 基准测试性能对比](images/Figure_2_Benchmark_Performance_Comparison.png)
**图2** 对比了本文方法与现有方法在多个基准数据集上的性能...

![Table 1: 模型性能指标](images/Table_1_Model_Performance_Metrics.png)
**表1** 列出了不同模型配置的详细性能指标...
```

## 常见问题（FAQ）
- 图片不显示：始终使用"相对于 MD 的相对路径"。若 MD 与 `images/` 同级，写 `images/...`；若在 `tests/` 下生成 MD，也写 `images/...`（确保与 MD 同级的 `images/` 存在）。
- 顶部正文或标题混入：优先 `--above <N>` + `--clip-height`，并启用 A/D（或调高 `--adjacent-th`、`--mask-top-frac`）。
- 多子图被截半：保持 row 级聚合；开启 B 的"近侧对齐 + 主/横轴并集"，必要时提高 `--autocrop-min-height-px` 或对该图 `--no-refine`。
- 需要从图注下方取图：`--below N` 覆盖方向判定（与 A/B/D、验收可叠加）。
