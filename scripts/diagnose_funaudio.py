#!/usr/bin/env python3
"""
诊断 FunAudio-ASR.pdf 的提取问题
"""

import fitz
import sys

def analyze_figure_context(pdf_path: str, page_num: int, fig_no: int, caption_y: float):
    """分析图注周围的上下文"""
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    page_rect = page.rect
    dict_data = page.get_text('dict')
    
    print(f"\n{'='*70}")
    print(f"Figure {fig_no} (Page {page_num+1}) 诊断")
    print(f"{'='*70}")
    print(f"页面高度: {page_rect.height:.1f}pt")
    print(f"Caption位置: y={caption_y:.1f}pt")
    
    # 收集图注上方的文本
    lines_above = []
    for blk in dict_data.get('blocks', []):
        if blk.get('type', 0) != 0:
            continue
        for ln in blk.get('lines', []):
            bbox = ln.get('bbox', [0,0,0,0])
            y_line = bbox[1]
            if y_line < caption_y:
                text = ''.join(sp.get('text', '') for sp in ln.get('spans', []))
                spans = ln.get('spans', [])
                font_sizes = [sp.get('size', 10) for sp in spans if 'size' in sp]
                avg_size = sum(font_sizes)/len(font_sizes) if font_sizes else 10
                width = bbox[2] - bbox[0]
                page_width = page_rect.width - 40
                width_ratio = width / page_width
                dist_to_caption = caption_y - bbox[3]  # 距离caption顶部
                lines_above.append({
                    'y': y_line,
                    'y_bottom': bbox[3],
                    'width_ratio': width_ratio,
                    'font': avg_size,
                    'dist': dist_to_caption,
                    'text': text
                })
    
    lines_above.sort(key=lambda x: x['y'], reverse=True)
    
    # 分析：哪些行应该被trimmed？
    print(f"\n【上方最近的20行文本分析】")
    print(f"{'距离(pt)':<10} {'宽度比':<8} {'字号':<6} {'判断':<15} 文本")
    print("-" * 70)
    
    for i, ln in enumerate(lines_above[:20]):
        dist = ln['dist']
        w_ratio = ln['width_ratio']
        font = ln['font']
        
        # 应用当前的text-trim条件
        # width_ratio >= 0.5, font 7-16pt, adjacent_th=24pt
        should_trim = False
        reason = ""
        
        if dist <= 24:
            if w_ratio >= 0.5 and 7 <= font <= 16:
                should_trim = True
                reason = "✓ TRIM (邻接)"
            else:
                reason = f"✗ 未trim(w={w_ratio:.2f})"
        else:
            reason = f"✗ 距离太远({dist:.0f}pt)"
        
        print(f"{dist:>8.1f}pt  {w_ratio:>6.2f}  {font:>4.1f}pt  {reason:<15} {ln['text'][:50]}")
    
    # 收集绘图对象
    drawings = []
    try:
        for dr in page.get_drawings():
            r = dr.get('rect')
            if r:
                drawings.append(fitz.Rect(*r))
    except:
        pass
    
    print(f"\n【绘图对象统计】")
    print(f"  总数: {len(drawings)}")
    if drawings:
        # 找到最接近caption的绘图对象
        closest_draw = None
        min_dist = float('inf')
        for r in drawings:
            if r.y1 <= caption_y:  # 在caption上方
                d = caption_y - r.y1
                if d < min_dist:
                    min_dist = d
                    closest_draw = r
        if closest_draw:
            print(f"  最接近caption的对象: y={closest_draw.y0:.1f}-{closest_draw.y1:.1f}, 距离={min_dist:.1f}pt")
    
    doc.close()


