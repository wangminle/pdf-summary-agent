# Agent 工作流指南

> 本文档为 AI Agent（如 Claude/GPT/Gemini）提供详细的工作流说明。

## 版本信息

- **版本**：V0.3.1（2026-01-10 模块化完成版）
- **架构**：三层模块化设计
  - `scripts/lib/`：模块化组件库（数据结构、算法、工具函数）
  - `scripts/core/`：核心入口（CLI 解析、主流程）
  - `scripts/extract_pdf_assets.py`：兼容导出层
- **主脚本**：
  - **新入口**：`scripts/extract_pdf_assets.py`（推荐，调用旧版完整实现）
  - **旧入口**：`scripts-old/extract_pdf_assets.py`（完整功能，过渡期保留）

### 代码结构

```
scripts/
├── extract_pdf_assets.py    # 兼容导出层（新入口）
├── core/
│   ├── __init__.py          # 核心入口包
│   └── extract_pdf_assets.py  # CLI + main()
├── lib/
│   ├── models.py            # 数据结构
│   ├── idents.py            # 标识符与正则
│   ├── output.py            # 输出与索引
│   ├── caption_detection.py # Caption 检测
│   ├── layout_model.py      # 版式模型
│   ├── text_extract.py      # 文本提取
│   ├── refine.py            # 精裁与验收
│   ├── debug_visual.py      # 调试可视化
│   ├── figure_contexts.py   # 图表上下文
│   ├── extract_figures.py   # Figure 提取（占位）
│   ├── extract_tables.py    # Table 提取（占位）
│   └── pdf_backend.py       # PDF 后端抽象
└── requirements.txt
scripts-old/                   # 过渡期保留
└── extract_pdf_assets.py      # 完整实现（~8500 行）
```

---

## 目标与产出

### 输入
- 一份论文 PDF 文件

### 过程
1. 使用 `scripts-old/extract_pdf_assets.py` 提取正文与图表（Figure x / Table x）
2. AI Agent 基于论文内容重命名图表文件（5-15 个单词）
3. 生成带图摘要

### 输出
- 一份 **1500–3000 字**的 Markdown 摘要
- **语言**：默认中文；如用户要求，可输出英文
- **内容**：嵌入论文全部图表 PNG，每个图表配 1–2 句精要解释
- **受众**：同专业高年级本科生（适当注释复杂术语）

### 重要提醒
生成摘要时，**必须同时提供**：
- `text/<paper>.txt` — 论文正文
- `images/*.png` — 全部图表

不要只给文本或只给图片！

---

## 目录与命名规范

### 输入
- PDF 文件：`<PDF_DIR>/<paper>.pdf`

### 输出（相对 PDF 所在目录）

| 路径 | 说明 |
|------|------|
| `text/<paper>.txt` | 纯文本 |
| `text/gathered_text.json` | 结构化文本（含页眉页脚移除、双栏重排） |
| `images/*.png` | Figure_* 与 Table_* 图表 PNG |
| `images/index.json` | 统一索引（可追溯格式） |
| `images/figure_contexts.json` | 图表首次提及上下文 |
| `images/layout_model.json` | 文档版式模型 |
| `images/rename_mapping.json` | 重命名计划记录 |

### 摘要文档命名
- 路径：与 PDF 同级
- 命名：`<paper>_阅读摘要-YYYYMMDD.md`
- 图片引用：使用 `images/...` 相对路径

---

## 环境与命令差异

> 执行命令前，请先确认当前运行环境！

| 操作 | macOS/Linux | Windows/PowerShell |
|------|-------------|-------------------|
| Python | `python3` | `python` |
| 移动文件 | `mv` | `Move-Item` |
| 复制文件 | `cp` | `Copy-Item` |
| 当前路径 | `pwd` | `Get-Location` |
| 当前日期 | `date +%Y%m%d` | `(Get-Date).ToString("yyyyMMdd")` |

### 示例：运行提取脚本

**推荐（新入口）**：

macOS/Linux：
```bash
python3 scripts/extract_pdf_assets.py --pdf "./<paper>.pdf" --preset robust
```

Windows/PowerShell：
```powershell
python .\scripts\extract_pdf_assets.py --pdf ".\<paper>.pdf" --preset robust
```

**兼容（旧入口）**：

macOS/Linux：
```bash
python3 scripts-old/extract_pdf_assets.py --pdf "./<paper>.pdf" --preset robust
```

Windows/PowerShell：
```powershell
python .\scripts-old\extract_pdf_assets.py --pdf ".\<paper>.pdf" --preset robust
```

### 示例：重命名图表文件

**macOS/Linux**：
```bash
cd images
mv "Figure_1_Overview.png" "Figure_1_Multimodal_Transformer_Architecture.png"
cd ..
python3 scripts-old/sync_index_after_rename.py .
```

