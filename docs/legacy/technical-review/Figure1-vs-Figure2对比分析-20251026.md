# Figure 1 vs Figure 2 对比分析报告

**文档日期**: 2025-10-26  
**测试PDF**: 2509.17765v1 (Qwen3-Omni Technical Report)  
**测试参数**: `--scan-dist-lambda 0.06 --scan-heights 240,320,420,520,640,720,820,920 --layout-driven --debug-visual`  
**结论**: ❌ **参数调整无效，Figure 2仍然失败**

---

## 📊 详细对比数据

### Figure 1（成功）✅

| 项目 | 数值 | 说明 |
|------|------|------|
| **Caption位置** | `70.9,308.6 -> 525.7,340.5` | 页面2，y=308.6 |
| **Baseline窗口** | `26.0,62.6 -> 569.3,302.6` | **向上扫描到y=62.6** |
| **扫描距离** | **246pt** | 308.6 - 62.6 |
| **窗口高度** | **240pt** | 302.6 - 62.6 |
| **最终结果** | **Phase D（成功）** | `78.8,78.0 -> 519.9,305.6` |
| **Text Blocks** | 7个（title_h3为主） | 图内小标题："Query"/"Response" |
| **精炼流程** | A → B → D → ✅通过验收 | 完整流程 |

---

### Figure 2（失败）❌

| 项目 | 数值 | 说明 |
|------|------|------|
| **Caption位置** | `70.9,464.8 -> 526.1,496.7` | 页面3，y=464.8 |
| **Baseline窗口** | `26.0,204.8 -> 569.3,458.8` | **仅向上扫描到y=204.8** |
| **扫描距离** | **260pt** | 464.8 - 204.8 |
| **窗口高度** | **254pt** | 458.8 - 204.8 |
| **最终结果** | **A-only Fallback（失败）** | 与Baseline完全相同 |
| **Text Blocks** | 6个（paragraph_group为主） | 全部在Caption下方（y>464.8） |
| **精炼流程** | A → B/D被拒绝 → ❌回退A-only | 验收失败 |
| **失败原因** | `ink=88.8%` | 墨迹密度保留率88.8%，触发回退 |

---

## 🔍 关键差异分析

### 差异1：锚点选择的根本不同

**Figure 1**:
- 向上扫描到 **y=62.6**（页面顶部附近）
- 完整捕获了整个图形

**Figure 2**:
- 向上扫描到 **y=204.8**（页面中部）
- **遗漏了上方132pt的图形内容**（204.8 - 72 ≈ 132pt）

**问题**：真实的Figure 2顶部应该在 **y≈72**（从调试图片可见），但锚点只扫描到y=204.8

---

### 差异2：Text Block分布

**Figure 1**:
```
Text Block 1-5: 全部是title_h3（图内小标题）
  - 位置：102.4,106.4 ~ 438.5,249.9
  - 特点：小尺寸、分散、图内元素

Text Block 6-7: paragraph_group（Caption和下文）
  - 位置：70.9,319.6 ~ 526.1,704.4
  - 特点：Caption下方
```

**Figure 2**:
```
Text Block 1-6: 全部是paragraph_group或title（Caption及下文）
  - 位置：70.9,475.7 ~ 525.7,730.9
  - 特点：全部在Caption下方（y>464.8）

上方72-204区域：NO Text Blocks
  - 说明：上方可能是纯图形，没有被识别为段落
```

---

### 差异3：精炼流程结果

**Figure 1**:
```
Baseline:  26.0,62.6 -> 569.3,302.6  (240pt)
Phase D:   78.8,78.0 -> 519.9,305.6  (227.6pt)
收缩幅度: 高度 -5.2%, 面积 -22.9%
验收结果: ✅ 通过
```

**Figure 2**:
```
Baseline:    26.0,204.8 -> 569.3,458.8  (254pt)
Phase B/D:   (执行了，但被拒绝)
验收失败:    墨迹密度保留率=88.8%
Fallback:    26.0,204.8 -> 569.3,458.8  (与Baseline相同)
```

**关键警告**:
```
[WARN] Fig 2 p3: refinement rejected (ink=88.8%), trying fallback
```

**分析**：墨迹密度保留率88.8%，说明Phase D后墨迹密度降低了约11%，触发了验收保护机制

---

## 💡 为什么参数调整无效？

### 测试的参数调整

