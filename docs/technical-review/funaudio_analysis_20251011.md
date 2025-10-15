# FunAudio-ASR.pdf 提取效果分析与优化建议

> **分析时间**：2025-10-11  
> **对比基准**：DeepSeek_V3_2.pdf（提取效果excellent）

## 📊 一、提取结果概览

### 1.1 基本统计
- **PDF信息**：15页
- **提取成果**：4图 + 8表 = 12个元素
- **成功率**：部分成功（位置基本正确，但上方截取过多）

### 1.2 问题分类

| 类别 | 元素 | 问题描述 | 严重程度 |
|------|------|---------|---------|
| **上方文字过多** | Figure 1, 3, 4 | 图片正确，但上方包含200-400pt的abstract/正文 | ⚠️ 中等 |
| **上方文字过多** | Table 1, 3, 7 | 表格正确，但上方包含大量正文 | ⚠️ 中等 |
| **Caption错误** | Table 8 | 完全错误，截取到引用而非真实表格 | ❌ 严重 |
| **正常** | Figure 2, Table 2, 4, 5, 6 | 提取正确，可接受 | ✅ 良好 |

---

## 🔍 二、问题根因分析

### 问题1：上方文字过多（Figure 1, 3, 4; Table 1, 3, 7）

#### 2.1.1 Figure 1 详细分析

**页面布局**：
```
Page 1 (高度792pt):
  y=93-446:  标题 + 作者 + Abstract（大段正文，~350pt高度）
  y=627.9:   Figure 1 caption
  y=....:    Figure 1 图像（未显示，但在caption上方）
```

**当前提取窗口**（假设clip_height=520pt）：
- 从 caption上方520pt开始 → y = 627.9 - 520 = 107.9
- **实际截取区域**：y=107.9 到 y=627.9（包含了Abstract的大部分正文！）

**text-trim为什么没生效**：
```python
# 当前逻辑（extract_pdf_assets.py Line 1719-1745）
if text_trim:
    # 只检查caption"邻近"区域（adjacent_th=24pt）
    if para_ratio >= text_trim_min_para_ratio:  # 段落占比>18%
        clip = _trim_clip_head_by_text(
            ...,
            adjacent_th=24.0  # ← 关键参数！
        )
```

**问题剖析**：
1. `adjacent_th=24pt` 意味着：只有距离caption **不到24pt**的文本才会被trim
2. 但Abstract正文距离caption **200-400pt**，远超24pt阈值
3. 结果：这些正文完全没有被trim考虑

#### 2.1.2 Figure 3 & 4 类似问题

- **Figure 3**：caption上方71-174pt处有section标题和正文
- **Figure 4**：caption上方182-297pt处有section正文
- **共同特征**：上方正文都超出了24pt的"邻接"范围


### 问题2：Table 8 Caption检测错误

#### 2.2.1 Page 12 布局

```
Page 12:
  y=93.1:   "Table 8 shows that RL plays a crucial role..." ← 正文引用
  y=292.7:  "Table 8: Comparison between the models w/ or w/o..." ← 真实图注
  y=...:    Table 8 表格
```

#### 2.2.2 智能Caption评分模拟

| 候选 | 位置 | 格式分 | 上下文分 | 结构分 | 预估总分 | 实际选择 |
|------|------|--------|---------|--------|---------|---------|
| 候选1（引用） | y=93.1 | 0（无冒号） | -15（"shows that"） | 0（长段落546字） | **15** | ❌ 错选 |
| 候选2（真实） | y=292.7 | 5（有冒号） | 10（"comparison"） | 8（短段落72字） | **53** | ✓ 应选 |

#### 2.2.3 为什么选错了？

**关键日志**：
```
[INFO] SMART CAPTION DETECTION (Tables): Currently using original logic
Smart detection for tables will be enhanced in future versions
```

**根本原因**：
- **表格尚未启用智能Caption检测**（Line 2099-2112注释说明）
- 使用原始逻辑：按顺序匹配第一个"Table 8" → 错选了候选1（正文引用）
- 智能检测本应选择候选2（得分53 vs 15）

---

## 💡 三、优化方案

### 方案A：增强text-trim策略（解决问题1）

#### A1. 双阈值text-trim（推荐）

