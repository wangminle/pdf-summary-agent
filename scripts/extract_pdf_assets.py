#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extract text and figure/table images from a PDF.

这是兼容导出层（Commit 13），提供向后兼容的导入路径。
真正的入口在 scripts/core/extract_pdf_assets.py。

使用方式：
    # 作为脚本运行
    python scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust
    
    # 作为模块导入
    from extract_pdf_assets import main, parse_args, AttachmentRecord

Features:
- Text extraction via PyMuPDF (fitz)
- Figure detection by caption blocks starting with "Figure N"
- Table detection by caption blocks starting with "Table N"
- Parameterized clipping window with margins
- Optional auto-cropping to trim white margins
- Sanitized file names from captions
- JSON index and CSV manifest output

Architecture (V0.3.x):
- scripts/lib/: 模块化组件库
- scripts/core/: 核心入口
- scripts/extract_pdf_assets.py: 兼容导出层（本文件）
- scripts-old/: 旧版完整实现（过渡期保留）
"""

from __future__ import annotations

import sys
import os

# 确保 scripts 目录在 path 中
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

# ============================================================================
# 从 core 导入核心入口
# ============================================================================

from core.extract_pdf_assets import (
    main,
    parse_args,
)

# ============================================================================
# 从 lib 重新导出常用符号（保持向后兼容）
# ============================================================================

from lib.models import (
    AttachmentRecord,
    CaptionCandidate,
    CaptionIndex,
    DocumentLayoutModel,
    GatheredParagraph,
    GatheredText,
    PDFValidationResult,
    QualityIssue,
    TextBlock,
)

from lib.idents import (
    FIGURE_LINE_RE,
    TABLE_LINE_RE,
    extract_figure_ident,
    extract_table_ident,
    sanitize_filename_from_caption,
)

from lib.output import (
    write_index_json,
    write_manifest,
    load_index_json_items,
    prune_unindexed_images,
    get_unique_path,
)

from lib.text_extract import (
    pre_validate_pdf,
    try_extract_text,
    gather_structured_text,
    extract_text_with_format,
)

from lib.caption_detection import (
    build_caption_index,
    select_best_caption,
    find_all_caption_candidates,
    score_caption_candidate,
)

from lib.layout_model import (
    should_enable_layout_driven,
    build_text_blocks,
    detect_columns,
)

from lib.figure_contexts import (
    build_figure_contexts,
)

from lib.pdf_backend import (
    open_pdf,
    PDFDocument,
    PDFPage,
)

from lib.refine import (
    detect_content_bbox_pixels,
    refine_clip_by_objects,
    adaptive_acceptance_thresholds,
    trim_clip_head_by_text,
    trim_clip_head_by_text_v2,
    is_caption_text,
    detect_exact_n_lines_of_text,
    merge_rects,
    build_text_masks_px,
    detect_far_side_text_evidence,
    trim_far_side_text_post_autocrop,
    snap_clip_edges,
    estimate_ink_ratio,
)

from lib.debug_visual import (
    save_debug_visualization,
    draw_rects_on_pix,
)

# ============================================================================
# 模块级导出
# ============================================================================

__all__ = [
    # 核心入口
    "main",
    "parse_args",
    # 数据结构
    "AttachmentRecord",
    "CaptionCandidate",
    "CaptionIndex",
    "DocumentLayoutModel",
    "GatheredParagraph",
    "GatheredText",
    "PDFValidationResult",
    "QualityIssue",
    "TextBlock",
    # 标识符
    "FIGURE_LINE_RE",
    "TABLE_LINE_RE",
    "extract_figure_ident",
    "extract_table_ident",
    "sanitize_filename_from_caption",
    # 输出
    "write_index_json",
    "write_manifest",
    "load_index_json_items",
    "prune_unindexed_images",
    "get_unique_path",
    # 文本提取
    "pre_validate_pdf",
    "try_extract_text",
    "gather_structured_text",
    "extract_text_with_format",
    # Caption 检测
    "build_caption_index",
    "select_best_caption",
    "find_all_caption_candidates",
    "score_caption_candidate",
    # 版式模型
    "should_enable_layout_driven",
    "build_text_blocks",
    "detect_columns",
    # 图表上下文
    "build_figure_contexts",
    # PDF 后端
    "open_pdf",
    "PDFDocument",
    "PDFPage",
    # 精裁
    "detect_content_bbox_pixels",
    "refine_clip_by_objects",
    "adaptive_acceptance_thresholds",
    "trim_clip_head_by_text",
    "trim_clip_head_by_text_v2",
    "is_caption_text",
    "detect_exact_n_lines_of_text",
    "merge_rects",
    "build_text_masks_px",
    "detect_far_side_text_evidence",
    "trim_far_side_text_post_autocrop",
    "snap_clip_edges",
    "estimate_ink_ratio",
    # 调试
    "save_debug_visualization",
    "draw_rects_on_pix",
]

# ============================================================================
# 脚本入口
# ============================================================================

if __name__ == "__main__":
    sys.exit(main())
