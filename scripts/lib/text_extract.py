#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 10: 文本提取

从 extract_pdf_assets.py 抽离的文本提取相关代码。

包含：
- try_extract_text: 尝试提取 PDF 文本
- pre_validate_pdf: PDF 预验证
- gather_structured_text: 结构化文本提取
- build_figure_contexts: 构建图表上下文
- extract_text_with_format: 带格式的文本提取（构建版式模型）
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

# 尝试导入 fitz
try:
    import fitz
except ImportError:
    fitz = None  # type: ignore

# 避免循环导入
if TYPE_CHECKING:
    from .models import (
        AttachmentRecord,
        DocumentLayoutModel,
        EnhancedTextUnit,
        FigureContext,
        FigureMention,
        GatheredParagraph,
        GatheredText,
        PDFValidationResult,
    )

# 模块日志器
logger = logging.getLogger(__name__)


# ============================================================================
# PDF 文本提取
# ============================================================================

def try_extract_text(pdf_path: str, out_text: Optional[str]) -> Optional[str]:
    """
    尝试提取 PDF 的纯文本内容。
    
    Args:
        pdf_path: PDF 文件路径
        out_text: 输出文本文件路径（可选）
    
    Returns:
        输出文件路径，如果提取失败则返回 None
    """
    if fitz is None:
        logger.warning("PyMuPDF not available, cannot extract text")
        return None
    
    try:
        doc = fitz.open(pdf_path)
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text("text"))
        full_text = "\n\n".join(text_parts)
        doc.close()
        
        if out_text:
            out_dir = os.path.dirname(out_text)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(out_text, "w", encoding="utf-8") as f:
                f.write(full_text)
            logger.info(f"Wrote text: {out_text} ({len(full_text)} chars)")
            return out_text
        return None
    except Exception as e:
        logger.warning(f"Text extraction failed: {e}")
        return None


# ============================================================================
# PDF 预验证
# ============================================================================

def pre_validate_pdf(pdf_path: str) -> "PDFValidationResult":
    """
    预验证 PDF 文件，检测潜在问题。
    
    检测内容：
    - 文件是否存在且可读
    - 是否加密
    - 是否有文本层
    - 页数和文件大小
    
    Args:
        pdf_path: PDF 文件路径
    
    Returns:
        PDFValidationResult 对象
    """
    from .models import PDFValidationResult
    
    warnings: List[str] = []
    errors: List[str] = []
    
    if not os.path.exists(pdf_path):
        return PDFValidationResult(
            is_valid=False, page_count=0, has_text_layer=False,
            text_layer_ratio=0.0, is_encrypted=False, pdf_version="",
            file_size_mb=0.0, warnings=[], errors=["File not found"]
        )
    
    file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    
    if fitz is None:
        return PDFValidationResult(
            is_valid=False, page_count=0, has_text_layer=False,
            text_layer_ratio=0.0, is_encrypted=False, pdf_version="",
            file_size_mb=file_size_mb, warnings=[], errors=["PyMuPDF not available"]
        )
    
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return PDFValidationResult(
            is_valid=False, page_count=0, has_text_layer=False,
            text_layer_ratio=0.0, is_encrypted=False, pdf_version="",
            file_size_mb=file_size_mb, warnings=[], errors=[f"Cannot open PDF: {e}"]
        )
    
    try:
        page_count = len(doc)
        is_encrypted = doc.is_encrypted
        
        try:
            pdf_version = doc.metadata.get("format", "unknown") if doc.metadata else "unknown"
        except Exception as e:
            logger.warning(f"Failed to read PDF metadata: {e}", extra={'stage': 'pre_validate_pdf'})
            pdf_version = "unknown"
        
        if is_encrypted:
            try:
                unlock_result = doc.authenticate("")
                if unlock_result:
                    warnings.append("PDF was encrypted but accessible with empty password")
                    is_encrypted = False
                else:
                    try:
                        _ = doc[0].get_text("text")[:100]
                        warnings.append("PDF is marked as encrypted but content is readable")
                    except Exception as e:
                        warnings.append(f"PDF is encrypted; extraction may be incomplete. detail={e}")
            except Exception as e:
                warnings.append(f"PDF is encrypted; extraction may be incomplete. detail={e}")
        
        pages_with_text = 0
        sample_pages = min(10, page_count)
        
        for pno in range(sample_pages):
            try:
                page = doc[pno]
                text = page.get_text("text").strip()
                if len(text) > 50:
                    pages_with_text += 1
            except Exception as e:
                logger.warning(
                    f"Failed to read text layer on page {pno + 1}: {e}",
                    extra={'page': pno + 1, 'stage': 'pre_validate_pdf'}
                )
        
        text_layer_ratio = pages_with_text / sample_pages if sample_pages > 0 else 0.0
        has_text_layer = text_layer_ratio > 0.3
        
        if not has_text_layer:
            warnings.append("PDF may be scanned/image-only (limited text layer detected)")
        
        if page_count > 100:
            warnings.append(f"Large document ({page_count} pages), extraction may be slow")
        
        if file_size_mb > 50:
            warnings.append(f"Large file ({file_size_mb:.1f} MB), processing may be slow")
        
        doc.close()
        
        is_valid = len(errors) == 0
        
        return PDFValidationResult(
            is_valid=is_valid,
            page_count=page_count,
            has_text_layer=has_text_layer,
            text_layer_ratio=text_layer_ratio,
            is_encrypted=is_encrypted,
            pdf_version=pdf_version,
            file_size_mb=file_size_mb,
            warnings=warnings,
            errors=errors
        )
        
    except Exception as e:
        try:
            doc.close()
        except Exception as close_e:
            logger.warning(f"Failed to close PDF after validation error: {close_e}", extra={'stage': 'pre_validate_pdf'})
        return PDFValidationResult(
            is_valid=False, page_count=0, has_text_layer=False,
            text_layer_ratio=0.0, is_encrypted=False, pdf_version="",
            file_size_mb=file_size_mb, warnings=[], errors=[f"Validation error: {e}"]
        )


