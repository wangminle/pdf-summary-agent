# Uni-Parser 启发：pdf-summary-agent 改造清单（讨论稿）

> 目标：把 Uni-Parser 的“结构化/分组优先”思路，转化为本项目（`scripts/extract_pdf_assets.py` 为核心）的可落地改造项，便于我们共同评审、排序、拆分迭代。

## 0. 背景：Uni-Parser 的工作流（我们关心的部分）

Uni-Parser 的核心不是“更强 OCR”，而是**先获得可消费的结构与跨模态对齐**，再把不同内容路由给专家模型并行处理，最后做结构聚合与输出格式化：

1. **Document Pre-Processing**：完整性检查、元信息识别（扫描件/页码/语言等）。
2. **Group-based Layout Detection（核心）**：输出“分组的层次化 layout tree”，直接把 `image–caption`、`table–title`、`formula–ID`、`molecule–identifier` 等成对元素绑定，保留阅读顺序线索。
3. **Semantic Contents Parsing（并行多专家）**：Text OCR / Table / Formula / Chemical(OCSR) / Chart / Figure captioning 等并行处理。
4. **Semantic Contents Gathering**：过滤 header/footer、重建阅读顺序、跨栏/跨页合并，把行内/单元格内的多模态重新回填到容器里。
5. **Output Formatting & Semantic Chunking**：输出 raw JSON + hierarchy，按任务导出 Markdown/HTML/JSON，并以“语义单元”做 chunking，面向下游 RAG/Agent/LLM。

参考图：`ref/images/Figure_1_*.png`、`ref/images/Figure_2_*.png`、`ref/images/Figure_8_*.png`（由本项目已抽取）。

## 1. 我们当前流程（项目现状）

当前 `scripts/extract_pdf_assets.py` 的产物主要是：

- 文本：`<pdf_dir>/text/<paper>.txt`（依赖 `pdfminer.six`）
- 图表：`<pdf_dir>/images/Figure_*.png`、`<pdf_dir>/images/Table_*.png`
- 索引：`<pdf_dir>/images/index.json`（`type/id/page/caption/file/continued`）

定位策略上以 **caption 驱动** 为主：在 `page.get_text("dict")` 找到像 “Figure N / Table N” 的行块，构造候选裁剪窗并评分（anchor v2），再做多阶段精裁（A/B/C/D）和验收回退。

## 2. 改造方向总览（从 Uni-Parser 视角映射到我们这里）

我们可借鉴的重点是两条主线：

1. **结构优先（Group/Tree > Pixel Crop）**：把“图表 PNG”看成结构化文档的一部分，先建立稳定的结构与配对关系（caption↔对象），再导出图像/文本产物。
2. **清晰的 Gathering 与输出契约**：让 “`text/*.txt + images/*.png + index.json`” 变成一个更明确、更可复现的“结构化输出”，服务于摘要/问答/RAG。

## 3. 改造清单（全量候选）

下面是“可参考”的全量候选项；不代表都必须做，建议我们按优先级拆成多轮迭代。

### 3.1 P0（强烈建议优先修：会导致错配/错截/不可复现）

**P0-01 修复 Supplementary 编号解析（S1/S2）与冲突问题（Figure/Table）**
- 现象：`Supplementary Figure S1` 可能被解析成 `id=1`，与 `Figure 1` 冲突；表格同理。
- 预期：`id` 保留 `S1`（或 `Figure_S1`）作为真实标识；`--t-below S1` 等控制项能命中；index 不再混号。
- 涉及：caption 正则、`AttachmentRecord.ident`、排序 `num_key()`、文件命名与 continued 逻辑、`images/index.json` 一致性。

**P0-02 让 anchor v2 支持按编号强制 `--above/--below` 与 `--t-above/--t-below`**
- 现象：当前强制方向只在 `anchor_mode == v1` 生效；v2 会忽略强制列表，导致“以为已强制但仍错截”。
- 预期：v2 的候选生成阶段直接根据强制列表限制 side（只扫 above 或只扫 below），并把选择原因写入 index（便于追溯）。

**P0-03 统一参数优先级：CLI 参数必须覆盖环境变量**
- 现象：主流程对 `EXTRACT_ANCHOR_MODE/SCAN_* /GLOBAL_ANCHOR*` 使用 `os.environ.setdefault`；若用户环境已设置，会悄悄覆盖本次 CLI 意图，导致复现困难。
- 预期：明确优先级 `CLI > ENV > default`，并在日志中打印最终生效值。

**P0-04 修复表格 `text_trim` 永远开启的问题**
- 现象：`extract_tables(..., text_trim=True if args.text_trim else True)` 等价于永远 True，无法关闭以做定位或适配特殊 PDF。
- 预期：表格与图片一致：`--text-trim/--no-text-trim` 能真正控制；同时保留 robust preset 默认开启。

**P0-05 输出隔离：避免旧 PNG 混入新结果**
- 现象：默认不会清理 `images/`，历史文件会混入“全量喂给模型”的集合。
- 预期：提供一种稳定策略：
  - 方案A：默认每次输出到 `images/run-<timestamp>/`，index 指向该 run（最稳，代价是路径变化）
  - 方案B：默认 `--prune-images`（或自动 prune），并在 index 写入 `run_id`/`generated_at` 以便追踪

