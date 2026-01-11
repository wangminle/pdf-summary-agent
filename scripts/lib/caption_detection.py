#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 08: Caption 检测

从 extract_pdf_assets.py 抽离的智能 caption 检测相关代码。

包含：
- find_all_caption_candidates: 查找所有 caption 候选项
- score_caption_candidate: 为候选项评分
- select_best_caption: 选择最佳 caption
- build_caption_index: 构建全文 caption 索引
- 辅助函数：get_page_images, get_page_drawings, is_bold_text 等
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from .pdf_backend import PDFDocument, PDFPage, create_rect

# 避免循环导入
if TYPE_CHECKING:
    from .models import CaptionCandidate, CaptionIndex

# 导入标识符提取函数
from .idents import extract_figure_ident, extract_table_ident

# 模块日志器
logger = logging.getLogger(__name__)


# ============================================================================
# 辅助函数
# ============================================================================

def _unwrap_page(page: Union[PDFPage, Any]) -> Any:
    return getattr(page, "raw", page)


def _unwrap_doc(doc: Union[PDFDocument, Any]) -> Any:
    return getattr(doc, "raw", doc)


def get_page_images(page: Union[PDFPage, Any]) -> List[Any]:
    """
    提取页面中所有图像对象的边界框。
    
    Args:
        page: PyMuPDF 页面对象
    
    Returns:
        fitz.Rect 列表
    """
    images: List[Any] = []
    try:
        raw_page = _unwrap_page(page)
        dict_data = raw_page.get_text("dict")
        for blk in dict_data.get("blocks", []):
            if blk.get("type", 0) == 1 and "bbox" in blk:  # type=1 表示图像
                images.append(create_rect(*blk["bbox"]))
    except Exception as e:
        page_no = getattr(_unwrap_page(page), "number", None)
        extra = {'stage': 'get_page_images'}
        if isinstance(page_no, int):
            extra['page'] = page_no + 1
        logger.warning(f"Failed to parse page images: {e}", extra=extra)
    return images


def get_page_drawings(page: Union[PDFPage, Any]) -> List[Any]:
    """
    提取页面中所有绘图对象的边界框。
    
    Args:
        page: PyMuPDF 页面对象
    
    Returns:
        fitz.Rect 列表
    """
    drawings: List[Any] = []
    try:
        raw_page = _unwrap_page(page)
        for dr in raw_page.get_drawings():
            r = dr.get("rect")
            if r:
                drawings.append(r)
    except Exception as e:
        page_no = getattr(_unwrap_page(page), "number", None)
        extra = {'stage': 'get_page_drawings'}
        if isinstance(page_no, int):
            extra['page'] = page_no + 1
        logger.warning(f"Failed to parse page drawings: {e}", extra=extra)
    return drawings


def get_next_line_text(block: Dict, current_line_idx: int) -> str:
    """
    获取当前行的下一行文本。
    
    Args:
        block: 文本块字典
        current_line_idx: 当前行索引
    
    Returns:
        下一行文本
    """
    lines = block.get("lines", [])
    if current_line_idx + 1 < len(lines):
        next_line = lines[current_line_idx + 1]
        text = "".join(sp.get("text", "") for sp in next_line.get("spans", []))
        return text.strip()
    return ""


def get_paragraph_length(block: Dict) -> int:
    """
    计算 block 中所有文本的总长度。
    
    Args:
        block: 文本块字典
    
    Returns:
        文本总长度
    """
    total_len = 0
    for ln in block.get("lines", []):
        for sp in ln.get("spans", []):
            total_len += len(sp.get("text", ""))
    return total_len


def is_bold_text(spans: List[Dict]) -> bool:
    """
    判断文本是否加粗（检查 font flags）。
    
    Font flags bit 4 (value 16) 表示 bold。
    
    Args:
        spans: spans 列表
    
    Returns:
        是否加粗
    """
    return any(sp.get("flags", 0) & 16 for sp in spans)


def min_distance_to_rects(rect: Any, rect_list: List[Any]) -> float:
    """
    计算 rect 到 rect_list 中所有矩形的最小距离。
    
    Args:
        rect: 源矩形
        rect_list: 目标矩形列表
    
    Returns:
        最小距离
    """
    if not rect_list:
        return float('inf')
    
    min_dist = float('inf')
    for r in rect_list:
        dist_above = abs(rect.y0 - r.y1)
        dist_below = abs(rect.y1 - r.y0)
        dist = min(dist_above, dist_below)
        min_dist = min(min_dist, dist)
    
    return min_dist


