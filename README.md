# pdf-summary-agent

## Overview (EN)
Extract text and figure/table PNGs from a research PDF and produce a JSON index. Designed for robust caption-anchored cropping (Anchor v2 with multi-scale scanning, global anchor consistency for both figures and tables), **smart caption detection** (distinguishes real captions from in-text references for both figures and tables), **far-side text trimming** (removes distant paragraphs like Abstract/Introduction), **adaptive line height** (auto-adjusts parameters based on document metrics), **layout-driven extraction** (V2 architecture with document layout modeling, now auto-enabled for complex layouts), optional auto-cropping, and safety checks to avoid over/under-trimming.

- Requirements: Python 3.12+, macOS/Linux recommended
- Dependencies: PyMuPDF (pymupdf)
- Outputs (relative to the input PDF directory):
  - `text/<paper>.txt` — plain text
  - `text/gathered_text.json` — structured paragraphs with header/footer removal **(P1-02 NEW)**
  - `images/*.png` (Figure_* and Table_*)
  - `images/index.json` — unified index with traceability metadata **(P1-06 enhanced)**
  - `images/figure_contexts.json` — first-mention context for each figure/table **(P1-09 NEW)**
  - `images/layout_model.json` — document layout model (auto-generated for complex layouts)
  - `images/rename_mapping.json` — rename plan record **(P1-10 NEW)**
- **NEW**: 
  - **(2025-12-29)** **P1 enhancements (P1-01~11)**: Layout-driven auto-enabled for complex layouts; Gathering stage with structured text; PDF pre-validation; QC independent stage; global anchor fallback for close margins; extended index.json with traceability; dynamic acceptance thresholds; regex coverage for Roman/S-prefix; figure context anchors; semi-automated rename workflow; structured input contract for summary generation.
  - **(2025-12-24)** **QA-06**: Enhanced QC reference detection with Roman numerals, S-prefix, Extended Data support.
  - **(2025-12-22)** **P0 fixes (P0-01~07)**: CLI args override env vars; Anchor v2 supports per-id `--above/--below` + `--t-above/--t-below`; fixes Supplementary IDs (S1) without collisions; protects 2-line captions from trimming; table `--no-text-trim` works; default pruning of stale PNGs; filename collision auto-disambiguation.
  - **(2025-10-27)** **Visual debug mode fixes**: Fixed dashes parameter for layout visualization (supports both figures and tables)
  - **(2025-10-21)** **Layout-driven extraction (V2)**: Use `--layout-driven` to build document layout model first, then use it to guide extraction (includes Step 3 layout-guided clipping)
  - **(2025-10-16)** **Adaptive line height**: Automatically adjusts trimming parameters based on document's typical line height (enabled by default)
  - **(2025-10-14)** **Two-stage naming workflow**: Script generates temporary filenames (12 words default), then AI agent renames figures/tables to final descriptive names (5-15 words) based on paper content before generating summary
  - **(2025-10-14)** Filename word limit: limit words after figure/table number in PNG filenames (default: 12 words, adjustable via `--max-caption-words`)
  - **(2025-10-11)** Smart caption detection now supports **both figures and tables** (4-dimensional scoring to distinguish real captions from references)
  - **(2025-10-11)** Far-side text trimming (Phase C) automatically removes distant paragraphs based on global anchor direction

### Install
- Quick: `python3 -m pip install --user pymupdf`
- Or: `python3 -m pip install --user -r scripts/requirements.txt` (if provided)

### Quickstart
```bash
# Basic usage (recommended) - layout-driven auto-enabled for complex layouts
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust

# Force layout-driven extraction ON (always)
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust --layout-driven on

# Force layout-driven extraction OFF
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust --layout-driven off

# With visual debugging (saves multi-stage boundary boxes)
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust --debug-visual

# Disable adaptive line height (use fixed parameters)
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust --no-adaptive-line-height

# Generate rename plan (P1-10)
python3 scripts/generate_rename_plan.py <PDF_DIR>
```
Common flags: `--allow-continued`, `--anchor-mode v1`, `--below/--above`, `--t-above/--t-below`, `--manifest <path>`, `--max-caption-words 10`, `--layout-driven [auto|on|off]`, `--debug-visual`, `--no-adaptive-line-height`, `--prune-images/--no-prune-images`, `--log-level [DEBUG|INFO|WARNING|ERROR]`.

