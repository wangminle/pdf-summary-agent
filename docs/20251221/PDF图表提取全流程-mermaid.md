# PDF 图表提取全流程（Mermaid）

> 对应实现：`scripts/extract_pdf_assets.py`（提取）+ `scripts/sync_index_after_rename.py`（重命名后同步索引，可选）。

```mermaid
flowchart TD
  A([开始：用户提供 PDF]) --> B[运行提取脚本<br/>python scripts/extract_pdf_assets.py --pdf &lt;paper&gt;.pdf ...]
  B --> C[parse_args 解析参数<br/>out-dir/out-text/index-json/manifest/preset/anchor-mode/...]
  C --> D[解析默认输出路径<br/>&lt;pdf_dir&gt;/images/, &lt;pdf_dir&gt;/text/&lt;paper&gt;.txt]
  D --> E[try_extract_text（可用则提取正文）<br/>输出：text/&lt;paper&gt;.txt]
  E --> F[应用 preset robust（可选）<br/>设置裁剪/精裁/验收等参数]
  F --> G[设置环境变量（供内部逻辑读取）<br/>EXTRACT_ANCHOR_MODE / SCAN_* / GLOBAL_ANCHOR* / ...]

  G --> H{是否启用 --layout-driven?}
  H -- 是 --> H1[extract_text_with_format 构建版式模型（V2）<br/>输出：images/layout_model.json]
  H -- 否 --> H2[跳过版式模型]
  H1 --> I
  H2 --> I

  I[提取 Figures：extract_figures] --> J{smart-caption-detection?}
  J -- 是 --> J1[build_caption_index 预扫全篇<br/>收集 Figure 编号的所有候选位置]
  J -- 否 --> J2[逐页简单正则匹配 Figure 行]
  J1 --> K
  J2 --> K

  K{adaptive-line-height?} -- 是 --> K1[统计典型行高/字号/行距<br/>动态调整 adjacent_th / far_text_th / ...]
  K -- 否 --> K2[使用固定阈值]
  K1 --> L
  K2 --> L

  L{GLOBAL_ANCHOR=auto?} -- 是 --> L1[全局预扫：估计 above/below 总分<br/>决定全篇 Figure 统一方向（可选）]
  L -- 否 --> L2[不启用全局统一方向]
  L1 --> M
  L2 --> M

  M[逐页处理（Figure）] --> N[收集本页 Figure captions<br/>- smart：按页/按全局最优（取决于 allow-continued）<br/>- simple：合并多行 caption]
  N --> O[对每个 caption 选择裁剪窗口（Anchor v1/v2）]

  O --> O1{anchor-mode=v1?}
  O1 -- 是 --> O1a[构造上/下两个候选窗口（above/below）<br/>支持 --above/--below/EXTRACT_FORCE_ABOVE 强制]
  O1 -- 否(v2) --> O1b[多尺度滑窗扫描（SCAN_HEIGHTS/SCAN_STEP）<br/>+ 中线护栏（CAPTION_MID_GUARD）<br/>+ 距离罚项（SCAN_DIST_LAMBDA）<br/>+ 截断检测惩罚 + 吸附]
  O1a --> P
  O1b --> P

  P[精裁/清理（可选组合）] --> P1[Phase A：图注邻近文字裁切 + 远距文字清除（含 far-side）]
  P1 --> P2[Phase B：对象连通域对齐/合并（保护多子图）]
  P2 --> P3[Phase D：autocrop 去白边（可配 text mask）]
  P3 --> P4[安全验收：面积/高度/覆盖率/密度阈值<br/>失败则回退到更保守结果]

  P4 --> Q{--debug-visual?}
  Q -- 是 --> Q1[输出可视化调试：images/debug/*_debug_stages.png + legend.txt]
  Q -- 否 --> Q2[跳过调试输出]
  Q1 --> R
  Q2 --> R

  R[生成文件名（基于 caption，限制 max-caption-words）<br/>allow-continued：追加 _continued_p{page}] --> S[渲染并保存 PNG（覆盖同名）<br/>记录 AttachmentRecord]
  S --> M

  M --> T[Figure records 输出（排序）]

  T --> U{include_tables?（默认是）}
  U -- 否 --> Z0
  U -- 是 --> V[提取 Tables：extract_tables]

  V --> W{smart-caption-detection?}
  W -- 是 --> W1[build_caption_index 预扫全篇（Table）<br/>- 非 continued：跨页选“最优”并缓存到所属页<br/>- continued：按页独立选择]
  W -- 否 --> W2[逐页简单正则匹配 Table 行]
  W1 --> X
  W2 --> X

  X{GLOBAL_ANCHOR_TABLE=auto?} -- 是 --> X1[表格全局预扫：决定全篇 Table 统一方向（可选）]
  X -- 否 --> X2[不启用表格全局统一方向]
  X1 --> Y
  X2 --> Y

  Y[逐页处理（Table）] --> Y1[选择窗口（Anchor v1/v2）<br/>支持 t-above/t-below/EXTRACT_FORCE_TABLE_ABOVE]
  Y1 --> Y2[表格精裁（A/B/D）+ 安全验收（可选）]
  Y2 --> Y3{--debug-visual?}
  Y3 -- 是 --> Y3a[输出表格调试可视化（同 debug/ 目录）]
  Y3 -- 否 --> Y3b[跳过]
  Y3a --> Y4
  Y3b --> Y4
  Y4[生成 Table 文件名 + continued 命名] --> Y5[保存 PNG（覆盖同名）+ 记录 AttachmentRecord]
  Y5 --> Y
  Y --> Z0[汇总 all_records（Figure+Table）]

  Z0 --> Z1[统一排序：page → (figure先) → id]
  Z1 --> Z2[write_index_json 写 images/index.json<br/>file 字段为相对路径]
  Z2 --> Z3{--prune-images?}
  Z3 -- 是 --> Z3a[清理 out-dir 中未被 index.json 引用的旧 Figure_/Table_ PNG]
  Z3 -- 否 --> Z3b[不清理旧图]
  Z3a --> Z4
  Z3b --> Z4
  Z4 --> Z5{--manifest?}
  Z5 -- 是 --> Z5a[write_manifest 写 CSV（file 字段为相对路径）]
  Z5 -- 否 --> Z5b[跳过 manifest]
  Z5a --> Z6
  Z5b --> Z6
  Z6 --> Z7[QC 汇总：输出提取数量 + 文本粗对齐统计]
  Z7 --> Z8([结束：得到 text/ + images/*.png + images/index.json])

  %% Optional post-step: rename and sync index
  Z8 -. 可选：两阶段命名 .-> R1[AI/人工重命名 images/Figure_*/Table_* PNG（保留前缀）]
  R1 -. 同步索引 .-> R2[运行 python scripts/sync_index_after_rename.py &lt;PDF_DIR&gt;<br/>更新 images/index.json 的 file 字段]
```