def is_likely_reference_context(text: str) -> bool:
    """
    判断文本是否像正文引用（而非图注描述）。
    
    Args:
        text: 文本内容
    
    Returns:
        是否像正文引用
    """
    text_lower = text.lower()
    
    reference_patterns = [
        r'as shown in', r'see (figure|table)', r'refer to',
        r'shown in (figure|table)', r'listed in (table)',
        r'如.*所示', r'见.*图', r'参见', r'如.*表.*所示',
        r'according to', r'based on', r'from (figure|table)',
    ]
    
    for pat in reference_patterns:
        if re.search(pat, text_lower):
            return True
    
    return False


def is_likely_caption_context(text: str) -> bool:
    """
    判断文本是否像图注描述（而非正文引用）。
    
    Args:
        text: 文本内容
    
    Returns:
        是否像图注描述
    """
    text_lower = text.lower()
    
    caption_patterns = [
        r'^(figure|table|fig\.|图|表)\s+\d+[:：.]',
        r'shows?', r'illustrates?', r'depicts?', r'displays?',
        r'compares?', r'presents?', r'demonstrates?',
        r'显示', r'展示', r'说明', r'比较', r'给出', r'呈现',
    ]
    
    for pat in caption_patterns:
        if re.search(pat, text_lower):
            return True
    
    return False


# ============================================================================
# Caption 候选项查找
# ============================================================================

def find_all_caption_candidates(
    page: "fitz.Page",
    page_num: int,
    pattern: re.Pattern,
    kind: str = 'figure'
) -> List["CaptionCandidate"]:
    """
    在单页中找到所有匹配 pattern 的候选 caption。
    
    Args:
        page: PyMuPDF 页面对象
        page_num: 页码（0-based）
        pattern: 匹配 caption 的正则表达式
        kind: 'figure' 或 'table'
    
    Returns:
        CaptionCandidate 列表
    """
    from .models import CaptionCandidate
    
    if fitz is None:
        return []
    
    candidates: List[CaptionCandidate] = []
    
    try:
        dict_data = page.get_text("dict")
        
        for blk_idx, blk in enumerate(dict_data.get("blocks", [])):
            if blk.get("type", 0) != 0:  # 只处理文本 block
                continue
            
            for ln_idx, ln in enumerate(blk.get("lines", [])):
                spans = ln.get("spans", [])
                if not spans:
                    continue
                
                text = "".join(sp.get("text", "") for sp in spans)
                text_stripped = text.strip()
                
                match = pattern.match(text_stripped)
                if match:
                    # 根据 kind 提取正确的编号
                    if kind == 'figure':
                        number = extract_figure_ident(match)
                    elif kind == 'table':
                        number = extract_table_ident(match)
                    else:
                        try:
                            number = (match.group(1) or "").strip()
                        except IndexError:
                            number = ""
                    
                    if not number:
                        continue
                    
                    candidate = CaptionCandidate(
                        rect=fitz.Rect(*ln.get("bbox", [0, 0, 0, 0])),
                        text=text_stripped,
                        number=number,
                        kind=kind,
                        page=page_num,
                        block_idx=blk_idx,
                        line_idx=ln_idx,
                        spans=spans,
                        block=blk,
                        score=0.0
                    )
                    candidates.append(candidate)
    
    except Exception as e:
        logger.warning(f"Failed to parse page {page_num + 1} for {kind} captions: {e}")
    
    return candidates


# ============================================================================
# Caption 候选项评分
# ============================================================================