**Windows/PowerShell**：
```powershell
Set-Location images
Move-Item "Figure_1_Overview.png" "Figure_1_Multimodal_Transformer_Architecture.png"
Set-Location ..
python .\scripts-old\sync_index_after_rename.py .
```

---

## 一次跑通（提取文本与图片）

### 环境要求
- Python 3.12+（推荐），3.10+ 兼容
- 依赖：`python3 -m pip install --user pymupdf`

### 基本执行

```bash
# 新入口（推荐）
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf

# 旧入口（兼容）
python3 scripts-old/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf
```

### 推荐：使用稳健预设

```bash
# 新入口（推荐）
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust

# 旧入口（兼容）
python3 scripts-old/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust
```

`--preset robust` 自动启用以下参数：
- 基础：`--dpi 300 --clip-height 520 --margin-x 26 --caption-gap 6`
- Phase A（文字裁切）：`--text-trim --adjacent-th 24`（表格 28）
- Phase B（对象对齐）：`--object-pad 8 --object-min-area-ratio 0.012`
- Phase D（Autocrop）：`--autocrop --autocrop-pad 30 --autocrop-mask-text`
- 防过裁：`--near-edge-pad-px 32 --protect-far-edge-px 18`
- 验收保护：高度≥0.6×、面积≥0.55×、对象覆盖率≥0.85×

---

## CLI 参数速查

### 输入/输出

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--pdf` | **必填** | PDF 文件路径 |
| `--out-text` | `<pdf_dir>/text/<name>.txt` | 文本输出路径 |
| `--out-dir` | `<pdf_dir>/images/` | 图片输出目录 |
| `--index-json` | `<out_dir>/index.json` | 索引文件路径 |
| `--prune-images` | `True` | 自动清理未引用旧图 |

### 渲染与裁剪

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--dpi` | `300` | 渲染分辨率 |
| `--clip-height` | `650.0` | 裁剪窗口高度（pt） |
| `--margin-x` | `20.0` | 水平边距（pt） |
| `--autocrop` | `False` | 启用白边自动裁切 |

### 方向与续页

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--below` | `""` | 强制从图注下方取图（如 `2,3`） |
| `--above` | `""` | 强制从图注上方取图 |
| `--t-below` | `""` | 表格强制下方（如 `1,S1`） |
| `--t-above` | `""` | 表格强制上方 |
| `--allow-continued` | `False` | 允许同号多页导出 |
| `--preset` | `None` | 预设（`robust`） |

### 锚点与扫描

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--anchor-mode` | `v2` | 锚点策略（`v1`/`v2`） |
| `--scan-heights` | `240,320,...,920` | V2 扫描高度（pt） |
| `--global-anchor` | `auto` | 图片全局锚点一致性 |
| `--global-anchor-table` | `auto` | 表格全局锚点一致性 |

### 智能识别与调试

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--smart-caption-detection` | `True` | 智能图注识别 |
| `--debug-captions` | `False` | 打印候选评分详情 |
| `--debug-visual` | `False` | 可视化调试模式 |
| `--layout-driven` | `on` | 版式驱动（`auto`/`on`/`off`） |
| `--adaptive-line-height` | `True` | 自适应行高 |

### 日志

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--log-level` | `INFO` | 日志级别（`DEBUG`/`INFO`/`WARNING`/`ERROR`） |
| `--log-jsonl` | `<out_dir>/run.log.jsonl` | 结构化日志 |

完整参数说明请参阅 [`docs/extract_pdf_assets_CLI参数说明.md`](docs/extract_pdf_assets_CLI参数说明.md)。

---

## 智能 Caption 识别

### 问题背景
论文中的图表标号（如 Figure 1）可能出现在：
1. **真实图注**：紧邻图表，作为图注首次出现
2. **前文引用**：正文中提前引用（如"如图1所示"）
3. **混合情况**：图注、前文、后文都出现该标号

### 四维评分机制

| 维度 | 分值 | 说明 |
|------|------|------|
| 位置特征 | 40分 | 与图像/绘图对象的距离 |
| 格式特征 | 30分 | 字体加粗、独立成段、标点 |
| 结构特征 | 20分 | 下一行有描述、段落长度 |
| 上下文特征 | 10分 | 语义分析（"展示"vs"如图所示"） |

### 使用示例

```bash
# 默认启用
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust

# 查看评分详情
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --debug-captions

# 关闭智能识别
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --no-smart-caption-detection
```

---

## 版式驱动提取（V2 Architecture）

### 三态控制

| 模式 | 说明 |
|------|------|
| `on`（默认） | 始终启用，确保正确排除章节标题 |
| `auto` | 复杂版式（双栏/密集文字）自动启用 |
| `off` | 禁用（不推荐） |

