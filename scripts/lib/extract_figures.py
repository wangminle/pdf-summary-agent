#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 12+: Figure 提取主循环

从 extract_pdf_assets.py 抽离的 Figure 提取逻辑。

V0.4.0 更新：集成完整的 Phase A/B/C 文本裁切逻辑

这个模块提供 extract_figures() 函数，用于从 PDF 中提取 Figure 图像。

主要处理流程：
1. 预扫描建立 Caption 索引（智能 Caption 检测）
2. 自适应参数计算（基于文档行高）
3. 全局锚点方向判定
4. 逐页扫描 Figure Caption
5. 裁剪窗口计算与精裁（Phase A/B/C）
6. 渲染与保存
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from .pdf_backend import create_rect, open_pdf

# 导入本地模块
from .models import AttachmentRecord, CaptionIndex, DocumentLayoutModel
from .idents import build_output_basename, extract_figure_ident
from .caption_detection import build_caption_index, select_best_caption, find_all_caption_candidates
from .refine import (
    refine_clip_by_objects,
    detect_content_bbox_pixels,
    adaptive_acceptance_thresholds,
    detect_far_side_text_evidence,
    trim_far_side_text_post_autocrop,
    trim_clip_head_by_text_v2,
    merge_rects,
    build_text_masks_px,
    snap_clip_edges,
)
from .extract_helpers import (
    collect_draw_items,
    collect_text_lines,
    estimate_ink_ratio,
    estimate_document_line_metrics,
    DrawItem,
)
from .output import get_unique_path

# 避免循环导入
if TYPE_CHECKING:
    import fitz

# 模块日志器
logger = logging.getLogger(__name__)

# Figure 正则表达式（支持多种格式）
FIGURE_LINE_RE = re.compile(
    r"^\s*(?P<label>Extended\s+Data\s+Figure|Supplementary\s+(?:Figure|Fig\.?)|Figure|Fig\.?|图表|附图|图)\s*"
    r"(?:(?P<s_prefix>S)\s*(?P<s_id>(?:\d+|[IVX]{1,6}))|(?P<roman>[IVX]{1,6})|(?P<num>\d+))"
    r"(?:\s*[-–]?\s*[A-Za-z]|\s*\([A-Za-z]\))?"  # 可选的子图标签
    r"(?:\s*\(continued\)|\s*续|\s*接上页)?",  # 可选的续页标记
    re.IGNORECASE,
)


