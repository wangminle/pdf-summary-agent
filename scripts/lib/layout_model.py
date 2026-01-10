#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 09: Layout Model（版式模型）

从 extract_pdf_assets.py 抽离的版式驱动提取相关代码。

包含：
- classify_text_types: 文本类型分类
- detect_columns: 检测单栏/双栏布局
- build_text_blocks: 构建文本区块
- detect_vacant_regions: 检测留白区域
- adjust_clip_with_layout: 使用版式信息优化裁剪
- should_enable_layout_driven: 判断是否需要启用版式驱动
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

# 尝试导入 fitz
try:
    import fitz
except ImportError:
    fitz = None  # type: ignore

# 避免循环导入
if TYPE_CHECKING:
    from .models import DocumentLayoutModel, EnhancedTextUnit, TextBlock

# 模块日志器
logger = logging.getLogger(__name__)


# ============================================================================
# 文本类型分类
# ============================================================================

def classify_text_types(
    all_units: Dict[int, List["EnhancedTextUnit"]],
    typical_font_size: float,
    typical_font_name: str,
    page_width: float,
    debug: bool = False
) -> Dict[int, List["EnhancedTextUnit"]]:
    """
    基于规则的文本类型分类器。
    
    分类规则：
    1. Caption（图注/表注）: 匹配正则 + 字号略小于正文
    2. Title（标题）: 加粗 + 字号大
    3. List（列表）: bullet点或编号
    4. In-Figure Text（图表内文字）: 字体不同/字号小/短文本
    5. Paragraph（段落）: 默认类型
    
    Args:
        all_units: 按页组织的文本单元字典
        typical_font_size: 典型字号
        typical_font_name: 典型字体名
        page_width: 页面宽度
        debug: 调试模式
    
    Returns:
        分类后的文本单元字典
    """
    if debug:
        print("\n[DEBUG] Text Type Classification")
        print("=" * 70)
        print(f"Typical font size: {typical_font_size:.1f}pt")
        print(f"Typical font name: {typical_font_name}")
        print(f"Page width: {page_width:.1f}pt")
    
    caption_pattern = re.compile(r'^\s*(Figure|Table|Fig\.|图|表)\s+\S', re.I)
    
    for pno, units in all_units.items():
        if debug and pno == 0:
            print(f"\n[Page {pno+1}] Classifying {len(units)} text units...")
        
        for unit in units:
            text_stripped = unit.text.strip()
            
            # 规则1: Caption检测
            if caption_pattern.match(text_stripped):
                if 'fig' in text_stripped.lower() or '图' in text_stripped:
                    unit.text_type = 'caption_figure'
                else:
                    unit.text_type = 'caption_table'
                unit.confidence = 0.95
                if debug and pno == 0:
                    print(f"  Caption: {text_stripped[:50]}...")
                continue
            
            # 规则2: Title检测
            if unit.font_weight == 'bold':
                ratio = unit.font_size / typical_font_size
                if ratio > 1.3:
                    unit.text_type = 'title_h1'
                    unit.confidence = 0.90
                elif ratio > 1.15:
                    unit.text_type = 'title_h2'
                    unit.confidence = 0.85
                elif ratio > 1.05:
                    unit.text_type = 'title_h3'
                    unit.confidence = 0.80
                else:
                    text_len = len(text_stripped)
                    is_numbered_title = bool(re.match(r'^\d+(\.\d+)*\s+[A-Z]', text_stripped))
                    
                    if is_numbered_title or (text_len < 60 and text_len > 5):
                        unit.text_type = 'title_h3'
                        unit.confidence = 0.75
                    else:
                        unit.text_type = 'paragraph'
                        unit.confidence = 0.70
                if debug and pno == 0 and unit.text_type.startswith('title'):
                    print(f"  {unit.text_type.upper()}: {text_stripped[:40]}...")
                continue
            
            # 规则3: List检测
            if re.match(r'^\s*[•\-\*]\s+', text_stripped) or re.match(r'^\s*\d+[\.\)]\s+', text_stripped):
                unit.text_type = 'list'
                unit.confidence = 0.85
                continue
            
            # 规则4: Equation检测
            special_chars = set('∫∑∏√±≈≠≤≥∞αβγδθλμσΔΩ')
            if len(set(text_stripped) & special_chars) > 0 and unit.bbox.width < 0.6 * page_width:
                unit.text_type = 'equation'
                unit.confidence = 0.75
                continue
            
            # 规则5: In-Figure Text检测
            is_different_font = (typical_font_name.lower() not in unit.font_name.lower() and 
                                unit.font_name.lower() not in typical_font_name.lower())
            is_small_font = unit.font_size < 0.85 * typical_font_size
            is_short_text = len(text_stripped) < 30
            is_narrow = unit.bbox.width < 0.4 * page_width
            
            infig_score = 0
            if is_different_font:
                infig_score += 2
            if is_small_font:
                infig_score += 1
            if is_short_text and is_narrow:
                infig_score += 1
            
            if infig_score >= 2:
                unit.text_type = 'in_figure_text'
                unit.confidence = 0.70
                if debug and pno == 0:
                    print(f"  In-Figure Text: {text_stripped[:30]}... (font={unit.font_name}, size={unit.font_size:.1f})")
                continue
            
            # 默认: Paragraph
            unit.text_type = 'paragraph'
            unit.confidence = 0.60
    
    return all_units


