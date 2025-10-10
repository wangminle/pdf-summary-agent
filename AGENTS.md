# 存储库指南（Agent 工作流）

## 目标与产出
- 输入：一份论文 PDF。
- 过程：用 `scripts/extract_pdf_assets.py` 提取正文与“附图与表格”（Figure x / Table x）。
- 输出：一份 1500–2400 字的中文 Markdown 摘要，嵌入论文全部“图与表”的 PNG，并为每个图表按照标号给出精要解释。
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

### 锚点 V2（默认）与“全局锚点一致性”
- 锚点 V2：围绕 caption 多尺度滑窗（默认高度：240,320,420,520,640,720,820），结合结构打分（墨迹/对象覆盖/段落占比；表格再加“列对齐峰+线段密度”），并做边缘“吸附”。
- 中线护栏：扫描窗口不会跨越相邻两条图注的中线（`--caption-mid-guard 6`，建议 6–10pt）。
- 距离罚项：候选离 caption 越远得分越低（`--scan-dist-lambda 0.15`，建议 0.15–0.2）。
- 全局锚点一致性（默认开启）：`--global-anchor auto` 预扫整篇后，若“下方总分”显著高于“上方总分”（或反之），本篇文档所有 Figure 统一采用该方向；阈值由 `--global-anchor-margin` 控制（默认 0.02）。可用 `--global-anchor off` 关闭。
- 模式切换与调试：可用 `--anchor-mode v1|v2` 显式指定锚点策略；扫描步长与高度可由 `--scan-step`、`--scan-heights` 调整；如需导出页面候选窗口用于调试，使用 `--dump-candidates`。

### 防“半幅/错截”的补救
- 远端外扩：若在精裁后远离图注的边仍被对象“贴边”，脚本会向该方向外扩（最多约 200pt）以补齐整幅；必要时可调大最高扫描高度（`--scan-heights`）或外扩上限（需要代码内改，默认 200pt）。

### 可选开关
- 对个别图禁用精裁：`--no-refine 2,3`（仅保留基线或 A）。
- 仅改靠近图注的一侧边界（默认开）：`--refine-near-edge-only`；如需禁用用于调试：`--no-refine-near-edge-only`。
- 调整自适应裁切的收缩保护：`--autocrop-shrink-limit 0.35`（最多收缩 35% 面积）、`--autocrop-min-height-px 80`（最小高度，随 DPI 换算）。
- 表格参数：`--table-*` 同名选项与图相近，但默认对表关闭文本掩膜、降低连通域面积阈值。
- 关闭表格提取：`--no-tables`（默认开启表格提取）。
- 导出 CSV 清单：`--manifest <path>` 可生成包含 `(type,id,page,caption,file,continued)` 的 CSV；与 `images/index.json` 字段一致。

### 推荐参数备忘（遇到边沿轻微过裁时）
- 仅靠近图注一侧再放宽：`--near-edge-pad-px 34~36`
- 同时保护远端上/下边：`--protect-far-edge-px 20~24`
- 图注密集页防跨图：`--caption-mid-guard 8~12` + `--scan-dist-lambda 0.18`

### 质量校验
- 确认生成 `text/<paper>.txt`，且 `images/` 中附图数量与原文一致或接近。
- 对多子图页，检查 (a)/(b) 是否完整保留。
 - 终端会输出 QC 汇总与弱对齐统计（从 txt 统计 Figure/Table/图/表 出现次数，供参考）。

### 关于“基线→精裁”的融合策略
- 基线：按“图注为锚点”的上/下候选窗口与评分挑选（row 级聚合，避免子图丢失）。
- 精裁：顺序执行 A（单边裁头）→ B（连通域近侧对齐 + 主/横轴并集）→ D（文本掩膜 autocrop，带收缩保护）。
- 验收：若触发保护门槛，自动回退到 A-only 或基线，避免“半幅/过裁”。

## 生成带图摘要（大模型提示词模板）
请务必同时提供 `text/<paper>.txt` 与 `images/*.png` 的完整集合。建议将 txt 的要点（或全文）与图片清单（图号+文件名）一并喂给模型。
```
请基于给定的 txt 与全部 PNG 附图与表格，生成一份1500–2000字的中文Markdown摘要：
- 结构包含：研究动机/方法/训练与后训练/评测与效率/局限与展望/结论。
- 按编号将所有“图与表”嵌入文档（相对路径如 images/Figure_1_*.png / images/Table_1_*.png），每个元素配1–2句精要解释。
- 语言准确、精炼，量化关键点（复杂度、算量、关键超参）。
```

## 常见问题（FAQ）
- 图片不显示：始终使用“相对于 MD 的相对路径”。若 MD 与 `images/` 同级，写 `images/...`；若在 `tests/` 下生成 MD，也写 `images/...`（确保与 MD 同级的 `images/` 存在）。
- 顶部正文或标题混入：优先 `--above <N>` + `--clip-height`，并启用 A/D（或调高 `--adjacent-th`、`--mask-top-frac`）。
- 多子图被截半：保持 row 级聚合；开启 B 的“近侧对齐 + 主/横轴并集”，必要时提高 `--autocrop-min-height-px` 或对该图 `--no-refine`。
- 需要从图注下方取图：`--below N` 覆盖方向判定（与 A/B/D、验收可叠加）。