> 跨平台说明：在 Windows/PowerShell 下通常使用 `python`、`Move-Item`、`Copy-Item`、`Get-Location`、`Get-Date`，而在 macOS/Linux 下使用 `python3`、`mv`、`cp`、`pwd`、`date`。详见 `AGENTS.md` 的“环境与命令差异”对照与示例。

### Notes
- Use relative paths like `images/...` when embedding figures/tables in Markdown next to the PDF.
- After renaming Figure_/Table_ PNGs, run `python scripts/sync_index_after_rename.py <PDF_DIR>` so `images/index.json` stays in sync. Use `python scripts/generate_rename_plan.py <PDF_DIR>` to generate a rename script with collision detection **(P1-10)**.
- **Image pruning**: Enabled by default; after writing `images/index.json`, the extractor removes unreferenced `Figure_*/Table_*` PNGs from `images/`. Use `--no-prune-images` to disable.
- **Forced direction**: With Anchor v2 (default) or v1, per-id `--above/--below` (figures) and `--t-above/--t-below` (tables) work as expected; use `--anchor-mode v1` only if you prefer the legacy anchoring strategy.
- **Smart caption detection**: Enabled by default, automatically distinguishes real captions from in-text references; use `--no-smart-caption-detection` to disable, or `--debug-captions` to see scoring details. See `AGENTS.md` for more.
- **Visual debug mode**: Use `--debug-visual` to save multi-stage boundary boxes overlaid on full pages (**supports both figures and tables**); outputs to `images/debug/Figure_N_pX_debug_stages.png` / `Table_N_pX_debug_stages.png` + legend files. Generated debug files are linked back in `images/index.json` via each item's `debug_artifacts` field. Paragraph boundaries are drawn as pink dashed lines when using `--layout-driven`. See `AGENTS.md` for color scheme and usage.
- **Adaptive line height**: Enabled by default, automatically adjusts trimming parameters (`adjacent_th`, `far_text_th`, etc.) based on document's typical line height; use `--no-adaptive-line-height` to disable and use fixed default parameters.
- **Layout-driven extraction (V2)**: Now auto-enabled for complex layouts (dual-column or dense text near figures). Use `--layout-driven on` to force enable, `--layout-driven off` to disable. Generates `layout_model.json` with page structure info **(P1-01)**.
- **Structured text gathering (P1-02)**: Automatically generates `text/gathered_text.json` with header/footer removal, dual-column reordering, and paragraph grouping.
- **PDF pre-validation (P1-03)**: Checks for encryption, text layer presence, and page count before extraction.
- **Quality control (P1-04)**: Independent QC stage checks extraction count vs text references, size reasonableness, and ID continuity.
- **Figure contexts (P1-09)**: Generates `images/figure_contexts.json` with first-mention location and surrounding paragraphs for each figure/table.
- **Structured input contract (P1-11)**: After extraction, displays contract status showing all required files for summary generation.
- **Table text masking**: For tables, text masking is disabled by default (table text is usually content); use `--table-mask-text` to enable if needed.
- **Robust preset**: The `--preset robust` enables A+B+D refinements with safety checks. Parameters differ for figures vs tables (e.g., `adjacent_th=24` for figures, `28` for tables). See `AGENTS.md` for complete parameter list.

### CLI Workflow (EN): place `AGENTS.md` and `scripts/` next to the PDF; let the Agent run it

Works with Codex / Claude Code / Gemini CLI or similar code-assistant CLIs.

- Prepare the folder:
```bash
# Copy this repo's AGENTS.md and scripts/ into the folder that contains <paper>.pdf, then cd into it
cp -R </path/to/pdf-summary-agent>/AGENTS.md </path/to/PDF_DIR>/
cp -R </path/to/pdf-summary-agent>/scripts </path/to/PDF_DIR>/
cd </path/to/PDF_DIR>
```