def score_caption_candidate(
    candidate: "CaptionCandidate",
    images: List[Any],
    drawings: List[Any],
    debug: bool = False
) -> float:
    """
    为候选 caption 打分，判断其是真实图注的可能性。
    
    评分维度（总分 100）：
    1. 位置特征（40分）：距离图像/绘图对象的距离
    2. 格式特征（30分）：字体加粗、独立成段、后续标点
    3. 结构特征（20分）：下一行有描述、段落长度
    4. 上下文特征（10分）：语义分析
    
    Args:
        candidate: 候选项
        images: 页面中所有图像对象
        drawings: 页面中所有绘图对象
        debug: 是否输出调试信息
    
    Returns:
        得分（0-100+）
    """
    score = 0.0
    details = {}
    
    # === 1. 位置特征（40分）===
    all_objects = images + drawings
    min_dist = min_distance_to_rects(candidate.rect, all_objects)
    
    if min_dist < 10:
        position_score = 40.0
    elif min_dist < 20:
        position_score = 35.0
    elif min_dist < 40:
        position_score = 28.0
    elif min_dist < 80:
        position_score = 18.0
    elif min_dist < 150:
        position_score = 8.0
    elif min_dist < float('inf'):
        position_score = max(0, 5.0 - min_dist / 50.0)
    else:
        position_score = 15.0
    
    score += position_score
    details['position'] = position_score
    details['min_dist'] = min_dist
    
    # === 2. 格式特征（30分）===
    format_score = 0.0
    
    if is_bold_text(candidate.spans):
        format_score += 15.0
        details['bold'] = True
    else:
        details['bold'] = False
    
    num_lines = len(candidate.block.get('lines', []))
    if num_lines == 1:
        format_score += 10.0
        details['lines'] = 1
    elif num_lines == 2:
        format_score += 8.0
        details['lines'] = 2
    elif num_lines <= 4:
        format_score += 5.0
        details['lines'] = num_lines
    else:
        format_score += 0.0
        details['lines'] = num_lines
    
    text_prefix = candidate.text[:40]
    if ':' in text_prefix or '：' in text_prefix:
        format_score += 5.0
        details['punctuation'] = 'colon'
    elif '.' in text_prefix and not text_prefix.endswith('et al.'):
        format_score += 3.0
        details['punctuation'] = 'period'
    elif '—' in text_prefix or '-' in text_prefix:
        format_score += 2.0
        details['punctuation'] = 'dash'
    else:
        details['punctuation'] = 'none'
    
    score += format_score
    details['format'] = format_score
    
    # === 3. 结构特征（20分）===
    structure_score = 0.0
    
    next_line_text = get_next_line_text(candidate.block, candidate.line_idx)
    if next_line_text:
        next_len = len(next_line_text)
        if next_len > 40:
            structure_score += 12.0
            details['next_line_len'] = next_len
        elif next_len > 15:
            structure_score += 8.0
            details['next_line_len'] = next_len
        else:
            structure_score += 3.0
            details['next_line_len'] = next_len
    else:
        details['next_line_len'] = 0
    
    para_length = get_paragraph_length(candidate.block)
    if para_length < 150:
        structure_score += 8.0
        details['para_length'] = para_length
    elif para_length < 300:
        structure_score += 4.0
        details['para_length'] = para_length
    elif para_length < 600:
        structure_score += 0.0
        details['para_length'] = para_length
    else:
        structure_score -= 8.0
        details['para_length'] = para_length
    
    score += structure_score
    details['structure'] = structure_score
    
    # === 4. 上下文特征（10分）===
    context_score = 0.0
    
    if is_likely_caption_context(candidate.text):
        context_score += 10.0
        details['context'] = 'caption'
    elif is_likely_reference_context(candidate.text):
        context_score -= 15.0
        details['context'] = 'reference'
    else:
        context_score += 0.0
        details['context'] = 'neutral'
    
    score += context_score
    details['context_score'] = context_score
    
    # === 总分 ===
    details['total'] = score
    
    if debug:
        print(f"\n=== Caption Scoring Debug ===")
        print(f"Candidate: {candidate.kind} {candidate.number} at page {candidate.page + 1}")
        print(f"Text: {candidate.text[:60].encode('utf-8', errors='replace').decode('utf-8')}...")
        print(f"Position score: {position_score:.1f} (min_dist={min_dist:.1f})")
        print(f"Format score: {format_score:.1f} (bold={details['bold']}, lines={details['lines']}, punct={details['punctuation']})")
        print(f"Structure score: {structure_score:.1f} (next_line={details['next_line_len']}, para={details['para_length']})")
        print(f"Context score: {context_score:.1f} ({details['context']})")
        print(f"Total score: {score:.1f}")
    
    return score


# ============================================================================
# 选择最佳 Caption
# ============================================================================

