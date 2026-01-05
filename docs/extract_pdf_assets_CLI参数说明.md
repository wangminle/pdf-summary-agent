# `scripts/extract_pdf_assets.py` CLI 参数说明

本文档整理主脚本 `scripts/extract_pdf_assets.py` 的命令行参数（默认值/可选值），便于后续重构与对外接口维护。

> 说明：部分“开关”类参数在实现上采用了“默认启用”的设计（例如 `--prune-images`、`--smart-caption-detection`、`--layout-driven` 等）。通常你不需要显式传入开启参数；如需关闭，请使用对应的 `--no-xxx` 参数。

---

## 1) 输入/输出

- `--pdf`：必填，PDF 文件路径
- `--out-text`：默认 `None`（若省略则写入 `<pdf_dir>/text/<pdf_name>.txt`）
- `--out-dir`：默认 `None`（若省略则写入 `<pdf_dir>/images/`）
- `--manifest`：默认 `None`（可选：导出 CSV 清单）
- `--index-json`：默认 `None`（若省略则写入 `<pdf_dir>/images/index.json`）
- `--prune-images`：默认 **启用**（`True`）；关闭用 `--no-prune-images`
- `--no-prune-images`：将 `prune_images` 设为 `False`（禁用自动清理未被 index 引用的旧 PNG）

---

## 2) 渲染与基础裁剪

- `--dpi`：默认 `300`
- `--clip-height`：默认 `650.0`（pt）
- `--margin-x`：默认 `20.0`（pt）
- `--caption-gap`：默认 `5.0`（pt）
- `--max-caption-chars`：默认 `160`（用于图注生成临时文件名）
- `--max-caption-words`：默认 `12`（用于图注生成临时文件名）
- `--min-figure`：默认 `1`
- `--max-figure`：默认 `999`
- `--autocrop`：默认 `False`（开启白边自动裁切）
- `--autocrop-pad`：默认 `30`（px）
- `--autocrop-white-th`：默认 `250`（0–255）

---

## 3) 强制方向 / 续页 / 预设

- `--below`：默认 `""`（逗号分隔图号；这些图强制“从图注下方取图”）
- `--above`：默认 `""`（逗号分隔图号；这些图强制“从图注上方取图”）
- `--allow-continued`：默认 `False`（允许同图号多页导出 continued）
- `--preset`：默认 `None`；可选值：`robust`

---

## 4) 锚点（Anchor v1/v2）与扫描（V2）

- `--anchor-mode`：默认 `v2`；可选值：`v1|v2`
- `--scan-step`：默认 `14.0`（pt）
- `--scan-heights`：默认 `"240,320,420,520,640,720,820,920"`（pt，逗号分隔）
- `--scan-dist-lambda`：默认 `0.12`
- `--scan-topk`：默认 `3`
- `--dump-candidates`：默认 `False`（导出候选窗口用于调试）
- `--caption-mid-guard`：默认 `6.0`（pt）

---

## 5) 智能图注识别 / 调试

- `--smart-caption-detection`：默认 **启用**（`True`）；关闭用 `--no-smart-caption-detection`
- `--no-smart-caption-detection`：将 `smart_caption_detection` 设为 `False`
- `--debug-captions`：默认 `False`（打印候选图注打分细节）
- `--debug-visual`：默认 `False`（输出可视化调试图到 `images/debug/`）

---

## 6) 版式驱动（Layout-Driven）

- `--layout-driven`：默认 `"on"`；可选值：`auto|on|off`
  - 兼容行为：`--layout-driven`（不带值）等价于 `--layout-driven on`
- `--layout-json`：默认 `None`（若省略则写入/读取 `<out_dir>/layout_model.json`）

---

## 7) 自适应行高

- `--adaptive-line-height`：默认 **启用**（`True`）；关闭用 `--no-adaptive-line-height`
- `--no-adaptive-line-height`：将 `adaptive_line_height` 设为 `False`

---

## 8) A：文字裁切（Text Trim）与远距文字