- Minimal instruction to paste into the CLI (no need to run the script manually):
```text
<paper>.pdf Please follow AGENTS.md in this folder: automatically call scripts/extract_pdf_assets.py to extract the main text and all figures/tables, then RENAME all figure/table PNGs to descriptive filenames (5-15 words) based on paper content, and finally produce a 1500–3000 word Chinese (default; English on request) Markdown summary. Embed every figure/table in order using the NEW filenames with relative paths (images/...), add a 1–2 sentence explanation for each, and save as <paper>_阅读摘要-YYYYMMDD.md.
```

- What the Agent will do automatically:
  - Install dependencies (pymupdf)
  - Run the extractor (equivalent to):
    ```bash
    python3 scripts/extract_pdf_assets.py --pdf "$(pwd)/<paper>.pdf" --preset robust --allow-continued
    # Generates temporary filenames like: Figure_1_Overview_of_the_proposed_deep_learning.png
    ```
  - **Rename all figures/tables** based on paper content (5-15 words):
    ```bash
    mv "images/Figure_1_Overview_of_the_proposed_deep_learning.png" "images/Figure_1_Multimodal_Transformer_Architecture_Overview_Diagram.png"
    mv "images/Figure_2_Experimental_results_on_benchmark_datasets.png" "images/Figure_2_Benchmark_Performance_Comparison_Across_Datasets.png"
    # ... rename all figures/tables
    ```
  - Use `text/<paper>.txt`, renamed `images/*.png`, and `images/index.json`
  - Generate `<paper>_阅读摘要-YYYYMMDD.md` with all images embedded via **new filenames** (e.g., `images/Figure_1_Multimodal_Transformer_Architecture_Overview_Diagram.png`)

- Optional tuning (override direction or fix slight over-trim):
```bash
python3 scripts/extract_pdf_assets.py \
  --pdf "$(pwd)/<paper>.pdf" \
  --preset robust \
  --below 2,3 \
  --allow-continued
```

- Verify: ensure `text/<paper>.txt`, `images/index.json`, and **renamed** `images/*.png` exist, and the generated `<paper>_阅读摘要-YYYYMMDD.md` displays all PNGs via relative `images/...` paths with **new descriptive filenames**.

---

## 概述 (ZH)
从论文 PDF 中提取正文文本与图表 PNG，并生成统一索引 JSON。内置稳健的基于图注定位（Anchor v2 多尺度滑窗，图与表独立全局锚点一致性）、**智能图注识别**（图与表均支持，区分真实图注与正文引用）、**远距文字清除**（自动移除Abstract/Introduction等大段正文）、**自适应行高**（根据文档特征自动调整参数）、**版式驱动提取**（V2架构，文档版式建模，复杂版式自动启用），可选像素级去白边，以及多重安全校验，避免过裁/漏裁。

- 环境：Python 3.12+（建议 macOS/Linux）
- 依赖：PyMuPDF（pymupdf）
- 输出（相对 PDF 所在目录）：
  - `text/<paper>.txt` — 纯文本
  - `text/gathered_text.json` — 结构化段落，含页眉页脚移除 **(P1-02 新增)**
  - `images/*.png`（含 Figure_* 与 Table_*）
  - `images/index.json` — 统一索引，含可追溯元数据 **(P1-06 增强)**
  - `images/figure_contexts.json` — 每个图表的首次提及上下文 **(P1-09 新增)**
  - `images/layout_model.json` — 文档版式模型（复杂版式自动生成）
  - `images/rename_mapping.json` — 重命名计划记录 **(P1-10 新增)**