**核心思想**：区分"紧邻文本"和"远距文本"，使用不同的trim策略

```python
def _trim_clip_head_by_text_v2(
    clip: fitz.Rect,
    page_rect: fitz.Rect,
    caption_rect: fitz.Rect,
    direction: str,
    text_lines: List[Tuple[fitz.Rect, float, str]],
    *,
    # 紧邻文本（原有）
    adjacent_th: float = 24.0,
    width_ratio: float = 0.5,
    font_min: float = 7.0,
    font_max: float = 16.0,
    gap: float = 6.0,
    # 远距文本（新增）
    far_text_th: float = 300.0,  # 检测caption上方300pt范围
    far_text_para_min_ratio: float = 0.30,  # 段落文本占比>30%则trim
    far_text_trim_mode: str = "aggressive",  # "aggressive" | "conservative"
) -> fitz.Rect:
    """
    双阈值text-trim：
    1. 紧邻文本（<24pt）：原有逻辑，移除flush到page margin的段落文本
    2. 远距文本（24-300pt）：新增逻辑，检测大段正文并整块移除
    """
    
    # 第一阶段：紧邻文本trim（原有逻辑）
    clip = _original_trim_logic(clip, ..., adjacent_th=adjacent_th)
    
    # 第二阶段：远距文本检测与trim（新增）
    if direction == 'above':
        # 检测caption上方far_text_th范围内的文本
        far_strip = fitz.Rect(clip.x0, caption_rect.y0 - far_text_th, 
                              clip.x1, caption_rect.y0 - adjacent_th)
        
        # 统计该范围内的段落文本占比
        para_lines = 0
        total_lines = 0
        for (lb, fs, text) in text_lines:
            inter = lb & far_strip
            if inter.width > 0 and inter.height > 0:
                total_lines += 1
                if (inter.width / clip.width) >= width_ratio and (font_min <= fs <= font_max):
                    para_lines += 1
        
        para_ratio = para_lines / max(1, total_lines)
        
        # 如果该区域段落占比高，说明是大段正文，应该整块移除
        if para_ratio >= far_text_para_min_ratio:
            if far_text_trim_mode == "aggressive":
                # 激进模式：直接将clip顶部移动到far_strip底部
                clip.y0 = max(clip.y0, far_strip.y1 + gap)
            else:
                # 保守模式：逐行trim（原有逻辑）
                # ... （保持原有逐行trim）
    
    return clip
```

**参数建议**：
- `far_text_th = 300pt`：检测caption上方300pt范围（覆盖abstract等大段正文）
- `far_text_para_min_ratio = 0.30`：如果该范围30%以上是段落文本，则认为是正文区
- `far_text_trim_mode = "aggressive"`：激进模式，整块移除

**预期效果**：
- Figure 1：上方的Abstract正文（350pt高）会被整块移除 ✅
- Figure 3：上方的section正文（71-174pt）会被移除 ✅
- Figure 4：上方的section正文（182-297pt）会被移除 ✅
- **不影响 DeepSeek_V3_2**：因为DeepSeek没有远距正文，双阈值不会误伤 ✅

#### A2. 参数微调方案（简单但效果有限）

**方案**：增大 `adjacent_th` 从 24pt → 50pt

```bash
python3 scripts/extract_pdf_assets.py \
  --pdf tests/FunAudio-ASR.pdf \
  --preset robust \
  --adjacent-th 50  # 增大到50pt
```

**效果预测**：
- ⚠️ 可能改善Figure 4（最近文本距离7-8pt）
- ❌ 无法改善Figure 1（最近文本距离207pt）
- ❌ 可能误伤DeepSeek_V3_2（如果有<50pt的合理文本）

**结论**：不推荐，效果有限且可能误伤


### 方案B：启用表格智能Caption检测（解决问题2）

#### B1. 代码修改（核心）

**位置**：`scripts/extract_pdf_assets.py` Line 2099-2125

**当前代码**：
```python
def extract_tables(...):
    # === Smart Caption Detection for Tables (if enabled) ===
    # Note: Currently tables use the original logic due to complexity
    if smart_caption_detection and debug_captions:
        print("SMART CAPTION DETECTION (Tables): Currently using original logic")
        print("Smart detection for tables will be enhanced in future versions")
    
    # 原始逻辑：按顺序匹配第一个
    for blk in dict_data.get("blocks", []):
        ...
        m = table_line_re.match(t)
        if m:
            # 直接使用第一个匹配，不评分
            ...
```

