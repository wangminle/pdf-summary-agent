#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 12: Figure 提取主循环

从 extract_pdf_assets.py 抽离的 Figure 提取逻辑。

这个模块提供 extract_figures() 函数，用于从 PDF 中提取 Figure 图像。

注意：完整实现仍在 scripts-old/extract_pdf_assets.py 中。
本模块提供模块化入口点，后续会逐步迁移完整逻辑。
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Dict, List, Optional

# 尝试导入 fitz
try:
    import fitz
except ImportError:
    fitz = None  # type: ignore

# 导入本地模块
from .models import AttachmentRecord, CaptionIndex, DocumentLayoutModel
from .idents import extract_figure_ident, sanitize_filename_from_caption
from .caption_detection import build_caption_index, select_best_caption, find_all_caption_candidates
from .refine import (
    refine_clip_by_objects,
    detect_content_bbox_pixels,
    adaptive_acceptance_thresholds,
    detect_far_side_text_evidence,
    trim_far_side_text_post_autocrop,
)
from .extract_helpers import (
    collect_draw_items,
    collect_text_lines,
    estimate_ink_ratio,
    estimate_document_line_metrics,
)
from .output import get_unique_path

# 避免循环导入
if TYPE_CHECKING:
    pass

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
    5. 裁剪窗口计算与精裁（Phase A/B/C/D）
    6. 渲染与保存
    
    Args:
        pdf_path: PDF 文件路径
        out_dir: 输出目录
        dpi: 渲染分辨率
        clip_height: 裁剪窗口高度（pt）
        margin_x: 水平边距（pt）
        caption_gap: Caption 与图像之间的间隙（pt）
        ... (更多参数见函数签名)
    
    Returns:
        AttachmentRecord 列表，记录提取的每个 Figure
    
    Note:
        完整实现仍在 scripts-old/extract_pdf_assets.py 中。
        本函数目前作为模块化入口点，调用原脚本中的实现。
    """
    if fitz is None:
        logger.error("PyMuPDF (fitz) is required for figure extraction")
        return []
    
    # 注意：这是一个占位实现
    # 完整的 extract_figures 逻辑非常复杂（约 2000 行）
    # 需要逐步从 scripts-old/extract_pdf_assets.py 迁移
    
    logger.warning(
        "extract_figures in lib/extract_figures.py is a stub. "
        "Use scripts-old/extract_pdf_assets.py for full functionality."
    )
    
    # 基础实现框架
    pdf_name = os.path.basename(pdf_path)
    doc = fitz.open(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    
    records: List[AttachmentRecord] = []
    seen_counts: Dict[str, int] = {}
    
    # Smart Caption Detection: 预扫描建立索引
    caption_index: Optional[CaptionIndex] = None
    if smart_caption_detection:
        if debug_captions:
            print(f"\n{'='*60}")
            print(f"SMART CAPTION DETECTION ENABLED")
            print(f"{'='*60}")
        caption_index = build_caption_index(doc, figure_pattern=FIGURE_LINE_RE, debug=debug_captions)
    
    # Adaptive Line Height: 统计文档行高
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
                basename = sanitize_filename_from_caption(
                    caption_for_name, 
                    prefix=f"Figure_{ident}_",
                    max_words=max_caption_words
                )
                out_path = os.path.join(out_dir, basename + ".png")
                out_path, _ = get_unique_path(out_path)
                
                # 计算裁剪窗口（简化版本 - 从 caption 上方取图）
                caption_bbox = fitz.Rect(*(ln.get("bbox", [0, 0, 0, 0])))
                x_left = page_rect.x0 + margin_x
                x_right = page_rect.x1 - margin_x
                y_bottom = caption_bbox.y0 - caption_gap
                y_top = max(page_rect.y0, y_bottom - clip_height)
                
                clip = fitz.Rect(x_left, y_top, x_right, y_bottom)
                
                # 渲染
                try:
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