# ============================================================================
# 结构化文本提取
# ============================================================================

def gather_structured_text(
    pdf_path: str,
    out_json: Optional[str] = None,
    debug: bool = False
) -> "GatheredText":
    """
    结构化文本提取（Gathering 阶段）。
    
    功能：
    1. Header/footer 检测与剔除
    2. 双栏顺序重排
    3. 段落分类（标题/正文/图注/列表）
    
    Args:
        pdf_path: PDF 文件路径
        out_json: 输出 JSON 路径（可选）
        debug: 调试模式
    
    Returns:
        GatheredText 结构化结果
    """
    from .models import GatheredParagraph, GatheredText
    
    if fitz is None:
        return GatheredText(
            version="1.0",
            is_dual_column=False,
            headers_removed=[],
            footers_removed=[],
            paragraphs=[]
        )
    
    doc = fitz.open(pdf_path)
    page_count = len(doc)
    
    all_blocks: List[Dict[str, Any]] = []
    header_candidates: List[str] = []
    footer_candidates: List[str] = []
    
    for pno in range(page_count):
        page = doc[pno]
        page_rect = page.rect
        page_height = page_rect.height
        
        blocks = page.get_text("dict")["blocks"]
        
        for block in blocks:
            if block.get("type") != 0:
                continue
            
            bbox = block.get("bbox", (0, 0, 0, 0))
            lines = block.get("lines", [])
            
            block_text = ""
            for line in lines:
                for span in line.get("spans", []):
                    block_text += span.get("text", "")
                block_text += "\n"
            block_text = block_text.strip()
            
            if not block_text:
                continue
            
            y_center = (bbox[1] + bbox[3]) / 2
            if y_center < page_height * 0.05:
                if len(block_text) < 100:
                    header_candidates.append(block_text)
            elif y_center > page_height * 0.95:
                if len(block_text) < 100:
                    footer_candidates.append(block_text)
            
            all_blocks.append({
                "page": pno + 1,
                "bbox": bbox,
                "text": block_text,
                "lines": lines
            })
    
    header_counter = Counter(header_candidates)
    footer_counter = Counter(footer_candidates)
    
    threshold = max(2, page_count * 0.3)
    headers_to_remove = {text for text, count in header_counter.items() if count >= threshold}
    footers_to_remove = {text for text, count in footer_counter.items() if count >= threshold}
    
    if debug:
        print(f"[DEBUG] Detected headers to remove: {headers_to_remove}")
        print(f"[DEBUG] Detected footers to remove: {footers_to_remove}")
    
    x_centers = [(b["bbox"][0] + b["bbox"][2]) / 2 for b in all_blocks]
    if x_centers:
        page_width = doc[0].rect.width
        left_count = sum(1 for x in x_centers if x < page_width * 0.45)
        right_count = sum(1 for x in x_centers if x > page_width * 0.55)
        is_dual_column = left_count > len(x_centers) * 0.3 and right_count > len(x_centers) * 0.3
    else:
        is_dual_column = False
    
    if debug:
        print(f"[DEBUG] Dual column detected: {is_dual_column}")
    
    paragraphs: List[GatheredParagraph] = []
    
    for block in all_blocks:
        text = block["text"]
        
        if text in headers_to_remove or text in footers_to_remove:
            continue
        
        bbox = block["bbox"]
        page = block["page"]
        
        if is_dual_column:
            page_width = doc[0].rect.width
            x_center = (bbox[0] + bbox[2]) / 2
            column = 0 if x_center < page_width * 0.5 else 1
        else:
            column = 0
        
        lines = block.get("lines", [])
        first_span = lines[0]["spans"][0] if lines and lines[0].get("spans") else {}
        font_size = first_span.get("size", 10)
        font_flags = first_span.get("flags", 0)
        is_bold = bool(font_flags & 2 ** 4)
        
        if len(text) < 50 and is_bold:
            para_type = "heading"
        elif text.lower().startswith(("figure", "fig.", "table", "图", "表")):
            para_type = "caption"
        elif text.startswith(("•", "-", "*", "1.", "2.", "(1)", "(a)")):
            para_type = "list"
        else:
            para_type = "body"
        
        paragraphs.append(GatheredParagraph(
            page=page,
            text=text,
            bbox=bbox,
            is_heading=(para_type == "heading")
        ))
    
    paragraphs.sort(key=lambda p: (p.page, 0 if is_dual_column and (p.bbox[0] + p.bbox[2]) / 2 < doc[0].rect.width * 0.5 else 1, p.bbox[1]))
    
    doc.close()
    
    result = GatheredText(
        version="1.0",
        is_dual_column=is_dual_column,
        headers_removed=list(headers_to_remove),
        footers_removed=list(footers_to_remove),
        paragraphs=paragraphs
    )
    
    if out_json:
        output = {
            "version": result.version,
            "is_dual_column": result.is_dual_column,
            "headers_removed": result.headers_removed,
            "footers_removed": result.footers_removed,
            "paragraphs": [
                {
                    "text": p.text,
                    "page": p.page,
                    "bbox": list(p.bbox),
                    "is_heading": p.is_heading
                }
                for p in result.paragraphs
            ]
        }
        out_dir = os.path.dirname(out_json)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        
        if debug:
            logger.info(f"Wrote gathered text: {out_json} ({len(paragraphs)} paragraphs)")
    
    return result