- **新功能**：
  - **(2025-12-29)** **P1 功能增强 (P1-01~11)**：版式驱动提取对复杂版式自动启用；Gathering 阶段生成结构化文本；PDF 预验证；QC 独立阶段；全局锚点微弱优势回退；index.json 扩展为可追溯格式；精裁验收阈值动态化；正则表达式覆盖罗马数字/S前缀；图表正文上下文锚点；重命名工作流半自动化；摘要生成结构化输入合同。
  - **(2025-12-24)** **QA-06**：QC 引用检测增强，支持罗马数字、S前缀、Extended Data 格式。
  - **(2025-12-22)** **P0-01~07 紧急修复**：参数优先级改为 **CLI 显式传参 > 环境变量 > 默认值**；默认 Anchor v2 下 `--above/--below` 与 `--t-above/--t-below` 按编号强制方向直接生效；支持 `S1` 等附录编号且不与 `1` 冲突；两行检测不再误裁图注；表格 `--no-text-trim` 生效；默认清理未引用旧图（`--prune-images`）；文件名碰撞自动消歧。
  - **(2025-10-27)** **可视化调试修复**：修复版式可视化中的虚线参数错误（图与表均支持）
  - **(2025-10-21)** **版式驱动提取（V2）**：使用 `--layout-driven` 先构建文档版式模型，再引导提取（包含 Step 3 版式引导裁剪）
  - **(2025-10-16)** **自适应行高**：根据文档典型行高自动调整裁切参数（默认启用）
  - **(2025-10-14)** **两阶段命名工作流**：脚本生成临时文件名（默认12个单词），大模型基于论文内容将图表重命名为最终描述性名称（5-15个单词）后再生成摘要
  - **(2025-10-14)** 文件命名单词限制：限制图表编号后的单词数量（默认12个，可通过 `--max-caption-words` 调整）
  - **(2025-10-11)** 智能图注识别现已支持**图与表**（四维评分机制，自动区分真实图注与引用）
  - **(2025-10-11)** 远距文字清除（Phase C）基于全局锚点方向自动移除远距大段正文

### 安装
- 直接安装：`python3 -m pip install --user pymupdf`
- 或使用清单：`python3 -m pip install --user -r scripts/requirements.txt`（如提供）

### 快速开始
```bash
# 基本用法（推荐）- 复杂版式自动启用版式驱动
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust

# 强制开启版式驱动提取
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust --layout-driven on

# 强制关闭版式驱动提取
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust --layout-driven off

# 启用可视化调试（保存多阶段边界框）
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust --debug-visual

# 禁用自适应行高（使用固定参数）
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust --no-adaptive-line-height

# 生成重命名计划（P1-10）
python3 scripts/generate_rename_plan.py <PDF_DIR>
```
常用参数：`--allow-continued`、`--anchor-mode v1`、`--below/--above`、`--t-above/--t-below`、`--manifest <path>`、`--max-caption-words 10`、`--layout-driven [auto|on|off]`、`--debug-visual`、`--no-adaptive-line-height`、`--prune-images/--no-prune-images`、`--log-level [DEBUG|INFO|WARNING|ERROR]`。

### 提示
- 在生成 Markdown 摘要时，始终使用相对路径嵌图（如 `images/...`）。
- 重命名 PNG 后，运行 `python scripts/sync_index_after_rename.py <PDF_DIR>` 同步 `images/index.json`，避免清单与文件名不一致。使用 `python scripts/generate_rename_plan.py <PDF_DIR>` 生成带碰撞检测的重命名脚本 **(P1-10)**。
- **输出隔离**：默认已启用 `--prune-images`（写入最新 `images/index.json` 后自动清理未引用旧图）；如需关闭，使用 `--no-prune-images`。
- **强制方向**：默认 Anchor v2 下（或切换 v1 也同样可用），`--above/--below`（图）与 `--t-above/--t-below`（表）按编号强制方向直接生效。
- **智能图注识别**：默认启用，自动区分真实图注与正文引用；如需关闭，使用 `--no-smart-caption-detection`；如需查看评分详情，使用 `--debug-captions`。详见 `AGENTS.md`。
- **可视化调试模式**：使用 `--debug-visual` 保存多阶段边界框叠加的完整页面（**图与表均支持**）；输出到 `images/debug/Figure_N_pX_debug_stages.png` / `Table_N_pX_debug_stages.png` 及图例文件；生成的 debug 文件会通过 `images/index.json` 中每个条目的 `debug_artifacts` 字段回链。配合 `--layout-driven` 使用时，段落边界以粉红色虚线显示。颜色方案和使用方法详见 `AGENTS.md`。
- **自适应行高**：默认启用，根据文档典型行高自动调整裁切参数（`adjacent_th`、`far_text_th` 等）；如需禁用并使用固定默认参数，使用 `--no-adaptive-line-height`。
- **版式驱动提取（V2）**：复杂版式（双栏或图表附近文字密集）自动启用。使用 `--layout-driven on` 强制开启，`--layout-driven off` 关闭。生成 `layout_model.json` 包含页面结构信息 **(P1-01)**。
- **结构化文本收集（P1-02）**：自动生成 `text/gathered_text.json`，含页眉页脚移除、双栏重排、段落分组。
- **PDF 预验证（P1-03）**：提取前检查加密、文本层存在性、页数。
- **质量控制（P1-04）**：独立的 QC 阶段，检查提取数量与文本引用一致性、尺寸合理性、编号连续性。
- **图表上下文锚点（P1-09）**：生成 `images/figure_contexts.json`，包含每个图表的首次提及位置和周围段落。
- **结构化输入合同（P1-11）**：提取完成后显示合同状态，列出摘要生成所需的全部文件。
- **表格文本掩膜**：对于表格，文本掩膜默认关闭（表格内文字通常是内容的一部分）；如需启用，使用 `--table-mask-text`。
- **robust 预设**：`--preset robust` 启用 A+B+D 精裁与安全验收。图表参数有所不同（如图的 `adjacent_th=24`，表的为 `28`）。完整参数列表详见 `AGENTS.md`。