# ============================================================================
# 栏位检测
# ============================================================================

def detect_columns(
    all_units: Dict[int, List["EnhancedTextUnit"]],
    page_width: float,
    debug: bool = False
) -> Tuple[int, float, Dict[int, List["EnhancedTextUnit"]]]:
    """
    检测文档是单栏还是双栏。
    
    方法：统计段落文本的 x0 分布，检测双峰。
    
    Args:
        all_units: 按页组织的文本单元字典
        page_width: 页面宽度
        debug: 调试模式
    
    Returns:
        (num_columns, column_gap, updated_units):
        - num_columns: 1=单栏, 2=双栏
        - column_gap: 双栏时的栏间距
        - updated_units: 标注了栏位的文本单元
    """
    if debug:
        print("\n[DEBUG] Column Detection")
        print("=" * 70)
    
    # 采样前5页的段落文本
    x0_values = []
    for pno in list(all_units.keys())[:5]:
        units = all_units.get(pno, [])
        for unit in units:
            if unit.text_type == 'paragraph':
                x0_values.append(unit.bbox.x0)
    
    if not x0_values or len(x0_values) < 10:
        if debug:
            print("Insufficient paragraph samples, assuming single column")
        num_columns = 1
        column_gap = 0.0
        for units in all_units.values():
            for unit in units:
                unit.column = -1
        return num_columns, column_gap, all_units
    
    # 使用 numpy 进行直方图分析
    try:
        import numpy as np
        x0_array = np.array(x0_values)
        hist, bins = np.histogram(x0_array, bins=20)
        
        threshold = np.mean(hist) * 1.5
        peaks_idx = np.where(hist > threshold)[0]
        
        if len(peaks_idx) >= 2:
            top_peaks = sorted(peaks_idx, key=lambda i: hist[i], reverse=True)[:2]
            top_peaks.sort()
            
            peak1_x = bins[top_peaks[0]]
            peak2_x = bins[top_peaks[1]]
            
            num_columns = 2
            column_gap = peak2_x - peak1_x - (page_width - peak2_x)
            mid_x = (peak1_x + peak2_x) / 2
            
            if debug:
                print(f"Detected TWO columns:")
                print(f"  Left column x0 ≈ {peak1_x:.1f}pt")
                print(f"  Right column x0 ≈ {peak2_x:.1f}pt")
                print(f"  Column gap ≈ {column_gap:.1f}pt")
            
            for units in all_units.values():
                for unit in units:
                    unit.column = 0 if unit.bbox.x0 < mid_x else 1
        else:
            num_columns = 1
            column_gap = 0.0
            
            if debug:
                print(f"Detected SINGLE column")
            
            for units in all_units.values():
                for unit in units:
                    unit.column = -1
    except ImportError:
        if debug:
            print("NumPy not available, assuming single column")
        num_columns = 1
        column_gap = 0.0
        for units in all_units.values():
            for unit in units:
                unit.column = -1
    
    return num_columns, column_gap, all_units


# ============================================================================
# 文本区块构建
# ============================================================================

