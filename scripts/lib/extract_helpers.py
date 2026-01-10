#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 12: 提取辅助函数

包含 extract_figures 和 extract_tables 共享的辅助函数。

这些函数从 extract_pdf_assets.py 中抽离，用于支持图表提取主循环。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

# 尝试导入 fitz
try:
    import fitz
except ImportError:
    fitz = None  # type: ignore

# 避免循环导入
if TYPE_CHECKING:
    from .models import DrawItem

# 模块日志器
logger = logging.getLogger(__name__)


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class DrawItem:
    """绘图项（用于线条/网格感知）"""
    rect: "fitz.Rect"
    orient: str  # 'H' | 'V' | 'O'


# ============================================================================
# 墨迹密度估计
# ============================================================================

def estimate_ink_ratio(pix: "fitz.Pixmap", white_threshold: int = 250) -> float:
    """
    估计位图中"有墨迹"的像素比例（0~1）。
    
    通过子采样快速近似；值越大表示内容越密集。
    
    Args:
        pix: PyMuPDF Pixmap 对象
        white_threshold: 白色阈值（默认 250）
    
    Returns:
        非白色像素占比（0.0~1.0）
    """
    if fitz is None:
        return 0.0
    
    w, h = pix.width, pix.height
    n = pix.n
    if pix.alpha:
        tmp = fitz.Pixmap(fitz.csRGB, pix)
        pix = tmp
        n = pix.n
    samples = memoryview(pix.samples)
    stride = pix.stride
    step_x = max(1, w // 800)
    step_y = max(1, h // 800)
    nonwhite = 0
    total = 0
    for y in range(0, h, step_y):
        row = samples[y * stride:(y + 1) * stride]
        for x in range(0, w, step_x):
            off = x * n
            r = row[off + 0]
            g = row[off + 1] if n > 1 else r
            b = row[off + 2] if n > 2 else r
            if r < white_threshold or g < white_threshold or b < white_threshold:
                nonwhite += 1
            total += 1
    if total == 0:
        return 0.0
    return nonwhite / float(total)


# ============================================================================
# 绘图项收集
# ============================================================================

def collect_draw_items(page: "fitz.Page") -> List[DrawItem]:
    """
    收集简化的绘图项（线条/矩形/路径）作为有方向的边界框。
    
    方向由边界框的长宽比确定：H（宽）、V（高）、O（其他）。
    
    Args:
        page: PyMuPDF 页面对象
    
    Returns:
        DrawItem 列表
    """
    if fitz is None:
        return []
    
    out: List[DrawItem] = []
    try:
        for dr in page.get_drawings():
            r = dr.get("rect")
            if r is None:
                # 回退：尝试通过项的边界框联合来近似
                union: Optional[fitz.Rect] = None
                for it in dr.get("items", []):
                    rb = it[0] if it and isinstance(it[0], fitz.Rect) else None
                    if rb:
                        union = rb if union is None else (union | rb)
                if union is None:
                    continue
                rect = fitz.Rect(*union)
            else:
                rect = fitz.Rect(*r)
            if rect.width <= 0 or rect.height <= 0:
                continue
            ar = rect.width / max(1e-6, rect.height)
            if ar >= 8.0:
                orient = 'H'
            elif ar <= 1/8.0:
                orient = 'V'
            else:
                orient = 'O'
            out.append(DrawItem(rect=rect, orient=orient))
    except Exception as e:
        page_no = getattr(page, "number", None)
        extra = {'stage': 'collect_draw_items'}
        if isinstance(page_no, int):
            extra['page'] = page_no + 1
        logger.warning(f"Failed to collect drawings: {e}", extra=extra)
    return out


# ============================================================================
# 文本行收集
# ============================================================================

def collect_text_lines(dict_data: Dict) -> List[Tuple["fitz.Rect", float, str]]:
    """
    从页面字典中收集行级文本条目。
    
    Args:
        dict_data: page.get_text("dict") 返回的字典
    
    Returns:
        (bbox, font_size_estimate, text) 元组列表
    """
    if fitz is None:
        return []
    
    out: List[Tuple[fitz.Rect, float, str]] = []
    for blk in dict_data.get("blocks", []):
        if blk.get("type", 0) != 0:
            continue
        for ln in blk.get("lines", []):
            bbox = fitz.Rect(*(ln.get("bbox", [0, 0, 0, 0])))
            text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            # 通过行中最大 span 大小估计字号（回退 10）
            sizes = [float(sp.get("size", 10.0)) for sp in ln.get("spans", []) if "size" in sp]
            size_est = max(sizes) if sizes else 10.0
            out.append((bbox, size_est, text))
    return out


# ============================================================================
# 图注文本检测
# ============================================================================

def is_caption_text(
    text: str,
    kind: str = 'figure',
    strict: bool = True
) -> bool:
    """
    判断文本是否看起来像图注（而非正文）。
    
    Args:
        text: 文本内容
        kind: 'figure' 或 'table'
        strict: 是否使用严格模式
    
    Returns:
        是否像图注
    """
    text_lower = text.lower().strip()
    
    if kind == 'figure':
        # Figure 图注模式
        patterns = [
            r'^(figure|fig\.?|图|附图)\s*',
            r'^extended\s+data\s+figure',
            r'^supplementary\s+(figure|fig)',
        ]
    else:
        # Table 表注模式
        patterns = [
            r'^(table|tab\.?|表)\s*',
            r'^extended\s+data\s+table',
            r'^supplementary\s+table',
        ]
    
    for pat in patterns:
        if re.match(pat, text_lower):
            return True
    
    return False


# ============================================================================
# 文档行高估计
# ============================================================================

def estimate_document_line_metrics(
    doc: "fitz.Document",
    sample_pages: int = 5,
    debug: bool = False
) -> Dict[str, float]:
    """
    估计文档的典型行高和字号。
    
    Args:
        doc: PyMuPDF 文档对象
        sample_pages: 采样页数
        debug: 是否输出调试信息
    
    Returns:
        包含 'typical_line_height', 'typical_font_size' 等的字典
    """
    if fitz is None:
        return {
            'typical_line_height': 12.0,
            'typical_font_size': 10.0,
            'typical_line_gap': 2.0,
        }
    
    line_heights: List[float] = []
    font_sizes: List[float] = []
    
    num_pages = min(sample_pages, len(doc))
    for pno in range(num_pages):
        page = doc[pno]
        dict_data = page.get_text("dict")
        
        for blk in dict_data.get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            
            lines = blk.get("lines", [])
            for i, ln in enumerate(lines):
                # 收集字号
                for sp in ln.get("spans", []):
                    size = sp.get("size", 0)
                    if 6 <= size <= 20:
                        font_sizes.append(size)
                
                # 计算行高（与前一行的间距）
                if i > 0:
                    prev_ln = lines[i - 1]
                    curr_y0 = ln.get("bbox", [0, 0, 0, 0])[1]
                    prev_y1 = prev_ln.get("bbox", [0, 0, 0, 0])[3]
                    gap = curr_y0 - prev_y1
                    height = ln.get("bbox", [0, 0, 0, 0])[3] - ln.get("bbox", [0, 0, 0, 0])[1]
                    if 0 < gap < 30 and 6 < height < 30:
                        line_heights.append(height + gap)
    
    typical_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 10.0
    typical_line_height = sum(line_heights) / len(line_heights) if line_heights else 12.0
    typical_line_gap = max(0.0, typical_line_height - typical_font_size)
    
    if debug:
        print(f"[DEBUG] Estimated metrics: font={typical_font_size:.1f}pt, "
              f"line_h={typical_line_height:.1f}pt, gap={typical_line_gap:.1f}pt")
    
    return {
        'typical_line_height': typical_line_height,
        'typical_font_size': typical_font_size,
        'typical_line_gap': typical_line_gap,
    }


# ============================================================================
# 列峰值估计（用于表格检测）
# ============================================================================

def estimate_column_peaks(
    clip: "fitz.Rect",
    text_lines: List[Tuple["fitz.Rect", float, str]],
    bin_width: float = 5.0
) -> int:
    """
    估计裁剪区域内的列数（通过文本行左边界的峰值）。
    
    用于检测表格结构。
    
    Args:
        clip: 裁剪区域
        text_lines: 文本行列表
        bin_width: 分箱宽度
    
    Returns:
        估计的列数
    """
    if fitz is None or not text_lines:
        return 0
    
    # 收集裁剪区域内的文本行左边界
    x_positions: List[float] = []
    for bbox, _, _ in text_lines:
        if bbox.intersects(clip):
            x_positions.append(bbox.x0)
    
    if not x_positions:
        return 0
    
    # 简单的峰值检测：统计不同 x 位置的分布
    min_x = min(x_positions)
    max_x = max(x_positions)
    if max_x - min_x < bin_width:
        return 1
    
    # 分箱统计
    num_bins = int((max_x - min_x) / bin_width) + 1
    bins = [0] * num_bins
    for x in x_positions:
        bin_idx = min(int((x - min_x) / bin_width), num_bins - 1)
        bins[bin_idx] += 1
    
    # 计算峰值数量（简化版：计算超过平均值的分箱数）
    avg = sum(bins) / len(bins) if bins else 0
    peaks = sum(1 for b in bins if b > avg * 0.5)
    
    return peaks


# ============================================================================
# 线密度计算（用于表格检测）
# ============================================================================

def line_density(
    clip: "fitz.Rect",
    draw_items: List[DrawItem]
) -> float:
    """
    计算裁剪区域内的线条密度。
    
    用于检测表格（表格通常有更多水平和垂直线条）。
    
    Args:
        clip: 裁剪区域
        draw_items: 绘图项列表
    
    Returns:
        线密度值（0.0~1.0）
    """
    if fitz is None or not draw_items:
        return 0.0
    
    h_count = 0
    v_count = 0
    
    for item in draw_items:
        if item.rect.intersects(clip):
            if item.orient == 'H':
                h_count += 1
            elif item.orient == 'V':
                v_count += 1
    
    # 归一化
    total = h_count + v_count
    if total == 0:
        return 0.0
    
    # 线密度 = 线条数 / 区域面积 * 1000（归一化）
    area = max(1.0, clip.width * clip.height)
    density = total / area * 1000
    
    return min(1.0, density)


# ============================================================================
# 段落比例计算
# ============================================================================

def paragraph_ratio(
    clip: "fitz.Rect",
    text_lines: List[Tuple["fitz.Rect", float, str]],
    width_threshold_ratio: float = 0.5
) -> float:
    """
    计算裁剪区域内"段落级"文本行的占比。
    
    段落级文本行指宽度超过裁剪区域一定比例的行。
    
    Args:
        clip: 裁剪区域
        text_lines: 文本行列表
        width_threshold_ratio: 宽度阈值比例
    
    Returns:
        段落比例（0.0~1.0）
    """
    if fitz is None or not text_lines:
        return 0.0
    
    width_threshold = clip.width * width_threshold_ratio
    total_in_clip = 0
    wide_count = 0
    
    for bbox, _, text in text_lines:
        if bbox.intersects(clip) and len(text.strip()) > 5:
            total_in_clip += 1
            if bbox.width >= width_threshold:
                wide_count += 1
    
    if total_in_clip == 0:
        return 0.0
    
    return wide_count / total_in_clip


# ============================================================================
# Rect 工具函数
# ============================================================================

def rect_to_list(r: "fitz.Rect") -> List[float]:
    """将 fitz.Rect 转换为列表 [x0, y0, x1, y1]"""
    return [round(float(r.x0), 1), round(float(r.y0), 1), 
            round(float(r.x1), 1), round(float(r.y1), 1)]


# ============================================================================
# 向后兼容别名
# ============================================================================

# 保持与旧代码的兼容性（内部函数名以下划线开头）
_collect_text_lines = collect_text_lines
_is_caption_text = is_caption_text
_estimate_document_line_metrics = estimate_document_line_metrics
_estimate_column_peaks = estimate_column_peaks
_line_density = line_density
_paragraph_ratio = paragraph_ratio
_rect_to_list = rect_to_list
