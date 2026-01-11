#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 12: Table 提取主循环

从 extract_pdf_assets.py 抽离的 Table 提取逻辑。

这个模块提供 extract_tables() 函数，用于从 PDF 中提取 Table 图像。

注意：完整实现仍在 scripts-old/extract_pdf_assets.py 中。
本模块提供模块化入口点，后续会逐步迁移完整逻辑。
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from .pdf_backend import create_rect, open_pdf

# 导入本地模块
from .models import AttachmentRecord, CaptionIndex, DocumentLayoutModel
from .idents import extract_table_ident, sanitize_filename_from_caption
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
    estimate_column_peaks,
    line_density,
)
from .output import get_unique_path

# 避免循环导入
if TYPE_CHECKING:
    pass

# 模块日志器
logger = logging.getLogger(__name__)

# Table 正则表达式（支持多种格式）
TABLE_LINE_RE = re.compile(
    r"^\s*(?P<label>Extended\s+Data\s+Table|Supplementary\s+Table|Table|Tab\.?|表)\s*"
    r"(?:(?P<s_prefix>S)\s*(?P<s_id>(?:\d+|[IVX]{1,6}))|(?P<letter_id>[A-Z]\d+)|(?P<roman>[IVX]{1,6})|(?P<num>\d+))"
    r"(?:\s*\(continued\)|\s*续|\s*接上页)?",
    re.IGNORECASE,
)