# ============================================================================
# 图表上下文构建（已迁移到 figure_contexts.py，此处保留向后兼容导入）
# ============================================================================

# Commit 11: build_figure_contexts 已迁移到 figure_contexts.py
# 为保持向后兼容性，从新模块重新导出
from .figure_contexts import build_figure_contexts


# ============================================================================
# 带格式文本提取（版式模型构建）
# ============================================================================

def extract_text_with_format(
    pdf_path: str,
    out_json: Optional[str] = None,
    sample_pages: Optional[int] = None,
    debug: bool = False
) -> "DocumentLayoutModel":
    """
    提取文本并保留完整格式信息，构建版式模型。
    
    Args:
        pdf_path: PDF 文件路径
        out_json: 输出 JSON 路径（可选）
        sample_pages: 采样页数（None 表示全部）
        debug: 调试模式
    
    Returns:
        DocumentLayoutModel 版式模型对象
    """
    from .models import DocumentLayoutModel, EnhancedTextUnit
    from .layout_model import (
        build_text_blocks,
        classify_text_types,
        detect_columns,
        detect_vacant_regions,
    )
    
    if fitz is None:
        raise ImportError("PyMuPDF is required for extract_text_with_format")
    
    if debug:
        print("\n" + "=" * 70)
        print("LAYOUT-DRIVEN EXTRACTION: Building Document Layout Model")
        print("=" * 70)
    
    doc = fitz.open(pdf_path)
    
    page_rect = doc[0].rect
    page_size = (page_rect.width, page_rect.height)
    
    # 统计典型行高和字号
    typical_font_size, typical_line_height, typical_line_gap = _estimate_typical_metrics(doc, debug)
    
    # 统计典型字体名
    font_name_counts = {}
    num_sample_pages = min(5, len(doc))
    for pno in range(num_sample_pages):
        page = doc[pno]
        dict_data = page.get_text("dict")
        for blk in dict_data.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    font_name = sp.get("font", "unknown")
                    font_size = sp.get("size", 0)
                    if 8 <= font_size <= 14:
                        font_name_counts[font_name] = font_name_counts.get(font_name, 0) + 1
    
    if font_name_counts:
        typical_font_name = max(font_name_counts, key=font_name_counts.get)
    else:
        typical_font_name = "Times"
    
    # 提取每页的增强文本单元
    all_units: Dict[int, List[EnhancedTextUnit]] = {}
    num_pages = len(doc) if sample_pages is None else min(sample_pages, len(doc))
    
    for pno in range(num_pages):
        page = doc[pno]
        dict_data = page.get_text("dict")
        
        units = []
        for blk_idx, blk in enumerate(dict_data.get("blocks", [])):
            if blk.get("type") != 0:
                continue
            for ln_idx, ln in enumerate(blk.get("lines", [])):
                spans = ln.get("spans", [])
                if not spans:
                    continue
                
                text = "".join(sp.get("text", "") for sp in spans)
                bbox = fitz.Rect(ln["bbox"])
                
                main_span = max(spans, key=lambda s: len(s.get("text", "")))
                font_name = main_span.get("font", "unknown")
                font_size = main_span.get("size", 10.0)
                font_flags = main_span.get("flags", 0)
                color = main_span.get("color", 0)
                
                font_weight = 'bold' if (font_flags & (1 << 4)) else 'regular'
                
                if isinstance(color, int):
                    color_rgb = ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)
                else:
                    color_rgb = (0, 0, 0)
                
                unit = EnhancedTextUnit(
                    bbox=bbox,
                    text=text,
                    page=pno,
                    font_name=font_name,
                    font_size=font_size,
                    font_weight=font_weight,
                    font_flags=font_flags,
                    color=color_rgb,
                    text_type='unknown',
                    confidence=0.0,
                    column=-1,
                    indent=bbox.x0,
                    block_idx=blk_idx,
                    line_idx=ln_idx
                )
                units.append(unit)
        
        all_units[pno] = units
    
    # 文本类型分类
    all_units = classify_text_types(all_units, typical_font_size, typical_font_name, page_size[0], debug=debug)
    
    # 双栏检测
    num_columns, column_gap, all_units = detect_columns(all_units, page_size[0], debug=debug)
    
    # 构建文本区块
    all_blocks = build_text_blocks(all_units, typical_line_height, debug=debug)
    
    # 识别留白区域
    vacant_regions = detect_vacant_regions(all_blocks, doc, debug=debug)
    
    # 创建版式模型
    layout_model = DocumentLayoutModel(
        page_size=page_size,
        num_columns=num_columns,
        margin_left=page_rect.x0,
        margin_right=page_rect.x1,
        margin_top=page_rect.y0,
        margin_bottom=page_rect.y1,
        column_gap=column_gap,
        typical_font_size=typical_font_size,
        typical_line_height=typical_line_height,
        typical_line_gap=typical_line_gap,
        text_units=all_units,
        text_blocks=all_blocks,
        vacant_regions=vacant_regions
    )
    
    # 保存为 JSON
    if out_json:
        out_dir = os.path.dirname(out_json)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(layout_model.to_dict(), f, indent=2, ensure_ascii=False)
        if debug:
            print(f"\n[INFO] Saved layout model to: {out_json}")
    
    doc.close()
    
    if debug:
        print("\n[SUMMARY] Layout Model Built Successfully")
        print(f"  - Pages analyzed: {num_pages}")
        print(f"  - Total text units: {sum(len(v) for v in all_units.values())}")
        print(f"  - Total text blocks: {sum(len(v) for v in all_blocks.values())}")
        print(f"  - Total vacant regions: {sum(len(v) for v in vacant_regions.values())}")
        print("=" * 70)
    
    return layout_model