### 3.2 P1（显著提升稳健性与“结构化程度”）

**P1-01 把“图表导出”升级为“结构化分组输出（轻量版 layout tree）”**
- 目标：在现有 caption 驱动基础上，实现“Uni-Parser 的最小可用思想”：
  - 每页输出 block 列表（bbox/type）
  - caption 与对象（image/vector region）的配对与 group_id
  - reading order rank（粗略即可）
- 产物建议：新增 `images/document.json`（或 `<pdf_stem>.structured.json`），作为摘要生成与后续扩展的主输入。

**P1-02 Gathering 阶段显式化：header/footer 清理 + 阅读顺序重建 + 跨栏/跨页合并**
- 目标：把“文本提取结果”从“杂糅的 plain text”提升到“可按段落/标题/表格/图注组织”的结构。
- 最小版本：
  - header/footer 检测与剔除（基于重复行/位置）
  - 双栏顺序重排（基于 x0/x1 与列检测）
  - continued 图表/跨页表格合并为同一逻辑条目（index 层面也合并）

**P1-03 caption 识别的鲁棒性补强（避免正文引用/页眉误触发）**
- 目标：把 smart caption detection 的产物写入 index（score、候选列表摘要、reject reason），并提供“严格模式”：
  - 若最低分不足则不导出该编号（或标记 uncertain）
  - 对“Figure5”这类无空格/无标点的 caption 行做更稳的 normalization

**P1-04 index.json 扩展为“可追溯/可复现”的最小诊断集合**
- 建议字段（不破坏现有兼容）：
  - `anchor_mode`、`side`（above/below）、`global_anchor_used`
  - `clip_rect_pt`（x0,y0,x1,y1）与 `dpi`
  - `stages_applied`（A/B/C/D/fallback）与关键阈值
  - `scores`（ink/coverage/para_ratio 等摘要值）
  - `source_pdf`（stem/hash）与 `run_id/generated_at`

**P1-05 图表“内容级”辅助（可选）：OCR/Captioning 用于摘要解释**
- 目标：让摘要阶段更依赖“图表内容”而不是仅 caption：
  - 对图表 PNG 运行轻量 OCR（仅提取关键标签/坐标轴/列名），写入 index
  - 或接入外部 VLM 做图像 caption（可配置、可缓存）

### 3.3 P2（长期演进：接近 Uni-Parser 的架构/能力）

**P2-01 模块化流水线：stage 明确、可插拔、多专家路由（本地版）**
- 把当前脚本内的大函数拆为稳定 stages：`Precheck -> Layout/Grouping -> Parse -> Gather -> Export`。
- 为每个 stage 定义输入/输出结构（JSON schema），便于替换（例如未来接入更强 layout model / table parser）。

**P2-02 并行化/队列化（吞吐导向）**
- 若未来做批量论文处理，可借鉴 Uni-Parser 的“pipeline queue + micro-batching + async”：
  - CPU 预处理队列、GPU 推理队列、CPU 后处理队列
  - 但这属于工程优化，通常不如结构化正确性优先。

**P2-03 丰富模态：公式、化学结构、图表数据表格化**
- 对齐 Uni-Parser：公式识别、OCSR、chart→data table；并将这些结果回填到文本/表格结构中。
- 对本项目价值：摘要更“懂图表”，但成本与依赖显著增加。

### 3.4 质量保证与可维护性（贯穿各优先级）

**QA-01 基准与回归测试**
- 为关键正则（Figure/Table/S1/罗马数字/附录表）添加单元测试。
- 为关键行为添加“golden index.json”对比测试（不一定纳入整 PDF，大文件可放在 `ref/` 并在 CI 中跳过）。

**QA-02 可视化调试统一化**
- 当前已有 `--debug-visual`/`--dump-candidates`：建议把输出与 index 关联（index 中写 `debug_artifacts` 列表），并保证命名稳定。

**QA-03 失败分级与可解释日志**
- 把 “refinement rejected / fallback” 从 print 提升为结构化事件（写到 index 或单独 `run.log.jsonl`），便于批量复盘。

**QA-04 命名与重命名工作流联动**
- 本项目摘要阶段要求“二次重命名图表文件并同步 index”：建议在结构化输出里额外存 `original_file` 与 `current_file`，避免重命名导致索引失配。

## 4. 建议的讨论产出（我们开会/评审时要达成的结论）

1. **P0 里哪些是必做**（通常 P0-01~P0-05 全做）
2. 是否引入 **新的结构化主输出**（P1-01/P1-04）：
   - 如果做：选定文件名与 schema，保证向后兼容 `images/index.json`
3. “Gathering”做到什么程度（P1-02）：
   - 最小可用：header/footer + 双栏顺序 + continued 合并
   - 下一步：章节层级融合与语义 chunking
4. 对外依赖策略（P1-05/P2-03）：
   - 是否允许可选依赖（PIL、OCR、VLM），以及缓存与离线运行策略

## 5. 参考：本次 Uni-Parser PDF 的抽取产物

- PDF：`ref/2512.15098v1-Uni-Parser.pdf`
- 文本：`ref/text/2512.15098v1-Uni-Parser.txt`
- 图表：`ref/images/*.png`
- 索引：`ref/images/index.json`