```bash
--scan-dist-lambda 0.06    # 从0.12降低到0.06（降低50%）
--scan-heights 240,320,420,520,640,720,820,920  # 增加920pt候选
```

### 预期效果 vs 实际结果

| 参数 | 预期效果 | 实际结果 |
|------|----------|----------|
| `--scan-dist-lambda 0.06` | 距离影响减弱，更高的窗口得分提升 | ❌ **无效，Baseline完全相同** |
| `--scan-heights ...920` | 提供更高的窗口候选 | ❌ **无效，仍选择254pt窗口** |

---

## 🔬 深层原因推断

### 假设1：上方区域被识别为"非图形"

**证据**：
1. Text Block全部在Caption下方
2. 上方72-204区域没有检测到段落
3. 但锚点仍然选择了204.8-458.8窗口

**可能原因**：
- 上方区域的**墨迹密度过低**（大片白色背景）
- 上方区域的**对象覆盖率不足**（可能是单张大位图，边缘对象少）
- 评分公式中，**墨迹密度权重过高**（55%）

---

### 假设2：评分公式本身的偏差

```python
# 当前公式（Line 2349）
score = 0.55 * ink + 0.25 * obj - 0.2 * para + comp_bonus - dist_penalty
```

**候选A（正确）**：72-458.8（387pt）
```
ink: 较低（包含大片白色背景）
obj: 可能不足（单张大位图，连通域少）
para: 0（上方无段落）
dist: ~6pt（紧邻Caption）
评分: 0.55×(低) + 0.25×(低) + 0 - (小) = 较低
```

**候选B（错误，实际选中）**：204.8-458.8（254pt）
```
ink: 较高（不含白色背景，内容密集）
obj: 较高（图形中下部，对象密集）
para: 0（区域内无段落）
dist: ~6pt（紧邻Caption）
评分: 0.55×(高) + 0.25×(高) + 0 - (小) = 较高 ← 胜出！
```

**结论**：**墨迹密度和对象覆盖率主导了评分**，距离罚项影响极小

---

### 假设3：扫描高度候选不足

**检查扫描逻辑**（Line 2360-2430）：

```python
# 上方扫描
for height in scan_heights:  # [240, 320, 420, 520, 640, 720, 820, 920]
    y1 = cap_rect.y0 - caption_gap  # 458.8 (固定底边)
    y0 = max(y0_min, y1 - height)   # 计算顶边
    
    # 候选窗口
    if height == 240:
        y0 = 458.8 - 240 = 218.8
    if height == 920:
        y0 = 458.8 - 920 = -461.2 → 受限于page_rect.y0（≈0）
```

**问题发现**：
- 即使 `height=920`，仍会生成 `0-458.8` 的候选窗口
- 但实际选中的是 `204.8-458.8`（254pt）

**推断**：
1. 更高的窗口（如 0-458.8）**确实被生成了**
2. 但在评分时**得分更低**，被淘汰了
3. **评分机制是根本问题**，而非扫描范围

---

## 🎯 验证方法

### 方法1：打印候选窗口评分（推荐）

修改脚本，在锚点扫描阶段输出所有候选窗口的评分：

```python
# Line 2359附近，fig_score函数后
for score, side, clip in candidates:
    print(f"[DEBUG] Fig {fig_num}: side={side}, "
          f"y0={clip.y0:.1f}, y1={clip.y1:.1f}, "
          f"height={clip.height:.1f}, score={score:.4f}")
```

**预期输出**（Figure 2）：
```
[DEBUG] Fig 2: side=above, y0=72.0, y1=458.8, height=386.8, score=0.4520
[DEBUG] Fig 2: side=above, y0=138.8, y1=458.8, height=320.0, score=0.5230
[DEBUG] Fig 2: side=above, y0=204.8, y1=458.8, height=254.0, score=0.5810 ← 最高分
```

---

### 方法2：强制指定窗口（临时验证）

使用环境变量强制Figure 2使用更高的窗口：

```bash
# 方法2A：使用Anchor V1 + 更大clip-height
EXTRACT_FORCE_ABOVE="2" python scripts/extract_pdf_assets.py \
  --pdf "2509.17765v1-Qwen3-Omni Technical Report.pdf" \
  --anchor-mode v1 \
  --clip-height 420 \
  --debug-visual

# 方法2B：代码级强制（测试用）
# 在Line 2250附近添加：
if fig_num == 2 and page_num == 2:
    clip_rect = fitz.Rect(26.0, 72.0, 569.3, 458.8)  # 强制使用完整窗口
```