### CLI 工作流示例：将 `AGENTS.md` 与 `scripts/` 放到 PDF 同目录，由 Agent 自动调用脚本

适用工具：Codex / Claude Code / Gemini CLI 等“代码助手”类 CLI。

- 目录准备（关键）：
```bash
# 将本仓库的 AGENTS.md 与 scripts/ 复制到论文 PDF 所在目录，然后进入该目录
cp -R </path/to/pdf-summary-agent>/AGENTS.md </path/to/PDF_DIR>/
cp -R </path/to/pdf-summary-agent>/scripts </path/to/PDF_DIR>/
cd </path/to/PDF_DIR>
```

- 在 CLI 中用"最小自然语言指令"发起任务（无需手动运行脚本）：
```text
<paper>.pdf 请"按本目录的 AGENTS.md"执行摘要任务：自动调用 scripts/extract_pdf_assets.py 提取正文文本与全部图表，然后基于论文内容将所有图表PNG重命名为描述性名称（5-15个单词），最后生成一份 1500–3000 字的中文（默认；如用户要求可输出英文）Markdown 摘要。请将所有图与表按编号嵌入（使用重命名后的相对路径 images/...），每个元素配 1–2 句精要解释，文件名为 <paper>_阅读摘要-YYYYMMDD.md。
```

- Agent 将自动完成以下步骤：
  - 安装 Python 依赖（pymupdf）
  - 运行提取脚本（等价于）：
    ```bash
    python3 scripts/extract_pdf_assets.py --pdf "$(pwd)/<paper>.pdf" --preset robust --allow-continued
    # 生成临时文件名，如：Figure_1_Overview_of_the_proposed_deep_learning.png
    ```
  - **重命名所有图表文件**（基于论文内容，5-15个单词）：
    ```bash
    mv "images/Figure_1_Overview_of_the_proposed_deep_learning.png" "images/Figure_1_Multimodal_Transformer_Architecture_Overview_Diagram.png"
    mv "images/Figure_2_Experimental_results_on_benchmark_datasets.png" "images/Figure_2_Benchmark_Performance_Comparison_Across_Datasets.png"
    # ... 重命名所有图表
    ```
  - 读取 `text/<paper>.txt` 与重命名后的 `images/*.png`、`images/index.json`
  - 生成带图摘要：`<paper>_阅读摘要-YYYYMMDD.md`（1500–3000 字，使用**新文件名**按编号完整嵌入全部图表）

- 常见调优（如需覆盖方向判定或修正轻微过裁）：
```bash
# 例如需要强制部分图从图注下方取图：
python3 scripts/extract_pdf_assets.py \
  --pdf "$(pwd)/<paper>.pdf" \
  --preset robust \
  --below 2,3 \
  --allow-continued
```

- 结果核对：确认存在 `text/<paper>.txt`、`images/index.json` 与**重命名后的** `images/*.png`，并确保生成的 `<paper>_阅读摘要-YYYYMMDD.md` 能以相对路径 `images/...` 正确显示所有 PNG（使用**新的描述性文件名**）。