def build_text_blocks(
    all_units: Dict[int, List["EnhancedTextUnit"]],
    typical_line_height: float,
    debug: bool = False
) -> Dict[int, List["TextBlock"]]:
    """
    将相邻的文本单元聚合成文本区块。
    
    聚合规则：
    1. 同类型（如都是 paragraph）
    2. 垂直距离 < 2×typical_line_height
    3. 同一栏
    
    Args:
        all_units: 按页组织的文本单元字典
        typical_line_height: 典型行高
        debug: 调试模式
    
    Returns:
        按页组织的文本区块字典
    """
    from .models import TextBlock
    
    if fitz is None:
        return {}
    
    if debug:
        print("\n[DEBUG] Building Text Blocks")
        print("=" * 70)
        print(f"Typical line height: {typical_line_height:.1f}pt")
    
    all_blocks: Dict[int, List[TextBlock]] = {}
    
    for pno, units in all_units.items():
        if not units:
            all_blocks[pno] = []
            continue
        
        sorted_units = sorted(units, key=lambda u: u.bbox.y0)
        
        blocks: List[TextBlock] = []
        current_block_units = [sorted_units[0]]
        current_type = sorted_units[0].text_type
        current_column = sorted_units[0].column
        
        for i in range(1, len(sorted_units)):
            unit = sorted_units[i]
            prev_unit = sorted_units[i-1]
            
            same_type = unit.text_type == current_type
            same_column = unit.column == current_column
            vertical_distance = unit.bbox.y0 - prev_unit.bbox.y1
            close_distance = vertical_distance < 2 * typical_line_height
            
            if same_type and same_column and close_distance:
                current_block_units.append(unit)
            else:
                # 创建新区块
                if current_type in ['paragraph', 'list'] and len(current_block_units) >= 2:
                    merged_bbox = fitz.Rect()
                    for u in current_block_units:
                        merged_bbox |= u.bbox
                    blocks.append(TextBlock(
                        bbox=merged_bbox,
                        units=current_block_units,
                        block_type=current_type + '_group',
                        page=pno,
                        column=current_column
                    ))
                elif current_type.startswith('title_') and len(current_block_units) >= 1:
                    merged_bbox = fitz.Rect()
                    for u in current_block_units:
                        merged_bbox |= u.bbox
                    blocks.append(TextBlock(
                        bbox=merged_bbox,
                        units=current_block_units,
                        block_type=current_type,
                        page=pno,
                        column=current_column
                    ))
                
                current_block_units = [unit]
                current_type = unit.text_type
                current_column = unit.column
        
        # 处理最后一个区块
        if current_type in ['paragraph', 'list'] and len(current_block_units) >= 2:
            merged_bbox = fitz.Rect()
            for u in current_block_units:
                merged_bbox |= u.bbox
            blocks.append(TextBlock(
                bbox=merged_bbox,
                units=current_block_units,
                block_type=current_type + '_group',
                page=pno,
                column=current_column
            ))
        elif current_type.startswith('title_') and len(current_block_units) >= 1:
            merged_bbox = fitz.Rect()
            for u in current_block_units:
                merged_bbox |= u.bbox
            blocks.append(TextBlock(
                bbox=merged_bbox,
                units=current_block_units,
                block_type=current_type,
                page=pno,
                column=current_column
            ))
        
        all_blocks[pno] = blocks
        
        if debug and pno == 0:
            print(f"[Page {pno+1}] Created {len(blocks)} text blocks")
            for i, block in enumerate(blocks[:5]):
                print(f"  Block {i+1}: {block.block_type}, {len(block.units)} units, bbox={block.bbox}")
    
    return all_blocks


# ============================================================================
# 留白区域检测
# ============================================================================

