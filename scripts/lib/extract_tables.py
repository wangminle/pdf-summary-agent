#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 12+: Table 提取主循环

从 extract_pdf_assets.py 抽离的 Table 提取逻辑。

V0.4.0 更新：集成完整的 Phase A/B/C/D 文本裁切与验收逻辑

这个模块提供 extract_tables() 函数，用于从 PDF 中提取 Table 图像。

主要处理流程：
1. 预扫描建立 Caption 索引（智能 Caption 检测）
2. 自适应参数计算（基于文档行高）
3. 全局锚点方向判定（表格通常在 caption 下方）
4. 逐页扫描 Table Caption
5. 裁剪窗口计算与精裁（Phase A/B/C/D）
6. 验收检查与回退机制
7. 渲染与保存
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Set, Tuple

from .pdf_backend import create_rect, open_pdf

# 导入本地模块
from .models import AttachmentRecord, CaptionIndex, DocumentLayoutModel
from .idents import build_output_basename, extract_table_ident
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
    estimate_ink_ratio,
)
from .extract_helpers import (
    collect_draw_items,
    collect_text_lines,
    estimate_document_line_metrics,
    estimate_column_peaks,
    line_density,
    DrawItem,
)
from .direction import compute_global_anchor, determine_direction
from .layout_model import adjust_clip_with_layout
from .debug_visual import save_debug_visualization
from .models import DebugStageInfo
from .output import get_unique_path