### 使用示例

```bash
# 默认 on
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust

# 使用 auto
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --layout-driven auto

# 关闭（不推荐）
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --layout-driven off
```

---

## 可视化调试模式

启用 `--debug-visual` 后，在 `images/debug/` 生成调试文件。

### 边界框颜色方案

| 阶段 | 颜色 | 说明 |
|------|------|------|
| Baseline | 🔵 蓝色 | 锚点选择阶段的原始窗口 |
| Phase A | 🟢 绿色 | 文本裁切后 |
| Phase B | 🟠 橙色 | 对象对齐后 |
| Phase D | 🔴 红色 | Autocrop 最终窗口 |
| Fallback | 🟡 黄色 | 验收失败回退 |
| Caption | 🟣 紫色 | 图注位置 |
| 标题 | 🟪 粉红实线 | 章节标题（H1/H2/H3） |
| 段落 | 🟪 粉红虚线 | 正文段落 |

---

## 图表重命名工作流

### 背景
脚本生成的临时文件名（如 `Figure_1_Overview_of_the_proposed.png`）基于原始图注。AI Agent 需要根据论文内容重命名为更具描述性的名称。

### 重命名规则

- **单词数量**：5-15 个单词（不含 `Figure_N_` 前缀）
- **命名原则**：
  - 准确反映图表核心内容
  - 使用专业但简洁的术语
  - 保持与论文术语一致
- **格式**：`Figure_N_<描述>.png` 或 `Table_N_<描述>.png`
- **字符**：仅允许字母、数字、下划线、连字符

### 示例

```bash
# 原始（脚本生成）
Figure_1_Overview_of_the_proposed_deep_learning.png

# 重命名后
Figure_1_Multimodal_Transformer_Architecture_Overview.png
```

### 执行步骤

```bash
# 1. 重命名文件
cd images
mv "Figure_1_Overview_of_the_proposed_deep_learning.png" "Figure_1_Multimodal_Transformer_Architecture.png"
# ... 重命名所有图表
cd ..

# 2. 同步索引
python3 scripts-old/sync_index_after_rename.py .
```

---

## 生成带图摘要（提示词模板）

### 必做任务清单

#### 任务1：图表重命名（必做）

基于论文内容，为每个图表 PNG 生成描述性名称（5-15 个单词）。

#### 任务2：生成摘要（必做）

生成 1500–3000 字的中文 Markdown 摘要：

**结构**：
- 研究动机
- 方法
- 训练与后训练
- 评测与效率
- 局限与展望
- 结论

**要求**：
- 按编号嵌入所有图表（使用**重命名后**的相对路径）
- 每个图表配 1–2 句精要解释
- 语言准确、精炼
- 量化关键点（复杂度、算量、关键超参）

### 完整工作流

```bash
# 步骤1：提取
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --allow-continued

# 步骤2：阅读论文，理解图表含义

# 步骤3：重命名图表
cd images
mv "Figure_1_xxx.png" "Figure_1_Architecture_Overview.png"
# ...
cd ..
python3 scripts-old/sync_index_after_rename.py .

# 步骤4：生成摘要 Markdown
```

### 摘要文档示例

```markdown
# 论文标题_阅读摘要-20260109.md

## 研究动机
...

## 方法
本文提出了一种多模态架构...

![Figure 1: 架构概览](images/Figure_1_Multimodal_Transformer_Architecture.png)
**图1** 展示了提出的多模态 Transformer 架构，包含...

## 实验结果
...

![Table 1: 性能对比](images/Table_1_Model_Performance_Comparison.png)
**表1** 列出了不同模型配置的性能指标...
```

---

## 常见问题（FAQ）

### 图片不显示
- 始终使用相对路径：`images/...`
- 确保 MD 文件与 `images/` 目录同级

### 顶部正文或标题混入
- 使用 `--above <N>` 强制方向
- 调高 `--adjacent-th` 或 `--mask-top-frac`
- 启用 `--layout-driven on`

### 多子图被截半
- 保持 row 级聚合
- 提高 `--autocrop-min-height-px`
- 对该图使用 `--no-refine N`

### 需要从图注下方取图
- 使用 `--below N` 覆盖方向判定

---

## 质量校验

```bash
# 确认文件存在
ls text/<paper>.txt
ls images/index.json
ls images/*.png

# 检查最新导出的 PNG（macOS/Linux）
ls -lt images/*.png | head -10

# Windows/PowerShell
Get-ChildItem images -Filter *.png | Sort-Object LastWriteTime -Descending | Select-Object -First 10
```

终端会输出 QC 汇总与弱对齐统计，供参考。