def detect_vacant_regions(
    all_blocks: Dict[int, List["TextBlock"]],
    doc: "fitz.Document",
    debug: bool = False
) -> Dict[int, List[Any]]:
    """
    识别页面中的留白区域（可能包含图表）。
    
    方法：
    1. 将页面划分为网格（50×50pt）
    2. 标记被文本区块覆盖的格子
    3. 连通未覆盖的格子，形成留白区域
    4. 过滤小区域
    
    Args:
        all_blocks: 按页组织的文本区块字典
        doc: PyMuPDF 文档对象
        debug: 调试模式
    
    Returns:
        按页组织的留白区域字典
    """
    if fitz is None:
        return {}
    
    if debug:
        print("\n[DEBUG] Detecting Vacant Regions")
        print("=" * 70)
    
    grid_size = 50
    all_vacant: Dict[int, List[Any]] = {}
    
    for pno in range(len(doc)):
        page = doc[pno]
        page_rect = page.rect
        
        nx = int(page_rect.width / grid_size) + 1
        ny = int(page_rect.height / grid_size) + 1
        
        try:
            import numpy as np
            grid = np.zeros((ny, nx), dtype=bool)
            
            blocks = all_blocks.get(pno, [])
            for block in blocks:
                if block.block_type in ['paragraph_group', 'list_group']:
                    x0_idx = max(0, int(block.bbox.x0 / grid_size))
                    y0_idx = max(0, int(block.bbox.y0 / grid_size))
                    x1_idx = min(nx, int(block.bbox.x1 / grid_size) + 1)
                    y1_idx = min(ny, int(block.bbox.y1 / grid_size) + 1)
                    grid[y0_idx:y1_idx, x0_idx:x1_idx] = True
            
            from scipy.ndimage import label as scipy_label
            labeled_grid, num_features = scipy_label(~grid)
            
            vacant_rects = []
            for region_id in range(1, num_features + 1):
                coords = np.argwhere(labeled_grid == region_id)
                if len(coords) == 0:
                    continue
                
                y_indices, x_indices = coords[:, 0], coords[:, 1]
                y0_idx = y_indices.min()
                y1_idx = y_indices.max()
                x0_idx = x_indices.min()
                x1_idx = x_indices.max()
                
                rect = fitz.Rect(
                    x0_idx * grid_size,
                    y0_idx * grid_size,
                    min((x1_idx + 1) * grid_size, page_rect.width),
                    min((y1_idx + 1) * grid_size, page_rect.height)
                )
                
                area_ratio = (rect.width * rect.height) / (page_rect.width * page_rect.height)
                if area_ratio > 0.05:
                    vacant_rects.append(rect)
            
            all_vacant[pno] = vacant_rects
            
            if debug and pno == 0:
                print(f"[Page {pno+1}] Found {len(vacant_rects)} vacant regions")
                for i, rect in enumerate(vacant_rects[:3]):
                    area_ratio = (rect.width * rect.height) / (page_rect.width * page_rect.height)
                    print(f"  Region {i+1}: {rect}, area={area_ratio:.1%}")
        
        except ImportError:
            if debug and pno == 0:
                print(f"[Page {pno+1}] NumPy/SciPy not available, skipping vacant region detection")
            all_vacant[pno] = []
    
    return all_vacant


# ============================================================================
# 版式优化裁剪
# ============================================================================