# 避免循环导入
if TYPE_CHECKING:
    import fitz

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
    no_refine_tables: Optional[List[str]] = None,
    # Smart caption detection
    smart_caption_detection: bool = True,
    debug_captions: bool = False,
    # Visual debug mode
    debug_visual: bool = False,
    # Adaptive line height
    adaptive_line_height: bool = True,
    # Layout model (V2 Architecture)
    layout_model: Optional[DocumentLayoutModel] = None,
    # Global anchor
    global_anchor_table: Optional[str] = None,
    global_anchor_table_margin: float = 0.03,
) -> List[AttachmentRecord]:
    """
    从 PDF 中提取 Table 图像。
    
    这是一个复杂的函数，包含多个阶段的处理：
    1. 预扫描建立 Caption 索引（智能 Caption 检测）
    2. 自适应参数计算（基于文档行高）
    3. 全局锚点方向判定（表格通常在 caption 下方）
    4. 逐页扫描 Table Caption
    5. 裁剪窗口计算与精裁（Phase A/B/C/D）
    6. 验收检查与回退机制
    7. 渲染与保存
    
    Args:
        pdf_path: PDF 文件路径
        out_dir: 输出目录
        dpi: 渲染分辨率
        table_clip_height: 裁剪窗口高度（pt）
        table_margin_x: 水平边距（pt）
        table_caption_gap: Caption 与表格之间的间隙（pt）
        t_below: 强制从 caption 下方取表格的 Table 列表
        t_above: 强制从 caption 上方取表格的 Table 列表
        text_trim: 是否启用文本裁切
        autocrop: 是否启用白边自动裁切（Phase D）
        ... (更多参数见函数签名)
    
    Returns:
        AttachmentRecord 列表，记录提取的每个 Table
    """
    # 基础实现框架
    pdf_name = os.path.basename(pdf_path)
    doc = open_pdf(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    
    records: List[AttachmentRecord] = []
    seen_counts: Dict[str, int] = {}
    
    # 处理方向覆盖参数
    t_below_set: Set[str] = set([str(x).strip() for x in (t_below or []) if str(x).strip()])
    t_above_set: Set[str] = set([str(x).strip() for x in (t_above or []) if str(x).strip()])
    no_refine_set: Set[str] = set([str(x).strip() for x in (no_refine_tables or []) if str(x).strip()])
    
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
    typical_line_h: Optional[float] = None
    if adaptive_line_height:
        line_metrics = estimate_document_line_metrics(doc, sample_pages=5, debug=debug_captions)
        typical_line_h = line_metrics['typical_line_height']
        
        # 自适应参数计算（表格使用不同的默认值）
        if adjacent_th == 28.0:
            adjacent_th = 2.0 * typical_line_h
        if far_text_th == 300.0:
            far_text_th = 15.0 * typical_line_h
        if text_trim_gap == 6.0:
            text_trim_gap = 0.5 * typical_line_h
        if far_side_min_dist == 50.0:
            far_side_min_dist = 3.0 * typical_line_h
    
    # Global Anchor: 预扫描计算全局方向（表格）
    effective_global_anchor: Optional[str] = global_anchor_table
    if global_anchor_table == 'auto' or global_anchor_table is None:
        effective_global_anchor = compute_global_anchor(
            doc, TABLE_LINE_RE,
            clip_height=table_clip_height,
            margin_x=table_margin_x,
            caption_gap=table_caption_gap,
            margin=global_anchor_table_margin,
            is_table=True,
            debug=debug_captions,
        )
        if debug_captions and effective_global_anchor:
            print(f"[GLOBAL_ANCHOR_TABLE] Computed: {effective_global_anchor}")
    
    scale = dpi / 72.0  # pt to px 转换比例
    
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
            # 表格检测：水平/垂直线条
            if item.orient in ('H', 'V'):
                vector_rects.append(item.rect)
            elif item.orient == 'O':
                vector_rects.append(item.rect)
        
        # 从 dict_data 收集图像
        for blk in dict_data.get("blocks", []):
            if blk.get("type") == 1:  # 图像块
                bbox = blk.get("bbox")
                if bbox:
                    image_rects.append(create_rect(*bbox))
        
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
                basename = build_output_basename(
                    "table",
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
                # 方向判定：表格通常在 caption 下方
                # ================================================================
                direction = determine_direction(
                    caption_bbox,
                    page_rect,
                    ident,
                    global_anchor=effective_global_anchor,
                    forced_below=t_below_set,
                    forced_above=t_above_set,
                    is_table=True,
                    page_position_heuristic=True,
                )
                
                # ================================================================
                # 计算基础裁剪窗口 (Baseline)
                # ================================================================
                x_left = page_rect.x0 + table_margin_x
                x_right = page_rect.x1 - table_margin_x
                
                if direction == 'below':
                    # 表格在 caption 下方
                    y_top = caption_bbox.y1 + table_caption_gap
                    y_bottom = min(page_rect.y1, y_top + table_clip_height)
                else:
                    # 表格在 caption 上方
                    y_bottom = caption_bbox.y0 - table_caption_gap
                    y_top = max(page_rect.y0, y_bottom - table_clip_height)
                
                base_clip = create_rect(x_left, y_top, x_right, y_bottom)
                clip = create_rect(x_left, y_top, x_right, y_bottom)  # 工作副本
                
                # ================================================================
                # Phase A: 文本裁切（表格模式：启用 skip_adjacent_sweep 保护表头）
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
                        skip_adjacent_sweep=True,  # 表格模式：跳过相邻扫描，保护表头
                        debug=debug_captions,
                    )
                
                clip_after_A = create_rect(clip.x0, clip.y0, clip.x1, clip.y1)
                
                # ================================================================
                # Phase B: 对象对齐（表格使用不同的参数）
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
                        use_horizontal_union=True,  # 表格可能有并排列
                    )
                
                clip_after_B = create_rect(clip.x0, clip.y0, clip.x1, clip.y1)
                
                # ================================================================
                # 版式驱动裁剪（如果提供了 layout_model）
                # ================================================================
                if layout_model is not None and ident not in no_refine_set:
                    clip = adjust_clip_with_layout(
                        clip,
                        caption_bbox,
                        layout_model,
                        pno,
                        direction,
                        debug=debug_captions,
                    )
                
                # ================================================================
                # Phase D: Autocrop（白边自动裁切）
                # ================================================================
                final_clip = clip
                
                if autocrop and ident not in no_refine_set:
                    try:
                        # 先渲染一版用于分析
                        pix_for_analysis = page.get_pixmap(dpi=dpi, clip=clip)
                        
                        # 构建文本遮罩（可选）
                        mask_rects_px: Optional[List[Tuple[int, int, int, int]]] = None
                        if autocrop_mask_text:
                            mask_rects_px = build_text_masks_px(
                                clip, text_lines,
                                scale=scale,
                                direction=direction,
                                near_frac=mask_top_frac,
                                width_ratio=mask_width_ratio,
                                font_max=mask_font_max,
                                mask_mode='auto',
                            )
                        
                        # 检测内容边界框
                        content_bbox_px = detect_content_bbox_pixels(
                            pix_for_analysis,
                            white_threshold=autocrop_white_threshold,
                            pad=autocrop_pad_px,
                            mask_rects_px=mask_rects_px,
                        )
                        
                        # 转换像素坐标回 pt 坐标
                        cx0_px, cy0_px, cx1_px, cy1_px = content_bbox_px
                        new_x0 = clip.x0 + cx0_px / scale
                        new_y0 = clip.y0 + cy0_px / scale
                        new_x1 = clip.x0 + cx1_px / scale
                        new_y1 = clip.y0 + cy1_px / scale
                        
                        autocrop_clip = create_rect(new_x0, new_y0, new_x1, new_y1)
                        
                        # 单调性约束：检测远端文本证据
                        has_far_evidence, far_limit = detect_far_side_text_evidence(
                            clip, text_lines, direction,
                            edge_zone=40.0,
                            min_width_ratio=0.30,
                        )
                        
                        if has_far_evidence:
                            if direction == 'below':
                                # 表格在下方，远端在底部，不应向下扩展
                                autocrop_clip = create_rect(
                                    autocrop_clip.x0,
                                    autocrop_clip.y0,
                                    autocrop_clip.x1,
                                    min(autocrop_clip.y1, far_limit)
                                )
                            else:
                                # 表格在上方，远端在顶部，不应向上扩展
                                autocrop_clip = create_rect(
                                    autocrop_clip.x0,
                                    max(autocrop_clip.y0, far_limit),
                                    autocrop_clip.x1,
                                    autocrop_clip.y1
                                )
                        
                        # Phase D 后处理：扫描并移除远端正文
                        autocrop_clip, _ = trim_far_side_text_post_autocrop(
                            autocrop_clip, text_lines, direction,
                            typical_line_h=typical_line_h,
                            scan_lines=3,
                        )
                        
                        # 验收检查：确保 autocrop 没有过度裁切
                        autocrop_h = autocrop_clip.height
                        base_h = base_clip.height
                        min_h_px = autocrop_min_height_px / scale
                        
                        if autocrop_h >= min_h_px and autocrop_h >= base_h * autocrop_shrink_limit:
                            final_clip = autocrop_clip
                        else:
                            logger.debug(f"Table {ident}: autocrop rejected (h={autocrop_h:.1f} < {base_h * autocrop_shrink_limit:.1f})")
                    except Exception as e:
                        logger.warning(f"Table {ident}: autocrop failed: {e}")
                
                # ================================================================
                # 验收检查与回退机制
                # ================================================================
                if refine_safe and ident not in no_refine_set:
                    # 计算验收阈值（表格使用不同阈值）
                    thresholds = adaptive_acceptance_thresholds(
                        base_clip.height,
                        is_table=True,
                        far_cov=0.0,  # 可扩展：计算实际远侧覆盖率
                    )
                    
                    # 检查高度比
                    height_ratio = final_clip.height / max(1.0, base_clip.height)
                    area_ratio = (final_clip.width * final_clip.height) / max(1.0, base_clip.width * base_clip.height)
                    
                    accepted = True
                    fallback_reason = ""
                    
                    if height_ratio < thresholds.height_ratio:
                        accepted = False
                        fallback_reason = f"height_ratio={height_ratio:.3f} < {thresholds.height_ratio:.3f}"
                    elif area_ratio < thresholds.area_ratio:
                        accepted = False
                        fallback_reason = f"area_ratio={area_ratio:.3f} < {thresholds.area_ratio:.3f}"
                    
                    if not accepted:
                        logger.info(f"Table {ident}: refined clip rejected ({fallback_reason}), falling back")
                        # 多级回退：先尝试 Phase A only，再回退到 baseline
                        if clip_after_A.height >= base_clip.height * thresholds.height_ratio:
                            final_clip = clip_after_A
                            logger.debug(f"Table {ident}: using Phase A clip")
                        else:
                            final_clip = base_clip
                            logger.debug(f"Table {ident}: using baseline clip")
                
                # ================================================================
                # Debug 可视化（如果启用）
                # ================================================================
                if debug_visual:
                    try:
                        stages: List[DebugStageInfo] = [
                            DebugStageInfo(stage='baseline', rect=base_clip),
                            DebugStageInfo(stage='phase_a', rect=clip_after_A),
                            DebugStageInfo(stage='phase_b', rect=clip_after_B),
                            DebugStageInfo(stage='phase_d' if autocrop else 'final', rect=final_clip),
                        ]
                        # 解析 ident 为数字（处理 S1, A1 等格式）
                        try:
                            fig_no = int(ident)
                        except ValueError:
                            fig_no = hash(ident) % 1000
                        
                        save_debug_visualization(
                            page,
                            out_dir,
                            fig_no,
                            pno + 1,
                            stages=stages,
                            caption_rect=caption_bbox,
                            kind='table',
                            layout_model=layout_model,
                        )
                    except Exception as e:
                        logger.debug(f"Failed to save debug visualization for Table {ident}: {e}")
                
                # ================================================================
                # 渲染与保存
                # ================================================================
                try:
                    pix = page.get_pixmap(dpi=dpi, clip=final_clip)
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