- `--text-trim`：默认 `False`；关闭用 `--no-text-trim`
- `--no-text-trim`：将 `text_trim` 设为 `False`（可覆盖 `--text-trim` 与 preset 默认）
- `--text-trim-width-ratio`：默认 `0.5`
- `--text-trim-font-min`：默认 `7.0`
- `--text-trim-font-max`：默认 `16.0`
- `--text-trim-gap`：默认 `6.0`（pt）
- `--adjacent-th`：默认 `24.0`（pt）

远距文字（far-text）：
- `--far-text-th`：默认 `300.0`（pt）
- `--far-text-para-min-ratio`：默认 `0.30`
- `--far-text-trim-mode`：默认 `aggressive`；可选值：`aggressive|conservative`

far-side（P1-1）：
- `--far-side-min-dist`：默认 `50.0`（pt）
- `--far-side-para-min-ratio`：默认 `0.12`

---

## 9) B：对象对齐（连通域/组件）

- `--object-pad`：默认 `8.0`（pt）
- `--object-min-area-ratio`：默认 `0.012`
- `--object-merge-gap`：默认 `6.0`（pt）

---

## 10) D：文本掩膜辅助 Autocrop

- `--autocrop-mask-text`：默认 `False`
- `--mask-font-max`：默认 `14.0`
- `--mask-width-ratio`：默认 `0.5`
- `--mask-top-frac`：默认 `0.6`
- `--text-trim-min-para-ratio`：默认 `0.18`
- `--protect-far-edge-px`：默认 `14`（px）
- `--near-edge-pad-px`：默认 `32`（px）

---

## 11) 全局锚点一致性

- `--global-anchor`：默认 `auto`；可选值：`off|auto`
- `--global-anchor-margin`：默认 `0.02`
- `--global-anchor-table`：默认 `auto`；可选值：`off|auto`
- `--global-anchor-table-margin`：默认 `0.03`

---

## 12) 精裁开关 / 安全门

- `--no-refine`：默认 `""`（逗号分隔图号：这些图禁用 B/D 精裁）

near-edge-only 行为：
- `--refine-near-edge-only`：默认 **启用**（`True`）
- `--no-refine-near-edge-only`：默认 `False`；若传入则置为 `True`，并在内部将 `refine_near_edge_only` 强制视为关闭（调试用）

安全门与回退：
- `--no-refine-safe`：默认 `False`（传入后禁用安全门与回退）
- `--autocrop-shrink-limit`：默认 `0.30`
- `--autocrop-min-height-px`：默认 `80`

---

## 13) 表格（Tables）

表格提取开关：
- `--include-tables`：默认 **启用**（`include_tables=True`）
- `--no-tables`：将 `include_tables` 设为 `False`

表格窗口与裁切：
- `--table-clip-height`：默认 `520.0`（pt）
- `--table-margin-x`：默认 `26.0`（pt）
- `--table-caption-gap`：默认 `6.0`（pt）
- `--t-below`：默认 `""`（例如：`1,3,S1`）
- `--t-above`：默认 `""`
- `--table-object-min-area-ratio`：默认 `0.005`
- `--table-object-merge-gap`：默认 `4.0`

表格 autocrop：
- `--table-autocrop`：默认 **启用**（`True`）；关闭用 `--no-table-autocrop`
- `--no-table-autocrop`：将 `table_autocrop` 设为 `False`
- `--table-autocrop-pad`：默认 `20`（px）
- `--table-autocrop-white-th`：默认 `250`

表格文本掩膜：
- `--table-mask-text`：默认 `False`（开启表格 autocrop 的文本掩膜）
- `--no-table-mask-text`：将 `table_mask_text` 设为 `False`（显式关闭）
- `--table-adjacent-th`：默认 `28.0`（pt）

---

## 14) 日志

- `--log-level`：默认 `INFO`；可选值：`DEBUG|INFO|WARNING|ERROR`
- `--log-file`：默认 `None`（可选：文本日志）
- `--log-jsonl`：argparse 默认 `None`；运行时若仍为 `None`，会自动设置为 `<out_dir>/run.log.jsonl`