**修改后代码**（启用智能检测）：
```python
def extract_tables(..., smart_caption_detection: bool = True, ...):
    # === Smart Caption Detection for Tables ===
    caption_index_table: Optional[CaptionIndex] = None
    if smart_caption_detection:
        if debug_captions:
            print("SMART CAPTION DETECTION ENABLED FOR TABLES")
        
        # 复用figure的智能检测逻辑
        caption_index_table = build_caption_index(
            doc, 
            table_pattern=table_line_re,  # 使用表格pattern
            debug=debug_captions
        )
    
    for pno in range(len(doc)):
        page = doc[pno]
        ...
        
        if smart_caption_detection and caption_index_table:
            # 智能选择
            for table_ident in found_table_ids_on_page:
                candidates = caption_index_table.get_candidates('table', table_ident)
                if candidates:
                    best_candidate = select_best_caption(
                        candidates, page, 
                        min_score_threshold=25.0,
                        debug=debug_captions
                    )
                    if best_candidate:
                        # 使用最佳候选
                        ident = best_candidate.number
                        cap_rect = best_candidate.rect
                        caption = best_candidate.text
                        ...
        else:
            # 原始逻辑（fallback）
            ...
```

**预期效果**：
- Table 8 候选1（引用）：得分15 ❌
- Table 8 候选2（真实）：得分53 ✅ **正确选择！**

#### B2. 罗马数字支持增强

**当前表格pattern**支持：
- 普通数字：Table 1, 2, 3, ...
- 罗马数字：Table I, II, III, IV, V
- 附录表：Table A1, B2, ...

**智能检测兼容性**：✅ 完全兼容（pattern匹配后，智能评分选择最佳）


### 方案C：全局锚点优化（可选，效果有限）

**当前全局锚点（表格）**：
```
[INFO] Global table anchor: ABOVE (above=2.37 vs below=1.95)
```

**分析**：
- ABOVE意味着"表格在图注上方"（正确方向）
- 但不能解决"上方包含过多正文"的问题
- **结论**：全局锚点方向正确，无需调整

---

## 🎯 四、推荐实施方案

### 阶段1：快速修复（2小时内可完成）

#### 1.1 启用表格智能Caption检测

**优先级**：🔥 **P0（紧急）**

**修改文件**：`scripts/extract_pdf_assets.py`

**修改范围**：
1. Line 2099-2125：移除"using original logic"，启用智能检测
2. Line 2236-2294：改为调用智能选择逻辑
3. 测试：Table 8应该正确选择候选2

**预期收益**：
- ✅ 修复 Table 8 完全错误的问题
- ✅ 不影响其他表格（因为它们只有1个候选）
- ✅ 不影响 DeepSeek_V3_2（因为它的表格也正常）

**风险**：⚠️ **低**（智能检测已在figure上验证，表格复用相同逻辑）


### 阶段2：双阈值text-trim（建议第二批次实施）

#### 2.1 实现双阈值逻辑

**优先级**：⚠️ **P1（重要但非紧急）**

**修改文件**：`scripts/extract_pdf_assets.py`

**实现步骤**：
1. 创建 `_trim_clip_head_by_text_v2()` 函数（新增400-500行）
2. 在 Line 1719-1745 调用新函数（替换旧逻辑）
3. 添加新参数：
   ```python
   --far-text-th 300             # 远距文本检测范围
   --far-text-para-min-ratio 0.30 # 段落占比阈值
   --far-text-trim-mode aggressive # aggressive|conservative
   ```
4. 在 `--preset robust` 中启用

**测试要求**：
- ✅ FunAudio-ASR: Figure 1, 3, 4 上方正文应被移除
- ✅ DeepSeek_V3_2: 所有图表不受影响（无远距正文）
- ✅ gemini_v2_5, Qwen3-Omni: 检查是否改善

**预期收益**：
- ✅ 修复 FunAudio 6个元素的"上方文字过多"问题
- ✅ 普遍改善其他PDF的类似问题
- ⚠️ 可能需要调优参数（far_text_th, para_min_ratio）

**风险**：⚠️ **中等**（需要充分测试，避免误伤正常图表）