def select_best_caption(
    candidates: List["CaptionCandidate"],
    page: Union[PDFPage, Any],
    *,
    doc: Optional[Union[PDFDocument, Any]] = None,
    min_score_threshold: float = 25.0,
    debug: bool = False
) -> Optional["CaptionCandidate"]:
    """
    从候选列表中选择得分最高的真实图注。
    
    Args:
        candidates: 候选列表
        page: 页面对象
        doc: 文档对象（用于获取其他页面的图像/绘图对象）
        min_score_threshold: 最低得分阈值
        debug: 是否输出调试信息
    
    Returns:
        得分最高的候选项，如果没有合格候选则返回 None
    """
    if not candidates:
        return None
    
    scored_candidates: List[Tuple[float, "CaptionCandidate"]] = []
    
    for cand in candidates:
        score_page = page
        if doc is not None:
            try:
                raw_doc = _unwrap_doc(doc)
                score_page = raw_doc[cand.page]
            except Exception as e:
                logger.warning(
                    f"Failed to access page {cand.page + 1} for caption scoring: {e}",
                    extra={'page': cand.page + 1, 'stage': 'select_best_caption'}
                )
                score_page = page
        
        images = get_page_images(score_page)
        drawings = get_page_drawings(score_page)
        score = score_caption_candidate(cand, images, drawings, debug=debug)
        cand.score = score
        scored_candidates.append((score, cand))
    
    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    
    if debug:
        print(f"\n=== All Candidates for {candidates[0].kind} {candidates[0].number} ===")
        for score, cand in scored_candidates:
            print(f"  Score {score:5.1f}: page {cand.page + 1}, y={cand.rect.y0:.1f}, text='{cand.text[:50]}...'")
    
    best_score, best_candidate = scored_candidates[0]
    
    if best_score < min_score_threshold:
        if debug:
            print(f"  >>> Best score {best_score:.1f} is below threshold {min_score_threshold}, rejecting all candidates")
        return None
    
    if debug:
        print(f"  >>> Selected: page {best_candidate.page + 1}, score {best_score:.1f}")
    
    return best_candidate


# ============================================================================
# 构建 Caption 索引
# ============================================================================

def build_caption_index(
    doc: Union[PDFDocument, Any],
    figure_pattern: Optional[re.Pattern] = None,
    table_pattern: Optional[re.Pattern] = None,
    debug: bool = False
) -> "CaptionIndex":
    """
    预扫描全文，建立 caption 索引。
    
    Args:
        doc: PyMuPDF 文档对象
        figure_pattern: Figure caption 匹配正则（可选）
        table_pattern: Table caption 匹配正则（可选）
        debug: 是否输出调试信息
    
    Returns:
        CaptionIndex 对象
    """
    from .models import CaptionIndex
    
    # 默认 Figure 正则
    if figure_pattern is None:
        figure_pattern = re.compile(
            r"^\s*(?P<label>Extended\s+Data\s+Figure|Supplementary\s+(?:Figure|Fig\.?)|Figure|Fig\.?|图表|附图|图)\s*"
            r"(?:(?P<s_prefix>S)\s*(?P<s_id>(?:\d+|[IVX]{1,6}))|(?P<roman>[IVX]{1,6})|(?P<num>\d+))"
            r"(?:\s*[-–]?\s*[A-Za-z]|\s*\([A-Za-z]\))?"
            r"(?:\s*\(continued\)|\s*续|\s*接上页)?",
            re.IGNORECASE
        )
    
    # 默认 Table 正则
    if table_pattern is None:
        table_pattern = re.compile(
            r"^\s*(?:Extended\s+Data\s+Table|Supplementary\s+Table|Table|Tab\.?|表)\s*"
            r"(?:"
            r"(S?\d+|[A-Z]\d+)|"
            r"([IVX]{1,6})|"
            r"(\d+)"
            r")"
            r"(?:\s*\(continued\)|\s*续|\s*接上页)?",
            re.IGNORECASE
        )
    
    all_candidates: Dict[str, List["CaptionCandidate"]] = {}
    
    raw_doc = _unwrap_doc(doc)
    for pno in range(len(raw_doc)):
        page = raw_doc[pno]
        
        # 查找 Figure candidates
        if figure_pattern is not None:
            figure_cands = find_all_caption_candidates(page, pno, figure_pattern, 'figure')
            for cand in figure_cands:
                key = f"figure_{cand.number}"
                if key not in all_candidates:
                    all_candidates[key] = []
                all_candidates[key].append(cand)
        
        # 查找 Table candidates
        if table_pattern is not None:
            table_cands = find_all_caption_candidates(page, pno, table_pattern, 'table')
            for cand in table_cands:
                key = f"table_{cand.number}"
                if key not in all_candidates:
                    all_candidates[key] = []
                all_candidates[key].append(cand)
    
    if debug:
        print(f"\n=== Caption Index Built ===")
        print(f"Total keys: {len(all_candidates)}")
        for key, cands in sorted(all_candidates.items()):
            print(f"  {key}: {len(cands)} candidates")
    
    return CaptionIndex(candidates=all_candidates)


# ============================================================================
# 向后兼容别名
# ============================================================================

# 这些函数可以直接从原脚本中调用
_extract_figure_ident = extract_figure_ident
_extract_table_ident = extract_table_ident