def extract_figures(
    pdf_path: str,
    out_dir: str,
    dpi: int = 300,
    clip_height: float = 650.0,
    margin_x: float = 20.0,
    caption_gap: float = 3.0,
    max_caption_chars: int = 160,
    max_caption_words: int = 12,
    min_figure: int = 1,
    max_figure: int = 999,
    autocrop: bool = False,
    autocrop_pad_px: int = 30,
    autocrop_white_threshold: int = 250,
    below_figs: Optional[List[str]] = None,
    above_figs: Optional[List[str]] = None,
    # A: text-trim options
    text_trim: bool = False,
    text_trim_width_ratio: float = 0.5,
    text_trim_font_min: float = 7.0,
    text_trim_font_max: float = 16.0,
    text_trim_gap: float = 6.0,
    adjacent_th: float = 24.0,
    # A+: far-text trim options
    far_text_th: float = 300.0,
    far_text_para_min_ratio: float = 0.30,
    far_text_trim_mode: str = "aggressive",
    far_side_min_dist: float = 50.0,
    far_side_para_min_ratio: float = 0.12,
    # B: object connectivity options
    object_pad: float = 8.0,
    object_min_area_ratio: float = 0.010,
    object_merge_gap: float = 6.0,
    # D: text-mask assisted autocrop
    autocrop_mask_text: bool = False,
    mask_font_max: float = 14.0,
    mask_width_ratio: float = 0.5,
    mask_top_frac: float = 0.6,
    # Safety & integration
    refine_near_edge_only: bool = True,
    no_refine_figs: Optional[List[str]] = None,
    refine_safe: bool = True,
    autocrop_shrink_limit: float = 0.35,
    autocrop_min_height_px: int = 80,
    # Heuristics tuners
    text_trim_min_para_ratio: float = 0.18,
    protect_far_edge_px: int = 12,
    near_edge_pad_px: int = 18,
    # Continuation handling
    allow_continued: bool = False,
    # Smart caption detection
    smart_caption_detection: bool = True,
    debug_captions: bool = False,
    # Visual debug mode
    debug_visual: bool = False,
    # Adaptive line height
    adaptive_line_height: bool = True,
    # Layout model (V2 Architecture)
    layout_model: Optional[DocumentLayoutModel] = None,
) -> List[AttachmentRecord]:
    """
    从 PDF 中提取 Figure 图像。
    
    这是一个复杂的函数，包含多个阶段的处理：
    1. 预扫描建立 Caption 索引（智能 Caption 检测）
    2. 自适应参数计算（基于文档行高）
    3. 全局锚点方向判定
    4. 逐页扫描 Figure Caption
    5. 裁剪窗口计算与精裁（Phase A/B/C）
    6. 渲染与保存
    
    Args:
        pdf_path: PDF 文件路径
        out_dir: 输出目录
        dpi: 渲染分辨率
        clip_height: 裁剪窗口高度（pt）
        margin_x: 水平边距（pt）
        caption_gap: Caption 与图像之间的间隙（pt）
        below_figs: 强制从 caption 下方取图的 Figure 列表
        above_figs: 强制从 caption 上方取图的 Figure 列表
        text_trim: 是否启用文本裁切
        ... (更多参数见函数签名)
    
    Returns:
        AttachmentRecord 列表，记录提取的每个 Figure
    """
    # 基础实现框架
    pdf_name = os.path.basename(pdf_path)
    doc = open_pdf(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    
    records: List[AttachmentRecord] = []
    seen_counts: Dict[str, int] = {}
    
    # 处理方向覆盖参数
    below_set: Set[str] = set([str(x).strip() for x in (below_figs or []) if str(x).strip()])
    above_set: Set[str] = set([str(x).strip() for x in (above_figs or []) if str(x).strip()])
    no_refine_set: Set[str] = set([str(x).strip() for x in (no_refine_figs or []) if str(x).strip()])
    
    # Smart Caption Detection: 预扫描建立索引
    caption_index: Optional[CaptionIndex] = None
    if smart_caption_detection:
        if debug_captions:
            print(f"\n{'='*60}")
            print(f"SMART CAPTION DETECTION ENABLED")
            print(f"{'='*60}")
        caption_index = build_caption_index(doc, figure_pattern=FIGURE_LINE_RE, debug=debug_captions)
    
    # Adaptive Line Height: 统计文档行高
    typical_line_h: Optional[float] = None
    if adaptive_line_height:
        line_metrics = estimate_document_line_metrics(doc, sample_pages=5, debug=debug_captions)
        typical_line_h = line_metrics['typical_line_height']
        
        # 自适应参数计算
        if adjacent_th == 24.0:
            adjacent_th = 2.0 * typical_line_h
        if far_text_th == 300.0:
            far_text_th = 15.0 * typical_line_h
        if text_trim_gap == 6.0:
            text_trim_gap = 0.5 * typical_line_h
        if far_side_min_dist == 50.0:
            far_side_min_dist = 3.0 * typical_line_h
    
    # 逐页扫描
    for pno in range(len(doc)):
        page = doc[pno]
        page_rect = page.rect
        dict_data = page.get_text("dict")
        
        # 收集该页的文本行和绘图项（用于 Phase A/B）
        text_lines = collect_text_lines(dict_data)
        draw_items = collect_draw_items(page)
        
        # 收集图像和矢量对象的边界框（用于 Phase B）
        image_rects: List = []
        vector_rects: List = []
        for item in draw_items:
            if item.orient == 'O':  # 其他形状
                vector_rects.append(item.rect)
        
        # 从 dict_data 收集图像
        for blk in dict_data.get("blocks", []):
            if blk.get("type") == 1:  # 图像块
                bbox = blk.get("bbox")
                if bbox:
                    image_rects.append(create_rect(*bbox))
        
        # 查找 Figure captions
        for blk in dict_data.get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            
            for ln in blk.get("lines", []):
                spans = ln.get("spans", [])
                if not spans:
                    continue
                
                text = "".join(sp.get("text", "") for sp in spans)
                text_stripped = text.strip()
                
                match = FIGURE_LINE_RE.match(text_stripped)
                if not match:
                    continue
                
                # 提取 Figure 编号
                ident = extract_figure_ident(match)
                if not ident:
                    continue
                
                # 检查编号范围
                try:
                    num = int(ident)
                    if num < min_figure or num > max_figure:
                        continue
                except ValueError:
                    pass  # 非数字编号（如 S1）
                
                # 检查是否已处理
                if ident in seen_counts and not allow_continued:
                    continue
                
                seen_counts[ident] = seen_counts.get(ident, 0) + 1
                is_continued = seen_counts[ident] > 1
                
                # 构建文件名
                caption_for_name = text_stripped[:max_caption_chars]
                basename = build_output_basename(
                    "figure",
                    ident,
                    caption_for_name,
                    max_chars=max_caption_chars,
                    max_words=max_caption_words,
                )
                out_path = os.path.join(out_dir, basename + ".png")
                out_path, _ = get_unique_path(out_path)
                
                # 获取 caption 边界框
                caption_bbox = create_rect(*(ln.get("bbox", [0, 0, 0, 0])))
                
                # ================================================================
                # 方向判定：决定从 caption 上方还是下方取图
                # ================================================================
                direction = 'above'  # 默认：图在 caption 上方
                
                # 1. 用户显式指定的方向覆盖
                if ident in below_set:
                    direction = 'below'
                elif ident in above_set:
                    direction = 'above'
                else:
                    # 2. 简单启发式：如果 caption 在页面顶部 1/3，则从下方取图
                    # （后续 Phase 2 会实现完整的智能方向判定）
                    page_third = page_rect.height / 3
                    if caption_bbox.y0 < page_rect.y0 + page_third:
                        direction = 'below'
                
                # ================================================================
                # 计算基础裁剪窗口 (Baseline)
                # ================================================================
                x_left = page_rect.x0 + margin_x
                x_right = page_rect.x1 - margin_x
                
                if direction == 'above':
                    # 图在 caption 上方
                    y_bottom = caption_bbox.y0 - caption_gap
                    y_top = max(page_rect.y0, y_bottom - clip_height)
                else:
                    # 图在 caption 下方
                    y_top = caption_bbox.y1 + caption_gap
                    y_bottom = min(page_rect.y1, y_top + clip_height)
                
                base_clip = create_rect(x_left, y_top, x_right, y_bottom)
                clip = create_rect(x_left, y_top, x_right, y_bottom)  # 工作副本
                
                # ================================================================
                # Phase A: 文本裁切
                # ================================================================
                if text_trim and ident not in no_refine_set:
                    clip = trim_clip_head_by_text_v2(
                        clip,
                        page_rect,
                        caption_bbox,
                        direction,
                        text_lines,
                        width_ratio=text_trim_width_ratio,
                        font_min=text_trim_font_min,
                        font_max=text_trim_font_max,
                        gap=text_trim_gap,
                        adjacent_th=adjacent_th,
                        far_text_th=far_text_th,
                        far_text_para_min_ratio=far_text_para_min_ratio,
                        far_text_trim_mode=far_text_trim_mode,
                        far_side_min_dist=far_side_min_dist,
                        far_side_para_min_ratio=far_side_para_min_ratio,
                        typical_line_h=typical_line_h,
                        skip_adjacent_sweep=False,  # Figure 不跳过
                        debug=debug_captions,
                    )
                
                clip_after_A = create_rect(clip.x0, clip.y0, clip.x1, clip.y1)
                
                # ================================================================
                # Phase B: 对象对齐（如果启用）
                # ================================================================
                if ident not in no_refine_set:
                    clip = refine_clip_by_objects(
                        clip,
                        caption_bbox,
                        direction,
                        image_rects,
                        vector_rects,
                        object_pad=object_pad,
                        min_area_ratio=object_min_area_ratio,
                        merge_gap=object_merge_gap,
                        near_edge_only=refine_near_edge_only,
                        use_axis_union=True,
                        use_horizontal_union=False,
                    )
                
                # ================================================================
                # 渲染与保存
                # ================================================================
                try:
                    scale = dpi / 72.0
                    pix = page.get_pixmap(dpi=dpi, clip=clip)
                    pix.save(out_path)
                    
                    records.append(AttachmentRecord(
                        kind='figure',
                        ident=ident,
                        page=pno + 1,
                        caption=text_stripped,
                        out_path=out_path,
                        continued=is_continued
                    ))
                    
                    logger.info(f"Extracted Figure {ident} from page {pno + 1}: {out_path}")
                except Exception as e:
                    logger.warning(f"Failed to extract Figure {ident}: {e}")
    
    doc.close()
    
    logger.info(f"Extracted {len(records)} figures from {pdf_name}")
    return records


# ============================================================================
# 向后兼容
# ============================================================================

# 提供与旧代码兼容的别名
_extract_figures = extract_figures