### 阶段3：参数auto-tune（长期优化）

**优先级**：💡 **P2（优化）**

**思路**：根据PDF特征自动调整参数
- 如果检测到大段abstract（page 1前半部分是段落文本）→ 启用far-text-trim
- 如果检测到caption附近无正文 → 使用默认adjacent_th
- 如果检测到多候选caption → 自动启用智能检测


---

## 📋 五、实施时间表

### Week 1（当前）

**Day 1**（今天）：
- [x] 完成 FunAudio-ASR 问题诊断
- [x] 撰写分析报告
- [ ] 实施阶段1：启用表格智能Caption
- [ ] 测试 Table 8 是否修复

**Day 2-3**：
- [ ] 设计并实现双阈值text-trim
- [ ] 单元测试（FunAudio Figure 1）
- [ ] 回归测试（DeepSeek_V3_2）

**Day 4-5**：
- [ ] 完整测试四个PDF
- [ ] 参数调优（far_text_th, para_min_ratio）
- [ ] 更新文档和README

### Week 2

**Day 1-2**：
- [ ] 测试 gemini_v2_5_report (70页，27元素)
- [ ] 测试 Qwen3-Omni (17页，23元素)
- [ ] 收集新问题，迭代优化

**Day 3-5**：
- [ ] 实施阶段3：参数auto-tune
- [ ] 编写使用指南
- [ ] 准备发布


---

## 🧪 六、测试计划

### 测试用例矩阵

| PDF | 元素数 | 当前问题 | 阶段1修复 | 阶段2修复 | 预期最终 |
|-----|--------|---------|-----------|-----------|---------|
| **DeepSeek_V3_2** | 5 | 无 ✅ | 无变化 ✅ | 无变化 ✅ | **excellent** ✅ |
| **FunAudio-ASR** | 12 | Table 8错误 ❌<br>6元素上方文字多 ⚠️ | Table 8修复 ✅ | 全部修复 ✅ | **excellent** ✅ |
| **gemini_v2_5** | 27 | 未知 ❓ | TBD | TBD | **good** ⚠️ |
| **Qwen3-Omni** | 23 | 未知 ❓ | TBD | TBD | **good** ⚠️ |

### 关键测试点

**阶段1测试**（表格智能Caption）：
1. ✅ Table 8 正确选择候选2（y=292.7）
2. ✅ Table 1-7 不受影响（只有1个候选）
3. ✅ DeepSeek Table 1 不受影响

**阶段2测试**（双阈值text-trim）：
1. ✅ Figure 1 上方Abstract被移除
2. ✅ Figure 3 上方section正文被移除
3. ✅ Figure 4 上方section正文被移除
4. ✅ DeepSeek Figure 1-4 不受影响
5. ⚠️ 边界case：如果caption正上方有小标题（<50pt），是否误伤？

---

## 📖 七、关键代码片段

### 7.1 当前问题代码（表格Caption）

```python
# scripts/extract_pdf_assets.py Line 2255-2294
for blk in dict_data.get("blocks", []):
    if blk.get("type", 0) != 0:
        continue
    lines = blk.get("lines", [])
    i = 0
    while i < len(lines):
        ln = lines[i]
        text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
        t = text.strip()
        m = table_line_re.match(t)
        if not m:
            i += 1
            continue
        # 提取表号
        ident = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        # ⚠️ 直接使用第一个匹配，没有评分
        # → Table 8 错选候选1（引用）
        cap_rect = fitz.Rect(*(ln.get("bbox", [0,0,0,0])))
        ...
```

### 7.2 修复后代码（启用智能检测）

```python
# 在 for pno in range(len(doc)) 之前
smart_caption_cache_table: Dict[str, Tuple[fitz.Rect, str, int]] = {}

if smart_caption_detection and caption_index_table:
    # 预先为所有表格选择最佳caption
    for pno in range(len(doc)):
        page = doc[pno]
        # 找到本页所有table编号
        page_table_ids = set()
        ...
        for table_id in page_table_ids:
            candidates = caption_index_table.get_candidates('table', str(table_id))
            if candidates:
                best = select_best_caption(candidates, page, min_score_threshold=25.0, debug=debug_captions)
                if best:
                    smart_caption_cache_table[table_id] = (best.rect, best.text, best.page)

# 在提取循环中
for pno in range(len(doc)):
    ...
    if smart_caption_detection and table_id in smart_caption_cache_table:
        # 使用智能选择的结果
        cap_rect, caption, cached_page = smart_caption_cache_table[table_id]
        if cached_page == pno:
            # 提取
            ...
    else:
        # fallback到原始逻辑
        ...
```