def adjust_clip_with_layout(
    clip_rect: Any,
    caption_rect: Any,
    layout_model: "DocumentLayoutModel",
    page_num: int,
    direction: str,
    debug: bool = False
) -> Any:
    """
    使用版式信息优化图表裁剪边界。
    
    策略：
    1. 检测 clip_rect 与正文段落的重叠
    2. 如果重叠过多，调整边界以贴合文本区块边界
    3. 使用文本区块边界作为"软约束"
    
    Args:
        clip_rect: 候选窗口
        caption_rect: 图注边界框
        layout_model: 版式模型
        page_num: 页码（0-based）
        direction: 图注方向（'above' | 'below'）
        debug: 调试模式
    
    Returns:
        调整后的边界框
    """
    if fitz is None:
        return clip_rect
    
    text_blocks = layout_model.text_blocks.get(page_num, [])
    if not text_blocks:
        return clip_rect
    
    protected_blocks = [b for b in text_blocks if b.block_type in ['paragraph_group', 'list_group'] or b.block_type.startswith('title_')]
    if not protected_blocks:
        return clip_rect
    
    # 区分内容区块和外部区块
    content_blocks = []
    external_blocks = []
    
    for block in protected_blocks:
        inter = clip_rect & block.bbox
        if inter.is_empty:
            external_blocks.append(block)
            continue
        
        overlap_with_clip = (inter.width * inter.height) / (block.bbox.width * block.bbox.height)
        
        # 处理标题
        if block.block_type.startswith('title_'):
            block_text = block.units[0].text.strip() if block.units else ""
            
            is_section_title = False
            if block_text:
                m = re.match(r'^(\d+(?:\.\d+)*)\s+(.*)$', block_text)
                if m:
                    after = (m.group(2) or "").strip()
                    if after and after[0].isalpha():
                        is_section_title = True
            
            if direction == 'below':
                dist_from_caption = block.bbox.y0 - caption_rect.y1
                dist_from_clip_far_edge = clip_rect.y1 - block.bbox.y0
            else:
                dist_from_caption = caption_rect.y0 - block.bbox.y1
                dist_from_clip_far_edge = block.bbox.y1 - clip_rect.y0
            
            is_near_far_edge = dist_from_clip_far_edge < 50
            
            should_exclude = False
            if is_section_title and dist_from_caption > 50:
                should_exclude = True
            elif is_near_far_edge and dist_from_caption > 100:
                should_exclude = True
            
            if should_exclude:
                external_blocks.append(block)
                continue
        
        if direction == 'below':
            if block.bbox.y0 >= caption_rect.y1 - 5 and overlap_with_clip > 0.5:
                content_blocks.append(block)
            else:
                external_blocks.append(block)
        else:
            if block.bbox.y1 <= caption_rect.y0 + 5 and overlap_with_clip > 0.5:
                content_blocks.append(block)
            else:
                external_blocks.append(block)
    
    # 计算外部区块重叠
    total_overlap_area = 0.0
    clip_area = clip_rect.width * clip_rect.height
    
    overlapping_blocks = []
    for block in external_blocks:
        inter = clip_rect & block.bbox
        if not inter.is_empty:
            overlap_area = inter.width * inter.height
            total_overlap_area += overlap_area
            overlap_ratio = overlap_area / clip_area
            threshold = 0.01 if block.block_type.startswith('title_') else 0.05
            if overlap_ratio > threshold:
                overlapping_blocks.append((block, inter, overlap_ratio))
    
    overlap_ratio_total = total_overlap_area / clip_area if clip_area > 0 else 0
    
    adjusted_clip = fitz.Rect(clip_rect)
    
    # 内容区块保护
    content_adjusted = False
    for block in content_blocks:
        if direction == 'below':
            if block.bbox.y0 < adjusted_clip.y0 < block.bbox.y1:
                adjusted_clip.y0 = block.bbox.y0 - 2
                content_adjusted = True
        else:
            if block.bbox.y0 < adjusted_clip.y1 < block.bbox.y1:
                adjusted_clip.y1 = block.bbox.y1 + 2
                content_adjusted = True
    
    if content_adjusted:
        return adjusted_clip
    
    # 边缘敏感裁剪
    try:
        typical_lh = getattr(layout_model, "typical_line_height", None)
        edge_strip_h = max(30.0, (3.0 * typical_lh) if (typical_lh and typical_lh > 0) else 45.0)
    except Exception:
        edge_strip_h = 45.0

    if direction == 'above':
        far_strip = fitz.Rect(adjusted_clip.x0, adjusted_clip.y0, adjusted_clip.x1, min(adjusted_clip.y1, adjusted_clip.y0 + edge_strip_h))
        candidate_blocks = []
        for b in protected_blocks:
            inter = b.bbox & far_strip
            if inter.is_empty:
                continue
            w_ratio = inter.width / max(1.0, adjusted_clip.width)
            if w_ratio >= 0.35:
                candidate_blocks.append((b, w_ratio))
        if candidate_blocks:
            new_y0 = max(b.bbox.y1 for (b, _) in candidate_blocks) + 6.0
            if new_y0 > adjusted_clip.y0 + 1e-3:
                adjusted_clip.y0 = min(new_y0, adjusted_clip.y1 - 10.0)
    else:
        far_strip = fitz.Rect(adjusted_clip.x0, max(adjusted_clip.y0, adjusted_clip.y1 - edge_strip_h), adjusted_clip.x1, adjusted_clip.y1)
        candidate_blocks = []
        for b in protected_blocks:
            inter = b.bbox & far_strip
            if inter.is_empty:
                continue
            w_ratio = inter.width / max(1.0, adjusted_clip.width)
            if w_ratio >= 0.35:
                candidate_blocks.append((b, w_ratio))
        if candidate_blocks:
            new_y1 = min(b.bbox.y0 for (b, _) in candidate_blocks) - 6.0
            if new_y1 < adjusted_clip.y1 - 1e-3:
                adjusted_clip.y1 = max(new_y1, adjusted_clip.y0 + 10.0)

    edge_changed = (abs(adjusted_clip.y0 - clip_rect.y0) > 1e-3) or (abs(adjusted_clip.y1 - clip_rect.y1) > 1e-3)

    has_title_overlap = any(b.block_type.startswith('title_') for b, _, _ in overlapping_blocks)
    
    if overlap_ratio_total < 0.20 and not has_title_overlap and not edge_changed:
        return clip_rect
    
    # 验证调整后的窗口
    if adjusted_clip.height < 0.5 * clip_rect.height or adjusted_clip.height < 80:
        return clip_rect
    
    return adjusted_clip