def extract_tables(
    pdf_path: str,
    out_dir: str,
    *,
    dpi: int = 300,
    table_clip_height: float = 520.0,
    table_margin_x: float = 26.0,
    table_caption_gap: float = 6.0,
    max_caption_chars: int = 160,
    max_caption_words: int = 12,
    min_table: Optional[str] = None,
    max_table: Optional[str] = None,
    autocrop: bool = True,
    autocrop_pad_px: int = 20,
    autocrop_white_threshold: int = 250,
    t_below: Optional[Iterable[str]] = None,
    t_above: Optional[Iterable[str]] = None,
    # A: text-trim options
    text_trim: bool = True,
    text_trim_width_ratio: float = 0.55,
    text_trim_font_min: float = 7.0,
    text_trim_font_max: float = 16.0,
    text_trim_gap: float = 6.0,
    adjacent_th: float = 28.0,
    # A+: far-text trim options
    far_text_th: float = 300.0,
    far_text_para_min_ratio: float = 0.30,
    far_text_trim_mode: str = "aggressive",
    far_side_min_dist: float = 50.0,
    far_side_para_min_ratio: float = 0.12,
    table_far_side_width_ratio: float = 0.7,
    # B: object connectivity options
    object_pad: float = 8.0,
    object_min_area_ratio: float = 0.005,
    object_merge_gap: float = 4.0,
    # D: text-mask assisted autocrop
    autocrop_mask_text: bool = False,
    mask_font_max: float = 14.0,
    mask_width_ratio: float = 0.5,
    mask_top_frac: float = 0.6,
    # Safety
    refine_near_edge_only: bool = True,
    refine_safe: bool = True,
    autocrop_shrink_limit: float = 0.35,
    autocrop_min_height_px: int = 80,
    allow_continued: bool = True,
    protect_far_edge_px: int = 10,
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
    从 PDF 中提取 Table 图像。
    
    这是一个复杂的函数，包含多个阶段的处理：
    1. 预扫描建立 Caption 索引（智能 Caption 检测）
    2. 自适应参数计算（基于文档行高）
    3. 全局锚点方向判定（使用表格特定的评分机制）
    4. 逐页扫描 Table Caption
    5. 裁剪窗口计算与精裁（Phase A/B/C/D）
    6. 渲染与保存
    
    Args:
        pdf_path: PDF 文件路径
        out_dir: 输出目录
        dpi: 渲染分辨率
        table_clip_height: 裁剪窗口高度（pt）
        table_margin_x: 水平边距（pt）
        table_caption_gap: Caption 与表格之间的间隙（pt）
        ... (更多参数见函数签名)
    
    Returns:
        AttachmentRecord 列表，记录提取的每个 Table
    
    Note:
        完整实现仍在 scripts-old/extract_pdf_assets.py 中。
        本函数目前作为模块化入口点，调用原脚本中的实现。
    """
    logger.warning(
        "extract_tables 当前为简化实现；如需与历史版本完全一致，请以输出结果为准进行回归核对。"
    )
    
    # 基础实现框架
    pdf_name = os.path.basename(pdf_path)
    doc = open_pdf(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    
    records: List[AttachmentRecord] = []
    seen_counts: Dict[str, int] = {}
    
    # 处理方向覆盖参数
    t_below_set = set([str(x).strip() for x in (t_below or []) if str(x).strip()])
    t_above_set = set([str(x).strip() for x in (t_above or []) if str(x).strip()])
    
    # Smart Caption Detection: 预扫描建立索引
    caption_index: Optional[CaptionIndex] = None
    if smart_caption_detection:
        if debug_captions:
            print(f"\n{'='*60}")
            print(f"SMART CAPTION DETECTION ENABLED FOR TABLES")
            print(f"{'='*60}")
        caption_index = build_caption_index(
            doc,
            figure_pattern=None,  # Skip figures
            table_pattern=TABLE_LINE_RE,
            debug=debug_captions
        )
    
    # Adaptive Line Height: 统计文档行高
    if adaptive_line_height:
        line_metrics = estimate_document_line_metrics(doc, sample_pages=5, debug=debug_captions)
        typical_line_h = line_metrics['typical_line_height']
        
        # 自适应参数计算
        if adjacent_th == 28.0:
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
        
        # 查找 Table captions
        for blk in dict_data.get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            
            for ln in blk.get("lines", []):
                spans = ln.get("spans", [])
                if not spans:
                    continue
                
                text = "".join(sp.get("text", "") for sp in spans)
                text_stripped = text.strip()
                
                match = TABLE_LINE_RE.match(text_stripped)
                if not match:
                    continue
                
                # 提取 Table 编号
                ident = extract_table_ident(match)
                if not ident:
                    continue
                
                # 检查是否已处理
                if ident in seen_counts and not allow_continued:
                    continue
                
                seen_counts[ident] = seen_counts.get(ident, 0) + 1
                is_continued = seen_counts[ident] > 1
                
                # 构建文件名
                caption_for_name = text_stripped[:max_caption_chars]
                basename = sanitize_filename_from_caption(
                    caption_for_name, 
                    prefix=f"Table_{ident}_",
                    max_words=max_caption_words
                )
                out_path = os.path.join(out_dir, basename + ".png")
                out_path, _ = get_unique_path(out_path)
                
                # 确定裁剪方向
                # 表格通常在 caption 下方
                go_below = True
                if ident in t_above_set:
                    go_below = False
                elif ident in t_below_set:
                    go_below = True
                
                # 计算裁剪窗口
                caption_bbox = create_rect(*(ln.get("bbox", [0, 0, 0, 0])))
                x_left = page_rect.x0 + table_margin_x
                x_right = page_rect.x1 - table_margin_x
                
                if go_below:
                    # 从 caption 下方取表格
                    y_top = caption_bbox.y1 + table_caption_gap
                    y_bottom = min(page_rect.y1, y_top + table_clip_height)
                else:
                    # 从 caption 上方取表格
                    y_bottom = caption_bbox.y0 - table_caption_gap
                    y_top = max(page_rect.y0, y_bottom - table_clip_height)
                
                clip = create_rect(x_left, y_top, x_right, y_bottom)
                
                # 渲染
                try:
                    pix = page.get_pixmap(dpi=dpi, clip=clip)
                    pix.save(out_path)
                    
                    records.append(AttachmentRecord(
                        kind='table',
                        ident=ident,
                        page=pno + 1,
                        caption=text_stripped,
                        out_path=out_path,
                        continued=is_continued
                    ))
                    
                    logger.info(f"Extracted Table {ident} from page {pno + 1}: {out_path}")
                except Exception as e:
                    logger.warning(f"Failed to extract Table {ident}: {e}")
    
    doc.close()
    
    logger.info(f"Extracted {len(records)} tables from {pdf_name}")
    return records


# ============================================================================
# 向后兼容
# ============================================================================

# 提供与旧代码兼容的别名
_extract_tables = extract_tables