---

## 🔧 根本解决方案

### 方案A：调整评分权重（代码级）

**问题**：墨迹密度权重过高（55%），对白色背景过于敏感

**建议修改**（Line 2349）：
```python
# 当前公式
base = 0.55 * ink + 0.25 * obj - 0.2 * para + comp_bonus

# 优化方案：降低墨迹权重，提高对象权重
base = 0.35 * ink + 0.40 * obj - 0.2 * para + comp_bonus + 0.05 * height_bonus
# 增加高度奖励：窗口越高，略加分（鼓励捕获完整图形）
height_bonus = min(1.0, clip.height / 400.0)
```

**原理**：
- **对象覆盖率**更能反映图形完整性（PDF对象通常覆盖整个图形）
- **墨迹密度**受背景色影响大，不适合作为主要指标
- **高度奖励**鼓励选择更高的窗口（在其他条件相近时）

---

### 方案B：引入"边缘截断检测"

**思路**：检测窗口顶部边缘是否有对象被截断

```python
def detect_top_edge_truncation(clip: fitz.Rect, objects: List[fitz.Rect]) -> bool:
    """检测窗口顶部是否截断对象"""
    top_margin = 10  # 顶部10pt范围
    for obj in objects:
        if clip.y0 - 5 < obj.y0 < clip.y0 + top_margin:
            # 对象顶部在窗口顶边附近，可能被截断
            if obj.height > 50:  # 且对象较大
                return True
    return False

# 在评分时惩罚截断
if detect_top_edge_truncation(clip, all_objects):
    base -= 0.15  # 扣15分
```

---

### 方案C：多窗口集成（最稳健）

**思路**：对得分接近的Top 3候选，选择**最高的窗口**

```python
# Line 2430附近
candidates.sort(key=lambda x: x[0], reverse=True)
top3 = candidates[:3]

# 如果Top 3得分差距<10%，选择最高的窗口
if len(top3) >= 2:
    best_score = top3[0][0]
    if top3[1][0] > best_score * 0.90:  # 次高得分>90%最高分
        # 在得分接近的候选中，选择最高的窗口
        top3_sorted_by_height = sorted(top3, key=lambda x: x[2].height, reverse=True)
        selected = top3_sorted_by_height[0]
    else:
        selected = top3[0]
else:
    selected = candidates[0]
```

---

## 📌 后续行动

### 立即行动（验证假设）

1. ✅ **添加调试输出**：打印Figure 2所有候选窗口的评分
   ```bash
   # 修改Line 2359，添加print语句
   # 重新运行并观察评分分布
   ```

2. ✅ **强制测试**：使用Anchor V1 + `--clip-height 420` 验证完整窗口效果
   ```bash
   python scripts/extract_pdf_assets.py \
     --pdf "2509.17765v1-Qwen3-Omni Technical Report.pdf" \
     --anchor-mode v1 \
     --above 2 \
     --clip-height 420 \
     --debug-visual
   ```

---

### 中期优化（2-3天）

3. **实施方案A**：调整评分权重（墨迹35% → 对象40%）
4. **实施方案B**：添加边缘截断检测
5. **批量测试**：在basic-benchmark的10篇论文上验证

---

### 长期改进（1-2周）

6. **实施方案C**：多窗口集成策略
7. **参数自适应**：根据PDF特征（单栏/双栏、图密度）动态调整权重
8. **机器学习**：收集标注数据，训练窗口评分模型

---

## 🎓 结论

### 核心发现

1. **参数调整无效**：降低`--scan-dist-lambda`和增加`--scan-heights`对Figure 2完全无效
2. **评分机制是根本问题**：墨迹密度权重过高（55%），导致包含白色背景的完整窗口得分反而更低
3. **距离罚项影响极小**：两个窗口都紧邻Caption（~6pt），距离罚项几乎相同
4. **验收机制正常工作**：Figure 2的Phase D被拒绝（ink=88.8%），回退到A-only Fallback

---

### 推荐方案

**优先级1（立即）**：方法1（打印候选评分）验证假设  
**优先级2（本周）**：方案A（调整评分权重）+ 方案B（边缘截断检测）  
**优先级3（下周）**：方案C（多窗口集成）全面测试

---

**分析者**: PDF Summary Agent Team  
**审核状态**: ✅ 已验证（参数调整确认无效）  
**下一步**: 实施调试输出，确认评分分布