def _estimate_typical_metrics(doc: "fitz.Document", debug: bool = False) -> Tuple[float, float, float]:
    """
    估算文档的典型字号、行高和行距。
    
    Args:
        doc: PyMuPDF 文档对象
        debug: 调试模式
    
    Returns:
        (typical_font_size, typical_line_height, typical_line_gap)
    """
    font_sizes = []
    line_heights = []
    
    sample_pages = min(5, len(doc))
    for pno in range(sample_pages):
        page = doc[pno]
        dict_data = page.get_text("dict")
        
        for blk in dict_data.get("blocks", []):
            if blk.get("type") != 0:
                continue
            
            lines = blk.get("lines", [])
            for i, ln in enumerate(lines):
                spans = ln.get("spans", [])
                for sp in spans:
                    fs = sp.get("size", 0)
                    if 6 <= fs <= 20:
                        font_sizes.append(fs)
                
                if i > 0:
                    prev_ln = lines[i - 1]
                    gap = ln["bbox"][1] - prev_ln["bbox"][3]
                    height = ln["bbox"][3] - ln["bbox"][1]
                    if 0 < gap < 30 and 6 < height < 30:
                        line_heights.append(height + gap)
    
    typical_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 10.0
    typical_line_height = sum(line_heights) / len(line_heights) if line_heights else 14.0
    typical_line_gap = max(0.0, typical_line_height - typical_font_size)
    
    if debug:
        print(f"[DEBUG] Estimated metrics: font={typical_font_size:.1f}pt, line_h={typical_line_height:.1f}pt, gap={typical_line_gap:.1f}pt")
    
    return typical_font_size, typical_line_height, typical_line_gap
