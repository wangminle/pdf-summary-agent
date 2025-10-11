# pdf-summary-agent

## Overview (EN)
Extract text and figure/table PNGs from a research PDF and produce a JSON index. Designed for robust caption-anchored cropping (Anchor v2 with multi-scale scanning, global anchor consistency for both figures and tables), **smart caption detection** (distinguishes real captions from in-text references for both figures and tables), **far-side text trimming** (removes distant paragraphs like Abstract/Introduction), optional auto-cropping, and safety checks to avoid over/under-trimming.

- Requirements: Python 3.12+, macOS/Linux recommended
- Dependencies: PyMuPDF (pymupdf), pdfminer.six
- Outputs (relative to the input PDF directory):
  - `text/<paper>.txt`
  - `images/*.png` (Figure_* and Table_*)
  - `images/index.json`
- **NEW (2025-01-11)**: 
  - Smart caption detection now supports **both figures and tables** (4-dimensional scoring to distinguish real captions from references)
  - Far-side text trimming (Phase C) automatically removes distant paragraphs based on global anchor direction

### Install
- Quick: `python3 -m pip install --user pymupdf pdfminer.six`
- Or: `python3 -m pip install --user -r scripts/requirements.txt` (if provided)

### Quickstart
```bash
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust
```
Common flags: `--allow-continued`, `--anchor-mode v1`, `--below/--above`, `--manifest <path>`.

### Notes
- Use relative paths like `images/...` when embedding figures/tables in Markdown next to the PDF.
- With Anchor v2 (default), per-id `--above/--below` works only if you switch to `--anchor-mode v1`.
- **Smart caption detection**: Enabled by default, automatically distinguishes real captions from in-text references; use `--no-smart-caption-detection` to disable, or `--debug-captions` to see scoring details. See `AGENTS.md` for more.

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
<paper>.pdf Please follow AGENTS.md in this folder: automatically call scripts/extract_pdf_assets.py to extract the main text and all figures/tables, then read the outputs and produce a 1500–2400 word Chinese Markdown summary. Embed every figure/table in order using relative paths (images/...), add a 1–2 sentence explanation for each, and save as <paper>_阅读摘要-YYYYMMDD.md.
```

- What the Agent will do automatically:
  - Install dependencies (pymupdf, pdfminer.six)
  - Run the extractor (equivalent to):
    ```bash
    python3 scripts/extract_pdf_assets.py --pdf "$(pwd)/<paper>.pdf" --preset robust --allow-continued
    ```
  - Use `text/<paper>.txt`, `images/*.png`, and `images/index.json`
  - Generate `<paper>_阅读摘要-YYYYMMDD.md` with all images embedded via `images/...`

- Optional tuning (override direction or fix slight over-trim):
```bash
python3 scripts/extract_pdf_assets.py \
  --pdf "$(pwd)/<paper>.pdf" \
  --preset robust \
  --anchor-mode v1 \
  --below 2,3 \
  --allow-continued
```

- Verify: ensure `text/<paper>.txt`, `images/index.json`, and `images/*.png` exist, and the generated `<paper>_阅读摘要-YYYYMMDD.md` displays all PNGs via relative `images/...` paths.

---

## 概述 (ZH)
从论文 PDF 中提取正文文本与图表 PNG，并生成统一索引 JSON。内置稳健的基于图注定位（Anchor v2 多尺度滑窗，图与表独立全局锚点一致性）、**智能图注识别**（图与表均支持，区分真实图注与正文引用）、**远距文字清除**（自动移除Abstract/Introduction等大段正文）、可选像素级去白边，以及多重安全校验，避免过裁/漏裁。

- 环境：Python 3.12+（建议 macOS/Linux）
- 依赖：PyMuPDF（pymupdf）、pdfminer.six
- 输出（相对 PDF 所在目录）：
  - `text/<paper>.txt`
  - `images/*.png`（含 Figure_* 与 Table_*）
  - `images/index.json`
- **新功能 (2025-01-11)**：
  - 智能图注识别现已支持**图与表**（四维评分机制，自动区分真实图注与引用）
  - 远距文字清除（Phase C）基于全局锚点方向自动移除远距大段正文

### 安装
- 直接安装：`python3 -m pip install --user pymupdf pdfminer.six`
- 或使用清单：`python3 -m pip install --user -r scripts/requirements.txt`（如提供）

### 快速开始
```bash
python3 scripts/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust
```
常用参数：`--allow-continued`、`--anchor-mode v1`、`--below/--above`、`--manifest <path>`。

### 提示
- 在生成 Markdown 摘要时，始终使用相对路径嵌图（如 `images/...`）。
- 默认 Anchor v2 下，若需按编号强制上/下方向，请切换 `--anchor-mode v1` 后再配合 `--above/--below`。
- **智能图注识别**：默认启用，自动区分真实图注与正文引用；如需关闭，使用 `--no-smart-caption-detection`；如需查看评分详情，使用 `--debug-captions`。详见 `AGENTS.md`。

### CLI 工作流示例：将 `AGENTS.md` 与 `scripts/` 放到 PDF 同目录，由 Agent 自动调用脚本

适用工具：Codex / Claude Code / Gemini CLI 等“代码助手”类 CLI。

- 目录准备（关键）：
```bash
# 将本仓库的 AGENTS.md 与 scripts/ 复制到论文 PDF 所在目录，然后进入该目录
cp -R </path/to/pdf-summary-agent>/AGENTS.md </path/to/PDF_DIR>/
cp -R </path/to/pdf-summary-agent>/scripts </path/to/PDF_DIR>/
cd </path/to/PDF_DIR>
```

- 在 CLI 中用“最小自然语言指令”发起任务（无需手动运行脚本）：
```text
<paper>.pdf 请“按本目录的 AGENTS.md”执行摘要任务：自动调用 scripts/extract_pdf_assets.py 提取正文文本与全部图表，随后整体阅读并生成一份 1500–2400 字的中文 Markdown 摘要。请将所有图与表按编号嵌入（相对路径 images/...），每个元素配 1–2 句精要解释，文件名为 <paper>_阅读摘要-YYYYMMDD.md。
```

- Agent 将自动完成以下步骤：
  - 安装 Python 依赖（pymupdf、pdfminer.six）
  - 运行提取脚本（等价于）：
    ```bash
    python3 scripts/extract_pdf_assets.py --pdf "$(pwd)/<paper>.pdf" --preset robust --allow-continued
    ```
  - 读取 `text/<paper>.txt` 与 `images/*.png`、`images/index.json`
  - 生成带图摘要：`<paper>_阅读摘要-YYYYMMDD.md`（1500–2400 字，按编号完整嵌入全部图表）

- 常见调优（如需覆盖方向判定或修正轻微过裁）：
```bash
# 例如需要强制部分图从图注下方取图：
python3 scripts/extract_pdf_assets.py \
  --pdf "$(pwd)/<paper>.pdf" \
  --preset robust \
  --anchor-mode v1 \
  --below 2,3 \
  --allow-continued
```

- 结果核对：确认存在 `text/<paper>.txt`、`images/index.json` 与 `images/*.png`，并确保生成的 `<paper>_阅读摘要-YYYYMMDD.md` 能以相对路径 `images/...` 正确显示所有 PNG。