def analyze_table8(pdf_path: str):
    """分析 Table 8 的Caption检测问题"""
    doc = fitz.open(pdf_path)
    page = doc[11]  # Page 12
    dict_data = page.get_text('dict')
    
    print(f"\n{'='*70}")
    print(f"Table 8 (Page 12) Caption检测诊断")
    print(f"{'='*70}")
    
    import re
    pattern = re.compile(r'Table\s+8', re.IGNORECASE)
    
    candidates = []
    for blk_idx, blk in enumerate(dict_data.get('blocks', [])):
        if blk.get('type', 0) != 0:
            continue
        for ln_idx, ln in enumerate(blk.get('lines', [])):
            text = ''.join(sp.get('text', '') for sp in ln.get('spans', []))
            if pattern.search(text):
                bbox = ln.get('bbox', [0,0,0,0])
                spans = ln.get('spans', [])
                
                # 模拟评分
                # 1. 格式分
                has_colon = ':' in text[:40] or '：' in text[:40]
                format_score = 5 if has_colon else 0
                
                # 2. 上下文分
                context_score = 0
                if 'shows that' in text.lower() or 'comparison' in text.lower():
                    if 'shows that' in text.lower():
                        context_score = -15  # 引用关键词，扣分
                    else:
                        context_score = 10  # Caption关键词
                
                # 3. 段落长度
                para_len = sum(len(''.join(sp.get('text', '') for sp in ln2.get('spans', []))) 
                              for ln2 in blk.get('lines', []))
                structure_score = 8 if para_len < 150 else (-8 if para_len > 600 else 0)
                
                candidates.append({
                    'y': bbox[1],
                    'text': text,
                    'has_colon': has_colon,
                    'format_score': format_score,
                    'context_score': context_score,
                    'structure_score': structure_score,
                    'para_len': para_len,
                    'total': format_score + context_score + structure_score + 30  # 假设position=30
                })
    
    print(f"\n找到 {len(candidates)} 个候选:")
    for i, cand in enumerate(candidates, 1):
        print(f"\n候选 {i}: y={cand['y']:.1f}pt")
        print(f"  文本: {cand['text'][:80]}...")
        print(f"  格式分: {cand['format_score']} (冒号={'✓' if cand['has_colon'] else '✗'})")
        print(f"  上下文分: {cand['context_score']}")
        print(f"  结构分: {cand['structure_score']} (段落长度={cand['para_len']})")
        print(f"  预估总分: {cand['total']}")
    
    doc.close()


def main():
    pdf_path = 'tests/FunAudio-ASR.pdf'
    
    # 分析有问题的Figures
    print("【问题分析：上方文字过多的图表】")
    analyze_figure_context(pdf_path, 0, 1, 627.9)    # Figure 1
    analyze_figure_context(pdf_path, 3, 3, 257.8)    # Figure 3
    analyze_figure_context(pdf_path, 5, 4, 381.4)    # Figure 4
    
    # 分析Table 8的Caption检测问题
    print("\n\n【问题分析：Caption检测错误】")
    analyze_table8(pdf_path)
    
    # 总结
    print(f"\n\n{'='*70}")
    print("【问题总结与优化建议】")
    print(f"{'='*70}")
    print("""
问题1：上方文字过多（Figure 1, 3, 4; Table 1, 3, 7）
  原因：
    - 这些正文距离caption > 24pt，超出了adjacent_th阈值
    - text-trim只移除"邻接"于caption的文本（<24pt）
    - 但实际上，abstract/section正文在更上方（200-400pt）
  
  当前策略的局限：
    ✗ adjacent_th=24pt 假设文本必须紧邻caption才是"多余"
    ✗ 但很多论文在caption上方有大段正文（如abstract）
    ✗ 这些正文不应该被包含在图表区域内

问题2：Table 8 Caption检测错误
  原因：
    - Page 12有两个"Table 8"候选
    - 候选1 (y=93.1): "Table 8 shows that..." - 正文引用
    - 候选2 (y=292.7): "Table 8: Comparison..." - 真实图注
    
  预期评分差异：
    - 候选1: 无冒号(-5), "shows that"是引用(-15) → 低分
    - 候选2: 有冒号(+5), "comparison"是caption(+10) → 高分
    
  当前问题：
    ✗ 表格的智能Caption检测尚未启用（"using original logic"）
    ✗ 原始逻辑按顺序取第一个匹配 → 选中了候选1（引用）
""")


if __name__ == '__main__':
    main()

