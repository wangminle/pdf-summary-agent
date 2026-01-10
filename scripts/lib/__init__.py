#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/lib/ 模块入口

包含所有从主脚本抽离的公共库模块：

已完成的模块：
- env_priority: ENV 优先级与参数处理 (Commit 01)
- pdf_backend: PDF 后端抽象层 (Commit 01B)
- models: 数据结构定义 (Commit 02)
- idents: 标识符与正则表达式 (Commit 03)
- extraction_logger: 日志系统（从旧版迁移）
- output: 输出与索引管理 (Commit 05)
- debug_visual: 调试可视化 (Commit 06)
- refine: 精裁与验收 (Commit 07)
- caption_detection: Caption 检测 (Commit 08)
- layout_model: 版式模型 (Commit 09)
- text_extract: 文本提取 (Commit 10)
- figure_contexts: 图表上下文构建 (Commit 11)
- extract_helpers: 提取辅助函数 (Commit 12)
- extract_figures: Figure 提取主循环 (Commit 12)
- extract_tables: Table 提取主循环 (Commit 12)
"""

# 导出所有公共 API
from .models import (
    AcceptanceThresholds,
    AttachmentRecord,
    CaptionCandidate,
    CaptionIndex,
    DebugStageInfo,
    DocumentLayoutModel,
    DrawItem,
    EnhancedTextUnit,
    FigureContext,
    FigureMention,
    GatheredParagraph,
    GatheredText,
    PDFValidationResult,
    QualityIssue,
    TextBlock,
)

from .idents import (
    FIGURE_LINE_RE,
    TABLE_LINE_RE,
    build_output_basename,
    categorize_idents,
    count_text_references,
    extract_figure_ident,
    extract_table_ident,
    ident_in_range,
    is_roman_numeral,
    parse_figure_ident,
    roman_to_int,
    sanitize_filename_from_caption,
)

from .output import (
    get_run_id,
    get_unique_path,
    load_index_json_items,
    prune_unindexed_images,
    set_run_id,
    write_index_json,
    write_manifest,
)

from .debug_visual import (
    STAGE_COLORS,
    draw_rects_on_pix,
    dump_page_candidates,
    save_debug_visualization,
)

from .refine import (
    adaptive_acceptance_thresholds,
    build_text_masks_px,
    detect_content_bbox_pixels,
    detect_far_side_text_evidence,
    estimate_ink_ratio,
    merge_rects,
    refine_clip_by_objects,
    snap_clip_edges,
    trim_far_side_text_post_autocrop,
)

from .caption_detection import (
    build_caption_index,
    find_all_caption_candidates,
    get_next_line_text,
    get_page_drawings,
    get_page_images,
    get_paragraph_length,
    is_bold_text,
    is_likely_caption_context,
    is_likely_reference_context,
    min_distance_to_rects,
    score_caption_candidate,
    select_best_caption,
)

from .layout_model import (
    adjust_clip_with_layout,
    build_text_blocks,
    classify_text_types,
    detect_columns,
    detect_vacant_regions,
    should_enable_layout_driven,
)

from .text_extract import (
    extract_text_with_format,
    gather_structured_text,
    pre_validate_pdf,
    try_extract_text,
)

from .figure_contexts import (
    build_figure_contexts,
)

from .pdf_backend import (
    PDFDocument,
    PDFPage,
    open_pdf,
    try_extract_tables_with_pdfplumber,
)

from .extract_helpers import (
    DrawItem as ExtractDrawItem,
    collect_draw_items,
    collect_text_lines,
    estimate_document_line_metrics,
    estimate_column_peaks,
    estimate_ink_ratio as helpers_estimate_ink_ratio,
    is_caption_text,
    line_density,
    paragraph_ratio,
    rect_to_list,
)

from .extract_figures import (
    FIGURE_LINE_RE as EXTRACT_FIGURE_LINE_RE,
    extract_figures,
)

from .extract_tables import (
    TABLE_LINE_RE as EXTRACT_TABLE_LINE_RE,
    extract_tables,
)

__all__ = [
    # models
    "AcceptanceThresholds",
    "AttachmentRecord",
    "CaptionCandidate",
    "CaptionIndex",
    "DebugStageInfo",
    "DocumentLayoutModel",
    "DrawItem",
    "EnhancedTextUnit",
    "FigureContext",
    "FigureMention",
    "GatheredParagraph",
    "GatheredText",
    "PDFValidationResult",
    "QualityIssue",
    "TextBlock",
    # idents
    "FIGURE_LINE_RE",
    "TABLE_LINE_RE",
    "build_output_basename",
    "categorize_idents",
    "count_text_references",
    "extract_figure_ident",
    "extract_table_ident",
    "ident_in_range",
    "is_roman_numeral",
    "parse_figure_ident",
    "roman_to_int",
    "sanitize_filename_from_caption",
    # output
    "get_run_id",
    "get_unique_path",
    "load_index_json_items",
    "prune_unindexed_images",
    "set_run_id",
    "write_index_json",
    "write_manifest",
    # debug_visual
    "STAGE_COLORS",
    "draw_rects_on_pix",
    "dump_page_candidates",
    "save_debug_visualization",
    # refine
    "adaptive_acceptance_thresholds",
    "build_text_masks_px",
    "detect_content_bbox_pixels",
    "detect_far_side_text_evidence",
    "estimate_ink_ratio",
    "merge_rects",
    "refine_clip_by_objects",
    "snap_clip_edges",
    "trim_far_side_text_post_autocrop",
    # caption_detection
    "build_caption_index",
    "find_all_caption_candidates",
    "get_next_line_text",
    "get_page_drawings",
    "get_page_images",
    "get_paragraph_length",
    "is_bold_text",
    "is_likely_caption_context",
    "is_likely_reference_context",
    "min_distance_to_rects",
    "score_caption_candidate",
    "select_best_caption",
    # layout_model
    "adjust_clip_with_layout",
    "build_text_blocks",
    "classify_text_types",
    "detect_columns",
    "detect_vacant_regions",
    "should_enable_layout_driven",
    # text_extract
    "extract_text_with_format",
    "gather_structured_text",
    "pre_validate_pdf",
    "try_extract_text",
    # figure_contexts
    "build_figure_contexts",
    # pdf_backend
    "PDFDocument",
    "PDFPage",
    "open_pdf",
    "try_extract_tables_with_pdfplumber",
]