# ============================================================================
# 版式驱动检测
# ============================================================================

def should_enable_layout_driven(pdf_path: str, debug: bool = False) -> Tuple[bool, str]:
    """
    快速预扫描 PDF，判断是否需要启用版式驱动提取。
    
    检测标准（满足任一即启用）：
    1. 双栏布局
    2. 图表附近存在密集正文段落
    3. 页面文本区块复杂度高
    
    Args:
        pdf_path: PDF 文件路径
        debug: 调试模式
    
    Returns:
        (enable, reason): 是否启用，以及原因说明
    """
    if fitz is None:
        return False, "PyMuPDF not available"
    
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return False, f"cannot open PDF: {e}"
    
    try:
        figure_table_pages = set()
        total_pages = len(doc)
        
        fig_table_pattern = re.compile(r'(Figure|Fig\.?|Table|Tab\.?)\s*\d', re.IGNORECASE)
        for pno in range(total_pages):
            page = doc[pno]
            text = page.get_text("text")[:2000]
            if fig_table_pattern.search(text):
                figure_table_pages.add(pno)
        
        sample_pages = set()
        sample_pages.add(0)
        sample_pages.add(total_pages // 2)
        sample_pages.add(total_pages - 1)
        for pno in sorted(figure_table_pages)[:3]:
            sample_pages.add(pno)
        sample_pages = {p for p in sample_pages if 0 <= p < total_pages}
        
        sample_count = len(sample_pages)
        sample_pages_list = sorted(sample_pages)
        
        dual_column_pages = 0
        dense_text_pages = 0
        figure_with_dense_text = 0
        
        for pno in sample_pages_list:
            page = doc[pno]
            page_rect = page.rect
            page_width = page_rect.width
            
            blocks = page.get_text("dict")["blocks"]
            text_blocks = [b for b in blocks if b.get("type") == 0]
            
            if not text_blocks:
                continue
            
            x_centers = []
            for block in text_blocks:
                bbox = block.get("bbox", (0, 0, 0, 0))
                x_center = (bbox[0] + bbox[2]) / 2
                x_centers.append(x_center)
            
            if x_centers:
                left_count = sum(1 for x in x_centers if x < page_width * 0.4)
                right_count = sum(1 for x in x_centers if x > page_width * 0.6)
                if left_count >= 3 and right_count >= 3:
                    dual_column_pages += 1
            
            total_text_area = sum(
                (b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1])
                for b in text_blocks
            )
            page_area = page_rect.width * page_rect.height
            text_density = total_text_area / page_area if page_area > 0 else 0
            
            if text_density > 0.4:
                dense_text_pages += 1
            
            images = page.get_images(full=True)
            if images and text_density > 0.3:
                figure_with_dense_text += 1
        
        doc.close()
        
        if dual_column_pages >= sample_count * 0.5:
            return True, f"dual-column layout detected ({dual_column_pages}/{sample_count} pages)"
        
        if figure_with_dense_text >= 2:
            return True, f"dense text near figures ({figure_with_dense_text} pages)"
        
        if dense_text_pages >= sample_count * 0.6:
            return True, f"high text density ({dense_text_pages}/{sample_count} pages)"
        
        return False, "simple layout, layout-driven not needed"
        
    except Exception as e:
        try:
            doc.close()
        except:
            pass
        return False, f"detection error: {e}"


# ============================================================================
# 向后兼容别名
# ============================================================================

_classify_text_types = classify_text_types
_detect_columns = detect_columns
_build_text_blocks = build_text_blocks
_detect_vacant_regions = detect_vacant_regions
_adjust_clip_with_layout = adjust_clip_with_layout
_should_enable_layout_driven = should_enable_layout_driven