### 7.3 双阈值text-trim伪代码

```python
def _trim_clip_head_by_text_v2(...):
    # Phase 1: 紧邻文本trim（原有逻辑）
    new_boundary = clip边界
    for (lb, fs, text) in text_lines:
        if 距离caption < adjacent_th (24pt):
            if 是段落文本 (width>50%, font 7-16pt):
                new_boundary = 移除该文本行
    clip = 应用new_boundary
    
    # Phase 2: 远距文本检测与trim（新增）
    far_strip = Rect(clip.x0, caption.y0 - far_text_th, clip.x1, caption.y0 - adjacent_th)
    # 统计far_strip内的段落占比
    para_ratio = 计算段落文本占比(far_strip)
    
    if para_ratio > far_text_para_min_ratio (30%):
        # 这是大段正文区域，整块移除
        clip.y0 = far_strip.y1 + gap
    
    return clip
```

---

## 🎓 八、经验总结

### 8.1 成功模式（DeepSeek_V3_2）

**为什么DeepSeek效果好？**
1. ✅ PDF简洁（5元素），caption独立成行
2. ✅ caption上方没有大段正文（abstract在前面页）
3. ✅ 图表与caption距离适中（7-10pt）
4. ✅ 矢量图形，边界清晰

**关键要素**：
- caption附近（±50pt）无干扰文本
- 图表内容密集（墨迹密度高）
- 单栏布局，无跨栏图表

### 8.2 挑战模式（FunAudio-ASR）

**为什么FunAudio有问题？**
1. ❌ PDF复杂（12元素），页面内容密集
2. ❌ 多个caption上方有大段正文（200-400pt）
3. ❌ Table 8 同页出现2次（引用 + 真实）
4. ⚠️ 部分caption后有子图标注（如Figure 4的(a)(b)）

**挑战要素**：
- caption上方有远距正文（超出adjacent_th）
- 多候选caption需要智能选择
- 页面内容密集，容易误选

### 8.3 设计原则

**原则1：不劣化已成功的PDF**
- DeepSeek_V3_2 当前excellent → 任何修改都不能让它变差
- 方法：双阈值、智能检测都是"增量"逻辑，有问题才触发

**原则2：渐进式优化**
- 先修复严重问题（Table 8 Caption错误）
- 再改善体验问题（上方文字过多）
- 最后优化用户体验（参数auto-tune）

**原则3：充分测试**
- 每个阶段都要跑完4个PDF回归测试
- 边界case要专门测试（如小标题、多子图）
- 参数调优要基于多PDF数据

---

## 📚 九、参考资料

### 相关文件
- `scripts/extract_pdf_assets.py` - 核心提取脚本（2894行）
- `scripts/analyze_extraction.py` - 提取结果分析工具
- `scripts/diagnose_funaudio.py` - FunAudio专用诊断工具
- `docs/deepseek_success_summary.md` - DeepSeek成功分析
- `docs/deepseek_v3_2_extraction_analysis.md` - DeepSeek详细分析
- `AGENTS.md` - 仓库指南

### 关键参数速查

| 参数 | 默认值 | FunAudio建议 | DeepSeek适用 |
|------|--------|-------------|-------------|
| `--adjacent-th` | 24pt | 24pt（保持） | 24pt ✅ |
| `--far-text-th` | N/A | **300pt**（新增） | 0（不启用）✅ |
| `--far-text-para-min-ratio` | N/A | **0.30**（新增） | N/A ✅ |
| `--smart-caption-detection` | ✓ figure | **✓ table**（启用） | ✓ figure ✅ |
| `--text-trim` | ✓ | ✓ | ✓ ✅ |
| `--autocrop` | ✓ | ✓ | ✓ ✅ |

---

**分析完成**：2025-10-11  
**下一步**：实施阶段1（启用表格智能Caption）  
**预计时间**：2小时内可验证修复效果

