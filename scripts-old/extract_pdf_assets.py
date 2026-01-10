#!/usr/bin/env python3
# ----------------------------------------
# 脚本中文说明
#
# 目标：
# - 从 PDF 中提取正文文本与图像（基于图注定位），并将图像导出为 PNG；
# - 可选写出 TXT 文本与 CSV 清单。
#
# 方法概述：
# - 文本提取：使用 PyMuPDF（fitz）导出纯文本（UTF-8）。
# - 图像/表格提取：扫描页面 text dict，定位以 “Figure N/图 N” 与 “Table N/表 N” 开头的图/表注行块；
#   在图注“上方”与“下方”分别构造候选裁剪窗口，通过简易评分（墨迹密度+对象占比）
#   或用户显式指定（--below）确定最终窗口，并按 DPI 渲染为 PNG；
#   可选启用像素级自动去白边（--autocrop）。
# - 文件命名：基于图注字符进行清洗规范化并限制长度，避免非法字符与过长路径。
# - 清单：可输出包含 图号/页码/原始图注/文件路径 的 CSV。
#
# 适配与注意：
# - 若论文图注在图的上/下方或跨页，需通过 --clip-height/--margin-x/--caption-gap 或
#   --below 精调；必要时开启 --autocrop 与 --autocrop-pad。
# - 仅添加注释，不改变任何代码与逻辑。
# ----------------------------------------
"""
Extract text and figure/table images from a PDF.

Features
- Text extraction via PyMuPDF (fitz)
- Figure detection by caption blocks starting with "Figure N"
- Table detection by caption blocks starting with "Table N"
- Parameterized clipping window above caption with margins
- Optional auto-cropping to trim white margins from rendered images
- Sanitized file names from captions with length limit
- Manifest (CSV) summarizing extracted figures

Usage
  python scripts/extract_pdf_assets.py \
    --pdf DeepSeek_V3_2.pdf \
    --out-text DeepSeek_V3_2.txt \
    --out-dir images \
    --dpi 300 --clip-height 600 --margin-x 20 --caption-gap 6 \
    # 默认不执行去白边；如需启用：
    --autocrop --autocrop-pad 30
  # Extract tables too (default ON). Table-specific controls:
    --include-tables --table-clip-height 520 --table-margin-x 26 --table-caption-gap 6 
    --t-above 1,3 --t-below S1 --table-autocrop --table-autocrop-pad 20 --no-table-mask-text

Notes
- Auto-cropping trims uniform white margins in the rendered bitmap; it helps when the
  initial heuristic window is larger than the figure area.
- If captions are above figures or span multiple pages, this simple heuristic may fail.
  Adjust --clip-height / --margin-x, or disable autocrop and tune manually.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Iterable, Any

# QA-02: 导入统一日志模块
# 支持多种运行方式：从项目根目录运行、从 scripts 目录运行、直接运行本文件
import sys as _sys
import os as _os
_scripts_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)

try:
    from lib.extraction_logger import (
        configure_logging, get_logger, get_run_id, log_event,
        log_warning, log_error, ExtractionError, FatalExtractionError,
    )
    _HAS_EXTRACTION_LOGGER = True
except ImportError:
    try:
        # 回退：兼容旧的导入路径
        from extraction_logger import (
            configure_logging, get_logger, get_run_id, log_event,
            log_warning, log_error, ExtractionError, FatalExtractionError,
        )
        _HAS_EXTRACTION_LOGGER = True
    except ImportError:
        # 回退：如果日志模块不可用，使用简单的 logging
        _HAS_EXTRACTION_LOGGER = False
        
        # Bug-2 修复：添加缓存机制，确保同一次运行使用相同的 run_id
        _fallback_run_id: Optional[str] = None
        
        def configure_logging(level="INFO", **kwargs):
            logging.basicConfig(
                level=getattr(logging, level.upper(), logging.INFO),
                format="[%(levelname)s] %(message)s"
            )
            # 配置时生成并缓存 run_id
            global _fallback_run_id
            _fallback_run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            return _fallback_run_id
        
        def get_logger(name="extract_pdf_assets"):
            return logging.getLogger(name)
        
        def get_run_id():
            # Bug-2 修复：使用缓存的 run_id，确保一致性
            global _fallback_run_id
            if _fallback_run_id is None:
                _fallback_run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            return _fallback_run_id
        
        def log_event(*args, **kwargs):
            pass
        
        def log_warning(logger, msg, **kwargs):
            logger.warning(msg)
        
        def log_error(logger, msg, **kwargs):
            logger.error(msg)
        
        class ExtractionError(Exception):
            pass
        
        class FatalExtractionError(Exception):
            pass

# 全局 logger（在 main() 中配置后使用）
logger = get_logger("extract_pdf_assets")

# 运行时版本检查：优先建议 Python 3.12+；在 3.10/3.11 上降级运行（给出警告，但不退出）
if sys.version_info < (3, 10):  # pragma: no cover
    print(f"[ERROR] Python 3.10+ is required; found {sys.version.split()[0]}", file=sys.stderr)
    raise SystemExit(3)
elif sys.version_info < (3, 12):  # pragma: no cover
    print(f"[WARN] Python 3.12+ is recommended; running with {sys.version.split()[0]}", file=sys.stderr)

# 依赖检查：PyMuPDF 是渲染与页面结构读取的核心依赖
try:
    import fitz  # PyMuPDF
except Exception as e:  # pragma: no cover
    print("[ERROR] PyMuPDF (pymupdf) is required: pip install pymupdf", file=sys.stderr)
    raise

def _rect_to_list(r: "fitz.Rect") -> List[float]:
    return [round(float(r.x0), 1), round(float(r.y0), 1), round(float(r.x1), 1), round(float(r.y1), 1)]

# 文本提取：若提供 out_text 路径，则将 PDF 全文提取为 UTF-8 文本文件（使用 PyMuPDF）。
# 返回写入路径或 None（未提取/失败）。
def try_extract_text(pdf_path: str, out_text: Optional[str]) -> Optional[str]:
    if out_text is None:
        # 未指定输出路径：直接跳过文本提取
        return None
    try:
        doc = fitz.open(pdf_path)
        try:
            pages = []
            for pno in range(len(doc)):
                pages.append(doc[pno].get_text("text"))
            txt = "\n\n".join(pages)
        finally:
            doc.close()
        with open(out_text, "w", encoding="utf-8") as f:
            f.write(txt)
        logger.info(f"Wrote text: {out_text} (chars={len(txt)})")
        return out_text
    except Exception as e:
        logger.warning(f"Text extraction failed: {e}")
        return None


# P1-03: PDF 预验证函数
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
    warnings: List[str] = []
    errors: List[str] = []
    
    # 检查文件存在性和大小
    if not os.path.exists(pdf_path):
        return PDFValidationResult(
            is_valid=False, page_count=0, has_text_layer=False,
            text_layer_ratio=0.0, is_encrypted=False, pdf_version="",
            file_size_mb=0.0, warnings=[], errors=["File not found"]
        )
    
    file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    
    # 尝试打开 PDF
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
        
        # 获取 PDF 版本
        try:
            pdf_version = doc.metadata.get("format", "unknown") if doc.metadata else "unknown"
        except Exception as e:
            logger.warning(f"Failed to read PDF metadata: {e}", extra={'stage': 'pre_validate_pdf'})
            pdf_version = "unknown"
        
        # 检测加密：尝试空密码解锁，很多"加密"PDF实际可用空密码打开
        if is_encrypted:
            # 尝试用空密码解锁
            try:
                unlock_result = doc.authenticate("")  # 空密码
                if unlock_result:
                    # 成功解锁，仅作为 warning
                    warnings.append("PDF was encrypted but accessible with empty password")
                    is_encrypted = False  # 标记为已解锁
                else:
                    # 空密码无效，但检查是否仍可读取内容
                    try:
                        _ = doc[0].get_text("text")[:100]  # 尝试读取首页部分内容
                        warnings.append("PDF is marked as encrypted but content is readable")
                    except Exception as e:
                        warnings.append(f"PDF is encrypted; extraction may be incomplete (consider providing password). detail={e}")
            except Exception as e:
                warnings.append(f"PDF is encrypted; extraction may be incomplete. detail={e}")
        
        # 检测文本层
        pages_with_text = 0
        sample_pages = min(10, page_count)  # 采样检测
        
        for pno in range(sample_pages):
            try:
                page = doc[pno]
                text = page.get_text("text").strip()
                if len(text) > 50:  # 至少 50 个字符才算有文本
                    pages_with_text += 1
            except Exception as e:
                logger.warning(
                    f"Failed to read text layer on page {pno + 1}: {e}",
                    extra={'page': pno + 1, 'stage': 'pre_validate_pdf'}
                )
        
        text_layer_ratio = pages_with_text / sample_pages if sample_pages > 0 else 0.0
        has_text_layer = text_layer_ratio > 0.3  # 超过 30% 页面有文本
        
        # 生成警告
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
# QA-06: QC 罗马编号与特殊格式覆盖增强
# ============================================================================

# QC 引用检测正则（与主提取正则对齐，支持：罗马数字/S前缀/Extended Data/中文/附录表）
# 注意：
# - 支持 Figure/Fig./Figs. 等多种形式
# - 英文正则使用 \b 边界避免 "figures in" 被误匹配为 "figures i"
# - 中文正则单独处理（不使用 \b）
_QC_FIGURE_REF_EN_RE = re.compile(
    r"(?:Extended\s+Data\s+)?(?:Supplementary\s+)?(?:Figures?|Figs?\.?)\s*"
    r"(?:S\s*)?(\d+|[IVX]{1,6})\b",
    re.IGNORECASE,
)

_QC_FIGURE_REF_CN_RE = re.compile(
    r"图\s*(\d+)",
)

_QC_TABLE_REF_EN_RE = re.compile(
    r"(?:Extended\s+Data\s+)?(?:Supplementary\s+)?(?:Tables?|Tab\.?)\s*"
    r"(?:S\s*)?([A-Z]?\d+|[IVX]{1,6})\b",
    re.IGNORECASE,
)

_QC_TABLE_REF_CN_RE = re.compile(
    r"表\s*(\d+)",
)

# 用于检测 S 前缀的辅助正则
_QC_S_PREFIX_RE = re.compile(r"\bS\s*(\d+|[IVX]{1,6})", re.IGNORECASE)


def _qc_is_roman_numeral(s: str) -> bool:
    """判断字符串是否为罗马数字（I~XX范围）"""
    if not s:
        return False
    return bool(re.fullmatch(r"[IVX]{1,6}", s.upper()))


def count_text_references(text: str) -> Dict[str, Set[str]]:
    """
    QA-06: 统计正文中的图表引用
    
    支持的格式：
    - Figure 1, Fig. 2, Fig 3（常规数字）
    - Figure I, Table II（罗马数字）
    - Figure S1, Table S2（S 前缀）
    - Figure SIV, Table SII（S 前缀 + 罗马）
    - Extended Data Figure 1, Extended Data Table 2
    - Supplementary Figure 3, Supplementary Table 4
    - 图1, 表2（中文）
    - Table A1, Table B2（附录表）
    
    Args:
        text: 正文内容
    
    Returns:
        {
            "figures": {"1", "2", "S1", "IV", "SIV", ...},
            "tables": {"1", "A1", "S2", "II", ...}
        }
    """
    figures: Set[str] = set()
    tables: Set[str] = set()
    
    # 查找所有英文 Figure 引用
    for m in _QC_FIGURE_REF_EN_RE.finditer(text):
        ident = m.group(1)
        full_match = m.group(0)
        
        # 检查是否有 S 前缀
        s_match = _QC_S_PREFIX_RE.search(full_match)
        if s_match:
            ident = f"S{s_match.group(1).upper()}"
        elif _qc_is_roman_numeral(ident):
            ident = ident.upper()
        
        figures.add(ident)
    
    # 查找所有中文图引用
    for m in _QC_FIGURE_REF_CN_RE.finditer(text):
        figures.add(m.group(1))
    
    # 查找所有英文 Table 引用
    for m in _QC_TABLE_REF_EN_RE.finditer(text):
        ident = m.group(1)
        full_match = m.group(0)
        
        # 检查是否有 S 前缀
        s_match = _QC_S_PREFIX_RE.search(full_match)
        if s_match:
            ident = f"S{s_match.group(1).upper()}"
        elif _qc_is_roman_numeral(ident):
            ident = ident.upper()
        elif ident and ident[0].isalpha() and len(ident) > 1:
            # 附录表编号如 A1, B2
            ident = ident.upper()
        
        tables.add(ident)
    
    # 查找所有中文表引用
    for m in _QC_TABLE_REF_CN_RE.finditer(text):
        tables.add(m.group(1))
    
    return {"figures": figures, "tables": tables}


def _categorize_idents(idents: Set[str]) -> Dict[str, Set[str]]:
    """
    QA-06: 对标识符进行分类
    
    返回:
        {
            "numeric": {"1", "2", "3"},
            "roman": {"I", "II", "IV"},
            "supplementary": {"S1", "SIV"},
            "appendix": {"A1", "B2"}
        }
    """
    result = {
        "numeric": set(),
        "roman": set(),
        "supplementary": set(),
        "appendix": set()
    }
    
    for ident in idents:
        if ident.upper().startswith('S'):
            result["supplementary"].add(ident)
        elif _qc_is_roman_numeral(ident):
            result["roman"].add(ident)
        elif ident and ident[0].isalpha():
            result["appendix"].add(ident)
        else:
            result["numeric"].add(ident)
    
    return result


# P1-04: 质量控制（QC）独立化
def quality_check(
    records: List["AttachmentRecord"],
    pdf_path: str,
    text_path: Optional[str] = None
) -> List["QualityIssue"]:
    """
    独立的质量检查阶段，验证提取结果的完整性和一致性。
    
    检查项：
    1. 提取数量与文本中引用的一致性
    2. 图像尺寸合理性
    3. 编号连续性
    4. 续页完整性
    
    Args:
        records: 提取记录列表
        pdf_path: PDF 文件路径
        text_path: 提取的文本文件路径（可选）
    
    Returns:
        QualityIssue 列表
    """
    issues: List[QualityIssue] = []
    
    # 分离图片和表格记录
    figures = [r for r in records if r.kind == 'figure']
    tables = [r for r in records if r.kind == 'table']
    
    # 1. 检查编号连续性（从最小编号开始，而非固定从 1 开始，避免对部分提取的误报）
    def check_numbering(items: List["AttachmentRecord"], kind: str) -> List["QualityIssue"]:
        numbering_issues = []
        # 过滤出非附录编号（纯数字）
        numeric_ids = []
        for item in items:
            try:
                if not item.ident.upper().startswith('S'):
                    numeric_ids.append(int(item.ident))
            except ValueError:
                pass
        
        if numeric_ids:
            numeric_ids = sorted(set(numeric_ids))
            min_id = min(numeric_ids)
            max_id = max(numeric_ids)
            # 从最小编号到最大编号检查连续性（而非固定从 1 开始）
            expected = list(range(min_id, max_id + 1))
            missing = set(expected) - set(numeric_ids)
            if missing:
                numbering_issues.append(QualityIssue(
                    level='warning',
                    category='numbering_gap',
                    message=f"{kind.title()} numbering has gaps: missing {sorted(missing)} (range: {min_id}-{max_id})",
                    details={'kind': kind, 'missing': sorted(missing), 'found': numeric_ids, 'range': [min_id, max_id]}
                ))
            # 额外提示：如果最小编号不是 1，提示用户可能是部分提取
            if min_id > 1:
                numbering_issues.append(QualityIssue(
                    level='info',
                    category='partial_extraction',
                    message=f"{kind.title()} starts from {min_id} (not 1), may be partial extraction",
                    details={'kind': kind, 'start': min_id, 'found': numeric_ids}
                ))
        return numbering_issues
    
    issues.extend(check_numbering(figures, 'figure'))
    issues.extend(check_numbering(tables, 'table'))
    
    # 2. 检查图像尺寸合理性
    for record in records:
        if os.path.exists(record.out_path):
            file_size = os.path.getsize(record.out_path)
            if file_size < 1000:  # 小于 1KB
                issues.append(QualityIssue(
                    level='warning',
                    category='size_anomaly',
                    message=f"{record.kind.title()} {record.ident} has very small file size ({file_size} bytes)",
                    details={'file': record.out_path, 'size': file_size}
                ))
            elif file_size > 10 * 1024 * 1024:  # 大于 10MB
                issues.append(QualityIssue(
                    level='info',
                    category='size_anomaly',
                    message=f"{record.kind.title()} {record.ident} has large file size ({file_size / 1024 / 1024:.1f} MB)",
                    details={'file': record.out_path, 'size': file_size}
                ))
    
    # 3. 检查续页完整性
    continued_records = [r for r in records if r.continued]
    for cr in continued_records:
        # 检查是否有对应的主记录
        main_records = [r for r in records if r.kind == cr.kind and r.ident == cr.ident and not r.continued]
        if not main_records:
            issues.append(QualityIssue(
                level='warning',
                category='continued_incomplete',
                message=f"Continued {cr.kind} {cr.ident} has no main record",
                details={'kind': cr.kind, 'ident': cr.ident, 'page': cr.page}
            ))
    
    # 4. 与文本中引用的一致性检查（QA-06 增强：支持罗马数字/S前缀/Extended Data/中文/附录表）
    if text_path and os.path.exists(text_path):
        try:
            with open(text_path, 'r', encoding='utf-8') as f:
                text_content = f.read()
            
            # QA-06: 使用增强的引用检测函数
            text_refs = count_text_references(text_content)
            text_figures = text_refs["figures"]
            text_tables = text_refs["tables"]
            
            # 归一化提取的标识符（统一大小写处理）
            extracted_figures = set()
            for r in figures:
                if not r.continued:
                    ident = r.ident.upper()
                    extracted_figures.add(ident)
            
            extracted_tables = set()
            for r in tables:
                if not r.continued:
                    ident = r.ident.upper()
                    extracted_tables.add(ident)
            
            # 检查文本中引用但未提取的
            missing_figures = text_figures - extracted_figures
            missing_tables = text_tables - extracted_tables
            
            # QA-06: 对缺失项进行分类，便于诊断
            if missing_figures:
                categorized = _categorize_idents(missing_figures)
                detail_parts = []
                if categorized["numeric"]:
                    detail_parts.append(f"numeric: {sorted(categorized['numeric'])}")
                if categorized["roman"]:
                    detail_parts.append(f"roman: {sorted(categorized['roman'])}")
                if categorized["supplementary"]:
                    detail_parts.append(f"supplementary: {sorted(categorized['supplementary'])}")
                
                issues.append(QualityIssue(
                    level='warning',
                    category='count_mismatch',
                    message=f"Figures referenced in text but not extracted: {sorted(missing_figures)} ({', '.join(detail_parts) if detail_parts else 'unknown type'})",
                    details={
                        'missing': sorted(missing_figures),
                        'missing_by_type': {k: sorted(v) for k, v in categorized.items() if v},
                        'text_refs': sorted(text_figures),
                        'extracted': sorted(extracted_figures)
                    }
                ))
            
            if missing_tables:
                categorized = _categorize_idents(missing_tables)
                detail_parts = []
                if categorized["numeric"]:
                    detail_parts.append(f"numeric: {sorted(categorized['numeric'])}")
                if categorized["roman"]:
                    detail_parts.append(f"roman: {sorted(categorized['roman'])}")
                if categorized["supplementary"]:
                    detail_parts.append(f"supplementary: {sorted(categorized['supplementary'])}")
                if categorized["appendix"]:
                    detail_parts.append(f"appendix: {sorted(categorized['appendix'])}")
                
                issues.append(QualityIssue(
                    level='warning',
                    category='count_mismatch',
                    message=f"Tables referenced in text but not extracted: {sorted(missing_tables)} ({', '.join(detail_parts) if detail_parts else 'unknown type'})",
                    details={
                        'missing': sorted(missing_tables),
                        'missing_by_type': {k: sorted(v) for k, v in categorized.items() if v},
                        'text_refs': sorted(text_tables),
                        'extracted': sorted(extracted_tables)
                    }
                ))
            
            # 检查提取了但文本中未引用的（可能是正常的，仅作 info 级别）
            extra_figures = extracted_figures - text_figures
            extra_tables = extracted_tables - text_tables
            
            if extra_figures:
                issues.append(QualityIssue(
                    level='info',
                    category='count_mismatch',
                    message=f"Figures extracted but not found in text references: {sorted(extra_figures)}",
                    details={'extra': sorted(extra_figures)}
                ))
            
            if extra_tables:
                issues.append(QualityIssue(
                    level='info',
                    category='count_mismatch',
                    message=f"Tables extracted but not found in text references: {sorted(extra_tables)}",
                    details={'extra': sorted(extra_tables)}
                ))
            
            # QA-06: 输出 QC 引用统计详情（info 级别）
            fig_categorized = _categorize_idents(text_figures)
            tbl_categorized = _categorize_idents(text_tables)
            
            issues.append(QualityIssue(
                level='info',
                category='reference_stats',
                message=f"QA-06 Reference Stats: Figures={len(text_figures)} (numeric:{len(fig_categorized['numeric'])}, roman:{len(fig_categorized['roman'])}, supp:{len(fig_categorized['supplementary'])}), Tables={len(text_tables)} (numeric:{len(tbl_categorized['numeric'])}, roman:{len(tbl_categorized['roman'])}, supp:{len(tbl_categorized['supplementary'])}, appendix:{len(tbl_categorized['appendix'])})",
                details={
                    'figures_in_text': sorted(text_figures),
                    'figures_by_type': {k: sorted(v) for k, v in fig_categorized.items() if v},
                    'tables_in_text': sorted(text_tables),
                    'tables_by_type': {k: sorted(v) for k, v in tbl_categorized.items() if v},
                    'figures_extracted': sorted(extracted_figures),
                    'tables_extracted': sorted(extracted_tables)
                }
            ))
            
        except Exception as e:
            issues.append(QualityIssue(
                level='info',
                category='validation_error',
                message=f"Could not validate against text file: {e}",
                details={'error': str(e)}
            ))
    
    return issues


# 限制文件名中标号后的单词数量
def _limit_words_after_prefix(filename: str, prefix_pattern: str, max_words: int = 12) -> str:
    """
    限制文件名中前缀（如 Figure_1, Table_S1）之后的单词数量。
    
    Args:
        filename: 完整文件名（不含扩展名）
        prefix_pattern: 前缀模式（如 'Figure_1', 'Table_2'）
        max_words: 标号后允许的最大单词数
    
    Returns:
        单词数量受限的文件名
    """
    # 找到前缀结束位置（标号之后的第一个下划线）
    parts = filename.split('_')
    if len(parts) <= 2:  # 如果只有 'Figure_1' 或更少，直接返回
        return filename
    
    # 前两部分是类型和编号（如 'Figure' + '1'），后面是描述
    prefix_parts = parts[:2]
    desc_parts = parts[2:]
    
    # 限制描述部分的单词数量
    if len(desc_parts) > max_words:
        desc_parts = desc_parts[:max_words]
    
    # 重新组合
    return '_'.join(prefix_parts + desc_parts)


# --- P0-03 + P1-08 修复：从正则匹配结果中提取完整的图表标识符 ---
def _extract_figure_ident(match: re.Match) -> str:
    """
    从 figure_line_re 的匹配结果中提取完整的图表标识符。
    
    支持两种捕获结构：
    1) 旧的分组结构（group 1..4）：
       group(1): S 前缀（可选），如 "S" 或 None
       group(2): S前缀后的数字编号，如 "1", "2"
       group(3): 罗马数字编号，如 "I", "II", "III"
       group(4): 普通数字编号，如 "1", "2"
    2) 新的命名分组结构（推荐）：
       label:  图注类型前缀（含 Supplementary/Extended Data 等）
       s_prefix/s_id: S 前缀 + 编号（阿拉伯或罗马）
       roman/num: 普通罗马/阿拉伯编号
    
    Returns:
        完整标识符，如 "S1", "1", "S2", "I", "II", "SIV" 等
    """
    # --- 新结构：命名分组（优先）---
    if getattr(match.re, "groupindex", None) and match.re.groupindex:
        gd = match.groupdict()
        label = (gd.get("label") or "").strip().lower()
        is_supp_kw = label.startswith("supplementary")

        s_prefix = (gd.get("s_prefix") or "").strip()
        s_id = (gd.get("s_id") or "").strip()
        roman = (gd.get("roman") or "").strip()
        number = (gd.get("num") or "").strip()

        ident = ""
        if s_prefix and s_id:
            ident = f"S{s_id}".strip().upper()
        elif roman:
            ident = roman.upper()
        elif number:
            ident = number

        # "Supplementary Figure IV" / "Supplementary Figure 1" 等：强制补齐 S 前缀，避免与正文 Figure 冲突
        if is_supp_kw and ident and (not ident.upper().startswith("S")):
            ident = f"S{ident}".upper()
        return ident.strip()

    # --- 旧结构：按 group(1..4) 回退 ---
    # 优先匹配 S前缀+数字（兼容旧版只支持 S+digits 的情况）
    try:
        s_prefix = match.group(1) or ""
        s_number = match.group(2) or ""
        if s_prefix and s_number:
            return (s_prefix + s_number).strip()
    except IndexError:
        pass

    # 其次匹配罗马数字
    try:
        roman = match.group(3) or ""
        if roman:
            return roman.strip().upper()
    except IndexError:
        pass

    # 最后匹配普通数字
    try:
        number = match.group(4) or ""
        return number.strip()
    except IndexError:
        return ""


def _extract_table_ident(match: re.Match) -> str:
    """
    从 table_line_re 的匹配结果中提取完整的表格标识符。

    兼容：
    - 新的命名分组结构（label/s_prefix/s_id/letter_id/roman/num）
    - 旧的分组结构（group 1..3）

    Returns:
        表格标识符，如 "1", "S1", "A1", "IV", "SIV" 等
    """
    # --- 新结构：命名分组（优先）---
    if getattr(match.re, "groupindex", None) and match.re.groupindex:
        gd = match.groupdict()
        label = (gd.get("label") or "").strip().lower()
        is_supp_kw = label.startswith("supplementary")

        s_prefix = (gd.get("s_prefix") or "").strip()
        s_id = (gd.get("s_id") or "").strip()
        letter_id = (gd.get("letter_id") or "").strip()
        roman = (gd.get("roman") or "").strip()
        number = (gd.get("num") or "").strip()

        ident = ""
        if s_prefix and s_id:
            ident = f"S{s_id}".strip().upper()
        elif letter_id:
            ident = letter_id.strip().upper()
        elif roman:
            ident = roman.strip().upper()
        elif number:
            ident = number.strip()

        if is_supp_kw and ident and (not ident.upper().startswith("S")):
            ident = f"S{ident}".upper()
        return ident.strip()

    # --- 旧结构：按 group(1..3) 回退 ---
    for idx in (1, 2, 3):
        try:
            value = match.group(idx)
        except IndexError:
            continue
        if value:
            return str(value).strip()
    return ""


def _roman_to_int(roman: str) -> int:
    """
    P1-08: 罗马数字转阿拉伯数字。
    
    Examples:
        "I" -> 1, "II" -> 2, "III" -> 3, "IV" -> 4, "V" -> 5,
        "VI" -> 6, "VII" -> 7, "VIII" -> 8, "IX" -> 9, "X" -> 10
    """
    roman_map = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
    roman = roman.upper()
    result = 0
    prev = 0
    for char in reversed(roman):
        val = roman_map.get(char, 0)
        if val < prev:
            result -= val
        else:
            result += val
        prev = val
    return result


def _is_roman_numeral(s: str) -> bool:
    """P1-08: 检查字符串是否为有效的罗马数字。"""
    if not s:
        return False
    return bool(re.match(r'^[IVXLCDMivxlcdm]+$', s))


def _parse_figure_ident(ident: str) -> Tuple[bool, int]:
    """
    解析图表标识符，返回 (is_supplementary, numeric_part)。
    
    P1-08 扩展：支持罗马数字。
    
    Examples:
        "S1" -> (True, 1)
        "1" -> (False, 1)
        "S12" -> (True, 12)
        "I" -> (False, 1)   # 罗马数字
        "II" -> (False, 2)
        "III" -> (False, 3)
    """
    if ident.upper().startswith('S') and len(ident) > 1:
        rest = ident[1:].strip()
        # 支持：S1 / S12
        if rest.isdigit():
            return True, int(rest)
        # 支持：SIV / SX 等（附录罗马数字）
        if _is_roman_numeral(rest):
            return True, _roman_to_int(rest)
        return True, 0
    elif _is_roman_numeral(ident):
        # 罗马数字：转换为阿拉伯数字
        return False, _roman_to_int(ident)
    else:
        try:
            return False, int(ident)
        except ValueError:
            return False, 0


def _ident_in_range(ident: str, min_val: int, max_val: int) -> bool:
    """
    检查标识符的数字部分是否在指定范围内。
    对于 S1 等附录编号，总是返回 True（不过滤附录图）。
    """
    is_supp, num = _parse_figure_ident(ident)
    if is_supp:
        return True  # 附录图不受 min_figure/max_figure 过滤
    return min_val <= num <= max_val


# 从图注文本生成安全的文件名：
# - 规范化分隔符与 Unicode；
# - 限制可用字符集合；
# - 压缩多余下划线并限制最大长度；
# - 确保以 Figure_<no> 开头，避免重复与歧义；
# - 限制标号后的单词数量在12个以内。
def sanitize_filename_from_caption(caption: str, figure_no: int, max_chars: int = 160, max_words: int = 12) -> str:
    s = caption.strip()
    # normalize & replace common separators
    s = s.replace("|", " ").replace("—", "-").replace("–", "-")
    s = unicodedata.normalize("NFKD", s)
    # keep a limited set of characters
    s = "".join(ch for ch in s if ch.isalnum() or ch in (" ", "_", "-", ".", "(", ")"))
    s = "_".join(s.split())
    s = re.sub(r"_+", "_", s).rstrip("._-")
    # enforce prefix & length
    if not s.lower().startswith("figure_"):
        s = f"Figure_{figure_no}_" + s
    if len(s) > max_chars:
        s = s[:max_chars].rstrip("._-")
    # 限制标号后的单词数量
    s = _limit_words_after_prefix(s, f"Figure_{figure_no}", max_words=max_words)
    return s


# 合并带连字符断行的多行文本（如 "BrowseC-" + "omp"），用于聚合图注预览文本
def join_hyphen_lines(lines: List[str], start_idx: int, max_lines: int = 8, max_chars: int = 200) -> str:
    out = ""
    for j in range(start_idx, min(start_idx + max_lines, len(lines))):
        ln = lines[j].rstrip()
        if j == start_idx:
            out += ln
        else:
            # merge hyphenated breaks like "BrowseC-" + "omp"
            if out.endswith("-"):
                out = out[:-1] + ln.lstrip()
            else:
                out += " " + ln
        if ln.endswith(".") or len(out) >= max_chars:
            break
    return out


# 在像素级估计非白色区域包围盒（带少量 padding），用于 autocrop 去除白边
def detect_content_bbox_pixels(
    pix: "fitz.Pixmap",
    white_threshold: int = 250,
    pad: int = 30,
    mask_rects_px: Optional[List[Tuple[int, int, int, int]]] = None,
) -> Tuple[int, int, int, int]:
    """Return (left, top, right, bottom) pixel bbox of non-white area with small padding.
    The bbox is in pixel coordinates relative to the given pixmap.

    mask_rects_px: optional list of rectangles in PIXEL coords to be considered as
    "whitened" (ignored) when detecting ink, typically text areas to be masked.
    """
    w, h = pix.width, pix.height
    n = pix.n  # samples per pixel
    # Convert to RGB for simplicity (avoid alpha complications)
    if pix.alpha:
        tmp = fitz.Pixmap(fitz.csRGB, pix)
        pix = tmp
        n = pix.n
    samples = memoryview(pix.samples)
    stride = pix.stride

    def in_mask(x: int, y: int) -> bool:
        if not mask_rects_px:
            return False
        for (lx, ty, rx, by) in mask_rects_px:
            if lx <= x < rx and ty <= y < by:
                return True
        return False

    def row_has_ink(y: int) -> bool:
        row = samples[y * stride:(y + 1) * stride]
        step = max(1, w // 1000)
        for x in range(0, w, step):
            off = x * n
            r = row[off + 0]
            g = row[off + 1] if n > 1 else r
            b = row[off + 2] if n > 2 else r
            if in_mask(x, y):
                continue
            if r < white_threshold or g < white_threshold or b < white_threshold:
                return True
        return False

    def col_has_ink(x: int) -> bool:
        step = max(1, h // 1000)
        off0 = x * n
        for y in range(0, h, step):
            row = samples[y * stride:(y + 1) * stride]
            r = row[off0 + 0]
            g = row[off0 + 1] if n > 1 else r
            b = row[off0 + 2] if n > 2 else r
            if in_mask(x, y):
                continue
            if r < white_threshold or g < white_threshold or b < white_threshold:
                return True
        return False

    top = 0
    while top < h and not row_has_ink(top):
        top += 1
    bottom = h - 1
    while bottom >= 0 and not row_has_ink(bottom):
        bottom -= 1
    left = 0
    while left < w and not col_has_ink(left):
        left += 1
    right = w - 1
    while right >= 0 and not col_has_ink(right):
        right -= 1

    if left >= right or top >= bottom:
        return (0, 0, w, h)

    # pad & clamp
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(w, right + 1 + pad)
    bottom = min(h, bottom + 1 + pad)
    return (left, top, right, bottom)


# 估计位图中“有墨迹”的像素比例（0~1），通过子采样快速近似；值越大表示内容越密集
def estimate_ink_ratio(pix: "fitz.Pixmap", white_threshold: int = 250) -> float:
    """Estimate ratio of non-white pixels for a pixmap using subsampling.
    Returns value in [0,1]. Higher means denser content (likely figure area).
    """
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


# P1-03: PDF 预验证结果数据类
@dataclass
class PDFValidationResult:
    """PDF 预验证结果，用于在提取前检测潜在问题"""
    is_valid: bool               # 是否可以正常处理
    page_count: int              # 页数
    has_text_layer: bool         # 是否有文本层
    text_layer_ratio: float      # 有文本层的页面占比（0.0~1.0）
    is_encrypted: bool           # 是否加密
    pdf_version: str             # PDF 版本
    file_size_mb: float          # 文件大小（MB）
    warnings: List[str]          # 警告列表
    errors: List[str]            # 错误列表
    
    def __str__(self) -> str:
        status = "VALID" if self.is_valid else "INVALID"
        return (f"PDFValidationResult({status}, pages={self.page_count}, "
                f"text_ratio={self.text_layer_ratio:.1%}, encrypted={self.is_encrypted})")


# P1-04: 质量问题数据类
@dataclass
class QualityIssue:
    """质量问题记录"""
    level: str        # 'error' | 'warning' | 'info'
    category: str     # 'count_mismatch' | 'size_anomaly' | 'numbering_gap' | 'continued_incomplete'
    message: str      # 问题描述
    details: Dict[str, Any] = None  # 详细信息（可选）
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}


@dataclass
class AttachmentRecord:
    # 统一记录：图（figure）或表（table）
    kind: str              # 'figure' | 'table'
    ident: str             # 标识：图/表号（保留原样，如 '1'/'S1'/'III'）
    page: int              # 1-based
    caption: str
    out_path: str
    continued: bool = False
    # QA-03: 将 debug 输出与 index 关联（相对 out_dir / images 目录的相对路径）
    debug_artifacts: List[str] = field(default_factory=list)

    def num_key(self) -> float:
        """用于排序的数值键：尽量将可解析的数字排在前面。"""
        try:
            return float(int(self.ident))
        except ValueError:
            return 1e9


# --- Drawing items (for line/grid awareness) ---
@dataclass
class DrawItem:
    rect: fitz.Rect
    orient: str  # 'H' | 'V' | 'O'


# --- Caption candidate structures (for smart caption detection) ---
@dataclass
class CaptionCandidate:
    """表示一个 caption 候选项（可能是真实图注，也可能是正文引用）"""
    rect: fitz.Rect          # 文本行的边界框
    text: str                # 完整文本内容
    number: str              # 提取的编号（如 '1', '2', 'S1'）
    kind: str                # 'figure' | 'table'
    page: int                # 页码（0-based）
    block_idx: int           # 所在 block 索引
    line_idx: int            # 在 block 中的 line 索引
    spans: List[Dict]        # spans 信息（字体、flags 等）
    block: Dict              # 所在 block 的完整信息
    score: float = 0.0       # 评分（越高越可能是真实图注）
    
    def __repr__(self):
        return f"CaptionCandidate({self.kind} {self.number}, page={self.page}, score={self.score:.1f}, y={self.rect.y0:.1f})"


@dataclass
class CaptionIndex:
    """全文 caption 索引，记录每个编号的所有出现位置"""
    candidates: Dict[str, List[CaptionCandidate]]  # key: 'figure_1' | 'table_2'
    
    def get_candidates(self, kind: str, number: str) -> List[CaptionCandidate]:
        """获取指定编号的所有候选项"""
        key = f"{kind}_{number}"
        return self.candidates.get(key, [])


# --- Layout-driven extraction structures (V2 architecture) ---
@dataclass
class EnhancedTextUnit:
    """增强的文本单元（行级），保留完整格式信息"""
    bbox: fitz.Rect              # 边界框
    text: str                    # 文本内容
    page: int                    # 页码（0-based）
    
    # 格式信息
    font_name: str               # 字体名称（如 'TimesNewRoman'）
    font_size: float             # 字号（pt）
    font_weight: str             # 'bold' | 'regular'
    font_flags: int              # PyMuPDF flags (bit flags)
    color: Tuple[int, int, int]  # RGB颜色
    
    # 类型标注（由分类器推断）
    text_type: str               # 'title_h1' | 'title_h2' | 'title_h3' | 'paragraph' | 
                                 # 'caption_figure' | 'caption_table' | 'list' | 'equation' | 'unknown'
    confidence: float            # 类型分类的置信度（0~1）
    
    # 排版信息
    column: int                  # 所在栏（0=左栏, 1=右栏, -1=单栏）
    indent: float                # 左边界（用于检测缩进）
    
    # 层级关系
    block_idx: int               # 所在 block 索引
    line_idx: int                # 所在 line 索引


@dataclass
class TextBlock:
    """文本密集区域的聚合单元"""
    bbox: fitz.Rect                      # 聚合后的边界框
    units: List[EnhancedTextUnit]        # 包含的文本单元
    block_type: str                      # 'paragraph_group' | 'caption' | 'title' | 'list'
    page: int                            # 页码
    column: int                          # 所在栏


@dataclass
class DocumentLayoutModel:
    """全文档的版式模型"""
    # 全局属性
    page_size: Tuple[float, float]  # (width, height) in pt
    num_columns: int                # 1=单栏, 2=双栏
    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float
    column_gap: float               # 双栏时的栏间距
    
    # 典型尺寸
    typical_font_size: float        # 正文字号
    typical_line_height: float      # 行高
    typical_line_gap: float         # 行距
    
    # 文本单元和区块（按页组织）
    text_units: Dict[int, List[EnhancedTextUnit]]  # key=page_num
    text_blocks: Dict[int, List[TextBlock]]        # key=page_num
    
    # 留白区域（可能包含图表的区域）
    vacant_regions: Dict[int, List[fitz.Rect]]     # key=page_num
    
    def to_dict(self, include_details: bool = True) -> Dict:
        """
        转换为可序列化的字典
        
        Args:
            include_details: 是否包含 text_blocks 的 bbox/type 细节（P2-3 增强）
        """
        result = {
            'page_size': self.page_size,
            'num_columns': self.num_columns,
            'margins': {
                'left': self.margin_left,
                'right': self.margin_right,
                'top': self.margin_top,
                'bottom': self.margin_bottom
            },
            'column_gap': self.column_gap,
            'typical_metrics': {
                'font_size': self.typical_font_size,
                'line_height': self.typical_line_height,
                'line_gap': self.typical_line_gap
            },
            'text_units_count': {str(k): len(v) for k, v in self.text_units.items()},
            'text_blocks_count': {str(k): len(v) for k, v in self.text_blocks.items()},
            'vacant_regions_count': {str(k): len(v) for k, v in self.vacant_regions.items()}
        }
        
        # P2-3 增强：落盘 text_blocks 的 bbox/type 细节
        if include_details:
            text_blocks_detail = {}
            for page_num, blocks in self.text_blocks.items():
                page_blocks = []
                for block in blocks:
                    block_info = {
                        'type': block.block_type,
                        'bbox': [round(block.bbox.x0, 2), round(block.bbox.y0, 2), 
                                 round(block.bbox.x1, 2), round(block.bbox.y1, 2)],
                        'column': block.column,
                        'units_count': len(block.units),
                    }
                    # 只保存前 100 字符的文本样本
                    sample_text = ' '.join(u.text[:50] for u in block.units[:2]).strip()
                    if sample_text:
                        block_info['sample'] = sample_text[:100]
                    page_blocks.append(block_info)
                text_blocks_detail[str(page_num)] = page_blocks
            result['text_blocks'] = text_blocks_detail
        
        return result
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'DocumentLayoutModel':
        """从字典创建（暂时简化版本）"""
        return cls(
            page_size=tuple(data['page_size']),
            num_columns=data['num_columns'],
            margin_left=data['margins']['left'],
            margin_right=data['margins']['right'],
            margin_top=data['margins']['top'],
            margin_bottom=data['margins']['bottom'],
            column_gap=data['column_gap'],
            typical_font_size=data['typical_metrics']['font_size'],
            typical_line_height=data['typical_metrics']['line_height'],
            typical_line_gap=data['typical_metrics']['line_gap'],
            text_units={},
            text_blocks={},
            vacant_regions={}
        )


def collect_draw_items(page: "fitz.Page") -> List[DrawItem]:
    """Collect simplified drawing items (lines/rects/paths) as oriented boxes.
    Orientation by aspect ratio of bbox: H (wide), V (tall), O (other).
    """
    out: List[DrawItem] = []
    try:
        for dr in page.get_drawings():
            r = dr.get("rect")
            if r is None:
                # Fallback: try to approximate by union of item bboxes
                union: Optional[fitz.Rect] = None
                for it in dr.get("items", []):
                    # Items can be lines, curves; attempt to use 'rect' if present
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


# -------- Enhancements for robust cropping (A + B + D) --------
# A) Trim top area inside chosen clip using text line bboxes
# B) Object connectivity guided clip refinement
# D) Text-mask-assisted auto-cropping (handled by detect_content_bbox_pixels via mask_rects)

def _collect_text_lines(dict_data: Dict) -> List[Tuple[fitz.Rect, float, str]]:
    """Collect line-level text entries from page dict.
    Returns list of (bbox, font_size_estimate, text).
    """
    out: List[Tuple[fitz.Rect, float, str]] = []
    for blk in dict_data.get("blocks", []):
        if blk.get("type", 0) != 0:
            continue
        for ln in blk.get("lines", []):
            bbox = fitz.Rect(*(ln.get("bbox", [0, 0, 0, 0])))
            text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            # estimate font size by max span size in the line (fallback 10)
            sizes = [float(sp.get("size", 10.0)) for sp in ln.get("spans", []) if "size" in sp]
            size_est = max(sizes) if sizes else 10.0
            out.append((bbox, size_est, text))
    return out


# --- P0-02 修复：检查文本行是否属于图注本身 ---
def _is_caption_text(
    lines: List[fitz.Rect],
    caption_rect: fitz.Rect,
    tolerance: float = 10.0
) -> bool:
    """
    检查给定的文本行是否与图注 caption_rect 重叠或非常接近。
    用于防止"两行检测"误裁图注本身（尤其是长标题换行的情况）。
    
    Args:
        lines: 待检查的文本行边界框列表
        caption_rect: 图注的边界框
        tolerance: 容差（pt），行与图注距离小于此值视为图注的一部分
    
    Returns:
        True 如果任何一行被判定为属于图注
    """
    for line_rect in lines:
        # 检查是否与图注重叠
        if line_rect.intersects(caption_rect):
            return True
        # 检查垂直距离是否在容差范围内
        # 图注可能在行的上方或下方
        v_dist_above = abs(line_rect.y0 - caption_rect.y1)  # 行在图注下方
        v_dist_below = abs(caption_rect.y0 - line_rect.y1)  # 行在图注上方
        if min(v_dist_above, v_dist_below) < tolerance:
            # 还需检查水平方向是否有重叠
            h_overlap = min(line_rect.x1, caption_rect.x1) - max(line_rect.x0, caption_rect.x0)
            if h_overlap > 0:
                return True
    return False


def _detect_exact_n_lines_of_text(
    clip_rect: fitz.Rect,
    text_lines: List[Tuple[fitz.Rect, float, str]],
    typical_line_h: float,
    n: int = 2,
    tolerance: float = 0.35
) -> Tuple[bool, List[fitz.Rect]]:
    """
    检测clip_rect中是否恰好包含n行文字。
    
    Args:
        clip_rect: 待检测的矩形区域
        text_lines: 文本行列表 (bbox, font_size, text)
        typical_line_h: 典型行高
        n: 期望的行数
        tolerance: 容差（相对于期望值的比例）
    
    Returns:
        (is_exact_n_lines, matched_line_bboxes)
    """
    # 筛选在区域内的文本行
    text_in_region = []
    for bbox, size_est, text in text_lines:
        if bbox.intersects(clip_rect) and bbox.height < typical_line_h * 1.5:
            text_in_region.append((bbox, size_est, text))
    
    if not text_in_region:
        return False, []
    
    # 按y坐标排序
    text_in_region.sort(key=lambda x: x[0].y0)
    
    # 计算实际行数（根据y间距判断是否为同一行）
    actual_lines = []
    current_line_bboxes = [text_in_region[0][0]]
    
    for i in range(1, len(text_in_region)):
        prev_bbox = text_in_region[i-1][0]
        curr_bbox = text_in_region[i][0]
        gap = curr_bbox.y0 - prev_bbox.y1
        
        if gap < typical_line_h * 0.8:  # 认为是同一行
            current_line_bboxes.append(curr_bbox)
        else:  # 新的一行
            # 合并当前行的所有bbox
            merged_bbox = current_line_bboxes[0]
            for bbox in current_line_bboxes[1:]:
                merged_bbox = merged_bbox | bbox
            actual_lines.append(merged_bbox)
            current_line_bboxes = [curr_bbox]
    
    # 添加最后一行
    if current_line_bboxes:
        merged_bbox = current_line_bboxes[0]
        for bbox in current_line_bboxes[1:]:
            merged_bbox = merged_bbox | bbox
        actual_lines.append(merged_bbox)
    
    # 检查行数是否匹配
    if abs(len(actual_lines) - n) > 1:
        return False, []
    
    # 检查总高度是否约等于n倍行高
    if len(actual_lines) > 0:
        total_height = actual_lines[-1].y1 - actual_lines[0].y0
        expected_height = n * typical_line_h
        
        if abs(total_height - expected_height) / expected_height > tolerance:
            return False, []
    
    return True, actual_lines


def _estimate_document_line_metrics(
    doc: fitz.Document,
    sample_pages: int = 5,
    debug: bool = False
) -> Dict[str, float]:
    """
    统计文档的典型行高、字号、行距等文本度量信息。
    
    通过采样前N页的文本行，统计正文的典型字号和行高，
    用于后续自适应参数计算（如相邻阈值、远距文字检测等）。
    
    Args:
        doc: PDF文档对象
        sample_pages: 采样页数（默认5页）
        debug: 是否输出调试信息
    
    Returns:
        字典包含:
        - typical_font_size: 正文典型字号（pt）
        - typical_line_height: 正文典型行高（pt）
        - typical_line_gap: 正文典型行距（pt）
        - median_line_height: 行高中位数（pt）
        - p75_line_height: 行高75分位数（pt）
    """
    all_lines = []
    
    # 采样前N页
    num_pages = min(sample_pages, len(doc))
    for pno in range(num_pages):
        page = doc[pno]
        dict_data = page.get_text("dict")
        
        for block in dict_data.get("blocks", []):
            if block.get("type") != 0:  # 仅文本块
                continue
            
            lines = block.get("lines", [])
            for i, line in enumerate(lines):
                bbox = fitz.Rect(line["bbox"])
                
                # 跳过异常小的行（可能是噪点）
                if bbox.height < 3 or bbox.width < 10:
                    continue
                
                # 统计字号（取行内最大字号）
                sizes = [sp.get("size", 10) for sp in line.get("spans", []) if "size" in sp]
                if not sizes:
                    continue
                
                font_size = max(sizes)
                line_height = bbox.height
                
                # 计算与下一行的间距（如果存在）
                line_gap = None
                if i + 1 < len(lines):
                    next_bbox = fitz.Rect(lines[i + 1]["bbox"])
                    line_gap = next_bbox.y0 - bbox.y1
                    # 过滤异常大的间距（可能是段落间距或跨列）
                    if line_gap > 50:
                        line_gap = None
                
                all_lines.append({
                    'font_size': font_size,
                    'line_height': line_height,
                    'line_gap': line_gap,
                    'y0': bbox.y0,
                    'y1': bbox.y1,
                })
    
    if not all_lines:
        # 回退默认值
        if debug:
            print("[WARN] No text lines found for line metrics estimation, using defaults")
        return {
            'typical_font_size': 10.5,
            'typical_line_height': 12.0,
            'typical_line_gap': 1.5,
            'median_line_height': 12.0,
            'p75_line_height': 13.0,
        }
    
    # 统计正文字号（过滤标题、图注等异常值：保留8-14pt范围）
    font_sizes = [ln['font_size'] for ln in all_lines if 8 <= ln['font_size'] <= 14]
    if not font_sizes:
        font_sizes = [ln['font_size'] for ln in all_lines]
    
    # 使用中位数作为典型字号（更稳健）
    typical_font = sorted(font_sizes)[len(font_sizes) // 2] if font_sizes else 10.5
    
    # 统计行高（仅统计接近正文字号的行，容差±2pt）
    main_lines = [ln for ln in all_lines if abs(ln['font_size'] - typical_font) < 2.5]
    if not main_lines:
        main_lines = all_lines
    
    line_heights = [ln['line_height'] for ln in main_lines]
    line_heights_sorted = sorted(line_heights)
    
    # 计算中位数和75分位数
    median_idx = len(line_heights_sorted) // 2
    p75_idx = int(len(line_heights_sorted) * 0.75)
    
    typical_line_h = line_heights_sorted[median_idx]
    p75_line_h = line_heights_sorted[p75_idx] if p75_idx < len(line_heights_sorted) else typical_line_h
    
    # 统计行距（仅统计有效的gap值）
    valid_gaps = [ln['line_gap'] for ln in main_lines if ln['line_gap'] is not None and 0 <= ln['line_gap'] < 20]
    typical_gap = sorted(valid_gaps)[len(valid_gaps) // 2] if valid_gaps else (typical_line_h - typical_font)
    
    # 确保gap为正值
    typical_gap = max(0.5, typical_gap)
    
    result = {
        'typical_font_size': round(typical_font, 1),
        'typical_line_height': round(typical_line_h, 1),
        'typical_line_gap': round(typical_gap, 1),
        'median_line_height': round(typical_line_h, 1),
        'p75_line_height': round(p75_line_h, 1),
    }
    
    if debug:
        print(f"\n{'='*60}")
        print(f"DOCUMENT LINE METRICS (sampled {num_pages} pages, {len(all_lines)} lines)")
        print(f"{'='*60}")
        print(f"  Typical Font Size:    {result['typical_font_size']:.1f} pt")
        print(f"  Typical Line Height:  {result['typical_line_height']:.1f} pt")
        print(f"  Typical Line Gap:     {result['typical_line_gap']:.1f} pt")
        print(f"  Median Line Height:   {result['median_line_height']:.1f} pt")
        print(f"  P75 Line Height:      {result['p75_line_height']:.1f} pt")
        print(f"{'='*60}\n")
    
    return result


def _trim_clip_head_by_text(
    clip: fitz.Rect,
    page_rect: fitz.Rect,
    caption_rect: fitz.Rect,
    direction: str,
    text_lines: List[Tuple[fitz.Rect, float, str]],
    *,
    width_ratio: float = 0.5,
    font_min: float = 7.0,
    font_max: float = 16.0,
    gap: float = 6.0,
    adjacent_th: float = 24.0,
) -> fitz.Rect:
    """Trim paragraph-like text near the caption side using line-level bboxes.
    Only adjusts the edge closer to the caption:
      - 'above': near side is BOTTOM (y1)
      - 'below': near side is TOP (y0)
    """
    if clip.height <= 1 or clip.width <= 1:
        return clip

    # which edge is near the caption?
    # above: near-bottom; below: near-top
    near_is_top = (direction == 'below')
    frac = 0.35
    new_top, new_bottom = clip.y0, clip.y1
    for (lb, size_est, text) in text_lines:
        if not text.strip():
            continue
        # Only consider lines overlapping horizontally and inside head region of the clip
        inter = lb & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        # Filter by paragraph heuristics
        width_ok = (inter.width / max(1.0, clip.width)) >= width_ratio
        size_ok = (font_min <= size_est <= font_max)
        if not (width_ok and size_ok):
            continue
        # Near-side gating: only consider top fraction for 'below' (near-top) and
        # bottom fraction for 'above' (near-bottom)
        if near_is_top:
            # only consider lines in the top fraction
            top_thresh = clip.y0 + max(40.0, frac * clip.height)
            if lb.y1 > top_thresh:
                continue
        else:
            # only consider lines in the bottom fraction
            bot_thresh = clip.y1 - max(40.0, frac * clip.height)
            if lb.y0 < bot_thresh:
                continue
        # Adjacency to caption: text close to previous/next caption is VERY likely body text
        near_caption = False
        if near_is_top:
            # distance between this line and caption top
            dist = caption_rect.y0 - lb.y1
            if 0 <= dist <= adjacent_th:
                near_caption = True
        else:
            dist = lb.y0 - caption_rect.y1
            if 0 <= dist <= adjacent_th:
                near_caption = True
        if not near_caption:
            # Even if not adjacent, if the line sits flush with page margin, also consider trimming
            if abs(lb.x0 - page_rect.x0) < 6.5 or abs(page_rect.x1 - lb.x1) < 6.5:
                near_caption = True
        if not near_caption:
            continue

        if near_is_top:
            new_top = max(new_top, lb.y1 + gap)
        else:
            new_bottom = min(new_bottom, lb.y0 - gap)

    # Enforce minimum height
    min_h = 40.0
    max_trim_ratio = 0.25
    base_h = clip.height
    if near_is_top and new_top > clip.y0:
        # limit trimming amount
        new_top = min(new_top, clip.y0 + max(min_h, max_trim_ratio * base_h))
        if new_bottom - new_top >= min_h:
            clip.y0 = new_top
    if (not near_is_top) and new_bottom < clip.y1:
        new_bottom = max(new_bottom, clip.y1 - max(min_h, max_trim_ratio * base_h))
        if new_bottom - new_top >= min_h:
            clip.y1 = new_bottom
    # Clamp to page
    clip = fitz.Rect(clip.x0, max(page_rect.y0, clip.y0), clip.x1, min(page_rect.y1, clip.y1))
    return clip


def _trim_clip_head_by_text_v2(
    clip: fitz.Rect,
    page_rect: fitz.Rect,
    caption_rect: fitz.Rect,
    direction: str,
    text_lines: List[Tuple[fitz.Rect, float, str]],
    *,
    width_ratio: float = 0.5,
    font_min: float = 7.0,
    font_max: float = 16.0,
    gap: float = 6.0,
    adjacent_th: float = 24.0,
    far_text_th: float = 300.0,
    far_text_para_min_ratio: float = 0.30,
    far_text_trim_mode: str = "aggressive",
    # Phase C tuners (far-side paragraphs)
    # P1-1: 下调阈值以覆盖"中间地带"（约 3-7 行）
    far_side_min_dist: float = 50.0,  # 从 100.0 降低到 50.0
    far_side_para_min_ratio: float = 0.12,  # 从 0.20 降低到 0.12
    # Adaptive line height
    typical_line_h: Optional[float] = None,
    # 2025-12-30 新增：表格保护 - 跳过 adjacent sweep
    # 表格内容（表头、数据行）紧邻正文段落末尾，容易被 adjacent sweep 误删
    skip_adjacent_sweep: bool = False,
    # Debug
    debug: bool = False,
) -> fitz.Rect:
    """
    Enhanced dual-threshold text trimming.
    
    Phase A: Trim adjacent text (<adjacent_th, default 24pt) using original logic
    Phase B: Detect and remove far-distance text blocks (adjacent_th ~ far_text_th)
    Phase C: Detect and remove far-side large paragraphs (远端大段落)
    
    Args:
        far_text_th: Maximum distance to detect far text (default 300pt)
        far_text_para_min_ratio: Minimum paragraph coverage ratio to trigger far-text trim (default 0.30)
        far_text_trim_mode: 'aggressive' (remove all far paragraphs) or 'conservative' (only if continuous)
    """
    if clip.height <= 1 or clip.width <= 1:
        return clip
    
    # Save original clip for far-text detection
    original_clip = fitz.Rect(clip)
    
    # === Phase A: Apply original adjacent-text trim ===
    clip = _trim_clip_head_by_text(
        clip, page_rect, caption_rect, direction, text_lines,
        width_ratio=width_ratio, font_min=font_min, font_max=font_max,
        gap=gap, adjacent_th=adjacent_th
    )
    
    # === Phase A+: Enhanced "Exact Two Lines" Detection ===
    # If we have typical_line_h, check if there are exactly 2 lines of text and use more aggressive trim
    if typical_line_h is not None and typical_line_h > 0:
        near_is_top_a = (direction == 'below')
        # Define the near-side strip to check (靠近图注的区域)
        if near_is_top_a:
            check_strip = fitz.Rect(
                original_clip.x0,
                original_clip.y0,
                original_clip.x1,
                min(original_clip.y1, original_clip.y0 + 3.5 * typical_line_h)  # 检查顶部3.5倍行高范围
            )
        else:
            check_strip = fitz.Rect(
                original_clip.x0,
                max(original_clip.y0, original_clip.y1 - 3.5 * typical_line_h),  # 检查底部3.5倍行高范围
                original_clip.x1,
                original_clip.y1
            )
        
        # 检测是否恰好有2行文字
        is_exact_two, matched_lines = _detect_exact_n_lines_of_text(
            check_strip, text_lines, typical_line_h, n=2, tolerance=0.35
        )
        
        if is_exact_two and len(matched_lines) == 2:
            # --- P0-02 修复：检查匹配到的文字是否属于图注本身 ---
            # 如果这两行文字与 caption_rect 重叠或非常接近，说明是长标题换行，不应裁切
            if _is_caption_text(matched_lines, caption_rect, tolerance=10.0):
                # 跳过裁切，保留图注
                pass
            else:
                # 使用更激进的裁切：移除这两行文字，并留一个小gap
                if near_is_top_a:
                    # 图在下方，裁切顶部的两行
                    new_y0 = matched_lines[-1].y1 + gap  # 最后一行底部 + gap
                    clip.y0 = max(clip.y0, new_y0)  # 确保不会扩大clip
                else:
                    # 图在上方，裁切底部的两行
                    new_y1 = matched_lines[0].y0 - gap  # 第一行顶部 - gap
                    clip.y1 = min(clip.y1, new_y1)  # 确保不会扩大clip
    
    # === Phase B: Detect and trim far-distance text ===
    # For figures cropped ABOVE the caption, the near side is bottom and the far side is TOP.
    # For figures cropped BELOW the caption, the near side is top and the far side is BOTTOM.
    near_is_top = (direction == 'below')
    
    # Collect far-distance paragraph lines (use ORIGINAL clip, not Phase A result)
    far_para_lines: List[Tuple[fitz.Rect, float, str]] = []
    for (lb, size_est, text) in text_lines:
        if not text.strip():
            continue
        # Must overlap horizontally with ORIGINAL clip
        inter = lb & original_clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        # Filter by paragraph heuristics
        width_ok = (inter.width / max(1.0, original_clip.width)) >= width_ratio
        size_ok = (font_min <= size_est <= font_max)
        if not (width_ok and size_ok):
            continue
        
        # Distance to caption (far-distance range: adjacent_th ~ far_text_th)
        if near_is_top:
            dist = caption_rect.y0 - lb.y1
        else:
            dist = lb.y0 - caption_rect.y1
        
        # Must be in far-distance range
        if adjacent_th < dist <= far_text_th:
            # Also check if line is in the near-side region (use ORIGINAL clip)
            if near_is_top:
                top_thresh = original_clip.y0 + max(40.0, 0.5 * original_clip.height)
                if lb.y1 <= top_thresh:
                    far_para_lines.append((lb, size_est, text))
            else:
                bot_thresh = original_clip.y1 - max(40.0, 0.5 * original_clip.height)
                if lb.y0 >= bot_thresh:
                    far_para_lines.append((lb, size_est, text))
    
    # (Near-side far-text detection completed)
    # Compute near-side paragraph coverage ratio for gating
    para_coverage_ratio = 0.0
    if far_para_lines:
        if near_is_top:
            # near side region = top portion up to mid of ORIGINAL clip
            region_start = original_clip.y0
            region_end = original_clip.y0 + max(40.0, 0.5 * original_clip.height)
            region_h = max(1.0, region_end - region_start)
            para_h = sum(lb.height for (lb, _, _) in far_para_lines)
            para_coverage_ratio = para_h / region_h
        else:
            # near side region = bottom portion from mid to end of ORIGINAL clip
            region_start = original_clip.y1 - max(40.0, 0.5 * original_clip.height)
            region_end = original_clip.y1
            region_h = max(1.0, region_end - region_start)
            para_h = sum(lb.height for (lb, _, _) in far_para_lines)
            para_coverage_ratio = para_h / region_h
    
    # Phase B trimming (near-side far text) – applied later after far-side handling as well
    
    # === Phase C: Detect and trim far-side large paragraphs ===
    far_is_top = not near_is_top  # Opposite side from caption
    far_side_para_lines: List[Tuple[fitz.Rect, float, str]] = []
    
    for (lb, size_est, text) in text_lines:
        if not text.strip():
            continue
        # Must overlap horizontally with ORIGINAL clip
        inter = lb & original_clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        # Filter by paragraph heuristics
        width_ok = (inter.width / max(1.0, original_clip.width)) >= width_ratio
        size_ok = (font_min <= size_est <= font_max)
        if not (width_ok and size_ok):
            continue
        
        # Distance to caption (far side, >100pt away)
        if far_is_top:
            dist = caption_rect.y0 - lb.y1
        else:
            dist = lb.y0 - caption_rect.y1
        
        # Must be far from caption (> far_side_min_dist)
        if dist > far_side_min_dist:
            # Check if line is in the far-side region
            if far_is_top:
                # Far side is TOP, check if in top half of original clip
                mid_point = original_clip.y0 + 0.5 * original_clip.height
                if lb.y0 < mid_point:
                    far_side_para_lines.append((lb, size_est, text))
            else:
                # Far side is BOTTOM, check if in bottom half of original clip
                mid_point = original_clip.y0 + 0.5 * original_clip.height
                if lb.y1 > mid_point:
                    far_side_para_lines.append((lb, size_est, text))
    
    # DEBUG: Report far-side detection
    if far_side_para_lines:
        far_side_para_lines.sort(key=lambda x: x[0].y0)
        # Calculate far-side paragraph coverage
        if far_is_top:
            far_side_region_start = original_clip.y0
            far_side_region_end = original_clip.y0 + 0.5 * original_clip.height
        else:
            far_side_region_start = original_clip.y0 + 0.5 * original_clip.height
            far_side_region_end = original_clip.y1
        
        far_side_region_height = max(1.0, far_side_region_end - far_side_region_start)
        far_side_total_para_height = sum(lb.height for (lb, _, _) in far_side_para_lines)
        far_side_para_coverage = far_side_total_para_height / far_side_region_height
        
        # Decision: trim far-side if coverage >= threshold (default 0.20)
        if far_side_para_coverage >= far_side_para_min_ratio:
            if debug:
                print(f"[DBG] Far-side trim: direction={'above' if near_is_top else 'below'} far_is_top={far_is_top} coverage={far_side_para_coverage:.3f} th={far_side_para_min_ratio}")
            if far_is_top:
                # Move clip.y0 down to after last far-side paragraph
                last_para_y1 = max(lb.y1 for (lb, _, _) in far_side_para_lines)
                new_y0 = last_para_y1 + gap
                # 2025-12-30 修复：当远端文字覆盖率高时放宽裁切限制
                # 高覆盖率（>15%）说明有大量正文需要移除，放宽到 65%
                # 低覆盖率使用保守的 50% 限制
                trim_ratio = 0.65 if far_side_para_coverage >= 0.15 else 0.50
                max_trim = original_clip.y0 + trim_ratio * original_clip.height
                clip.y0 = min(new_y0, max_trim)
                if debug and new_y0 > max_trim:
                    print(f"[DBG] Far-side trim limited by max_trim ({trim_ratio:.0%}): {new_y0:.1f} -> {clip.y0:.1f}")
                
                # 2025-12-30 增强：邻近短行清扫
                # 检测紧邻已裁切边界的短行（宽度不足但在正文附近，很可能是段落尾行）
                # 例如 "images of molecules" 这类短行，虽然宽度只有 16%，但紧邻正文段落
                # 2025-12-30 修复：表格保护 - 跳过 adjacent sweep（表头紧邻正文末尾会被误删）
                if not skip_adjacent_sweep:
                    adjacent_zone = max(40.0, 4.0 * (typical_line_h or 12.0))  # 4行高范围
                    for (lb, size_est, txt) in text_lines:
                        if not txt.strip() or len(txt.strip()) < 3:
                            continue
                        inter = lb & clip
                        if inter.width <= 0 or inter.height <= 0:
                            continue
                        # 只检查紧邻当前 y0 边界的行（在 adjacent_zone 内）
                        if lb.y0 >= clip.y0 and lb.y0 < clip.y0 + adjacent_zone:
                            # 宽度门槛放宽到 5%
                            w_ok = (inter.width / max(1.0, clip.width)) >= 0.05
                            s_ok = (font_min <= size_est <= font_max)
                            if w_ok and s_ok:
                                # 推进 y0 到这行底部
                                candidate_y0 = lb.y1 + gap
                                if candidate_y0 > clip.y0 and candidate_y0 <= max_trim:
                                    clip.y0 = candidate_y0
                                    if debug:
                                        print(f"[DBG] Far-side adjacent sweep: '{txt.strip()[:25]}...' y0 -> {clip.y0:.1f}")
            else:
                # Move clip.y1 up to before first far-side paragraph
                first_para_y0 = min(lb.y0 for (lb, _, _) in far_side_para_lines)
                new_y1 = first_para_y0 - gap
                # 2025-12-30 修复：当远端文字覆盖率高时放宽裁切限制
                trim_ratio = 0.65 if far_side_para_coverage >= 0.15 else 0.50
                min_trim = original_clip.y1 - trim_ratio * original_clip.height
                clip.y1 = max(new_y1, min_trim)
                if debug and new_y1 < min_trim:
                    print(f"[DBG] Far-side trim limited by min_trim ({trim_ratio:.0%}): {new_y1:.1f} -> {clip.y1:.1f}")
                
                # 2025-12-30 增强：邻近短行清扫（bottom 方向）
                # 2025-12-30 修复：表格保护 - 跳过 adjacent sweep（表头紧邻正文末尾会被误删）
                if not skip_adjacent_sweep:
                    adjacent_zone = max(40.0, 4.0 * (typical_line_h or 12.0))
                    for (lb, size_est, txt) in text_lines:
                        if not txt.strip() or len(txt.strip()) < 3:
                            continue
                        inter = lb & clip
                        if inter.width <= 0 or inter.height <= 0:
                            continue
                        # 只检查紧邻当前 y1 边界的行
                        if lb.y1 <= clip.y1 and lb.y1 > clip.y1 - adjacent_zone:
                            w_ok = (inter.width / max(1.0, clip.width)) >= 0.05
                            s_ok = (font_min <= size_est <= font_max)
                            if w_ok and s_ok:
                                candidate_y1 = lb.y0 - gap
                                if candidate_y1 < clip.y1 and candidate_y1 >= min_trim:
                                    clip.y1 = candidate_y1
                                    if debug:
                                        print(f"[DBG] Far-side adjacent sweep: '{txt.strip()[:25]}...' y1 -> {clip.y1:.1f}")
            
            # 2025-12-30 增强：Phase C 主逻辑执行后，**迭代扫描**短行文字
            # 每次检测到短行后更新 clip，再检测下一批，直到没有新的短行
            # 这解决了 Figure 22 "images of molecules" 等短行残留问题
            # 2025-12-30 修复：表格保护 - 跳过迭代扫描（表头紧邻正文末尾会被误删）
            if skip_adjacent_sweep:
                pass  # 表格模式：跳过迭代扫描
            else:
                max_iterations = 5  # 防止无限循环
                for _iter in range(max_iterations):
                    _extra_short_lines: List[fitz.Rect] = []
                    for (lb, size_est, text) in text_lines:
                        txt = text.strip()
                        if not txt or len(txt) < 5:
                            continue
                        inter = lb & clip  # 使用当前 clip
                        if inter.width <= 0 or inter.height <= 0:
                            continue
                        # 检查是否在远端区域（扩大到整个远端 50%）
                        if far_is_top:
                            far_region_end = clip.y0 + 0.5 * clip.height
                            in_far = (lb.y0 < far_region_end)
                        else:
                            far_region_start = clip.y1 - 0.5 * clip.height
                            in_far = (lb.y1 > far_region_start)
                        if not in_far:
                            continue
                        # 宽度门槛放宽到 8%
                        w_ratio_extra = inter.width / max(1.0, clip.width)
                        if w_ratio_extra < 0.08:
                            continue
                        # 字号检查
                        if not (font_min <= size_est <= font_max):
                            continue
                        _extra_short_lines.append(lb)
                    
                    if not _extra_short_lines:
                        break  # 没有新的短行，停止迭代
                    
                    if far_is_top:
                        new_y0 = max(lb.y1 for lb in _extra_short_lines) + gap
                        # 使用与主裁切相同的 trim_ratio
                        max_trim2 = original_clip.y0 + trim_ratio * original_clip.height
                        if new_y0 > clip.y0 + 1e-3:
                            clip.y0 = min(new_y0, max_trim2)
                            if debug:
                                print(f"[DBG] Far-side short-line sweep (iter {_iter+1}): +{len(_extra_short_lines)} lines, y0 -> {clip.y0:.1f}")
                        else:
                            break  # 无法再推进，停止
                    else:
                        new_y1 = min(lb.y0 for lb in _extra_short_lines) - gap
                        # 使用与主裁切相同的 trim_ratio
                        min_trim2 = original_clip.y1 - trim_ratio * original_clip.height
                        if new_y1 < clip.y1 - 1e-3:
                            clip.y1 = max(new_y1, min_trim2)
                            if debug:
                                print(f"[DBG] Far-side short-line sweep (iter {_iter+1}): +{len(_extra_short_lines)} lines, y1 -> {clip.y1:.1f}")
                        else:
                            break  # 无法再推进，停止
        else:
            # Fallback: if no strong paragraph coverage on far side, still trim
            # obvious top/bottom stray lines that are far from the caption.
            # 改进：更激进地检测，包括普通段落文字（不仅仅是bullet）
            fallback_lines: List[fitz.Rect] = []
            for (lb, size_est, text) in text_lines:
                if not text.strip():
                    continue
                inter = lb & original_clip
                if inter.width <= 0 or inter.height <= 0:
                    continue
                # 先检查是否是明显的正文标记（bullet 或超长文本）
                txt = text.strip()
                has_bullet = txt.startswith('•') or txt.startswith('·') or txt.startswith('- ') or txt.startswith('○') or txt.startswith('–')
                is_very_long_line = len(txt) > 60  # 超长文本行（>60字符）几乎肯定是段落
                is_long_line = len(txt) > 30  # 长文本行（>30字符）
                
                # 如果是 bullet 或超长文本，跳过宽度和字体检查
                if has_bullet or is_very_long_line:
                    pass  # 直接进入距离判断
                else:
                    # 普通文字需要满足宽度和字体条件
                    width_ok_small = (inter.width / max(1.0, original_clip.width)) >= max(0.10, width_ratio * 0.3)
                    size_ok = (font_min <= size_est <= font_max)
                    if not (width_ok_small and size_ok):
                        continue
                
                # Compute distance to caption and check far side
                if far_is_top:
                    dist = caption_rect.y0 - lb.y1
                    # 扩大检测区域从25%到50%
                    in_far_region = (lb.y0 < original_clip.y0 + 0.50 * original_clip.height)
                else:
                    dist = lb.y0 - caption_rect.y1
                    in_far_region = (lb.y1 > original_clip.y0 + 0.50 * original_clip.height)
                
                # 分层判断：bullet/超长文本 > 长文本 > 普通文字
                should_trim = False
                if has_bullet:
                    # Bullet: 距离 >15pt 且在远侧区域即可
                    should_trim = (dist > 15.0 and in_far_region)
                elif is_very_long_line:
                    # 超长文本: 距离 >18pt 且在远侧区域
                    should_trim = (dist > 18.0 and in_far_region)
                elif is_long_line:
                    # 长文本: 距离 >20pt 且在远侧区域
                    should_trim = (dist > 20.0 and in_far_region)
                else:
                    # 普通段落: 距离 >25pt 且在远侧区域
                    should_trim = (dist > max(25.0, far_side_min_dist * 0.7) and in_far_region)
                
                if should_trim:
                    fallback_lines.append(lb)
            if fallback_lines:
                if debug:
                    print(f"[DBG] Far-side fallback trim: lines={len(fallback_lines)}")
                if far_is_top:
                    new_y0 = max(lb.y1 for lb in fallback_lines) + gap
                    max_trim = original_clip.y0 + 0.5 * original_clip.height
                    clip.y0 = min(new_y0, max_trim)
                else:
                    new_y1 = min(lb.y0 for lb in fallback_lines) - gap
                    min_trim = original_clip.y1 - 0.5 * original_clip.height
                    clip.y1 = max(new_y1, min_trim)

    # Now handle Phase B (near-side far text) if applicable
    if far_para_lines and para_coverage_ratio >= far_text_para_min_ratio:
        if far_text_trim_mode == "aggressive":
            # Trim to the start of the first far paragraph (based on ORIGINAL clip)
            if near_is_top:
                # Move clip.y0 down to after the last far paragraph
                last_para_y1 = max(lb.y1 for (lb, _, _) in far_para_lines)
                new_y0 = last_para_y1 + gap
                # Safety: don't trim more than 60% of original clip height
                max_trim = original_clip.y0 + 0.6 * original_clip.height
                clip.y0 = min(new_y0, max_trim)
            else:
                # Move clip.y1 up to before the first far paragraph
                first_para_y0 = min(lb.y0 for (lb, _, _) in far_para_lines)
                new_y1 = first_para_y0 - gap
                # Safety: don't trim more than 60% of original clip height
                min_trim = original_clip.y1 - 0.6 * original_clip.height
                clip.y1 = max(new_y1, min_trim)
        elif far_text_trim_mode == "conservative":
            # Only trim if paragraphs are continuous (gap between lines < 20pt)
            is_continuous = True
            for i in range(len(far_para_lines) - 1):
                gap_between = far_para_lines[i+1][0].y0 - far_para_lines[i][0].y1
                if gap_between > 20.0:
                    is_continuous = False
                    break
            if is_continuous:
                # Apply same trim as aggressive (based on ORIGINAL clip)
                if near_is_top:
                    last_para_y1 = max(lb.y1 for (lb, _, _) in far_para_lines)
                    new_y0 = last_para_y1 + gap
                    max_trim = original_clip.y0 + 0.6 * original_clip.height
                    clip.y0 = min(new_y0, max_trim)
                else:
                    first_para_y0 = min(lb.y0 for (lb, _, _) in far_para_lines)
                    new_y1 = first_para_y0 - gap
                    min_trim = original_clip.y1 - 0.6 * original_clip.height
                    clip.y1 = max(new_y1, min_trim)
    
    # Enforce minimum height
    min_h = 40.0
    if clip.height < min_h:
        # Revert to Phase A result
        return _trim_clip_head_by_text(
            fitz.Rect(page_rect.x0, caption_rect.y0 - 600, page_rect.x1, caption_rect.y1 + 600) & page_rect,
            page_rect, caption_rect, direction, text_lines,
            width_ratio=width_ratio, font_min=font_min, font_max=font_max,
            gap=gap, adjacent_th=adjacent_th
        )
    
    # Clamp to page
    clip = fitz.Rect(clip.x0, max(page_rect.y0, clip.y0), clip.x1, min(page_rect.y1, clip.y1))
    return clip


def _merge_rects(rects: List[fitz.Rect], merge_gap: float = 6.0) -> List[fitz.Rect]:
    if not rects:
        return []
    # Expand by small gap then merge intersecting boxes iteratively
    expanded = [fitz.Rect(r.x0 - merge_gap, r.y0 - merge_gap, r.x1 + merge_gap, r.y1 + merge_gap) for r in rects]
    changed = True
    while changed:
        changed = False
        out: List[fitz.Rect] = []
        for r in expanded:
            merged = False
            for i, o in enumerate(out):
                if (r & o).width > 0 and (r & o).height > 0:
                    out[i] = o | r
                    merged = True
                    changed = True
                    break
            if not merged:
                out.append(r)
        expanded = out
    # Remove the initial gap expansion effect by keeping merged boxes as-is (still fine)
    return expanded


def _refine_clip_by_objects(
    clip: fitz.Rect,
    caption_rect: fitz.Rect,
    direction: str,
    image_rects: List[fitz.Rect],
    vector_rects: List[fitz.Rect],
    *,
    object_pad: float = 8.0,
    min_area_ratio: float = 0.015,
    merge_gap: float = 6.0,
    near_edge_only: bool = True,
    use_axis_union: bool = True,
    use_horizontal_union: bool = False,
) -> fitz.Rect:
    """Refine clip using object components.
    - near_edge_only: only adjust boundary near caption side (avoid shrinking far side)
    - use_axis_union: if multiple vertical components (sub-figures), take union extent
    """
    area = max(1.0, clip.width * clip.height)
    cand: List[fitz.Rect] = []
    for r in image_rects + vector_rects:
        inter = r & clip
        if inter.width > 0 and inter.height > 0:
            if (inter.width * inter.height) / area >= min_area_ratio:
                cand.append(inter)
    if not cand:
        return clip

    comps = _merge_rects(cand, merge_gap=merge_gap)
    if not comps:
        return clip

    # choose the component closest to caption side
    def comp_score(r: fitz.Rect) -> float:
        if direction == 'above':
            dist = max(0.0, caption_rect.y0 - r.y1)
        else:
            dist = max(0.0, r.y0 - caption_rect.y1)
        # prefer larger area when distance ties
        return dist + (-0.0001 * r.width * r.height)

    comps.sort(key=comp_score)
    chosen = comps[0]
    # Union along vertical axis when multiple stacked components likely present
    if use_axis_union and len(comps) >= 2:
        # detect vertical stacking by x-overlap ratio
        overlaps = []
        for r in comps:
            inter_w = max(0.0, min(r.x1, chosen.x1) - max(r.x0, chosen.x0))
            overlaps.append(inter_w / max(1.0, min(r.width, chosen.width)))
        if sum(1 for v in overlaps if v >= 0.6) >= 2:
            union = comps[0]
            for r in comps[1:]:
                union = union | r
            chosen = union

    # Union along horizontal axis when side-by-side panels present
    if use_horizontal_union and len(comps) >= 2:
        y_overlaps = []
        for r in comps:
            inter_h = max(0.0, min(r.y1, chosen.y1) - max(r.y0, chosen.y0))
            y_overlaps.append(inter_h / max(1.0, min(r.height, chosen.height)))
        if sum(1 for v in y_overlaps if v >= 0.6) >= 2:
            union = comps[0]
            for r in comps[1:]:
                union = union | r
            chosen = union

    # Apply padding
    chosen = fitz.Rect(
        chosen.x0 - object_pad,
        chosen.y0 - object_pad,
        chosen.x1 + object_pad,
        chosen.y1 + object_pad,
    )

    # Non-symmetric update: adjust only the boundary near caption side
    result = fitz.Rect(clip)
    if near_edge_only:
        if direction == 'above':
            # near side is bottom
            result.y1 = min(clip.y1, max(chosen.y1, clip.y0 + 40.0))
        else:
            # near side is top
            result.y0 = max(clip.y0, min(chosen.y0, clip.y1 - 40.0))
        # do not shrink width; optionally expand within clip
        result.x0 = min(result.x0, chosen.x0)
        result.x1 = max(result.x1, chosen.x1)
        # clamp to original clip
        result = result & clip
        return result if result.height >= 40 else clip
    else:
        # symmetric: intersect with chosen (older behavior but safer clamped)
        result = (chosen & clip)
        return result if result.height >= 40 else clip


def _build_text_masks_px(
    clip: fitz.Rect,
    text_lines: List[Tuple[fitz.Rect, float, str]],
    *,
    scale: float,
    direction: str = 'above',
    near_frac: float = 0.6,
    width_ratio: float = 0.5,
    font_max: float = 14.0,
    mask_mode: str = 'auto',  # P0-2: 'near' | 'both' | 'auto'
    far_edge_zone: float = 40.0,  # P0-2: 远端检测区域（pt）
) -> List[Tuple[int, int, int, int]]:
    """Convert selected text line rects to PIXEL-space masks relative to clip.
    
    P0-2 增强：支持远端掩膜模式
    - 'near'：仅掩膜靠近 caption 的一侧（原行为）
    - 'both'：同时掩膜近端和远端的正文行
    - 'auto'（默认）：智能判断，近端总是掩膜，远端仅当检测到正文行时才掩膜
    
    对于 'above'：near side = bottom，far side = top
    对于 'below'：near side = top，far side = bottom
    """
    masks: List[Tuple[int, int, int, int]] = []
    y_thresh_top = clip.y0 + near_frac * clip.height
    y_thresh_bot = clip.y1 - near_frac * clip.height
    
    # 确定掩膜区域
    mask_near = True  # 近端总是掩膜
    mask_far = (mask_mode == 'both')  # 'both' 模式时掩膜远端
    
    # 'auto' 模式：检测远端是否有正文行，有则掩膜
    far_side_lines: List[Tuple[fitz.Rect, float, str]] = []
    if mask_mode == 'auto':
        far_is_top = (direction == 'above')
        for (lb, fs, text) in text_lines:
            txt = text.strip()
            if not txt:
                continue
            if fs > font_max:
                continue
            inter = lb & clip
            if inter.width <= 0 or inter.height <= 0:
                continue
            # 正文特征：宽度覆盖 + 长度 > 10
            if (inter.width / max(1.0, clip.width)) < width_ratio:
                continue
            if len(txt) < 10:
                continue
            # 检查是否在远端边缘附近
            if far_is_top:
                dist = lb.y0 - clip.y0
                if dist < far_edge_zone:
                    far_side_lines.append((lb, fs, text))
            else:
                dist = clip.y1 - lb.y1
                if dist < far_edge_zone:
                    far_side_lines.append((lb, fs, text))
        
        mask_far = len(far_side_lines) > 0
    
    for (lb, fs, text) in text_lines:
        if not text.strip():
            continue
        if fs > font_max:
            continue
        inter = lb & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        if (inter.width / max(1.0, clip.width)) < width_ratio:
            continue
        
        # 判断该文本行在近端还是远端
        in_near_side = False
        in_far_side = False
        
        if direction == 'above':
            # near side is bottom, far side is top
            if inter.y0 >= y_thresh_bot:
                in_near_side = True
            if inter.y1 <= y_thresh_top:
                in_far_side = True
        else:
            # near side is top, far side is bottom
            if inter.y1 <= y_thresh_top:
                in_near_side = True
            if inter.y0 >= y_thresh_bot:
                in_far_side = True
        
        # 根据掩膜模式决定是否添加
        should_mask = False
        if mask_near and in_near_side:
            should_mask = True
        if mask_far and in_far_side:
            should_mask = True
        
        if not should_mask:
            continue
        
        # convert to pixel coords
        l = int(max(0, (inter.x0 - clip.x0) * scale))
        t = int(max(0, (inter.y0 - clip.y0) * scale))
        r = int(min((clip.x1 - clip.x0) * scale, (inter.x1 - clip.x0) * scale))
        b = int(min((clip.y1 - clip.y0) * scale, (inter.y1 - clip.y0) * scale))
        if r - l > 1 and b - t > 1:
            masks.append((l, t, r, b))
    return masks


# ---------- P0-1: 远端正文证据检测（用于 Phase D 单调性约束） ----------
def _detect_far_side_text_evidence(
    clip: fitz.Rect,
    text_lines: List[Tuple[fitz.Rect, float, str]],
    direction: str,
    edge_zone: float = 40.0,  # 检测远端边缘附近 40pt 范围
    min_width_ratio: float = 0.30,  # 正文行的最小宽度比例
    font_min: float = 7.0,
    font_max: float = 16.0,
) -> Tuple[bool, float]:
    """
    检测远端边缘附近是否有正文行证据。
    
    用于 P0-1 单调性约束：当远端附近有正文行时，Phase D 不应该扩展到这些行的区域。
    
    Args:
        clip: 当前裁剪区域
        text_lines: 所有文本行 [(rect, font_size, text), ...]
        direction: 'above' 或 'below'，表示图在 caption 的哪一侧
        edge_zone: 远端边缘附近的检测范围（pt）
        min_width_ratio: 被认为是正文的最小宽度比例
        font_min/font_max: 正文字号范围
    
    Returns:
        (has_evidence, suggested_limit):
        - has_evidence: 是否检测到正文证据
        - suggested_limit: 如果有证据，建议的边界限制（远端不应超过此值）
    """
    if clip.height <= 1 or clip.width <= 1:
        return False, 0.0
    
    far_is_top = (direction == 'above')
    evidence_lines: List[fitz.Rect] = []
    
    for (lb, fs, text) in text_lines:
        txt = text.strip()
        if not txt:
            continue
        
        inter = lb & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        
        # 检查宽度比例（正文特征：跨越较大宽度）
        width_ratio = inter.width / max(1.0, clip.width)
        if width_ratio < min_width_ratio:
            continue
        
        # 检查字号（正文范围）
        if not (font_min <= fs <= font_max):
            continue
        
        # 检查文本长度（正文通常较长）
        if len(txt) < 10:
            continue
        
        # 检查是否在远端边缘附近
        if far_is_top:
            dist_to_far_edge = lb.y0 - clip.y0
            if dist_to_far_edge < edge_zone:
                evidence_lines.append(lb)
        else:
            dist_to_far_edge = clip.y1 - lb.y1
            if dist_to_far_edge < edge_zone:
                evidence_lines.append(lb)
    
    if evidence_lines:
        # 建议的边界限制 = 最靠近图表内容的正文行边界 + gap
        gap = 6.0
        if far_is_top:
            # 远端是顶部，限制 = 正文行的最大 y1 + gap
            suggested_limit = max(lb.y1 for lb in evidence_lines) + gap
        else:
            # 远端是底部，限制 = 正文行的最小 y0 - gap
            suggested_limit = min(lb.y0 for lb in evidence_lines) - gap
        return True, suggested_limit
    
    return False, 0.0


# ---------- P0-3: Phase D 后轻量去正文后处理 ----------
def _trim_far_side_text_post_autocrop(
    clip: fitz.Rect,
    text_lines: List[Tuple[fitz.Rect, float, str]],
    direction: str,
    *,
    typical_line_h: Optional[float] = None,
    scan_lines: int = 3,  # 扫描顶部/底部多少行
    min_width_ratio: float = 0.30,
    min_text_len: int = 15,
    font_min: float = 7.0,
    font_max: float = 16.0,
    gap: float = 6.0,
) -> Tuple[fitz.Rect, bool]:
    """
    Phase D 后的轻量去正文后处理（P0-3）。
    
    在 autocrop 完成后，扫描远端边缘附近 1-3 个行高范围内的正文行，
    如果检测到明确的正文，向内推 y0/y1（只动 y，不动 x）。
    
    这是对 P0-1 和 P0-2 的补充，用于处理：
    - 少量正文（1-2 行）
    - overlap 不够 20%
    - Phase C 没有触发
    
    Args:
        clip: 当前裁剪区域（Phase D autocrop 后）
        text_lines: 所有文本行 [(rect, font_size, text), ...]
        direction: 'above' 或 'below'
        typical_line_h: 典型行高（用于计算扫描范围）
        scan_lines: 扫描多少行（默认 3）
        min_width_ratio: 正文的最小宽度比例
        min_text_len: 正文的最小长度
        font_min/font_max: 正文字号范围
        gap: 裁剪后的间隙
    
    Returns:
        (new_clip, was_trimmed): 新的裁剪区域和是否进行了裁剪
    """
    if clip.height <= 1 or clip.width <= 1:
        return clip, False
    
    # 确定扫描范围
    if typical_line_h and typical_line_h > 0:
        scan_range = typical_line_h * scan_lines
    else:
        scan_range = 45.0  # 默认约 3 行（15pt/行）
    
    far_is_top = (direction == 'above')
    text_to_trim: List[fitz.Rect] = []
    
    for (lb, fs, text) in text_lines:
        txt = text.strip()
        if not txt:
            continue
        
        inter = lb & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        
        # 正文特征检查
        width_ratio = inter.width / max(1.0, clip.width)
        if width_ratio < min_width_ratio:
            continue
        if len(txt) < min_text_len:
            continue
        if not (font_min <= fs <= font_max):
            continue
        
        # 检查是否在远端边缘的扫描范围内
        if far_is_top:
            dist = lb.y0 - clip.y0
            if dist < scan_range:
                text_to_trim.append(lb)
        else:
            dist = clip.y1 - lb.y1
            if dist < scan_range:
                text_to_trim.append(lb)
    
    if not text_to_trim:
        return clip, False
    
    # 计算新边界
    new_clip = fitz.Rect(clip)
    if far_is_top:
        # 远端是顶部，向内推 y0
        max_y1 = max(lb.y1 for lb in text_to_trim)
        new_y0 = max_y1 + gap
        # 安全检查：不能裁剪超过 50% 高度
        if new_y0 < clip.y0 + 0.5 * clip.height:
            new_clip = fitz.Rect(clip.x0, new_y0, clip.x1, clip.y1)
    else:
        # 远端是底部，向内推 y1
        min_y0 = min(lb.y0 for lb in text_to_trim)
        new_y1 = min_y0 - gap
        # 安全检查：不能裁剪超过 50% 高度
        if new_y1 > clip.y0 + 0.5 * clip.height:
            new_clip = fitz.Rect(clip.x0, clip.y0, clip.x1, new_y1)
    
    was_trimmed = (new_clip != clip)
    return new_clip, was_trimmed


# ---------- P1-07: 精裁验收阈值动态化 ----------
@dataclass
class AcceptanceThresholds:
    """验收阈值配置（根据图表尺寸动态调整）"""
    relax_h: float      # 高度保留比例阈值
    relax_a: float      # 面积保留比例阈值
    relax_ink: float    # 墨迹密度保留比例阈值
    relax_cov: float    # 对象覆盖率保留比例阈值（用于 Figure）
    relax_text: float   # 文本行数保留比例阈值（用于 Table）
    description: str    # 阈值级别描述


def _adaptive_acceptance_thresholds(
    base_height: float,
    *,
    is_table: bool = False,
    far_cov: float = 0.0,
) -> AcceptanceThresholds:
    """
    P1-07: 根据基线高度和远侧覆盖率动态计算验收阈值。
    
    策略：
    - 大图（>400pt）：允许更激进的精裁，因为大图有更多余量
    - 中等图（200-400pt）：使用默认阈值
    - 小图（<200pt）：更保守，避免过度裁切导致内容丢失
    - 远侧文字覆盖率越高，允许缩小得越多（优先移除正文）
    
    Args:
        base_height: 基线窗口高度（pt）
        is_table: 是否为表格（表格使用更宽松的阈值）
        far_cov: 远侧文字覆盖率（0.0-1.0）
    
    Returns:
        AcceptanceThresholds 对象
    """
    # 基础阈值（根据尺寸分层）
    if base_height > 400:
        # 大图：可以更激进
        base_h, base_a = (0.50, 0.45) if is_table else (0.55, 0.50)
        base_ink, base_cov, base_text = 0.85, 0.80, 0.70
        desc = "large"
    elif base_height > 200:
        # 中等图：默认阈值
        base_h, base_a = (0.50, 0.45) if is_table else (0.60, 0.55)
        base_ink, base_cov, base_text = 0.90, 0.85, 0.75
        desc = "medium"
    else:
        # 小图：更保守
        base_h, base_a = (0.65, 0.60) if is_table else (0.70, 0.65)
        base_ink, base_cov, base_text = 0.92, 0.88, 0.80
        desc = "small"
    
    # 根据远侧覆盖率进一步调整（远侧文字越多，允许缩小得越多）
    # 这部分逻辑与原来的分层策略一致，但现在集中在一个函数中
    if far_cov >= 0.60:  # 极高覆盖率（>60%）：很可能是大段正文
        base_h = min(base_h, 0.35)
        base_a = min(base_a, 0.25)
        base_ink = min(base_ink, 0.70)
        base_cov = min(base_cov, 0.70)
        base_text = min(base_text, 0.55)
        desc += "+high_far_cov"
    elif far_cov >= 0.30:  # 高覆盖率（30-60%）：可能是多行段落
        base_h = min(base_h, 0.45)
        base_a = min(base_a, 0.35)
        base_ink = min(base_ink, 0.75)
        base_cov = min(base_cov, 0.75)
        base_text = min(base_text, 0.60)
        desc += "+med_far_cov"
    elif far_cov >= 0.18:  # 中等覆盖率（18-30%）：少量文字
        base_h = min(base_h, 0.50)
        base_a = min(base_a, 0.40)
        base_ink = min(base_ink, 0.80)
        base_cov = min(base_cov, 0.80)
        base_text = min(base_text, 0.65)
        desc += "+low_far_cov"
    
    return AcceptanceThresholds(
        relax_h=base_h,
        relax_a=base_a,
        relax_ink=base_ink,
        relax_cov=base_cov,
        relax_text=base_text,
        description=desc
    )


# ---------- Paragraph/column heuristics for table scoring ----------
def _paragraph_ratio(
    clip: fitz.Rect,
    text_lines: List[Tuple[fitz.Rect, float, str]],
    *,
    width_ratio: float = 0.55,
    font_min: float = 7.0,
    font_max: float = 16.0,
) -> float:
    total = 0
    para = 0
    for (lb, fs, tx) in text_lines:
        inter = lb & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        total += 1
        if (inter.width / max(1.0, clip.width)) >= width_ratio and (font_min <= fs <= font_max):
            para += 1
    if total == 0:
        return 0.0
    return para / float(total)


def _estimate_column_peaks(
    clip: fitz.Rect,
    text_lines: List[Tuple[fitz.Rect, float, str]],
    *,
    bin_size: float = 12.0,
    min_lines_per_peak: int = 3,
) -> int:
    # Histogram of left x0 positions within clip
    bins: Dict[int, int] = {}
    for (lb, fs, tx) in text_lines:
        inter = lb & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        b = int(max(0.0, (inter.x0 - clip.x0)) // max(1.0, bin_size))
        bins[b] = bins.get(b, 0) + 1
    if not bins:
        return 0
    # Count contiguous runs above threshold as one peak
    peaks = 0
    prev_on = False
    for idx in range(0, max(bins.keys()) + 1):
        on = bins.get(idx, 0) >= min_lines_per_peak
        if on and not prev_on:
            peaks += 1
        prev_on = on
    return peaks


def _line_density(
    clip: fitz.Rect,
    draw_items: List[DrawItem],
    *,
    min_width_frac: float = 0.4,
) -> float:
    H = 0
    V = 0
    for it in draw_items:
        inter = it.rect & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        if it.orient == 'H' and (inter.width / max(1.0, clip.width)) >= min_width_frac:
            H += 1
        elif it.orient == 'V' and (inter.height / max(1.0, clip.height)) >= min_width_frac:
            V += 1
    # Normalize roughly assuming 8 lines as dense
    return min(1.0, (H + V) / 8.0)


def snap_clip_edges(
    clip: fitz.Rect,
    draw_items: List[DrawItem],
    *,
    snap_px: float = 14.0,
) -> fitz.Rect:
    # Snap top/bottom to nearest horizontal line within +/- snap_px
    top = clip.y0
    bottom = clip.y1
    best_top = top
    best_bot = bottom
    best_top_dist = snap_px + 1
    best_bot_dist = snap_px + 1
    for it in draw_items:
        if it.orient != 'H':
            continue
        y_mid = 0.5 * (it.rect.y0 + it.rect.y1)
        # Top snap
        d_top = abs(y_mid - top)
        if d_top <= snap_px and d_top < best_top_dist:
            best_top_dist = d_top
            best_top = y_mid
        # Bottom snap
        d_bot = abs(y_mid - bottom)
        if d_bot <= snap_px and d_bot < best_bot_dist:
            best_bot_dist = d_bot
            best_bot = y_mid
    if best_bot - best_top >= 40.0:
        return fitz.Rect(clip.x0, best_top, clip.x1, best_bot)
    return clip


# ============================================================================
# Caption Detection Helper Functions (for smart caption identification)
# ============================================================================

def get_page_images(page: "fitz.Page") -> List[fitz.Rect]:
    """提取页面中所有图像对象的边界框"""
    images: List[fitz.Rect] = []
    try:
        dict_data = page.get_text("dict")
        for blk in dict_data.get("blocks", []):
            if blk.get("type", 0) == 1 and "bbox" in blk:  # type=1 表示图像
                images.append(fitz.Rect(*blk["bbox"]))
    except Exception as e:
        page_no = getattr(page, "number", None)
        extra = {'stage': 'get_page_images'}
        if isinstance(page_no, int):
            extra['page'] = page_no + 1
        logger.warning(f"Failed to parse page images: {e}", extra=extra)
    return images


def get_page_drawings(page: "fitz.Page") -> List[fitz.Rect]:
    """提取页面中所有绘图对象的边界框"""
    drawings: List[fitz.Rect] = []
    try:
        for dr in page.get_drawings():
            r = dr.get("rect")
            if r and isinstance(r, fitz.Rect):
                drawings.append(r)
    except Exception as e:
        page_no = getattr(page, "number", None)
        extra = {'stage': 'get_page_drawings'}
        if isinstance(page_no, int):
            extra['page'] = page_no + 1
        logger.warning(f"Failed to parse page drawings: {e}", extra=extra)
    return drawings


def get_next_line_text(block: Dict, current_line_idx: int) -> str:
    """获取当前行的下一行文本"""
    lines = block.get("lines", [])
    if current_line_idx + 1 < len(lines):
        next_line = lines[current_line_idx + 1]
        text = "".join(sp.get("text", "") for sp in next_line.get("spans", []))
        return text.strip()
    return ""


def get_paragraph_length(block: Dict) -> int:
    """计算 block 中所有文本的总长度"""
    total_len = 0
    for ln in block.get("lines", []):
        for sp in ln.get("spans", []):
            total_len += len(sp.get("text", ""))
    return total_len


def is_bold_text(spans: List[Dict]) -> bool:
    """判断文本是否加粗（检查 font flags）"""
    # Font flags bit 4 (value 16) 表示 bold
    return any(sp.get("flags", 0) & 16 for sp in spans)


def min_distance_to_rects(rect: fitz.Rect, rect_list: List[fitz.Rect]) -> float:
    """计算 rect 到 rect_list 中所有矩形的最小距离"""
    if not rect_list:
        return float('inf')
    
    min_dist = float('inf')
    for r in rect_list:
        # 计算垂直距离（caption 通常在图像的上方或下方）
        dist_above = abs(rect.y0 - r.y1)  # caption 在图下方
        dist_below = abs(rect.y1 - r.y0)  # caption 在图上方
        dist = min(dist_above, dist_below)
        min_dist = min(min_dist, dist)
    
    return min_dist


def is_likely_reference_context(text: str) -> bool:
    """判断文本是否像正文引用（而非图注描述）"""
    text_lower = text.lower()
    
    # 正文引用特征关键词
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
    """判断文本是否像图注描述（而非正文引用）"""
    text_lower = text.lower()
    
    # 图注特征关键词
    caption_patterns = [
        r'^(figure|table|fig\.|图|表)\s+\d+[:：.]',  # 以 "Figure 1:" 开头
        r'shows?', r'illustrates?', r'depicts?', r'displays?',
        r'compares?', r'presents?', r'demonstrates?',
        r'显示', r'展示', r'说明', r'比较', r'给出', r'呈现',
    ]
    
    for pat in caption_patterns:
        if re.search(pat, text_lower):
            return True
    
    return False


def find_all_caption_candidates(
    page: "fitz.Page",
    page_num: int,
    pattern: re.Pattern,
    kind: str = 'figure'
) -> List[CaptionCandidate]:
    """
    在单页中找到所有匹配 pattern 的候选 caption。
    
    参数:
        page: PyMuPDF 页面对象
        page_num: 页码（0-based）
        pattern: 匹配 caption 的正则表达式（需要有一个捕获组提取编号）
        kind: 'figure' 或 'table'
    
    返回:
        CaptionCandidate 列表
    """
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
                
                # 拼接当前行的完整文本
                text = "".join(sp.get("text", "") for sp in spans)
                text_stripped = text.strip()
                
                # 尝试匹配 pattern
                match = pattern.match(text_stripped)
                if match:
                    # --- P0-03 + P1-08: 根据 kind 提取正确的编号（兼容多种捕获结构）---
                    if kind == 'figure':
                        number = _extract_figure_ident(match)
                    elif kind == 'table':
                        number = _extract_table_ident(match)
                    else:
                        try:
                            number = (match.group(1) or "").strip()
                        except IndexError:
                            number = ""
                    
                    if not number:
                        continue  # 跳过无效的编号
                    
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
                        score=0.0  # 初始分数为 0
                    )
                    candidates.append(candidate)
    
    except Exception as e:
        # 如果页面解析失败，返回空列表
        logger.warning(f"Failed to parse page {page_num + 1} for {kind} captions: {e}")
    
    return candidates


def score_caption_candidate(
    candidate: CaptionCandidate,
    images: List[fitz.Rect],
    drawings: List[fitz.Rect],
    debug: bool = False
) -> float:
    """
    为候选 caption 打分，判断其是真实图注的可能性。
    
    评分维度（总分 100）：
    1. 位置特征（40分）：距离图像/绘图对象的距离
    2. 格式特征（30分）：字体加粗、独立成段、后续标点
    3. 结构特征（20分）：下一行有描述、段落长度
    4. 上下文特征（10分）：语义分析（图注描述 vs 正文引用）
    
    参数:
        candidate: 候选项
        images: 页面中所有图像对象
        drawings: 页面中所有绘图对象
        debug: 是否输出调试信息
    
    返回:
        得分（0-100+）
    """
    score = 0.0
    details = {}  # 用于调试
    
    # === 1. 位置特征（40分）===
    # 计算与图像/绘图对象的最小距离
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
        # 距离过远，但还有对象，给予少量分数
        position_score = max(0, 5.0 - min_dist / 50.0)
    else:
        # 页面没有任何图像对象，无法判断（给予中等分数）
        position_score = 15.0
    
    score += position_score
    details['position'] = position_score
    details['min_dist'] = min_dist
    
    # === 2. 格式特征（30分）===
    format_score = 0.0
    
    # 2.1 检查是否加粗（15分）
    if is_bold_text(candidate.spans):
        format_score += 15.0
        details['bold'] = True
    else:
        details['bold'] = False
    
    # 2.2 检查是否独立成段或行数较少（10分）
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
        # 行数过多，可能是长段落中的引用
        format_score += 0.0
        details['lines'] = num_lines
    
    # 2.3 检查后续是否有标点符号（冒号、句点、破折号）（5分）
    text_prefix = candidate.text[:40]  # 只检查前 40 个字符
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
    
    # 3.1 检查下一行是否有描述性文字（12分）
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
    
    # 3.2 检查段落总长度（长段落可能是正文引用，扣分）（8分）
    para_length = get_paragraph_length(candidate.block)
    if para_length < 150:
        # 短段落，很可能是图注
        structure_score += 8.0
        details['para_length'] = para_length
    elif para_length < 300:
        structure_score += 4.0
        details['para_length'] = para_length
    elif para_length < 600:
        # 中等长度
        structure_score += 0.0
        details['para_length'] = para_length
    else:
        # 长段落，很可能是正文引用，扣分
        structure_score -= 8.0
        details['para_length'] = para_length
    
    score += structure_score
    details['structure'] = structure_score
    
    # === 4. 上下文特征（10分）===
    context_score = 0.0
    
    # 4.1 检查是否像图注描述（加分）
    if is_likely_caption_context(candidate.text):
        context_score += 10.0
        details['context'] = 'caption'
    # 4.2 检查是否像正文引用（扣分）
    elif is_likely_reference_context(candidate.text):
        context_score -= 15.0  # 正文引用给予较重的负分
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


def select_best_caption(
    candidates: List[CaptionCandidate],
    page: "fitz.Page",
    *,
    doc: Optional["fitz.Document"] = None,
    min_score_threshold: float = 25.0,
    debug: bool = False
) -> Optional[CaptionCandidate]:
    """
    从候选列表中选择得分最高的真实图注。
    
    参数:
        candidates: 候选列表
        page: 页面对象（用于获取图像/绘图对象）
        min_score_threshold: 最低得分阈值（低于此值的候选项将被忽略）
        debug: 是否输出调试信息
    
    返回:
        得分最高的候选项，如果没有合格候选则返回 None
    """
    if not candidates:
        return None
    
    # 为每个候选项评分
    scored_candidates: List[Tuple[float, CaptionCandidate]] = []
    for cand in candidates:
        score_page = page
        if doc is not None:
            try:
                score_page = doc[cand.page]
            except Exception as e:
                logger.warning(
                    f"Failed to access page {cand.page + 1} for caption scoring: {e}",
                    extra={'page': cand.page + 1, 'stage': 'select_best_caption'}
                )
                score_page = page
        images = get_page_images(score_page)
        drawings = get_page_drawings(score_page)
        score = score_caption_candidate(cand, images, drawings, debug=debug)
        cand.score = score  # 更新候选项的得分
        scored_candidates.append((score, cand))
    
    # 按得分降序排序
    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    
    if debug:
        print(f"\n=== All Candidates for {candidates[0].kind} {candidates[0].number} ===")
        for score, cand in scored_candidates:
            print(f"  Score {score:5.1f}: page {cand.page + 1}, y={cand.rect.y0:.1f}, text='{cand.text[:50]}...'")
    
    # 选择得分最高的候选
    best_score, best_candidate = scored_candidates[0]
    
    # 检查是否达到最低分数阈值
    if best_score < min_score_threshold:
        if debug:
            print(f"  >>> Best score {best_score:.1f} is below threshold {min_score_threshold}, rejecting all candidates")
        return None
    
    if debug:
        print(f"  >>> Selected: page {best_candidate.page + 1}, score {best_score:.1f}")
    
    return best_candidate


def build_caption_index(
    doc: "fitz.Document",
    figure_pattern: Optional[re.Pattern] = None,
    table_pattern: Optional[re.Pattern] = None,
    debug: bool = False
) -> CaptionIndex:
    """
    预扫描全文，建立 caption 索引（记录所有 Figure/Table 编号的所有出现位置）。
    
    参数:
        doc: PyMuPDF 文档对象
        figure_pattern: 匹配 Figure caption 的正则表达式
        table_pattern: 匹配 Table caption 的正则表达式
        debug: 是否输出调试信息
    
    返回:
        CaptionIndex 对象
    """
    # --- P0-03 修复：默认 pattern 使用更新后的捕获组结构 ---
    if figure_pattern is None:
        # Figure 正则（命名分组）：与 extract_figures 内部的 figure_line_re 对齐，供 _extract_figure_ident 解析
        figure_pattern = re.compile(
            r"^\s*(?P<label>Extended\s+Data\s+Figure|Supplementary\s+(?:Figure|Fig\.?)|Figure|Fig\.?|图表|附图|图)\s*"
            r"(?:(?P<s_prefix>S)\s*(?P<s_id>(?:\d+|[IVX]{1,6}))|(?P<roman>[IVX]{1,6})|(?P<num>\d+))"
            r"(?:\s*[-–]?\s*[A-Za-z]|\s*\([A-Za-z]\))?"
            r"(?:\s*\(continued\)|\s*续|\s*接上页)?",
            re.IGNORECASE
        )
    
    if table_pattern is None:
        # Table 正则：group(1) = 附录表(A1/S1), group(2) = 罗马数字, group(3) = 普通数字
        table_pattern = re.compile(
            r"^\s*(?:Extended\s+Data\s+Table|Supplementary\s+Table|Table|Tab\.?|表)\s*"
            r"(?:"
            r"(S?\d+|[A-Z]\d+)|"        # group(1): S前缀编号或附录表 (S1, A1, B2)
            r"([IVX]{1,5})|"            # group(2): 罗马数字 (I, II, III, IV, V)
            r"(\d+)"                    # group(3): 普通数字 (1, 2, 3)
            r")"
            r"(?:\s*\(continued\)|\s*续|\s*接上页)?",  # 可选的续页标记
            re.IGNORECASE
        )
    
    index_dict: Dict[str, List[CaptionCandidate]] = {}
    
    if debug:
        print(f"\n=== Building Caption Index (total {len(doc)} pages) ===")
    
    # 扫描每一页
    for pno in range(len(doc)):
        page = doc[pno]
        
        # 查找 Figure 候选
        fig_candidates = find_all_caption_candidates(page, pno, figure_pattern, kind='figure')
        for cand in fig_candidates:
            key = f"figure_{cand.number}"
            if key not in index_dict:
                index_dict[key] = []
            index_dict[key].append(cand)
        
        # 查找 Table 候选
        table_candidates = find_all_caption_candidates(page, pno, table_pattern, kind='table')
        for cand in table_candidates:
            key = f"table_{cand.number}"
            if key not in index_dict:
                index_dict[key] = []
            index_dict[key].append(cand)
    
    if debug:
        print(f"  Found {len(index_dict)} unique figure/table numbers")
        for key, cands in sorted(index_dict.items()):
            print(f"    {key}: {len(cands)} occurrence(s) across pages {', '.join(str(c.page+1) for c in cands)}")
    
    return CaptionIndex(candidates=index_dict)


# 主流程：从 PDF 提取各图（通过图注定位）并导出 PNG
# 参数说明：
# - pdf_path：PDF 路径
# - out_dir：输出图片目录（会自动创建）
# - dpi：渲染分辨率（影响清晰度与性能）
# - clip_height：图注上方候选窗口高度（点，72pt=1英寸）
# - margin_x：左右留白（点）
# - caption_gap：图注与裁剪下边界的间距（点）
# - max_caption_chars：基于图注的文件名最大字符数
# - min_figure/max_figure：选择提取的图号范围
# - autocrop：是否启用像素级去白边
# - autocrop_pad_px：去白边后保留的像素级 padding
# - autocrop_white_threshold：白色阈值，越低越“严”
# - below_figs：强制对给定图号从图注“下方”裁剪
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
    # --- P0-03 修复：改为 List[str] 以支持 "S1" 等附录编号 ---
    below_figs: Optional[List[str]] = None,
    above_figs: Optional[List[str]] = None,
    # A: text-trim options
    text_trim: bool = False,
    text_trim_width_ratio: float = 0.5,
    text_trim_font_min: float = 7.0,
    text_trim_font_max: float = 16.0,
    text_trim_gap: float = 6.0,
    adjacent_th: float = 24.0,
    # A+: far-text trim options (dual-threshold)
    far_text_th: float = 300.0,
    far_text_para_min_ratio: float = 0.30,
    far_text_trim_mode: str = "aggressive",
    # P1-1: 下调阈值以覆盖"中间地带"（约 3-7 行）
    far_side_min_dist: float = 50.0,  # 从 100.0 降低到 50.0
    far_side_para_min_ratio: float = 0.12,  # 从 0.20 降低到 0.12
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
    # --- P0-03 修复：改为 List[str] 以支持 "S1" 等附录编号 ---
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
    pdf_name = os.path.basename(pdf_path)
    # 打开 PDF 文档并准备输出目录
    doc = fitz.open(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    # --- P0-03 + P1-08 修复：匹配多种图注格式，支持 S 前缀、罗马数字、子图标签 ---
    # 2025-12-23 补充：支持 Supplementary + 罗马数字（如 "Supplementary Figure IV" / "Figure SIV"）
    # 命名分组说明（供 _extract_figure_ident 使用）：
    #   label:  图注类型前缀（含 Supplementary/Extended Data 等）
    #   s_prefix/s_id: 显式 S 前缀 + 编号（阿拉伯或罗马）
    #   roman:  普通罗马数字编号（I, II, III, IV, ...）
    #   num:    普通数字编号（1, 2, 3, ...）
    # P1-08 新增支持：
    #   - "Fig. 1A" / "Figure 1a"（带子图标签）
    #   - "图1"（中文无空格）
    #   - "Figure I" / "Figure II"（罗马数字）
    #   - "Figure 1 (a)"（子图在括号中）
    figure_line_re = re.compile(
        r"^\s*(?P<label>Extended\s+Data\s+Figure|Supplementary\s+(?:Figure|Fig\.?)|Figure|Fig\.?|图表|附图|图)\s*"
        r"(?:(?P<s_prefix>S)\s*(?P<s_id>(?:\d+|[IVX]{1,6}))|(?P<roman>[IVX]{1,6})|(?P<num>\d+))"
        r"(?:\s*[-–]?\s*[A-Za-z]|\s*\([A-Za-z]\))?"  # 可选的子图标签（如 1a, 1-a, 1(a)）
        r"(?:\s*\(continued\)|\s*续|\s*接上页)?",  # 可选的续页标记
        re.IGNORECASE,
    )
    seen_counts: Dict[int, int] = {}
    records: List[AttachmentRecord] = []
    
    # === Smart Caption Detection: 预扫描建立索引 ===
    caption_index: Optional[CaptionIndex] = None
    if smart_caption_detection:
        if debug_captions:
            print(f"\n{'='*60}")
            print(f"SMART CAPTION DETECTION ENABLED")
            print(f"{'='*60}")
        caption_index = build_caption_index(doc, figure_pattern=figure_line_re, debug=debug_captions)
    
    # === Adaptive Line Height: 统计文档行高并自适应调整参数 ===
    if adaptive_line_height:
        line_metrics = _estimate_document_line_metrics(doc, sample_pages=5, debug=debug_captions)
        typical_line_h = line_metrics['typical_line_height']
        
        # 自适应参数计算（基于行高的倍数）
        # 仅当参数为默认值时才替换（避免用户自定义参数被覆盖）
        if adjacent_th == 24.0:  # 默认值
            adjacent_th = 2.0 * typical_line_h
        if far_text_th == 300.0:  # 默认值
            # 2025-12-30 修复：提高倍数从 10.0 到 15.0
            # 原因：Figure 18/22/29 残留文字距离 caption 128-142pt，超出 10×行高(109pt)
            # 15×行高 ≈ 163.5pt 可以覆盖这些情况
            far_text_th = 15.0 * typical_line_h
        if text_trim_gap == 6.0:  # 默认值
            text_trim_gap = 0.5 * typical_line_h
        if far_side_min_dist == 50.0:  # P1-1 调整后的新默认值
            # P1-1 调整：使用 3.0× 行高，以便检测"中间地带"文字（约 3-7 行）
            # 3.0 × line_height ≈ 33pt，能够覆盖更多的干扰文字
            far_side_min_dist = 3.0 * typical_line_h
        
        if debug_captions:
            print(f"ADAPTIVE PARAMETERS (based on line_height={typical_line_h:.1f}pt):")
            print(f"  adjacent_th:      {adjacent_th:.1f} pt (2.0× line_height)")
            print(f"  far_text_th:      {far_text_th:.1f} pt (15.0× line_height)")
            print(f"  text_trim_gap:    {text_trim_gap:.1f} pt (0.5× line_height)")
            print(f"  far_side_min_dist:{far_side_min_dist:.1f} pt (3.0× line_height)")
            print()

    # --- P0-03 修复：改为返回 List[str] 以支持 "S1" 等附录编号 ---
    def _parse_fig_list(s: str) -> List[str]:
        """解析图表编号列表，支持 "1,2,S1,S2" 等格式"""
        out: List[str] = []
        for part in (s or "").split(','):
            part = part.strip()
            if not part:
                continue
            # 保留原始字符串标识符
            out.append(part)
        return out

    anchor_mode = os.getenv('EXTRACT_ANCHOR_MODE', '').lower()
    # Global side prescan (figures)
    global_side: Optional[str] = None
    if os.getenv('GLOBAL_ANCHOR', 'auto').lower() == 'auto':
        try:
            ga_margin = float(os.getenv('GLOBAL_ANCHOR_MARGIN', '0.02'))
        except ValueError:
            ga_margin = 0.02
        above_total = 0.0
        below_total = 0.0
        for pno_scan in range(len(doc)):
            page_s = doc[pno_scan]
            page_rect_s = page_s.rect
            dict_data_s = page_s.get_text("dict")
            # simple image/vector coverage for quick scoring
            imgs: List[fitz.Rect] = []
            for blk in dict_data_s.get("blocks", []):
                if blk.get("type", 0) == 1 and "bbox" in blk:
                    imgs.append(fitz.Rect(*blk["bbox"]))
            vecs: List[fitz.Rect] = []
            try:
                for dr in page_s.get_drawings():
                    if isinstance(dr, dict) and "rect" in dr:
                        vecs.append(fitz.Rect(*dr["rect"]))
            except Exception as e:
                logger.warning(f"Failed to get drawings on page {pno_scan + 1}: {e}", extra={'page': pno_scan + 1, 'stage': 'global_anchor_prescan'})
            def obj_ratio(clip: fitz.Rect) -> float:
                area = max(1.0, clip.width * clip.height)
                acc = 0.0
                for r in imgs:
                    inter = r & clip
                    if inter.height > 0 and inter.width > 0:
                        acc += inter.width * inter.height
                for r in vecs:
                    inter = r & clip
                    if inter.height > 0 and inter.width > 0:
                        acc += inter.width * inter.height
                return min(1.0, acc / area)
            # find figure captions
            cap_re = re.compile(r"^\s*(?:(?:Extended\s+Data\s+Figure|Supplementary\s+Figure|Figure|Fig\.?|图表|附图|图)\s*(?:S\s*)?(\d+))\b", re.IGNORECASE)
            # flatten lines
            lines: List[Tuple[fitz.Rect, str]] = []
            for blk in dict_data_s.get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                for ln in blk.get("lines", []):
                    text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
                    lines.append((fitz.Rect(*(ln.get("bbox", [0,0,0,0]))), text))
            caps: List[fitz.Rect] = [r for (r,t) in lines if cap_re.match(t.strip())]
            caps.sort(key=lambda r: r.y0)
            x_left_s = page_rect_s.x0 + margin_x
            x_right_s = page_rect_s.x1 - margin_x
            for i_c, cap in enumerate(caps):
                prev_c = caps[i_c-1] if i_c-1 >= 0 else None
                next_c = caps[i_c+1] if i_c+1 < len(caps) else None
                topb = (prev_c.y1 + 8) if prev_c else page_rect_s.y0
                botb = cap.y0 - caption_gap
                yt = max(page_rect_s.y0, botb - clip_height, topb)
                yb = min(botb, yt + clip_height)
                yb = max(yt + 40, yb)
                clip_above = fitz.Rect(x_left_s, yt, x_right_s, min(yb, page_rect_s.y1))
                top2 = cap.y1 + caption_gap
                bot2 = (next_c.y0 - 8) if next_c else page_rect_s.y1
                y0b = min(max(page_rect_s.y0, top2), page_rect_s.y1 - 40)
                y1b = min(bot2, y0b + clip_height)
                y1b = max(y0b + 40, min(y1b, page_rect_s.y1))
                clip_below = fitz.Rect(x_left_s, y0b, x_right_s, y1b)
                try:
                    pix_a = page_s.get_pixmap(matrix=fitz.Matrix(1,1), clip=clip_above, alpha=False)
                    ink_a = estimate_ink_ratio(pix_a)
                except Exception as e:
                    logger.warning(f"Failed to render prescan clip (above) on page {pno_scan + 1}: {e}", extra={'page': pno_scan + 1, 'stage': 'global_anchor_prescan'})
                    ink_a = 0.0
                try:
                    pix_b = page_s.get_pixmap(matrix=fitz.Matrix(1,1), clip=clip_below, alpha=False)
                    ink_b = estimate_ink_ratio(pix_b)
                except Exception as e:
                    logger.warning(f"Failed to render prescan clip (below) on page {pno_scan + 1}: {e}", extra={'page': pno_scan + 1, 'stage': 'global_anchor_prescan'})
                    ink_b = 0.0
                above_total += 0.6 * ink_a + 0.4 * obj_ratio(clip_above)
                below_total += 0.6 * ink_b + 0.4 * obj_ratio(clip_below)
        # P1-05: 全局锚点微弱优势回退 - 当差距很小时回退到按页独立决策
        total_score = above_total + below_total
        if total_score > 0:
            score_diff_ratio = abs(below_total - above_total) / total_score
        else:
            score_diff_ratio = 0
        
        CLOSE_MARGIN = 0.05  # 5% 以内视为"势均力敌"
        
        if score_diff_ratio < CLOSE_MARGIN:
            # 差距太小，不使用全局方向，按页独立决策
            global_side = None
            logger.info(f"Global figure anchor: UNDECIDED (diff={score_diff_ratio:.1%} < {CLOSE_MARGIN:.0%}, using per-page decision)")
        elif below_total > above_total * (1.0 + ga_margin):
            global_side = 'below'
            logger.info(f"Global figure anchor: BELOW (below={below_total:.2f} vs above={above_total:.2f}, diff={score_diff_ratio:.1%})")
        elif above_total > below_total * (1.0 + ga_margin):
            global_side = 'above'
            logger.info(f"Global figure anchor: ABOVE (above={above_total:.2f} vs below={below_total:.2f}, diff={score_diff_ratio:.1%})")
        else:
            global_side = None
            logger.info(f"Global figure anchor: AUTO (no clear preference, diff={score_diff_ratio:.1%})")
    # === 存储智能选择的结果（用于跨页查找）===
    smart_caption_cache: Dict[int, Tuple[fitz.Rect, str, int]] = {}  # {fig_no: (rect, caption, page_num)}
    
    for pno in range(len(doc)):
        # 遍历每一页，读取文本与对象布局
        page = doc[pno]
        page_rect = page.rect
        dict_data = page.get_text("dict")

        # 收集本页所有图注（line-level 聚合）：
        # 将连续的行在遇到下一处图注前合并为同一条 caption。
        # P0-03 类型修正：第一个元素是字符串标识符（如 "1", "S1"）
        captions_on_page: List[Tuple[str, fitz.Rect, str]] = []
        
        # === 智能 Caption 选择（如果启用）===
        if smart_caption_detection and caption_index:
            # 使用智能选择逻辑
            # 1. 找到本页所有潜在的 figure 编号
            # --- P0-03 修复：使用字符串标识符以支持 S1/S2 等附录编号 ---
            page_fig_idents: set[str] = set()
            for blk in dict_data.get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                for ln in blk.get("lines", []):
                    text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
                    m = figure_line_re.match(text.strip())
                    if m:
                        ident = _extract_figure_ident(m)
                        if ident and _ident_in_range(ident, min_figure, max_figure):
                            page_fig_idents.add(ident)
            
            # 2. 对每个 figure 编号，从索引中获取候选项并选择最佳的
            for fig_ident in sorted(page_fig_idents, key=lambda x: (not x.isdigit(), x)):
                # 从索引中获取所有候选项
                # --- P0-03 修复：使用字符串标识符 ---
                candidates = caption_index.get_candidates('figure', fig_ident)
                if not candidates:
                    continue

                if allow_continued:
                    # Continued 模式：按"页"独立判断（同号多页都可能是有效图注）
                    candidates_on_page = [c for c in candidates if c.page == pno]
                    if not candidates_on_page:
                        continue
                    best_candidate = select_best_caption(
                        candidates_on_page,
                        page,
                        doc=doc,
                        min_score_threshold=25.0,
                        debug=debug_captions,
                    )
                else:
                    # 非 continued：跨页选择"全局最优"图注，并缓存到其所属页（用于跳过正文引用页）
                    if fig_ident in smart_caption_cache:
                        cached_rect, cached_caption, cached_page = smart_caption_cache[fig_ident]
                        if cached_page == pno:
                            captions_on_page.append((fig_ident, cached_rect, cached_caption))
                        continue
                    best_candidate = select_best_caption(
                        candidates,
                        page,
                        doc=doc,
                        min_score_threshold=25.0,
                        debug=debug_captions,
                    )

                if best_candidate:
                    # 收集完整 caption 文本（合并后续行）
                    full_caption = best_candidate.text
                    cap_rect = best_candidate.rect

                    # 尝试合并后续行（同一 block 内）
                    block = best_candidate.block
                    lines = block.get("lines", [])
                    start_idx = best_candidate.line_idx + 1
                    parts = [full_caption]
                    for j in range(start_idx, len(lines)):
                        ln = lines[j]
                        t2 = "".join(sp.get("text", "") for sp in ln.get("spans", [])).strip()
                        if not t2 or figure_line_re.match(t2):
                            break
                        parts.append(t2)
                        cap_rect = cap_rect | fitz.Rect(*(ln.get("bbox", [0,0,0,0])))
                        if t2.endswith('.') or sum(len(p) for p in parts) > 240:
                            break
                    full_caption = " ".join(parts)

                    # Continued 模式：当前页命中即加入；非 continued：只加入 best 所在页
                    if allow_continued:
                        captions_on_page.append((fig_ident, cap_rect, full_caption))
                    else:
                        if best_candidate.page == pno:
                            captions_on_page.append((fig_ident, cap_rect, full_caption))
                        smart_caption_cache[fig_ident] = (cap_rect, full_caption, best_candidate.page)
        else:
            # === 原有逻辑：简单匹配 ===
            for blk in dict_data.get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                lines = blk.get("lines", [])
                i = 0
                while i < len(lines):
                    ln = lines[i]
                    text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
                    t = text.strip()
                    m = figure_line_re.match(t)
                    if not m:
                        i += 1
                        continue
                    # --- P0-03 修复：提取完整标识符（含 S 前缀）---
                    fig_ident = _extract_figure_ident(m)
                    if not fig_ident:
                        i += 1
                        continue
                    # 初始图注边界框来自当前行的 bbox
                    cap_rect = fitz.Rect(*(ln.get("bbox", [0,0,0,0])))
                    parts = [t]
                    char_count = len(t)
                    j = i + 1
                    while j < len(lines):
                        ln2 = lines[j]
                        t2 = "".join(sp.get("text", "") for sp in ln2.get("spans", [])).strip()
                        if not t2:
                            break
                        if figure_line_re.match(t2):
                            break
                        # 合并后续非空行到当前 caption，扩展边界框
                        parts.append(t2)
                        char_count += len(t2)
                        cap_rect = cap_rect | fitz.Rect(*(ln2.get("bbox", [0,0,0,0])))
                        if t2.endswith('.') or char_count > 240:
                            j += 1
                            break
                        j += 1
                    caption = " ".join(parts)
                    # --- P0-03 修复：使用字符串标识符进行范围检查 ---
                    if _ident_in_range(fig_ident, min_figure, max_figure):
                        captions_on_page.append((fig_ident, cap_rect, caption))
                    i = max(i+1, j)

        captions_on_page.sort(key=lambda t: t[1].y0)

        x_left = page_rect.x0 + margin_x
        x_right = page_rect.x1 - margin_x

        # 收集位图与矢量对象区域，后续用于估计“对象覆盖率”，辅助判断图区位置
        image_rects: List[fitz.Rect] = []
        for blk in dict_data.get("blocks", []):
            if blk.get("type", 0) == 1 and "bbox" in blk:
                image_rects.append(fitz.Rect(*blk["bbox"]))
        vector_rects: List[fitz.Rect] = []
        try:
            for dr in page.get_drawings():
                if isinstance(dr, dict) and "rect" in dr:
                    vector_rects.append(fitz.Rect(*dr["rect"]))
        except Exception as e:
            logger.warning(f"Failed to get drawings on page {pno + 1}: {e}", extra={'page': pno + 1, 'stage': 'extract_figures'})
        draw_items = collect_draw_items(page)

        def object_area_ratio(clip: fitz.Rect) -> float:
            # 计算候选裁剪区域中被位图/矢量对象覆盖的面积占比（0~1）
            area = max(1.0, clip.width * clip.height)
            acc = 0.0
            for r in image_rects:
                inter = r & clip
                if inter.width > 0 and inter.height > 0:
                    acc += inter.width * inter.height
            for r in vector_rects:
                inter = r & clip
                if inter.width > 0 and inter.height > 0:
                    acc += inter.width * inter.height
            return min(1.0, acc / area)

        def figure_score(clip: fitz.Rect) -> float:
            # 对候选窗口进行评分：低分辨率渲染的“墨迹密度”与“对象覆盖率”的加权和
            small_scale = 1.0
            mat_small = fitz.Matrix(small_scale, small_scale)
            try:
                pix = page.get_pixmap(matrix=mat_small, clip=clip, alpha=False)
                ink = estimate_ink_ratio(pix)
            except Exception as e:
                logger.warning(
                    f"Failed to render figure_score clip on page {pno + 1}: {e}",
                    extra={'page': pno + 1, 'stage': 'figure_score'}
                )
                ink = 0.0
            obj = object_area_ratio(clip)
            return 0.6 * ink + 0.4 * obj

        force_above = set(_parse_fig_list(os.getenv('EXTRACT_FORCE_ABOVE','')))
        def comp_count(clip: fitz.Rect) -> int:
            area = max(1.0, clip.width * clip.height)
            cand: List[fitz.Rect] = []
            for r in image_rects + vector_rects:
                inter = r & clip
                if inter.width > 0 and inter.height > 0:
                    if (inter.width * inter.height) / area >= object_min_area_ratio:
                        cand.append(inter)
            return len(_merge_rects(cand, merge_gap=object_merge_gap)) if cand else 0

        def ink_ratio_small(clip: fitz.Rect) -> float:
            small_scale = 1.0
            mat_small = fitz.Matrix(small_scale, small_scale)
            try:
                pix = page.get_pixmap(matrix=mat_small, clip=clip, alpha=False)
                return estimate_ink_ratio(pix)
            except Exception as e:
                logger.warning(
                    f"Failed to render ink_ratio_small clip on page {pno + 1}: {e}",
                    extra={'page': pno + 1, 'stage': 'ink_ratio_small'}
                )
                return 0.0
        # collect text lines once for this page (used by A / D)
        text_lines_all = _collect_text_lines(dict_data)

        for idx, (fig_no, cap_rect, caption) in enumerate(captions_on_page):
            count_prev = seen_counts.get(fig_no, 0)
            if count_prev >= 1 and not allow_continued:
                continue

            # QA-03: 收集并关联本条目的 debug 产物（相对 out_dir）
            debug_artifacts: List[str] = []

            prev_cap = captions_on_page[idx-1][1] if idx-1 >= 0 else None
            next_cap = captions_on_page[idx+1][1] if idx+1 < len(captions_on_page) else None

            # 选择窗口（Anchor V1 or V2）
            if anchor_mode == 'v1':
                # 旧逻辑保留（上/下两个窗口）
                top_bound = (prev_cap.y1 + 8) if prev_cap else page_rect.y0
                bot_bound = cap_rect.y0 - caption_gap
                yt_above = max(page_rect.y0, bot_bound - clip_height, top_bound)
                yb_above = min(bot_bound, yt_above + clip_height)
                yb_above = max(yt_above + 40, yb_above)
                clip_above = fitz.Rect(x_left, yt_above, x_right, min(yb_above, page_rect.y1))

                top2 = cap_rect.y1 + caption_gap
                bot2 = (next_cap.y0 - 8) if next_cap else page_rect.y1
                yt_below = min(max(page_rect.y0, top2), page_rect.y1 - 40)
                yb_below = min(bot2, yt_below + clip_height)
                yb_below = max(yt_below + 40, min(yb_below, page_rect.y1))
                clip_below = fitz.Rect(x_left, yt_below, x_right, yb_below)

                crop_below = (below_figs is not None and fig_no in below_figs)
                crop_above = (above_figs is not None and fig_no in above_figs) or (fig_no in force_above)
                side = 'above'
                chosen_clip = clip_above
                if crop_below:
                    side, chosen_clip = 'below', clip_below
                elif crop_above:
                    side, chosen_clip = 'above', clip_above
                else:
                    try:
                        ra = figure_score(clip_above)
                        rb = figure_score(clip_below)
                        if rb > ra * 1.02:
                            side, chosen_clip = 'below', clip_below
                        else:
                            side, chosen_clip = 'above', clip_above
                    except Exception as e:
                        logger.warning(f"Figure score comparison failed on page {pno + 1}: {e}", extra={'page': pno + 1, 'stage': 'anchor_v1'})
                        side, chosen_clip = 'above', clip_above
                clip = chosen_clip
            else:
                # Anchor V2：多尺度滑窗
                # --- P0-04 修复：V2 也支持 --above/--below 强制方向 ---
                # 检查当前图号是否被强制指定方向
                forced_side: Optional[str] = None
                if below_figs is not None and fig_no in below_figs:
                    forced_side = 'below'
                    if debug_captions:
                        print(f"[DBG] Figure {fig_no}: forced direction=below (--below)")
                elif above_figs is not None and fig_no in above_figs:
                    forced_side = 'above'
                    if debug_captions:
                        print(f"[DBG] Figure {fig_no}: forced direction=above (--above)")
                elif fig_no in force_above:
                    forced_side = 'above'
                    if debug_captions:
                        print(f"[DBG] Figure {fig_no}: forced direction=above (EXTRACT_FORCE_ABOVE)")
                
                # 确定扫描方向：强制方向 > 全局方向 > 双向扫描
                effective_side = forced_side if forced_side else global_side
                
                scan_heights = os.getenv('SCAN_HEIGHTS', '')
                if scan_heights:
                    try:
                        heights = [float(h) for h in scan_heights.split(',') if h.strip()]
                    except ValueError as e:
                        logger.warning(
                            f"Invalid SCAN_HEIGHTS='{scan_heights}', using defaults: {e}",
                            extra={'page': pno + 1, 'stage': 'anchor_v2'}
                        )
                        heights = [240.0, 320.0, 420.0, 520.0, 640.0, 720.0, 820.0, 920.0]
                else:
                    # 与 argparse 默认值对齐（--scan-heights）
                    heights = [240.0, 320.0, 420.0, 520.0, 640.0, 720.0, 820.0, 920.0]
                step = 14.0
                try:
                    step = float(os.getenv('SCAN_STEP', '14'))
                except ValueError as e:
                    logger.warning(
                        f"Invalid SCAN_STEP='{os.getenv('SCAN_STEP', '')}', using default 14: {e}",
                        extra={'page': pno + 1, 'stage': 'anchor_v2'}
                    )

                dist_lambda = 0.0
                try:
                    # 与 argparse 默认值对齐（--scan-dist-lambda）
                    dist_lambda = float(os.getenv('SCAN_DIST_LAMBDA', '0.12'))
                except ValueError as e:
                    logger.warning(
                        f"Invalid SCAN_DIST_LAMBDA='{os.getenv('SCAN_DIST_LAMBDA', '')}', using default 0.12: {e}",
                        extra={'page': pno + 1, 'stage': 'anchor_v2'}
                    )
                    dist_lambda = 0.12

                def detect_top_edge_truncation(clip: fitz.Rect, objects: List[fitz.Rect], side: str) -> bool:
                    """
                    检测窗口边缘是否截断对象（方案B）
                    
                    参数:
                        clip: 候选窗口
                        objects: 页面中的所有对象（图像+绘图）
                        side: 窗口方向（'above' 或 'below'）
                    
                    返回:
                        True 如果检测到边缘截断大对象
                    
                    修复说明（2025-10-27）:
                        原逻辑反转：当对象边缘与clip重合时误判为截断，导致完整窗口被扣分
                        正确逻辑：检测对象是否延伸到clip外面（被clip边界截断）
                    """
                    min_obj_height = 50.0  # 最小对象高度阈值（pt）
                    
                    for obj in objects:
                        # 检查对象是否与窗口水平重叠
                        if not (obj.x0 < clip.x1 and obj.x1 > clip.x0):
                            continue
                        
                        # 根据方向检测边缘截断
                        if side == 'above':
                            # 检查顶部边缘（远离Caption一侧）
                            # 如果对象顶部在clip外面，且对象底部在clip内足够深度 → 被截断
                            if obj.y0 < clip.y0 and obj.y1 > clip.y0 + min_obj_height:
                                return True
                        else:  # below
                            # 检查底部边缘（远离Caption一侧）
                            # 如果对象底部在clip外面，且对象顶部在clip内足够深度 → 被截断
                            if obj.y1 > clip.y1 and obj.y0 < clip.y1 - min_obj_height:
                                return True
                    
                    return False
                
                def detect_excluded_sibling_objects(clip: fitz.Rect, objects: List[fitz.Rect], side: str) -> float:
                    """
                    2025-12-30 新增：检测窗口外是否存在"同属一组"的绘图对象
                    
                    问题背景：
                        对于多行子图（如 3x2 布局的 Figure 5），当窗口边缘恰好落在两行子图之间的间隙时，
                        detect_top_edge_truncation 不会检测到截断（因为没有单个对象跨越边界），
                        但实际上排除了上方的整行子图。
                    
                    检测逻辑：
                        1. 计算窗口内绘图对象的覆盖区域
                        2. 检测窗口外（远端）是否存在与窗口内对象"水平对齐"的绘图对象
                        3. 如果存在，返回被排除对象的面积占比（作为惩罚系数）
                    
                    参数:
                        clip: 候选窗口
                        objects: 页面中的所有对象
                        side: 窗口方向（'above' = 图在 caption 上方，远端是顶部）
                    
                    返回:
                        被排除对象面积 / 窗口内对象面积（比例越高说明截断越严重）
                    """
                    # 收集窗口内和窗口外的对象
                    inside_objs: List[fitz.Rect] = []
                    outside_objs: List[fitz.Rect] = []
                    
                    for obj in objects:
                        # 检查对象是否与窗口水平重叠
                        if not (obj.x0 < clip.x1 and obj.x1 > clip.x0):
                            continue
                        
                        # 对象中心点
                        obj_cy = (obj.y0 + obj.y1) / 2
                        
                        if clip.y0 <= obj_cy <= clip.y1:
                            # 对象中心在窗口内
                            inside_objs.append(obj)
                        elif side == 'above' and obj_cy < clip.y0:
                            # 对象在窗口上方（远端）
                            outside_objs.append(obj)
                        elif side == 'below' and obj_cy > clip.y1:
                            # 对象在窗口下方（远端）
                            outside_objs.append(obj)
                    
                    if not inside_objs or not outside_objs:
                        return 0.0
                    
                    # 计算窗口内对象的总面积
                    inside_area = sum(o.width * o.height for o in inside_objs)
                    if inside_area < 1.0:
                        return 0.0
                    
                    # 计算被排除对象的总面积
                    outside_area = sum(o.width * o.height for o in outside_objs)
                    
                    # 返回比例
                    return outside_area / inside_area

                def fig_score(clip: fitz.Rect) -> float:
                    # 小分辨率渲染估计墨迹
                    small_scale = 1.0
                    try:
                        pix = page.get_pixmap(matrix=fitz.Matrix(small_scale, small_scale), clip=clip, alpha=False)
                        ink = estimate_ink_ratio(pix)
                    except Exception as e:
                        logger.warning(
                            f"Failed to render fig_score clip on page {pno + 1}: {e}",
                            extra={'page': pno + 1, 'kind': 'figure', 'id': str(fig_no), 'stage': 'fig_score'}
                        )
                        ink = 0.0
                    obj = object_area_ratio(clip)
                    para = _paragraph_ratio(clip, text_lines_all, width_ratio=text_trim_width_ratio, font_min=text_trim_font_min, font_max=text_trim_font_max)
                    # 增加组件数量奖励（鼓励捕获更多子图）
                    comp_cnt = comp_count(clip)
                    comp_bonus = 0.08 * min(1.0, comp_cnt / 3.0)  # 3+组件额外加分
                    
                    # 方案A：调整评分权重（墨迹35% → 对象40%）
                    # 增加高度奖励（鼓励完整捕获）
                    height_bonus = 0.05 * min(1.0, clip.height / 400.0)
                    base = 0.35 * ink + 0.40 * obj - 0.2 * para + comp_bonus + height_bonus
                    
                    # 距离罚项：候选窗离 caption 越远，得分越低
                    if cap_rect:
                        if clip.y1 <= cap_rect.y0:  # above
                            dist = abs(cap_rect.y0 - clip.y1)
                        else:  # below
                            dist = abs(clip.y0 - cap_rect.y1)
                        base -= dist_lambda * (dist / max(1.0, page_rect.height))
                    return base

                # 获取页面所有对象（用于边缘截断检测）
                all_page_objects = image_rects + vector_rects
                
                candidates: List[Tuple[float, str, fitz.Rect]] = []
                # above scanning
                top_bound = (prev_cap.y1 + 8) if prev_cap else page_rect.y0
                bot_bound = cap_rect.y0 - caption_gap
                # 防跨：上方窗口不得越过上一/当前 caption 的中线
                # 使用环境变量传递 guard（避免函数内依赖 args）
                try:
                    cap_mid_guard = float(os.getenv('CAPTION_MID_GUARD', '6.0'))
                except ValueError as e:
                    logger.warning(
                        f"Invalid CAPTION_MID_GUARD='{os.getenv('CAPTION_MID_GUARD', '')}', using default 6.0: {e}",
                        extra={'page': pno + 1, 'stage': 'anchor_v2'}
                    )
                    cap_mid_guard = 6.0
                y0_min_guard = top_bound
                if prev_cap is not None:
                    mid_prev = 0.5 * (prev_cap.y1 + cap_rect.y0)
                    y0_min_guard = max(y0_min_guard, mid_prev + cap_mid_guard)
                # P0-04: 使用 effective_side（含强制方向）控制扫描
                if effective_side in (None, 'above'):
                    for h in heights:
                        y1 = bot_bound
                        y0_min = max(page_rect.y0, y0_min_guard)
                        y0 = max(y0_min, y1 - h)
                        while y0 + 40.0 <= y1:
                            c = fitz.Rect(x_left, y0, x_right, y1)
                            sc = fig_score(c)
                            # 方案B：边缘截断检测并扣分
                            if detect_top_edge_truncation(c, all_page_objects, 'above'):
                                sc -= 0.15
                            # 2025-12-30 新增：检测被排除的兄弟对象（多行子图场景）
                            # 当窗口边缘落在子图行之间的间隙时，detect_top_edge_truncation 不会触发，
                            # 但实际上排除了同一图表的其他子图行
                            sibling_ratio = detect_excluded_sibling_objects(c, all_page_objects, 'above')
                            if sibling_ratio > 0.3:  # 被排除对象面积 > 窗口内对象面积的 30%
                                # 惩罚力度与被排除比例成正比，最高 0.20
                                sc -= min(0.20, 0.15 * sibling_ratio)
                            candidates.append((sc, 'above', c))
                            y0 -= step
                            if y0 < y0_min:
                                break
                
                # ============================================================
                # P2-2: 同页多图冲突修复（Caption Midline Guard 自适应放松）
                # ============================================================
                # 问题：当相邻两个 caption 距离较远，但当前图表内容跨越“中线”，
                #       固定 midline guard 会硬性抬高 y0_min_guard，导致窗口无法覆盖完整图表。
                # 典型：gpt-5-system-card Figure 23（同页 Figure 22/23）。
                #
                # 策略：仅在以下条件同时满足时放松 guard：
                # 1) prev_cap 存在且 midline guard 实际生效（y0_min_guard > top_bound）
                # 2) 在 guard 约束下得到的最优 above 候选，顶部被对象截断（detect_top_edge_truncation==True）
                # 3) 最优候选的 y0 接近 y0_min_guard（说明确实被 guard 卡住）
                #
                # 放松方式：将 y0_min_guard 回退到 top_bound（仍不越过 prev_cap.y1+8），再补扫一次 above 候选。
                # ============================================================
                if prev_cap is not None and effective_side in (None, 'above') and (y0_min_guard > top_bound + 1e-3):
                    cand_above = [t for t in candidates if t[1] == 'above']
                    if cand_above:
                        best_above = max(cand_above, key=lambda t: t[0])
                        best_clip = best_above[2]
                        if abs(best_clip.y0 - y0_min_guard) <= 2.5 and detect_top_edge_truncation(best_clip, all_page_objects, 'above'):
                            if debug_captions:
                                print(f"[DBG] P2-2 relax mid-guard for Figure {fig_no} p{pno+1}: y0_min_guard {y0_min_guard:.1f} -> {top_bound:.1f} (best_above truncated at top)")
                            y0_min_relaxed = max(page_rect.y0, top_bound)
                            for h in heights:
                                y1 = bot_bound
                                y0 = max(y0_min_relaxed, y1 - h)
                                while y0 + 40.0 <= y1:
                                    c = fitz.Rect(x_left, y0, x_right, y1)
                                    sc = fig_score(c)
                                    if detect_top_edge_truncation(c, all_page_objects, 'above'):
                                        sc -= 0.15
                                    # 2025-12-30: 放松扫描也需要兄弟对象检测
                                    sibling_ratio = detect_excluded_sibling_objects(c, all_page_objects, 'above')
                                    if sibling_ratio > 0.3:
                                        sc -= min(0.20, 0.15 * sibling_ratio)
                                    candidates.append((sc, 'above', c))
                                    y0 -= step
                                    if y0 < y0_min_relaxed:
                                        break
                # below scanning
                top2 = cap_rect.y1 + caption_gap
                bot2 = (next_cap.y0 - 8) if next_cap else page_rect.y1
                # 防跨：下方窗口不得越过当前/下一 caption 的中线
                y1_max_guard = min(bot2, page_rect.y1)
                if next_cap is not None:
                    mid_next = 0.5 * (cap_rect.y1 + next_cap.y0)
                    y1_max_guard = min(y1_max_guard, mid_next - cap_mid_guard)
                # P0-04: 使用 effective_side（含强制方向）控制扫描
                if effective_side in (None, 'below'):
                    for h in heights:
                        y0 = min(max(page_rect.y0, top2), page_rect.y1 - 40)
                        y1_max = y1_max_guard
                        y1 = min(y1_max, y0 + h)
                        while y1 - 40.0 >= y0:
                            c = fitz.Rect(x_left, y0, x_right, y1)
                            sc = fig_score(c)
                            # 方案B：边缘截断检测并扣分
                            if detect_top_edge_truncation(c, all_page_objects, 'below'):
                                sc -= 0.15
                            # 2025-12-30: below 扫描也需要兄弟对象检测
                            sibling_ratio = detect_excluded_sibling_objects(c, all_page_objects, 'below')
                            if sibling_ratio > 0.3:
                                sc -= min(0.20, 0.15 * sibling_ratio)
                            candidates.append((sc, 'below', c))
                            y0 += step
                            y1 = min(y1_max, y0 + h)
                            if y0 >= y1_max:
                                break
                
                # P2-2 对称放松：若 midline guard 卡住了 below 候选的底部且检测到对象被底部截断，则放松到 bot2
                if next_cap is not None and effective_side in (None, 'below') and (y1_max_guard < min(bot2, page_rect.y1) - 1e-3):
                    cand_below = [t for t in candidates if t[1] == 'below']
                    if cand_below:
                        best_below = max(cand_below, key=lambda t: t[0])
                        best_clip = best_below[2]
                        if abs(best_clip.y1 - y1_max_guard) <= 2.5 and detect_top_edge_truncation(best_clip, all_page_objects, 'below'):
                            if debug_captions:
                                print(f"[DBG] P2-2 relax mid-guard (below) for Figure {fig_no} p{pno+1}: y1_max_guard {y1_max_guard:.1f} -> {min(bot2, page_rect.y1):.1f} (best_below truncated at bottom)")
                            y1_max_relaxed = min(bot2, page_rect.y1)
                            for h in heights:
                                y0 = min(max(page_rect.y0, top2), page_rect.y1 - 40)
                                y1 = min(y1_max_relaxed, y0 + h)
                                while y1 - 40.0 >= y0:
                                    c = fitz.Rect(x_left, y0, x_right, y1)
                                    sc = fig_score(c)
                                    if detect_top_edge_truncation(c, all_page_objects, 'below'):
                                        sc -= 0.15
                                    candidates.append((sc, 'below', c))
                                    y0 += step
                                    y1 = min(y1_max_relaxed, y0 + h)
                                    if y0 >= y1_max_relaxed:
                                        break
                if not candidates:
                    clip = fitz.Rect(x_left, max(page_rect.y0, cap_rect.y0 - 200), x_right, min(page_rect.y1, cap_rect.y1 + 200))
                    side = 'above'
                else:
                    candidates.sort(key=lambda t: t[0], reverse=True)
                    best = candidates[0]
                    if os.getenv('DUMP_CANDIDATES', '0') == '1':
                        dbg_dir = os.path.join(out_dir, "debug")
                        os.makedirs(dbg_dir, exist_ok=True)
                        dbg_abs = dump_page_candidates(
                            page,
                            os.path.join(dbg_dir, f"Figure_{fig_no}_p{pno+1}_debug_candidates.png"),
                            candidates=candidates,
                            best=best,
                            caption_rect=cap_rect,
                        )
                        if dbg_abs:
                            debug_artifacts.append(
                                os.path.relpath(os.path.abspath(dbg_abs), os.path.abspath(out_dir)).replace('\\', '/')
                            )
                    side = best[1]
                    clip = snap_clip_edges(best[2], draw_items)
                    if debug_captions:
                        print(f"[DBG] Select side={side} for Figure {fig_no} on page {pno+1}")

            # clip 已选定（V1/V2）
            
            # === Step 3: Layout-Guided Adjustment (如果启用) ===
            if layout_model is not None:
                clip_before_layout = fitz.Rect(clip)
                clip = _adjust_clip_with_layout(
                    clip_rect=clip,
                    caption_rect=cap_rect,
                    layout_model=layout_model,
                    page_num=pno,  # 0-based
                    direction=side,
                    debug=debug_captions
                )
                if debug_captions and clip != clip_before_layout:
                    logger.debug(f"Figure {fig_no}: Layout-guided adjustment applied")
            
            # Baseline metrics for acceptance gating
            base_clip = fitz.Rect(clip)
            base_height = max(1.0, base_clip.height)
            base_area = max(1.0, base_clip.width * base_clip.height)
            base_cov = object_area_ratio(base_clip)
            base_ink = ink_ratio_small(base_clip)
            base_comp = comp_count(base_clip)

            # === Visual Debug: 初始化并收集 Baseline ===
            debug_stages: List[DebugStageInfo] = []
            if debug_visual:
                debug_stages.append(DebugStageInfo(
                    name="Baseline (Anchor Selection)",
                    rect=fitz.Rect(base_clip),
                    color=(0, 102, 255),  # 蓝色
                    description=f"Initial window from anchor {side} selection"
                ))

            # A) 文本邻接裁切：增加"段落占比"门槛，防止误剪图边
            clip_after_A = fitz.Rect(clip)
            if text_trim:
                # Always run Phase C (far-side trim) regardless of para_ratio
                # This handles cases where large paragraphs are far from caption
                # 获取典型行高用于两行检测
                typical_lh = line_metrics.get('typical_line_height') if (adaptive_line_height and 'line_metrics' in locals()) else None
                clip = _trim_clip_head_by_text_v2(
                    clip,
                    page_rect,
                    cap_rect,
                    side,
                    text_lines_all,
                    width_ratio=text_trim_width_ratio,
                    font_min=text_trim_font_min,
                    font_max=text_trim_font_max,
                    gap=text_trim_gap,
                    adjacent_th=adjacent_th,
                    far_text_th=far_text_th,
                    far_text_para_min_ratio=far_text_para_min_ratio,
                    far_text_trim_mode=far_text_trim_mode,
                    # IMPORTANT: also pass far-side controls so callers can tune them
                    far_side_min_dist=far_side_min_dist,
                    far_side_para_min_ratio=far_side_para_min_ratio,
                    typical_line_h=typical_lh,
                    debug=debug_captions,
                )
                clip_after_A = fitz.Rect(clip)
                
                # Debug: 收集 Phase A 后的边界框
                if debug_visual and (clip_after_A != base_clip):
                    debug_stages.append(DebugStageInfo(
                        name="Phase A (Text Trimming)",
                        rect=fitz.Rect(clip_after_A),
                        color=(0, 200, 0),  # 绿色
                        description="After removing adjacent text (Phase A+B+C)"
                    ))

            # B) 对象连通域引导（可按图号禁用）
            clip_after_B = fitz.Rect(clip)
            if not (no_refine_figs and (fig_no in no_refine_figs)):
                clip = _refine_clip_by_objects(
                    clip,
                    cap_rect,
                    side,
                    image_rects,
                    vector_rects,
                    object_pad=object_pad,
                    min_area_ratio=object_min_area_ratio,
                    merge_gap=object_merge_gap,
                    near_edge_only=refine_near_edge_only,
                    use_axis_union=True,
                    use_horizontal_union=True,
                )
                clip_after_B = fitz.Rect(clip)
                
                # Debug: 收集 Phase B 后的边界框
                if debug_visual and (clip_after_B != clip_after_A):
                    debug_stages.append(DebugStageInfo(
                        name="Phase B (Object Alignment)",
                        rect=fitz.Rect(clip_after_B),
                        color=(255, 140, 0),  # 橙色
                        description="After object connectivity refinement"
                    ))

            # 额外：若远端边（非靠 caption 一侧）仍有大量对象紧贴，尝试向远端外扩，避免"半幅"
            def _touch_far_edge(c: fitz.Rect) -> bool:
                eps = 2.0
                if side == 'above':  # far = top
                    y = c.y0 + eps
                    for r in image_rects + vector_rects:
                        inter = r & c
                        if inter.height > 0 and inter.width > 0 and inter.y0 <= c.y0 + eps:
                            return True
                else:  # far = bottom
                    for r in image_rects + vector_rects:
                        inter = r & c
                        if inter.height > 0 and inter.width > 0 and inter.y1 >= c.y1 - eps:
                            return True
                return False

            extend_limit = 200.0
            extend_step = 60.0
            tried = 0.0
            while _touch_far_edge(clip) and tried < extend_limit:
                if side == 'above':
                    new_y0 = max(page_rect.y0, clip.y0 - extend_step)
                    if new_y0 >= clip.y0 - 1e-3:
                        break
                    clip = fitz.Rect(clip.x0, new_y0, clip.x1, clip.y1)
                else:
                    new_y1 = min(page_rect.y1, clip.y1 + extend_step)
                    if new_y1 <= clip.y1 + 1e-3:
                        break
                    clip = fitz.Rect(clip.x0, clip.y0, clip.x1, new_y1)
                tried += extend_step

            # 渲染导出前：在不越过 caption 的前提下，对靠近 caption 的边做轻微回扩
            if near_edge_pad_px and near_edge_pad_px > 0:
                pad_pt = (near_edge_pad_px * 72.0) / max(1.0, dpi)
                if side == 'above':
                    limit = cap_rect.y0 - max(1.0, caption_gap * 0.5)
                    clip = fitz.Rect(clip.x0, clip.y0, clip.x1, min(limit, clip.y1 + pad_pt))
                else:
                    limit = cap_rect.y1 + max(1.0, caption_gap * 0.5)
                    clip = fitz.Rect(clip.x0, max(limit, clip.y0 - pad_pt), clip.x1, clip.y1)

            # 渲染导出：按 DPI 缩放矩阵渲染为位图
            scale = dpi / 72.0
            mat = fitz.Matrix(scale, scale)
            try:
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
            except Exception as e:
                logger.warning(f"Render failed: {e}", extra={'page': pno+1, 'kind': 'figure', 'id': str(fig_no)})
                continue

            if autocrop:
                try:
                    # 通过像素扫描检测非白区域包围盒，带指定 padding，并重新渲染紧致区域
                    masks_px: Optional[List[Tuple[int, int, int, int]]] = None
                    if autocrop_mask_text and not (no_refine_figs and (fig_no in no_refine_figs)):
                        masks_px = _build_text_masks_px(
                            clip,
                            text_lines_all,
                            scale=scale,
                            direction=side,
                            near_frac=mask_top_frac,
                            width_ratio=mask_width_ratio,
                            font_max=mask_font_max,
                        )
                    l, t, r, b = detect_content_bbox_pixels(
                        pix,
                        white_threshold=autocrop_white_threshold,
                        pad=autocrop_pad_px,
                        mask_rects_px=masks_px,
                    )
                    tight = fitz.Rect(
                        clip.x0 + l / scale,
                        clip.y0 + t / scale,
                        clip.x0 + r / scale,
                        clip.y0 + b / scale,
                    )
                    
                    # ============================================================
                    # 【P0-1 核心约束】Phase D 远端边界单调性约束
                    # ============================================================
                    # 核心原则：Phase D 在远端方向上不应该超过 Phase A/C 已经确定的边界
                    # 
                    # 触发条件（满足其一即触发）：
                    # 1. Phase A/C 在远端做了裁剪（>2pt）
                    # 2. 远端附近（<40pt）检测到正文行证据
                    # 
                    # 约束逻辑：
                    # - 记录 far_bound_limit（远端边界上限）
                    # - Phase D 的所有操作（autocrop、protect_far_edge、etc）都不能超过此边界
                    # ============================================================
                    far_bound_limit: Optional[float] = None
                    far_bound_reason = ""
                    
                    if side == 'above':
                        # 远端是顶部
                        # 条件1：检查 Phase A/C 是否裁剪了顶部
                        if clip_after_A is not None:
                            phase_a_far_trim = clip_after_A.y0 - base_clip.y0
                            if phase_a_far_trim > 2.0:  # 降低阈值从 5pt 到 2pt
                                far_bound_limit = clip_after_A.y0
                                far_bound_reason = f"Phase A trimmed {phase_a_far_trim:.1f}pt"
                        
                        # 条件2：检测远端附近是否有正文行证据
                        if far_bound_limit is None:
                            has_evidence, suggested_limit = _detect_far_side_text_evidence(
                                base_clip, text_lines_all, side,
                                edge_zone=40.0, min_width_ratio=0.30
                            )
                            if has_evidence:
                                far_bound_limit = suggested_limit
                                far_bound_reason = "far-side text evidence detected"
                    else:
                        # 远端是底部
                        # 条件1：检查 Phase A/C 是否裁剪了底部
                        if clip_after_A is not None:
                            phase_a_far_trim = base_clip.y1 - clip_after_A.y1
                            if phase_a_far_trim > 2.0:  # 降低阈值从 5pt 到 2pt
                                far_bound_limit = clip_after_A.y1
                                far_bound_reason = f"Phase A trimmed {phase_a_far_trim:.1f}pt"
                        
                        # 条件2：检测远端附近是否有正文行证据
                        if far_bound_limit is None:
                            has_evidence, suggested_limit = _detect_far_side_text_evidence(
                                base_clip, text_lines_all, side,
                                edge_zone=40.0, min_width_ratio=0.30
                            )
                            if has_evidence:
                                far_bound_limit = suggested_limit
                                far_bound_reason = "far-side text evidence detected"
                    
                    # 应用远端边界约束
                    if far_bound_limit is not None:
                        if side == 'above':
                            if tight.y0 < far_bound_limit:
                                if debug_captions:
                                    logger.debug(f"Figure {fig_no}: [P0-1 FAR BOUND] Limiting top from {tight.y0:.1f} to {far_bound_limit:.1f} ({far_bound_reason})")
                                tight = fitz.Rect(tight.x0, far_bound_limit, tight.x1, tight.y1)
                        else:
                            if tight.y1 > far_bound_limit:
                                if debug_captions:
                                    logger.debug(f"Figure {fig_no}: [P0-1 FAR BOUND] Limiting bottom from {tight.y1:.1f} to {far_bound_limit:.1f} ({far_bound_reason})")
                                tight = fitz.Rect(tight.x0, tight.y0, tight.x1, far_bound_limit)
                    
                    # 远端边缘保护：在远离 caption 的一侧向外扩 保护像素，避免轻微顶部/底部被裁
                    # 【重要】保护扩展不能超过 far_bound_limit
                    far_pad_pt = max(0.0, protect_far_edge_px / scale)
                    if far_pad_pt > 0:
                        if side == 'above':
                            # far edge = TOP
                            new_y0 = max(page_rect.y0, tight.y0 - far_pad_pt)
                            # 确保不超过约束边界
                            if far_bound_limit is not None:
                                new_y0 = max(new_y0, far_bound_limit)
                            tight = fitz.Rect(tight.x0, new_y0, tight.x1, tight.y1)
                        else:
                            # far edge = BOTTOM
                            new_y1 = min(page_rect.y1, tight.y1 + far_pad_pt)
                            # 确保不超过约束边界
                            if far_bound_limit is not None:
                                new_y1 = min(new_y1, far_bound_limit)
                            tight = fitz.Rect(tight.x0, tight.y0, tight.x1, new_y1)
                    # Enforce minimal size in pt, anchored to near-caption side
                    if (autocrop_min_height_px or autocrop_shrink_limit is not None):
                        min_h_pt = max(0.0, (autocrop_min_height_px / scale))
                        # shrink limit relative to previous clip
                        if autocrop_shrink_limit is not None:
                            min_h_pt = max(min_h_pt, clip.height * (1.0 - autocrop_shrink_limit))
                        if side == 'above':
                            # adjust bottom edge only
                            y1_new = max(tight.y1, min(clip.y1, clip.y0 + min_h_pt))
                            tight = fitz.Rect(tight.x0, tight.y0, tight.x1, y1_new)
                        else:
                            # adjust top edge only
                            y0_new = min(tight.y0, max(clip.y0, clip.y1 - min_h_pt))
                            tight = fitz.Rect(tight.x0, y0_new, tight.x1, tight.y1)
                    
                    # 2025-12-30 新增：宽度方向收缩保护
                    # 避免 autocrop 在 x 方向过度收缩（如裁掉 y 轴标签）
                    # 最大允许宽度收缩为 autocrop_shrink_limit（默认 35%）
                    if autocrop_shrink_limit is not None:
                        min_w_pt = clip.width * (1.0 - autocrop_shrink_limit)
                        if tight.width < min_w_pt:
                            # 计算需要回扩的量，左右各扩一半
                            expand_total = min_w_pt - tight.width
                            expand_each = expand_total / 2.0
                            new_x0 = max(page_rect.x0, tight.x0 - expand_each)
                            new_x1 = min(page_rect.x1, tight.x1 + expand_each)
                            # 确保回扩后宽度达到 min_w_pt
                            if (new_x1 - new_x0) < min_w_pt:
                                # 如果一侧到边了，另一侧多扩
                                if new_x0 == page_rect.x0:
                                    new_x1 = min(page_rect.x1, new_x0 + min_w_pt)
                                elif new_x1 == page_rect.x1:
                                    new_x0 = max(page_rect.x0, new_x1 - min_w_pt)
                            tight = fitz.Rect(new_x0, tight.y0, new_x1, tight.y1)
                            if debug_captions:
                                logger.debug(f"Figure {fig_no}: [WIDTH PROTECT] Expanded width from {tight.width:.1f}pt to {new_x1-new_x0:.1f}pt (min={min_w_pt:.1f}pt)")
                    
                    # Near-edge overshoot pad: expand a bit towards caption side to avoid missing axes/labels
                    if near_edge_pad_px and near_edge_pad_px > 0:
                        pad_pt = near_edge_pad_px / scale
                        if side == 'above':
                            # near = bottom; do not cross caption baseline (cap_rect.y0 - caption_gap*0.5)
                            limit = cap_rect.y0 - max(1.0, caption_gap * 0.5)
                            tight = fitz.Rect(tight.x0, tight.y0, tight.x1, min(limit, tight.y1 + pad_pt))
                        else:
                            # near = top; do not cross caption baseline (cap_rect.y1 + caption_gap*0.5)
                            limit = cap_rect.y1 + max(1.0, caption_gap * 0.5)
                            tight = fitz.Rect(tight.x0, max(limit, tight.y0 - pad_pt), tight.x1, tight.y1)
                    
                    # Step 3.5: 在 autocrop 后再次应用版式引导，确保不切断文本块
                    if layout_model is not None:
                        clip_before_post_layout = fitz.Rect(tight)
                        tight = _adjust_clip_with_layout(
                            clip_rect=tight,
                            caption_rect=cap_rect,
                            layout_model=layout_model,
                            page_num=pno,  # 0-based
                            direction=side,
                            debug=debug_captions
                        )
                        if debug_captions and tight != clip_before_post_layout:
                            logger.debug(f"Figure {fig_no}: Post-autocrop layout adjustment applied")
                    
                    # ============================================================
                    # 【P0-3】Phase D 后轻量去正文后处理
                    # ============================================================
                    # 在 autocrop 完成后，扫描远端边缘附近的正文行，如果存在则向内推边界
                    tight_before_post = fitz.Rect(tight)
                    tight, was_post_trimmed = _trim_far_side_text_post_autocrop(
                        tight, text_lines_all, side,
                        typical_line_h=typical_lh,
                        scan_lines=3,
                        min_width_ratio=0.30,
                        min_text_len=15,
                        gap=6.0,
                    )
                    if was_post_trimmed and debug_captions:
                        if side == 'above':
                            logger.debug(f"Figure {fig_no}: [P0-3 POST TRIM] y0 pushed from {tight_before_post.y0:.1f} to {tight.y0:.1f}")
                        else:
                            logger.debug(f"Figure {fig_no}: [P0-3 POST TRIM] y1 pushed from {tight_before_post.y1:.1f} to {tight.y1:.1f}")
                    
                    pix = page.get_pixmap(matrix=mat, clip=tight, alpha=False)
                    clip = tight
                except Exception as e:
                    logger.warning(f"Autocrop failed: {e}", extra={'page': pno+1, 'kind': 'figure', 'id': str(fig_no), 'stage': 'phase_d'})

            # Safety gate & fallback: compare to baseline
            if refine_safe and not (no_refine_figs and (fig_no in no_refine_figs)):
                refined = fitz.Rect(clip)
                r_height = max(1.0, refined.height)
                r_area = max(1.0, refined.width * refined.height)
                r_cov = object_area_ratio(refined)
                r_ink = ink_ratio_small(refined)
                r_comp = comp_count(refined)
                # P1-07: 动态计算验收阈值（基于基线高度和远侧覆盖率）
                # 先计算远侧覆盖率，再调用统一的阈值函数
                far_cov = 0.0
                try:
                    near_is_top = (side == 'below')
                    far_is_top = not near_is_top
                    # estimate far-side paragraph coverage on BASE clip
                    far_lines: List[fitz.Rect] = []
                    for (lb, fs, tx) in text_lines_all:
                        if not tx.strip():
                            continue
                        inter = lb & base_clip
                        if inter.width <= 0 or inter.height <= 0:
                            continue
                        width_ok = (inter.width / max(1.0, base_clip.width)) >= max(0.35, text_trim_width_ratio * 0.7)
                        size_ok = (text_trim_font_min <= fs <= text_trim_font_max)
                        if not (width_ok and size_ok):
                            continue
                        if far_is_top:
                            in_far = (lb.y0 < base_clip.y0 + 0.5 * base_clip.height)
                        else:
                            in_far = (lb.y1 > base_clip.y0 + 0.5 * base_clip.height)
                        if in_far:
                            far_lines.append(lb)
                    if far_lines:
                        if far_is_top:
                            region_h = max(1.0, (base_clip.y0 + 0.5 * base_clip.height) - base_clip.y0)
                        else:
                            region_h = max(1.0, base_clip.y1 - (base_clip.y0 + 0.5 * base_clip.height))
                        far_cov = sum(lb.height for lb in far_lines) / region_h
                except Exception as e:
                    logger.warning(
                        f"Failed to estimate far-side text coverage: {e}",
                        extra={'page': pno + 1, 'kind': 'figure', 'id': str(fig_no), 'stage': 'validation'}
                    )
                
                # P1-07: 使用动态阈值函数
                thresholds = _adaptive_acceptance_thresholds(
                    base_height=base_height,
                    is_table=False,
                    far_cov=far_cov
                )
                relax_h = thresholds.relax_h
                relax_a = thresholds.relax_a
                relax_ink = thresholds.relax_ink
                relax_cov = thresholds.relax_cov
                ok_h = (r_height >= relax_h * base_height)
                ok_a = (r_area >= relax_a * base_area)
                
                # ============================================================
                # P2-1: 从密度比转向 mass/保留量指标
                # ============================================================
                # 问题：密度比会误伤"更大但更对/留白更多"的 refined clip
                # 解决：使用 mass (= ratio × area) 代替单纯的 ratio
                # ============================================================
                # 计算 mass 指标
                base_ink_mass = base_ink * base_area
                r_ink_mass = r_ink * r_area
                base_cov_mass = base_cov * base_area
                r_cov_mass = r_cov * r_area
                
                # 使用 mass 进行验收（更宽松，减少误拒绝）
                ok_ink_mass = (r_ink_mass >= relax_ink * base_ink_mass) if base_ink_mass > 1e-9 else True
                ok_cov_mass = (r_cov_mass >= relax_cov * base_cov_mass) if base_cov_mass > 1e-9 else True
                
                # 额外：仅当显著收缩时（< 70% 面积）启用更严格的密度检查作为补充
                significant_shrink = (r_area < 0.70 * base_area)
                if significant_shrink:
                    # 显著收缩时：密度不能下降太多（> 60%）
                    ok_ink_density = (r_ink >= 0.60 * base_ink) if base_ink > 1e-9 else True
                    ok_cov_density = (r_cov >= 0.60 * base_cov) if base_cov > 1e-9 else True
                else:
                    ok_ink_density = True
                    ok_cov_density = True
                
                # 综合验收：mass 和密度检查都要通过
                ok_c = ok_cov_mass and ok_cov_density
                ok_i = ok_ink_mass and ok_ink_density
                
                # If stacked components shrink to 1, be cautious
                ok_comp = (r_comp >= min(2, base_comp)) if base_comp >= 2 else True
                if not (ok_h and ok_a and ok_c and ok_i and ok_comp):
                    # 收集失败原因用于调试（P2-1 增强：显示 mass 和密度指标）
                    reasons = []
                    if not ok_h: reasons.append(f"height={r_height/base_height:.1%}")
                    if not ok_a: reasons.append(f"area={r_area/base_area:.1%}")
                    if not ok_c:
                        if not ok_cov_mass:
                            reasons.append(f"cov_mass={r_cov_mass/base_cov_mass:.1%}" if base_cov_mass > 1e-9 else "cov_mass=low")
                        if significant_shrink and not ok_cov_density:
                            reasons.append(f"cov_density={r_cov/base_cov:.1%}" if base_cov > 1e-9 else "cov_density=low")
                    if not ok_i:
                        if not ok_ink_mass:
                            reasons.append(f"ink_mass={r_ink_mass/base_ink_mass:.1%}" if base_ink_mass > 1e-9 else "ink_mass=low")
                        if significant_shrink and not ok_ink_density:
                            reasons.append(f"ink_density={r_ink/base_ink:.1%}" if base_ink > 1e-9 else "ink_density=low")
                    if not ok_comp: reasons.append(f"comp={r_comp}/{base_comp}")
                    logger.warning(
                        f"Fig {fig_no} p{pno+1}: refinement rejected ({', '.join(reasons)}), trying fallback",
                        extra={'page': pno + 1, 'kind': 'figure', 'id': str(fig_no), 'stage': 'validation'}
                    )
                    log_event(
                        "refine_rejected",
                        level="warning",
                        pdf=pdf_name,
                        page=pno + 1,
                        kind="figure",
                        id=str(fig_no),
                        stage="validation",
                        message="Refinement rejected; trying fallback",
                        reasons=reasons,
                        side=side,
                        far_cov=round(float(far_cov), 4),
                        thresholds={
                            "description": thresholds.description,
                            "relax_h": round(float(relax_h), 4),
                            "relax_a": round(float(relax_a), 4),
                            "relax_cov": round(float(relax_cov), 4),
                            "relax_ink": round(float(relax_ink), 4),
                        },
                        metrics={
                            "base": {
                                "height": round(float(base_height), 2),
                                "area": round(float(base_area), 2),
                                "cov": round(float(base_cov), 6),
                                "ink": round(float(base_ink), 6),
                                "comp": int(base_comp),
                            },
                            "refined": {
                                "height": round(float(r_height), 2),
                                "area": round(float(r_area), 2),
                                "cov": round(float(r_cov), 6),
                                "ink": round(float(r_ink), 6),
                                "comp": int(r_comp),
                            },
                        },
                        clips={
                            "base": _rect_to_list(base_clip),
                            "refined": _rect_to_list(refined),
                        },
                    )
                    # try A-only fallback
                    typical_lh_fallback = line_metrics.get('typical_line_height') if (adaptive_line_height and 'line_metrics' in locals()) else None
                    clip_A = _trim_clip_head_by_text_v2(
                        base_clip, page_rect, cap_rect, side, text_lines_all,
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
                        typical_line_h=typical_lh_fallback,
                        debug=debug_captions,
                    ) if text_trim else base_clip
                    rA_h, rA_a = max(1.0, clip_A.height), max(1.0, clip_A.width * clip_A.height)
                    # P1-07: A-only fallback 也使用动态阈值（必须沿用同页 far_cov，否则会误拒绝并回退到 baseline）
                    fallback_th = _adaptive_acceptance_thresholds(base_height, is_table=False, far_cov=far_cov)
                    if (rA_h >= fallback_th.relax_h * base_height) and (rA_a >= fallback_th.relax_a * base_area):
                        clip = clip_A
                        logger.info(f"Fig {fig_no} p{pno+1}: using A-only fallback (thresholds: {fallback_th.description})")
                        log_event(
                            "refine_fallback_a_only",
                            level="info",
                            pdf=pdf_name,
                            page=pno + 1,
                            kind="figure",
                            id=str(fig_no),
                            stage="validation",
                            message="Using A-only fallback after refinement rejection",
                            side=side,
                            fallback_thresholds={
                                "description": fallback_th.description,
                                "relax_h": round(float(fallback_th.relax_h), 4),
                                "relax_a": round(float(fallback_th.relax_a), 4),
                            },
                            metrics={
                                "fallback_a": {
                                    "height": round(float(rA_h), 2),
                                    "area": round(float(rA_a), 2),
                                }
                            },
                            clips={
                                "fallback_a": _rect_to_list(clip_A),
                                "final": _rect_to_list(clip),
                            },
                        )
                    else:
                        clip = base_clip
                        logger.info(f"Fig {fig_no} p{pno+1}: reverted to baseline")
                        log_event(
                            "refine_revert_baseline",
                            level="warning",
                            pdf=pdf_name,
                            page=pno + 1,
                            kind="figure",
                            id=str(fig_no),
                            stage="validation",
                            message="Reverted to baseline after refinement rejection (A-only fallback also rejected)",
                            side=side,
                            fallback_thresholds={
                                "description": fallback_th.description,
                                "relax_h": round(float(fallback_th.relax_h), 4),
                                "relax_a": round(float(fallback_th.relax_a), 4),
                            },
                            metrics={
                                "fallback_a": {
                                    "height": round(float(rA_h), 2),
                                    "area": round(float(rA_a), 2),
                                }
                            },
                            clips={
                                "baseline": _rect_to_list(base_clip),
                                "fallback_a": _rect_to_list(clip_A),
                                "final": _rect_to_list(clip),
                            },
                        )
                        # Debug: 标记 Fallback to Baseline
                        if debug_visual:
                            debug_stages.append(DebugStageInfo(
                                name="Fallback (Reverted to Baseline)",
                                rect=fitz.Rect(clip),
                                color=(255, 255, 0),  # 黄色
                                description="Refinement rejected, reverted to baseline"
                            ))
            
            # Debug: 标记最终结果（成功的精炼或 A-only fallback）
            if debug_visual:
                # 检查是否使用了 autocrop（通过比较当前 clip 和之前的阶段）
                if autocrop and (clip != base_clip) and (clip != clip_after_A):
                    # 成功的 autocrop 结果
                    debug_stages.append(DebugStageInfo(
                        name="Phase D (Final - Autocrop)",
                        rect=fitz.Rect(clip),
                        color=(255, 0, 0),  # 红色
                        description="Final result after A+B+D refinement"
                    ))
                elif clip == clip_after_A and text_trim:
                    # A-only fallback（没有其他阶段改变了边界）
                    if not any(stage.name.startswith("Fallback") for stage in debug_stages):
                        debug_stages.append(DebugStageInfo(
                            name="Final (A-only Fallback)",
                            rect=fitz.Rect(clip),
                            color=(255, 200, 0),  # 金黄色
                            description="A-only fallback result (B/D rejected)"
                        ))
            
            # === Visual Debug: 保存可视化 ===
            if debug_visual:
                try:
                    artifacts = save_debug_visualization(
                        page=page,
                        out_dir=out_dir,
                        fig_no=fig_no,
                        page_num=pno + 1,
                        stages=debug_stages,
                        caption_rect=cap_rect,
                        kind='figure',
                        layout_model=layout_model  # V2 Architecture
                    )
                    if artifacts:
                        debug_artifacts.extend(artifacts)
                except Exception as e:
                    logger.warning(f"Debug visualization failed: {e}", extra={'page': pno+1, 'kind': 'figure', 'id': str(fig_no)})

            # 生成安全文件名；若同名已存在（例如多页同名），则附加页码后缀
            base = sanitize_filename_from_caption(caption, fig_no, max_chars=max_caption_chars, max_words=max_caption_words)
            # 同号多页：根据选项决定是否允许继续导出，并命名为 continued
            if count_prev >= 1 and allow_continued:
                base = f"{base}_continued_p{pno+1}"
            out_path = os.path.join(out_dir, base + ".png")
            # P0-07: 文件名碰撞处理
            out_path, had_collision = get_unique_path(out_path)
            pix.save(out_path)
            seen_counts[fig_no] = count_prev + 1
            records.append(AttachmentRecord('figure', str(fig_no), pno + 1, caption, out_path, continued=(count_prev>=1), debug_artifacts=debug_artifacts))
            logger.info(f"Figure {fig_no} page {pno+1} -> {out_path}")

    # 按数字键排序，兼容新结构
    records.sort(key=lambda r: r.num_key())
    return records


# 将导出的图信息写入 CSV 清单（可选）
def write_manifest(records: List[AttachmentRecord], manifest_path: Optional[str]) -> Optional[str]:
    if not manifest_path:
        return None
    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    if base_dir:
        os.makedirs(base_dir, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        # 统一为 (type,id,page,caption,file,continued)
        w.writerow(["type", "id", "page", "caption", "file", "continued"])
        for r in records:
            rel = os.path.relpath(os.path.abspath(r.out_path), base_dir).replace('\\', '/')
            w.writerow([r.kind, r.ident, r.page, r.caption, rel, int(r.continued)])
    logger.info(f"Wrote manifest: {manifest_path} (items={len(records)})")
    return manifest_path


def _load_index_json_items(index_json_path: str) -> List[Dict[str, Any]]:
    """
    兼容层：从 index.json 中加载 items 列表，同时支持旧格式（list）和新格式（dict）。
    
    旧格式: [{"type": ..., "id": ..., "file": ...}, ...]
    新格式: {"version": "2.0", "items": [...], "figures": [...], "tables": [...], ...}
    
    Returns:
        items 列表
    """
    import json
    with open(index_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    if isinstance(data, list):
        # 旧格式：直接是 items 列表
        return data
    elif isinstance(data, dict):
        # 新格式：从 "items" 字段获取，或合并 "figures" + "tables"
        if "items" in data:
            return data["items"]
        else:
            # 兜底：合并 figures 和 tables
            figures = data.get("figures", [])
            tables = data.get("tables", [])
            return figures + tables
    else:
        return []


def prune_unindexed_images(*, out_dir: str, index_json_path: str) -> int:
    """Remove Figure_*/Table_* PNGs in out_dir that are NOT referenced by index_json_path."""
    try:
        base_dir = os.path.dirname(os.path.abspath(index_json_path))
        items = _load_index_json_items(index_json_path)
        referenced_abs: set[str] = set()
        for it in items:
            rel = (it.get("file") or "").replace("\\", "/")
            if not rel:
                continue
            referenced_abs.add(os.path.abspath(os.path.join(base_dir, rel)))

        removed = 0
        for name in os.listdir(out_dir):
            if not name.lower().endswith(".png"):
                continue
            if not (name.startswith("Figure_") or name.startswith("Table_")):
                continue
            abs_path = os.path.abspath(os.path.join(out_dir, name))
            if abs_path in referenced_abs:
                continue
            try:
                os.remove(abs_path)
                removed += 1
            except Exception as e:
                logger.warning(f"Failed to remove file during prune: {abs_path}: {e}", extra={'stage': 'prune_images'})
        return removed
    except Exception as e:
        logger.warning(f"Prune failed: {e}", extra={'stage': 'prune_images'})
        return 0


# ---- 通用：从 kind/ident + caption 生成输出基名（不含扩展名） ----
def build_output_basename(kind: str, ident: str, caption: str, max_chars: int = 160, max_words: int = 12) -> str:
    # 基于现有 sanitize 逻辑，但前缀由 kind + ident 组成
    s = caption.strip()
    s = s.replace("|", " ").replace("—", "-").replace("–", "-")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if ch.isalnum() or ch in (" ", "_", "-", ".", "(", ")"))
    s = "_".join(s.split())
    s = re.sub(r"_+", "_", s).rstrip("._-")
    prefix = f"{kind.capitalize()}_{ident}"
    if not s.lower().startswith(prefix.lower() + "_"):
        s = f"{prefix}_" + s
    if len(s) > max_chars:
        s = s[:max_chars].rstrip("._-")
    # 限制标号后的单词数量
    s = _limit_words_after_prefix(s, prefix, max_words=max_words)
    return s


# ---- P0-07: 文件名碰撞处理 ----
def get_unique_path(base_path: str) -> Tuple[str, bool]:
    """
    检查文件路径是否存在，如果存在则追加后缀 _1, _2, ... 直到找到唯一路径。
    
    返回：(unique_path, had_collision)
    - unique_path: 唯一的文件路径
    - had_collision: 是否发生了碰撞
    """
    if not os.path.exists(base_path):
        return base_path, False
    
    stem, ext = os.path.splitext(base_path)
    counter = 1
    while os.path.exists(f"{stem}_{counter}{ext}"):
        counter += 1
    unique_path = f"{stem}_{counter}{ext}"
    print(f"[WARN] Filename collision detected: {os.path.basename(base_path)} -> {os.path.basename(unique_path)}")
    return unique_path, True


# ---- JSON 索引：images/index.json ----
# P1-06: 扩展版 index.json 写入函数
def write_index_json(
    records: List[AttachmentRecord],
    index_path: str,
    *,
    # P1-06: 可选的元数据参数
    pdf_path: Optional[str] = None,
    preset: Optional[str] = None,
    # QA-04/QA-05: 运行追踪与重命名映射
    run_id: Optional[str] = None,
    log_jsonl: Optional[str] = None,
    layout_model: Optional["DocumentLayoutModel"] = None,
    validation: Optional["PDFValidationResult"] = None,
    qc_issues: Optional[List["QualityIssue"]] = None,
    extractor_version: str = "2.0.0"
) -> Optional[str]:
    """
    写入扩展版 index.json，包含元数据便于复现和诊断。
    
    P1-06: 新增字段
    - version: index 格式版本
    - meta: PDF 信息、提取时间、版本、preset 等
    - layout: 版式信息（如果启用了 layout-driven）
    - quality_issues: 质量检查结果
    - figures/tables: 分开的图表列表
    """
    import json
    from datetime import datetime
    import hashlib
    
    base_dir = os.path.dirname(os.path.abspath(index_path))
    os.makedirs(base_dir, exist_ok=True)
    
    # 计算 PDF 文件哈希（用于可复现性验证）
    pdf_hash = ""
    pdf_pages = 0
    if pdf_path and os.path.exists(pdf_path):
        try:
            with open(pdf_path, 'rb') as f:
                pdf_hash = f"sha256:{hashlib.sha256(f.read()).hexdigest()[:16]}"
            doc = fitz.open(pdf_path)
            pdf_pages = len(doc)
            doc.close()
        except Exception as e:
            logger.warning(f"Failed to compute PDF hash/pages: {e}", extra={'stage': 'write_index_json_extended'})
    
    # 构建记录列表
    figures_list: List[Dict[str, Any]] = []
    tables_list: List[Dict[str, Any]] = []
    
    for r in records:
        rel = os.path.relpath(os.path.abspath(r.out_path), base_dir).replace('\\', '/')
        entry = {
            "type": r.kind,
            "id": r.ident,
            "page": r.page,
            "caption": r.caption,
            "file": rel,
            # QA-05: 记录重命名映射（首次提取时 original == current == file）
            "original_file": rel,
            "current_file": rel,
            "continued": bool(r.continued),
        }
        if getattr(r, "debug_artifacts", None):
            entry["debug_artifacts"] = list(r.debug_artifacts)
        if r.kind == 'figure':
            figures_list.append(entry)
        else:
            tables_list.append(entry)
    
    # 构建输出结构
    output: Dict[str, Any] = {
        "version": "2.0",
        "meta": {
            "pdf": os.path.basename(pdf_path) if pdf_path else "",
            "pdf_hash": pdf_hash,
            "pages": pdf_pages,
            "extracted_at": datetime.now().isoformat(),
            "extractor_version": extractor_version,
            "preset": preset or "custom",
            # QA-04: 运行追踪信息
            "run_id": run_id or get_run_id(),
            "run_log_jsonl": (
                os.path.relpath(os.path.abspath(log_jsonl), base_dir).replace("\\", "/")
                if log_jsonl else ""
            ),
        },
        "figures": figures_list,
        "tables": tables_list,
    }
    
    # 添加版式信息（如果有）
    if layout_model is not None:
        output["layout"] = {
            "columns": layout_model.num_columns,
            "typical_line_height": round(layout_model.typical_line_height, 2),
            "typical_font_size": round(layout_model.typical_font_size, 2),
            "page_size": [round(layout_model.page_size[0], 1), round(layout_model.page_size[1], 1)],
        }
    
    # 添加验证信息（如果有）
    if validation is not None:
        output["validation"] = {
            "is_valid": validation.is_valid,
            "has_text_layer": validation.has_text_layer,
            "text_layer_ratio": round(validation.text_layer_ratio, 2),
            "warnings": validation.warnings,
        }
    
    # 添加质量问题（如果有）
    if qc_issues:
        output["quality_issues"] = [
            {
                "level": issue.level,
                "category": issue.category,
                "message": issue.message,
            }
            for issue in qc_issues
        ]
    
    # 兼容性：保留 items 字段（旧版格式）
    all_items = figures_list + tables_list
    output["items"] = all_items
    
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Wrote index: {index_path} (figures={len(figures_list)}, tables={len(tables_list)})")
    return index_path


def _draw_rects_on_pix(pix: "fitz.Pixmap", rects: List[Tuple[fitz.Rect, Tuple[int, int, int]]], *, scale: float, line_width: int = 1) -> None:
    """Draw rectangle edges on a pixmap in-place with RGB colors.
    rects: list of (rect, (r,g,b))
    line_width: thickness of the border lines (default: 1)
    """
    # Ensure no alpha
    if pix.alpha:
        tmp = fitz.Pixmap(fitz.csRGB, pix)
        pix = tmp
    w, h = pix.width, pix.height
    n = pix.n
    # Convert to mutable bytearray for pixel modification
    samples = bytearray(pix.samples)
    stride = pix.stride

    def set_px(x: int, y: int, color: Tuple[int, int, int]):
        if 0 <= x < w and 0 <= y < h:
            off = y * stride + x * n
            samples[off + 0] = color[0]
            if n > 1:
                samples[off + 1] = color[1]
            if n > 2:
                samples[off + 2] = color[2]

    for r, col in rects:
        lx = int(max(0, (r.x0) * scale))
        rx = int(min(w - 1, (r.x1) * scale))
        ty = int(max(0, (r.y0) * scale))
        by = int(min(h - 1, (r.y1) * scale))
        
        # Draw border with line_width
        for offset in range(line_width):
            # Top and bottom edges
            for x in range(lx, rx + 1):
                set_px(x, ty + offset, col)
                set_px(x, by - offset, col)
            # Left and right edges
            for y in range(ty, by + 1):
                set_px(lx + offset, y, col)
                set_px(rx - offset, y, col)
    
    # Write modified samples back to pixmap
    pix.set_samples(bytes(samples))


# Debug: dump top-k candidates per page
def dump_page_candidates(
    page: "fitz.Page",
    out_path: str,
    *,
    candidates: List[Tuple[float, str, fitz.Rect]],
    best: Tuple[float, str, fitz.Rect],
    caption_rect: fitz.Rect,
) -> Optional[str]:
    try:
        scale = 1.0
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        rects: List[Tuple[fitz.Rect, Tuple[int, int, int]]] = []
        # Caption in blue
        rects.append((caption_rect, (0, 102, 255)))
        # Candidates
        for sc, side, r in candidates[:10]:
            rects.append((r, (255, 85, 85)))
        # Best in green (overwrite color at end)
        rects.append((best[2], (0, 200, 0)))
        _draw_rects_on_pix(pix, rects, scale=scale, line_width=1)
        pix.save(out_path)
        return out_path
    except Exception as e:
        page_no = getattr(page, "number", None)
        extra = {'stage': 'dump_page_candidates'}
        if isinstance(page_no, int):
            extra['page'] = page_no + 1
        logger.warning(f"Failed to dump page candidates: {e}", extra=extra)
        return None


# ---- Visual Debug: 保存多阶段边界框可视化 ----
@dataclass
class DebugStageInfo:
    """调试阶段信息"""
    name: str              # 阶段名称
    rect: fitz.Rect        # 边界框
    color: Tuple[int, int, int]  # RGB 颜色
    description: str       # 描述信息


def save_debug_visualization(
    page: "fitz.Page",
    out_dir: str,
    fig_no: int,
    page_num: int,
    *,
    stages: List[DebugStageInfo],
    caption_rect: fitz.Rect,
    kind: str = 'figure',
    layout_model: Optional[DocumentLayoutModel] = None,
    run_id: Optional[str] = None,
) -> Optional[List[str]]:
    """
    保存带多色线框的调试可视化图片
    
    Args:
        page: 页面对象
        out_dir: 输出目录
        fig_no: 图/表编号
        page_num: 页码（1-based）
        stages: 阶段信息列表
        caption_rect: 图注边界框
        kind: 'figure' 或 'table'
        layout_model: 可选的版式模型（用于显示文本区块）
        run_id: 运行 ID（用于创建隔离的 debug 目录，避免覆盖）
    
    Returns:
        创建的 debug 文件相对路径列表（相对于 out_dir），如 ["debug/<run_id>/Figure_1_p3_debug_stages.png", ...]
    """
    try:
        # QA-03: 使用 run_id 创建隔离的 debug 目录
        if run_id:
            debug_dir = os.path.join(out_dir, "debug", run_id)
        else:
            debug_dir = os.path.join(out_dir, "debug")
        os.makedirs(debug_dir, exist_ok=True)
        
        # 创建一个临时 PDF 页面副本用于绘图
        # 使用 PyMuPDF 的 Shape 对象在页面上绘制矩形
        src_doc = page.parent
        # 创建临时 PDF 文档
        temp_doc = fitz.open()
        temp_page = temp_doc.new_page(width=page.rect.width, height=page.rect.height)
        
        # 先渲染原始页面内容
        scale_render = 2.0  # 2x 分辨率
        pix = page.get_pixmap(matrix=fitz.Matrix(scale_render, scale_render), alpha=False)
        
        # 在 temp_page 上插入原始页面的图像
        temp_page.insert_image(temp_page.rect, pixmap=pix)
        
        # 绘制边界框（按从大到小排序，确保小的框在上面）
        sorted_stages = sorted(stages, key=lambda s: s.rect.width * s.rect.height, reverse=True)
        
        shape = temp_page.new_shape()
        
        # 绘制所有阶段的边界框
        for stage in sorted_stages:
            r = stage.rect
            color_normalized = tuple(c / 255.0 for c in stage.color)  # PyMuPDF 使用 0-1 范围
            shape.draw_rect(r)
            shape.finish(color=color_normalized, width=3)
        
        # 绘制文本区块（如果提供了layout_model）
        # Step 3 增强：标题用实线，段落用虚线
        text_blocks_drawn = []
        if layout_model is not None:
            pno_zero_based = page_num - 1  # page_num是1-based，转换为0-based
            text_blocks = layout_model.text_blocks.get(pno_zero_based, [])
            pink_color = (255/255.0, 105/255.0, 180/255.0)  # Hot Pink: RGB(255, 105, 180)
            
            for block in text_blocks:
                if block.block_type in ['paragraph_group', 'list_group']:
                    # 段落/列表：粉红色虚线
                    shape.draw_rect(block.bbox)
                    shape.finish(color=pink_color, width=2, dashes=[3, 3])
                    text_blocks_drawn.append(block)
                elif block.block_type.startswith('title_'):
                    # 标题：粉红色实线（Step 3 新增）
                    shape.draw_rect(block.bbox)
                    shape.finish(color=pink_color, width=2)  # 实线
                    text_blocks_drawn.append(block)
        
        # 绘制 caption（紫色）
        caption_color = (148/255.0, 0, 211/255.0)
        shape.draw_rect(caption_rect)
        shape.finish(color=caption_color, width=3)
        
        shape.commit()
        
        # 渲染最终结果
        final_pix = temp_page.get_pixmap(matrix=fitz.Matrix(scale_render, scale_render), alpha=False)
        
        # 保存可视化图片
        prefix = kind.capitalize()
        vis_path = os.path.join(debug_dir, f"{prefix}_{fig_no}_p{page_num}_debug_stages.png")
        final_pix.save(vis_path)
        
        # 关闭临时文档
        temp_doc.close()
        
        # 生成文字图例
        legend_path = os.path.join(debug_dir, f"{prefix}_{fig_no}_p{page_num}_legend.txt")
        with open(legend_path, 'w', encoding='utf-8') as f:
            f.write(f"=== {prefix} {fig_no} Debug Legend (Page {page_num}) ===\n\n")
            f.write(f"Caption: {caption_rect.x0:.1f},{caption_rect.y0:.1f} -> {caption_rect.x1:.1f},{caption_rect.y1:.1f} "
                    f"({caption_rect.width:.1f}×{caption_rect.height:.1f}pt)\n\n")
            
            # 写入文本区块信息（如果有）
            if text_blocks_drawn:
                f.write("=" * 70 + "\n")
                f.write(f"TEXT BLOCKS (Layout Model - V2 Architecture Step 3)\n")
                f.write("=" * 70 + "\n")
                f.write(f"Total text blocks on this page: {len(text_blocks_drawn)}\n")
                f.write("Color: RGB(255, 105, 180) - Hot Pink\n")
                f.write("Style: Solid line (title) | Dashed line (paragraph/list)\n\n")
                
                for i, block in enumerate(text_blocks_drawn, 1):
                    r = block.bbox
                    f.write(f"Text Block {i} ({block.block_type}):\n")
                    f.write(f"  Position: {r.x0:.1f},{r.y0:.1f} -> {r.x1:.1f},{r.y1:.1f}\n")
                    f.write(f"  Size: {r.width:.1f}×{r.height:.1f}pt ({r.width * r.height / 72.0 / 72.0:.2f} sq.in)\n")
                    f.write(f"  Column: {block.column} (-1=single, 0=left, 1=right)\n")
                    f.write(f"  Text units: {len(block.units)}\n")
                    # 显示前50个字符
                    sample_text = " ".join(u.text for u in block.units[:2])
                    if len(sample_text) > 80:
                        sample_text = sample_text[:77] + "..."
                    f.write(f"  Sample: {sample_text}\n\n")
                
                f.write("=" * 70 + "\n\n")
            
            # 写入阶段信息
            for stage in stages:
                r = stage.rect
                f.write(f"{stage.name}:\n")
                f.write(f"  Position: {r.x0:.1f},{r.y0:.1f} -> {r.x1:.1f},{r.y1:.1f}\n")
                f.write(f"  Size: {r.width:.1f}×{r.height:.1f}pt ({r.width * r.height / 72.0 / 72.0:.2f} sq.in)\n")
                f.write(f"  Color: RGB{stage.color}\n")
                f.write(f"  Description: {stage.description}\n\n")
        
        print(f"[DEBUG] Saved visualization: {vis_path}")
        print(f"[DEBUG] Saved legend: {legend_path}")

        # QA-03: 返回相对 out_dir 的稳定路径，写入 index.json 的 debug_artifacts
        rel_vis = os.path.relpath(os.path.abspath(vis_path), os.path.abspath(out_dir)).replace('\\', '/')
        rel_legend = os.path.relpath(os.path.abspath(legend_path), os.path.abspath(out_dir)).replace('\\', '/')
        return [rel_vis, rel_legend]
    except Exception as e:
        logger.warning(f"Debug visualization failed: {e}")
        import traceback
        traceback.print_exc()
        return None

# ---- 表格提取（Table/表） ----
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
    # A)
    text_trim: bool = True,
    text_trim_width_ratio: float = 0.55,
    text_trim_font_min: float = 7.0,
    text_trim_font_max: float = 16.0,
    text_trim_gap: float = 6.0,
    adjacent_th: float = 28.0,
    # A+: far-text trim options (dual-threshold)
    far_text_th: float = 300.0,
    far_text_para_min_ratio: float = 0.30,
    far_text_trim_mode: str = "aggressive",
    # P1-1: 下调阈值以覆盖"中间地带"（约 3-7 行）
    far_side_min_dist: float = 50.0,  # 从 100.0 降低到 50.0
    far_side_para_min_ratio: float = 0.12,  # 从 0.20 降低到 0.12
    # 2025-12-30 修复：表格 Phase C 使用更高的宽度阈值
    # 表格内容（表头、数据行）通常是短文本，而正文段落是长行
    # 使用更高的宽度阈值可以正确识别正文段落，同时不误判表格内容
    table_far_side_width_ratio: float = 0.7,  # 表格的 Phase C 使用 70% 宽度阈值（默认 50%）
    # B)
    object_pad: float = 8.0,
    object_min_area_ratio: float = 0.005,
    object_merge_gap: float = 4.0,
    # D)
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
    pdf_name = os.path.basename(pdf_path)
    doc = fitz.open(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    
    # === Smart Caption Detection for Tables (ENABLED) ===
    caption_index_table: Optional[CaptionIndex] = None
    if smart_caption_detection:
        if debug_captions:
            print(f"\n{'='*60}")
            print(f"SMART CAPTION DETECTION ENABLED FOR TABLES")
            print(f"{'='*60}")
        # Build caption index for tables (reuse figure logic)
        caption_index_table = build_caption_index(
            doc,
            figure_pattern=None,  # Skip figures
            table_pattern=re.compile(
                r"^\s*(?P<label>Extended\s+Data\s+Table|Supplementary\s+Table|Table|Tab\.?|表)\s*"
                r"(?:(?P<s_prefix>S)\s*(?P<s_id>(?:\d+|[IVX]{1,6}))|(?P<letter_id>[A-Z]\d+)|(?P<roman>[IVX]{1,6})|(?P<num>\d+))"
                r"(?:\s*\(continued\)|\s*续|\s*接上页)?",
                re.IGNORECASE
            ),
            debug=debug_captions
        )
    
    # === Adaptive Line Height: 统计文档行高并自适应调整参数 ===
    if adaptive_line_height:
        line_metrics = _estimate_document_line_metrics(doc, sample_pages=5, debug=debug_captions)
        typical_line_h = line_metrics['typical_line_height']
        
        # 自适应参数计算（基于行高的倍数）
        # 仅当参数为默认值时才替换（避免用户自定义参数被覆盖）
        if adjacent_th == 28.0:  # 表格默认值
            adjacent_th = 2.0 * typical_line_h
        if far_text_th == 300.0:  # 默认值
            # 2025-12-30 修复：提高倍数从 10.0 到 15.0（与 Figure 保持一致）
            far_text_th = 15.0 * typical_line_h
        if text_trim_gap == 6.0:  # 默认值
            text_trim_gap = 0.5 * typical_line_h
        if far_side_min_dist == 50.0:  # P1-1 调整后的新默认值
            # P1-1 调整：使用 3.0× 行高，以便检测"中间地带"文字（约 3-7 行）
            far_side_min_dist = 3.0 * typical_line_h
        
        if debug_captions:
            print(f"ADAPTIVE TABLE PARAMETERS (based on line_height={typical_line_h:.1f}pt):")
            print(f"  adjacent_th:      {adjacent_th:.1f} pt (2.0× line_height)")
            print(f"  far_text_th:      {far_text_th:.1f} pt (15.0× line_height)")
            print(f"  text_trim_gap:    {text_trim_gap:.1f} pt (0.5× line_height)")
            print(f"  far_side_min_dist:{far_side_min_dist:.1f} pt (3.0× line_height)")
            print()

    # --- P0-03 + P1-08 修复：表格编号解析，支持 S 前缀 + 罗马数字（如 "Supplementary Table IV" / "Table SIV"）---
    # 命名分组说明（供 _extract_table_ident 使用）：
    #   label:  表注类型前缀（含 Supplementary/Extended Data 等）
    #   s_prefix/s_id: 显式 S 前缀 + 编号（阿拉伯或罗马）
    #   letter_id: 附录表编号（A1/B2/...）
    #   roman:  普通罗马数字编号
    #   num:    普通数字编号
    table_line_re = re.compile(
        r"^\s*(?P<label>Extended\s+Data\s+Table|Supplementary\s+Table|Table|Tab\.?|表)\s*"
        r"(?:(?P<s_prefix>S)\s*(?P<s_id>(?:\d+|[IVX]{1,6}))|(?P<letter_id>[A-Z]\d+)|(?P<roman>[IVX]{1,6})|(?P<num>\d+))"
        r"(?:\s*\(continued\)|\s*续|\s*接上页)?",
        re.IGNORECASE,
    )

    force_above_env = os.getenv('EXTRACT_FORCE_TABLE_ABOVE', '')
    force_above_set = set([s.strip() for s in force_above_env.split(',') if s.strip()])
    t_below_set = set([str(x).strip() for x in (t_below or []) if str(x).strip()])
    t_above_set = set([str(x).strip() for x in (t_above or []) if str(x).strip()]) | force_above_set

    records: List[AttachmentRecord] = []
    seen_counts: Dict[str, int] = {}

    anchor_mode = os.getenv('EXTRACT_ANCHOR_MODE', '').lower()
    
    # Global side prescan for tables (similar to figures)
    global_side_table: Optional[str] = None
    if os.getenv('GLOBAL_ANCHOR_TABLE', 'auto').lower() == 'auto':
        try:
            ga_margin_tbl = float(os.getenv('GLOBAL_ANCHOR_TABLE_MARGIN', '0.03'))
        except ValueError:
            ga_margin_tbl = 0.03
        above_total_tbl = 0.0
        below_total_tbl = 0.0
        for pno_scan in range(len(doc)):
            page_s = doc[pno_scan]
            page_rect_s = page_s.rect
            dict_data_s = page_s.get_text("dict")
            text_lines_s = _collect_text_lines(dict_data_s)
            imgs_s: List[fitz.Rect] = []
            for blk in dict_data_s.get("blocks", []):
                if blk.get("type", 0) == 1 and "bbox" in blk:
                    imgs_s.append(fitz.Rect(*blk["bbox"]))
            vecs_s: List[fitz.Rect] = []
            try:
                for dr in page_s.get_drawings():
                    if isinstance(dr, dict) and "rect" in dr:
                        vecs_s.append(fitz.Rect(*dr["rect"]))
            except Exception as e:
                logger.warning(f"Failed to get drawings on page {pno_scan + 1}: {e}", extra={'page': pno_scan + 1, 'stage': 'global_anchor_table_prescan'})
            draw_items_s = collect_draw_items(page_s)
            def obj_ratio_s(clip: fitz.Rect) -> float:
                area = max(1.0, clip.width * clip.height)
                acc = 0.0
                for r in imgs_s + vecs_s:
                    inter = r & clip
                    if inter.height > 0 and inter.width > 0:
                        acc += inter.width * inter.height
                return min(1.0, acc / area)
            # Find table captions
            cap_re_tbl = re.compile(
                r"^\s*(?:(?:Extended\s+Data\s+Table|Supplementary\s+Table|Table|Tab\.?|表)\s*(?:S\s*)?[A-Z0-9IVX]+)\b",
                re.IGNORECASE
            )
            lines_s: List[Tuple[fitz.Rect, str]] = []
            for blk in dict_data_s.get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                for ln in blk.get("lines", []):
                    text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
                    lines_s.append((fitz.Rect(*(ln.get("bbox", [0,0,0,0]))), text))
            caps_tbl: List[fitz.Rect] = [r for (r,t) in lines_s if cap_re_tbl.match(t.strip())]
            caps_tbl.sort(key=lambda r: r.y0)
            x_left_s = page_rect_s.x0 + table_margin_x
            x_right_s = page_rect_s.x1 - table_margin_x
            for i_c, cap in enumerate(caps_tbl):
                prev_c = caps_tbl[i_c-1] if i_c-1 >= 0 else None
                next_c = caps_tbl[i_c+1] if i_c+1 < len(caps_tbl) else None
                # Above window
                topb = (prev_c.y1 + 8) if prev_c else page_rect_s.y0
                botb = cap.y0 - table_caption_gap
                yt = max(page_rect_s.y0, botb - table_clip_height, topb)
                yb = min(botb, yt + table_clip_height)
                yb = max(yt + 40, yb)
                clip_above = fitz.Rect(x_left_s, yt, x_right_s, min(yb, page_rect_s.y1))
                # Below window
                top2 = cap.y1 + table_caption_gap
                bot2 = (next_c.y0 - 8) if next_c else page_rect_s.y1
                y0b = min(max(page_rect_s.y0, top2), page_rect_s.y1 - 40)
                y1b = min(bot2, y0b + table_clip_height)
                y1b = max(y0b + 40, min(y1b, page_rect_s.y1))
                clip_below = fitz.Rect(x_left_s, y0b, x_right_s, y1b)
                # Score using table-specific metrics
                try:
                    pix_a = page_s.get_pixmap(matrix=fitz.Matrix(1,1), clip=clip_above, alpha=False)
                    ink_a = estimate_ink_ratio(pix_a)
                except Exception as e:
                    logger.warning(f"Failed to render table prescan clip (above) on page {pno_scan + 1}: {e}", extra={'page': pno_scan + 1, 'stage': 'global_anchor_table_prescan'})
                    ink_a = 0.0
                try:
                    pix_b = page_s.get_pixmap(matrix=fitz.Matrix(1,1), clip=clip_below, alpha=False)
                    ink_b = estimate_ink_ratio(pix_b)
                except Exception as e:
                    logger.warning(f"Failed to render table prescan clip (below) on page {pno_scan + 1}: {e}", extra={'page': pno_scan + 1, 'stage': 'global_anchor_table_prescan'})
                    ink_b = 0.0
                obj_a = obj_ratio_s(clip_above)
                obj_b = obj_ratio_s(clip_below)
                cols_a = _estimate_column_peaks(clip_above, text_lines_s) / 3.0
                cols_b = _estimate_column_peaks(clip_below, text_lines_s) / 3.0
                line_a = _line_density(clip_above, draw_items_s)
                line_b = _line_density(clip_below, draw_items_s)
                # Table score: ink + cols + lines + obj
                score_a = 0.4 * ink_a + 0.25 * min(1.0, cols_a) + 0.2 * line_a + 0.15 * obj_a
                score_b = 0.4 * ink_b + 0.25 * min(1.0, cols_b) + 0.2 * line_b + 0.15 * obj_b
                above_total_tbl += score_a
                below_total_tbl += score_b
        # P1-05: 全局锚点微弱优势回退（表格）
        total_score_tbl = above_total_tbl + below_total_tbl
        if total_score_tbl > 0:
            score_diff_ratio_tbl = abs(below_total_tbl - above_total_tbl) / total_score_tbl
        else:
            score_diff_ratio_tbl = 0
        
        CLOSE_MARGIN_TBL = 0.05  # 5% 以内视为"势均力敌"
        
        if score_diff_ratio_tbl < CLOSE_MARGIN_TBL:
            # 差距太小，不使用全局方向，按页独立决策
            global_side_table = None
            logger.info(f"Global table anchor: UNDECIDED (diff={score_diff_ratio_tbl:.1%} < {CLOSE_MARGIN_TBL:.0%}, using per-page decision)")
        elif below_total_tbl > above_total_tbl * (1.0 + ga_margin_tbl):
            global_side_table = 'below'
            logger.info(f"Global table anchor: BELOW (below={below_total_tbl:.2f} vs above={above_total_tbl:.2f}, diff={score_diff_ratio_tbl:.1%})")
        elif above_total_tbl > below_total_tbl * (1.0 + ga_margin_tbl):
            global_side_table = 'above'
            logger.info(f"Global table anchor: ABOVE (above={above_total_tbl:.2f} vs below={below_total_tbl:.2f}, diff={score_diff_ratio_tbl:.1%})")
        else:
            global_side_table = None
            logger.info(f"Global table anchor: AUTO (no clear preference, diff={score_diff_ratio_tbl:.1%})")
    
    # === Cache for smart-selected table captions ===
    smart_caption_cache_table: Dict[str, Tuple[fitz.Rect, str, int]] = {}
    
    if smart_caption_detection and caption_index_table and (not allow_continued):
        # Pre-select best captions for all tables
        for pno_pre in range(len(doc)):
            page_pre = doc[pno_pre]
            dict_data_pre = page_pre.get_text("dict")
            # Find all table IDs on this page
            page_table_ids = set()
            for blk in dict_data_pre.get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                for ln in blk.get("lines", []):
                    text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
                    m = table_line_re.match(text.strip())
                    if m:
                        ident = _extract_table_ident(m)
                        if ident:
                            page_table_ids.add(ident)
            
            # For each table ID, select best caption
            for table_id in page_table_ids:
                if table_id in smart_caption_cache_table:
                    continue  # Already cached
                candidates = caption_index_table.get_candidates('table', str(table_id))
                if candidates:
                    best = select_best_caption(candidates, page_pre, doc=doc, min_score_threshold=25.0, debug=debug_captions)
                    if best:
                        # Build full caption (merge subsequent lines)
                        full_caption = best.text
                        cap_rect = best.rect
                        block = best.block
                        lines_in_block = block.get("lines", [])
                        start_idx = best.line_idx + 1
                        parts = [full_caption]
                        for j in range(start_idx, len(lines_in_block)):
                            ln = lines_in_block[j]
                            t2 = "".join(sp.get("text", "") for sp in ln.get("spans", [])).strip()
                            if not t2 or table_line_re.match(t2):
                                break
                            parts.append(t2)
                            cap_rect = cap_rect | fitz.Rect(*(ln.get("bbox", [0,0,0,0])))
                            if t2.endswith('.') or sum(len(p) for p in parts) > 240:
                                break
                        full_caption = " ".join(parts)
                        smart_caption_cache_table[table_id] = (cap_rect, full_caption, best.page)
    
    for pno in range(len(doc)):
        page = doc[pno]
        page_rect = page.rect
        dict_data = page.get_text("dict")

        text_lines_all = _collect_text_lines(dict_data)
        image_rects: List[fitz.Rect] = []
        for blk in dict_data.get("blocks", []):
            if blk.get("type", 0) == 1 and "bbox" in blk:
                image_rects.append(fitz.Rect(*blk["bbox"]))
        vector_rects: List[fitz.Rect] = []
        try:
            for dr in page.get_drawings():
                if isinstance(dr, dict) and "rect" in dr:
                    vector_rects.append(fitz.Rect(*dr["rect"]))
        except Exception as e:
            logger.warning(f"Failed to get drawings on page {pno + 1}: {e}", extra={'page': pno + 1, 'stage': 'extract_tables'})
        draw_items = collect_draw_items(page)

        captions_on_page: List[Tuple[str, fitz.Rect, str]] = []
        
        # === Use smart-selected captions if available ===
        if smart_caption_detection and caption_index_table:
            if allow_continued:
                # Continued 模式：按页独立选择（同号多页均可输出）
                page_table_ids = set()
                for blk in dict_data.get("blocks", []):
                    if blk.get("type", 0) != 0:
                        continue
                    for ln in blk.get("lines", []):
                        text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
                        m = table_line_re.match(text.strip())
                        if m:
                            ident = _extract_table_ident(m)
                            if ident:
                                page_table_ids.add(ident)

                for table_id in sorted(page_table_ids):
                    candidates = caption_index_table.get_candidates('table', str(table_id))
                    candidates_on_page = [c for c in candidates if c.page == pno]
                    if not candidates_on_page:
                        continue
                    best = select_best_caption(candidates_on_page, page, doc=doc, min_score_threshold=25.0, debug=debug_captions)
                    if not best:
                        continue
                    full_caption = best.text
                    cap_rect = best.rect
                    block = best.block
                    lines_in_block = block.get("lines", [])
                    start_idx = best.line_idx + 1
                    parts = [full_caption]
                    for j in range(start_idx, len(lines_in_block)):
                        ln = lines_in_block[j]
                        t2 = "".join(sp.get("text", "") for sp in ln.get("spans", [])).strip()
                        if not t2 or table_line_re.match(t2):
                            break
                        parts.append(t2)
                        cap_rect = cap_rect | fitz.Rect(*(ln.get("bbox", [0,0,0,0])))
                        if t2.endswith('.') or sum(len(p) for p in parts) > 240:
                            break
                    full_caption = " ".join(parts)
                    captions_on_page.append((table_id, cap_rect, full_caption))
            else:
                # 非 continued：每个表号只取“跨页最优”图注所在页
                for table_id, (cap_rect, caption, cached_page) in smart_caption_cache_table.items():
                    if cached_page == pno:
                        captions_on_page.append((table_id, cap_rect, caption))
        else:
            # Fallback: Original logic
            for blk in dict_data.get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                lines = blk.get("lines", [])
                i = 0
                while i < len(lines):
                    ln = lines[i]
                    text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
                    t = text.strip()
                    m = table_line_re.match(t)
                    if not m:
                        i += 1
                        continue
                    # 提取表号：优先附录表、罗马数字、普通数字
                    ident = _extract_table_ident(m)
                    if not ident:
                        i += 1
                        continue
                    cap_rect = fitz.Rect(*(ln.get("bbox", [0,0,0,0])))
                    parts = [t]
                    char_count = len(t)
                    j = i + 1
                    while j < len(lines):
                        ln2 = lines[j]
                        t2 = "".join(sp.get("text", "") for sp in ln2.get("spans", [])).strip()
                        if not t2:
                            break
                        if table_line_re.match(t2):
                            break
                        parts.append(t2)
                        char_count += len(t2)
                        cap_rect = cap_rect | fitz.Rect(*(ln2.get("bbox", [0,0,0,0])))
                        if t2.endswith('.') or char_count > 240:
                            j += 1
                            break
                        j += 1
                    caption = " ".join(parts)
                    captions_on_page.append((ident, cap_rect, caption))
                    i = max(i+1, j)

        captions_on_page.sort(key=lambda t: t[1].y0)

        x_left = page_rect.x0 + table_margin_x
        x_right = page_rect.x1 - table_margin_x

        def object_area_ratio(clip: fitz.Rect) -> float:
            area = max(1.0, clip.width * clip.height)
            acc = 0.0
            for r in image_rects:
                inter = r & clip
                if inter.width > 0 and inter.height > 0:
                    acc += inter.width * inter.height
            for r in vector_rects:
                inter = r & clip
                if inter.width > 0 and inter.height > 0:
                    acc += inter.width * inter.height
            return min(1.0, acc / area)

        def comp_count(clip: fitz.Rect) -> int:
            area = max(1.0, clip.width * clip.height)
            cand: List[fitz.Rect] = []
            for r in image_rects + vector_rects:
                inter = r & clip
                if inter.width > 0 and inter.height > 0:
                    if (inter.width * inter.height) / area >= object_min_area_ratio:
                        cand.append(inter)
            return len(_merge_rects(cand, merge_gap=object_merge_gap)) if cand else 0

        def text_line_count(clip: fitz.Rect) -> int:
            c = 0
            for (lb, fs, tx) in text_lines_all:
                inter = lb & clip
                if inter.width > 0 and inter.height > 0:
                    c += 1
            return c

        for idx, (ident, cap_rect, caption) in enumerate(captions_on_page):
            prev_cap = captions_on_page[idx-1][1] if idx-1 >= 0 else None
            next_cap = captions_on_page[idx+1][1] if idx+1 < len(captions_on_page) else None

            # QA-03: 收集并关联本条目的 debug 产物（相对 out_dir）
            debug_artifacts: List[str] = []

            try:
                dist_lambda = float(os.getenv('SCAN_DIST_LAMBDA', '0.12'))
            except ValueError as e:
                logger.warning(
                    f"Invalid SCAN_DIST_LAMBDA='{os.getenv('SCAN_DIST_LAMBDA', '')}', using default 0.12: {e}",
                    extra={'page': pno + 1, 'kind': 'table', 'id': ident, 'stage': 'anchor_v2'}
                )
                dist_lambda = 0.12

            def score_table_clip(clip: fitz.Rect) -> float:
                small_scale = 1.0
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(small_scale, small_scale), clip=clip, alpha=False)
                    ink = estimate_ink_ratio(pix)
                except Exception as e:
                    logger.warning(
                        f"Failed to render score_table_clip on page {pno + 1}: {e}",
                        extra={'page': pno + 1, 'kind': 'table', 'id': ident, 'stage': 'score_table_clip'}
                    )
                    ink = 0.0
                obj = object_area_ratio(clip)
                cols = _estimate_column_peaks(clip, text_lines_all)
                cols_norm = min(1.0, cols / 3.0)
                line_d = _line_density(clip, draw_items)
                para = _paragraph_ratio(clip, text_lines_all, width_ratio=text_trim_width_ratio, font_min=text_trim_font_min, font_max=text_trim_font_max)
                
                # 方案A：调整表格评分权重（与图片保持一致的优化思路）
                # 降低墨迹权重，保留表格特有的列对齐和线密度特征
                # 增加高度奖励
                height_bonus = 0.03 * min(1.0, clip.height / 400.0)  # 表格高度奖励稍低
                base = 0.35 * ink + 0.18 * cols_norm + 0.12 * line_d + 0.35 * obj - 0.25 * para + height_bonus
                
                # 距离罚项
                if clip.y1 <= cap_rect.y0:
                    dist = abs(cap_rect.y0 - clip.y1)
                else:
                    dist = abs(clip.y0 - cap_rect.y1)
                base -= dist_lambda * (dist / max(1.0, page_rect.height))
                return base

            if anchor_mode == 'v1':
                top_bound = (prev_cap.y1 + 8) if prev_cap else page_rect.y0
                bot_bound = cap_rect.y0 - table_caption_gap
                yt_above = max(page_rect.y0, bot_bound - table_clip_height, top_bound)
                yb_above = min(bot_bound, yt_above + table_clip_height)
                yb_above = max(yt_above + 40, yb_above)
                clip_above = fitz.Rect(x_left, yt_above, x_right, min(yb_above, page_rect.y1))

                top2 = cap_rect.y1 + table_caption_gap
                bot2 = (next_cap.y0 - 8) if next_cap else page_rect.y1
                yt_below = min(max(page_rect.y0, top2), page_rect.y1 - 40)
                yb_below = min(bot2, yt_below + table_clip_height)
                yb_below = max(yt_below + 40, min(yb_below, page_rect.y1))
                clip_below = fitz.Rect(x_left, yt_below, x_right, yb_below)

                side = 'above'
                chosen_clip = clip_above
                if ident in t_below_set:
                    side, chosen_clip = 'below', clip_below
                elif ident in t_above_set:
                    side, chosen_clip = 'above', clip_above
                else:
                    try:
                        ra = score_table_clip(clip_above)
                        rb = score_table_clip(clip_below)
                        if ra >= rb * 0.98:
                            side, chosen_clip = 'above', clip_above
                        else:
                            side, chosen_clip = 'below', clip_below
                    except Exception as e:
                        logger.warning(
                            f"Table score comparison failed on page {pno + 1}: {e}",
                            extra={'page': pno + 1, 'kind': 'table', 'id': ident, 'stage': 'anchor_v1'}
                        )
                        side, chosen_clip = 'above', clip_above
                clip = chosen_clip
            else:
                # Anchor V2：多尺度滑窗 + 吸附
                # --- P0-04 修复：V2 也支持 --t-above/--t-below 强制方向 ---
                # 检查当前表号是否被强制指定方向
                forced_side_table: Optional[str] = None
                if ident in t_below_set:
                    forced_side_table = 'below'
                    if debug_captions:
                        print(f"[DBG] Table {ident}: forced direction=below (--t-below)")
                elif ident in t_above_set:
                    forced_side_table = 'above'
                    if debug_captions:
                        print(f"[DBG] Table {ident}: forced direction=above (--t-above)")
                
                # 确定扫描方向：强制方向 > 全局方向 > 双向扫描
                effective_side_table = forced_side_table if forced_side_table else global_side_table
                
                scan_heights = os.getenv('SCAN_HEIGHTS', '')
                if scan_heights:
                    try:
                        heights = [float(h) for h in scan_heights.split(',') if h.strip()]
                    except ValueError as e:
                        logger.warning(
                            f"Invalid SCAN_HEIGHTS='{scan_heights}', using defaults: {e}",
                            extra={'page': pno + 1, 'kind': 'table', 'id': ident, 'stage': 'anchor_v2'}
                        )
                        heights = [240.0, 320.0, 420.0, 520.0, 640.0, 720.0, 820.0, 920.0]
                else:
                    heights = [240.0, 320.0, 420.0, 520.0, 640.0, 720.0, 820.0, 920.0]
                try:
                    step = float(os.getenv('SCAN_STEP', '14'))
                except ValueError as e:
                    logger.warning(
                        f"Invalid SCAN_STEP='{os.getenv('SCAN_STEP', '')}', using default 14: {e}",
                        extra={'page': pno + 1, 'kind': 'table', 'id': ident, 'stage': 'anchor_v2'}
                    )
                    step = 14.0
                
                # 方案B：获取页面所有对象（用于边缘截断检测）
                all_table_objects = image_rects + vector_rects
                
                # 定义边缘截断检测函数（表格版本）
                def detect_top_edge_truncation_table(clip: fitz.Rect, objects: List[fitz.Rect], side: str) -> bool:
                    """
                    检测表格窗口边缘是否截断对象
                    
                    修复说明（2025-10-27）:
                        原逻辑反转：当对象边缘与clip重合时误判为截断，导致完整窗口被扣分
                        正确逻辑：检测对象是否延伸到clip外面（被clip边界截断）
                    """
                    min_obj_height = 50.0
                    for obj in objects:
                        if not (obj.x0 < clip.x1 and obj.x1 > clip.x0):
                            continue
                        if side == 'above':
                            # 如果对象顶部在clip外面，且对象底部在clip内足够深度 → 被截断
                            if obj.y0 < clip.y0 and obj.y1 > clip.y0 + min_obj_height:
                                return True
                        else:  # below
                            # 如果对象底部在clip外面，且对象顶部在clip内足够深度 → 被截断
                            if obj.y1 > clip.y1 and obj.y0 < clip.y1 - min_obj_height:
                                return True
                    return False
                
                cands: List[Tuple[float, str, fitz.Rect]] = []

                # above (respect forced/global anchor for tables)
                # P0-04: 使用 effective_side_table（含强制方向）控制扫描
                if effective_side_table in (None, 'above'):
                    top_bound = (prev_cap.y1 + 8) if prev_cap else page_rect.y0
                    bot_bound = cap_rect.y0 - table_caption_gap
                    for h in heights:
                        y1 = bot_bound
                        y0_min = max(page_rect.y0, top_bound)
                        y0 = max(y0_min, y1 - h)
                        while y0 + 40.0 <= y1:
                            c = fitz.Rect(x_left, y0, x_right, y1)
                            sc = score_table_clip(c)
                            # 方案B：边缘截断检测并扣分
                            if detect_top_edge_truncation_table(c, all_table_objects, 'above'):
                                sc -= 0.15
                            cands.append((sc, 'above', c))
                            y0 -= step
                            if y0 < y0_min:
                                break
                # below (respect forced/global anchor for tables)
                # P0-04: 使用 effective_side_table（含强制方向）控制扫描
                if effective_side_table in (None, 'below'):
                    top2 = cap_rect.y1 + table_caption_gap
                    bot2 = (next_cap.y0 - 8) if next_cap else page_rect.y1
                    for h in heights:
                        y0 = min(max(page_rect.y0, top2), page_rect.y1 - 40)
                        y1_max = min(bot2, page_rect.y1)
                        y1 = min(y1_max, y0 + h)
                        while y1 - 40.0 >= y0:
                            c = fitz.Rect(x_left, y0, x_right, y1)
                            sc = score_table_clip(c)
                            # 方案B：边缘截断检测并扣分
                            if detect_top_edge_truncation_table(c, all_table_objects, 'below'):
                                sc -= 0.15
                            cands.append((sc, 'below', c))
                            y0 += step
                            y1 = min(y1_max, y0 + h)
                            if y0 >= y1_max:
                                break
                if not cands:
                    side = 'above'
                    clip = fitz.Rect(x_left, max(page_rect.y0, cap_rect.y0 - table_clip_height), x_right, min(page_rect.y1, cap_rect.y1 + table_clip_height))
                else:
                    cands.sort(key=lambda t: t[0], reverse=True)
                    best = cands[0]
                    if os.getenv('DUMP_CANDIDATES', '0') == '1':
                        dbg_dir = os.path.join(out_dir, "debug")
                        os.makedirs(dbg_dir, exist_ok=True)
                        dbg_abs = dump_page_candidates(
                            page,
                            os.path.join(dbg_dir, f"Table_{ident}_p{pno+1}_debug_candidates.png"),
                            candidates=cands,
                            best=best,
                            caption_rect=cap_rect,
                        )
                        if dbg_abs:
                            debug_artifacts.append(
                                os.path.relpath(os.path.abspath(dbg_abs), os.path.abspath(out_dir)).replace('\\', '/')
                            )
                    side = best[1]
                    clip = snap_clip_edges(best[2], draw_items)
            
            # === Step 3: Layout-Guided Adjustment (如果启用) ===
            if layout_model is not None:
                clip_before_layout = fitz.Rect(clip)
                clip = _adjust_clip_with_layout(
                    clip_rect=clip,
                    caption_rect=cap_rect,
                    layout_model=layout_model,
                    page_num=pno,  # 0-based
                    direction=side,
                    debug=debug_captions
                )
                if debug_captions and clip != clip_before_layout:
                    logger.debug(f"Table {ident}: Layout-guided adjustment applied")

            base_clip = fitz.Rect(clip)
            base_height = max(1.0, base_clip.height)
            base_area = max(1.0, base_clip.width * base_clip.height)
            base_ink = 0.0
            try:
                pix_small = page.get_pixmap(matrix=fitz.Matrix(1,1), clip=base_clip, alpha=False)
                base_ink = estimate_ink_ratio(pix_small)
            except Exception as e:
                logger.warning(
                    f"Failed to render base clip for ink estimation: {e}",
                    extra={'page': pno + 1, 'kind': 'table', 'id': ident, 'stage': 'validation'}
                )
            base_comp = comp_count(base_clip)
            base_text = text_line_count(base_clip)

            # === Visual Debug (TABLE): 初始化并收集 Baseline ===
            debug_stages_tbl: List[DebugStageInfo] = []
            if debug_visual:
                debug_stages_tbl.append(DebugStageInfo(
                    name="Baseline (Anchor Selection)",
                    rect=fitz.Rect(base_clip),
                    color=(0, 102, 255),  # 蓝色
                    description=f"Initial window from anchor {side} selection"
                ))

            # A) 文本邻接裁切（含远侧文字 Phase C）
            # 2025-12-30 修复：表格内容保护 - 跳过 Phase C 以避免表头被误判为正文
            clip_after_A = fitz.Rect(clip)
            if text_trim:
                # 获取典型行高用于两行检测
                typical_lh = line_metrics.get('typical_line_height') if (adaptive_line_height and 'line_metrics' in locals()) else None
                # 2025-12-30 修复：表格使用更高的宽度阈值（70%）来识别正文段落
                # 这样表格内的短文本（单元格）不会被当作段落，但跨越整行的正文段落仍会被裁剪
                table_width_ratio = max(text_trim_width_ratio, table_far_side_width_ratio)
                clip = _trim_clip_head_by_text_v2(
                    clip,
                    page_rect,
                    cap_rect,
                    side,
                    text_lines_all,
                    width_ratio=table_width_ratio,  # 表格使用更高的宽度阈值
                    font_min=text_trim_font_min,
                    font_max=text_trim_font_max,
                    gap=text_trim_gap,
                    adjacent_th=adjacent_th,
                    far_text_th=far_text_th,
                    far_text_para_min_ratio=far_text_para_min_ratio,
                    far_text_trim_mode=far_text_trim_mode,
                    far_side_min_dist=far_side_min_dist,
                    far_side_para_min_ratio=far_side_para_min_ratio,
                    typical_line_h=typical_lh,
                    skip_adjacent_sweep=True,  # 2025-12-30 修复：表格跳过 adjacent sweep（保护表头）
                    debug=debug_captions,
                )
                clip_after_A = fitz.Rect(clip)
                if debug_visual and (clip_after_A != base_clip):
                    debug_stages_tbl.append(DebugStageInfo(
                        name="Phase A (Text Trimming)",
                        rect=fitz.Rect(clip_after_A),
                        color=(0, 200, 0),  # 绿色
                        description=f"After removing adjacent text (table width_ratio={table_width_ratio:.0%})"
                    ))

            # B) 对象连通域引导
            clip_after_B = fitz.Rect(clip)
            clip = _refine_clip_by_objects(
                clip,
                cap_rect,
                side,
                image_rects,
                vector_rects,
                object_pad=object_pad,
                min_area_ratio=object_min_area_ratio,
                merge_gap=object_merge_gap,
                near_edge_only=refine_near_edge_only,
                use_axis_union=True,
                use_horizontal_union=True,
            )
            clip_after_B = fitz.Rect(clip)
            if debug_visual and (clip_after_B != clip_after_A):
                debug_stages_tbl.append(DebugStageInfo(
                    name="Phase B (Object Alignment)",
                    rect=fitz.Rect(clip_after_B),
                    color=(255, 140, 0),  # 橙色
                    description="After object connectivity refinement"
                ))

            scale = dpi / 72.0
            mat = fitz.Matrix(scale, scale)
            try:
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
            except Exception as e:
                logger.warning(f"Render failed: {e}", extra={'page': pno+1, 'kind': 'table', 'id': ident})
                continue

            if autocrop:
                try:
                    masks_px: Optional[List[Tuple[int, int, int, int]]] = None
                    if autocrop_mask_text:
                        masks_px = _build_text_masks_px(
                            clip,
                            text_lines_all,
                            scale=scale,
                            direction=side,
                            near_frac=mask_top_frac,
                            width_ratio=mask_width_ratio,
                            font_max=mask_font_max,
                        )
                    l, t, r, b = detect_content_bbox_pixels(
                        pix,
                        white_threshold=autocrop_white_threshold,
                        pad=autocrop_pad_px,
                        mask_rects_px=masks_px,
                    )
                    tight = fitz.Rect(
                        clip.x0 + l / scale,
                        clip.y0 + t / scale,
                        clip.x0 + r / scale,
                        clip.y0 + b / scale,
                    )
                    
                    # ============================================================
                    # 【P0-1 核心约束】Phase D 远端边界单调性约束（表格）
                    # ============================================================
                    far_bound_limit_tbl: Optional[float] = None
                    far_bound_reason_tbl = ""
                    
                    if side == 'above':
                        # 远端是顶部
                        if clip_after_A is not None:
                            phase_a_far_trim = clip_after_A.y0 - base_clip.y0
                            if phase_a_far_trim > 2.0:  # 降低阈值从 5pt 到 2pt
                                far_bound_limit_tbl = clip_after_A.y0
                                far_bound_reason_tbl = f"Phase A trimmed {phase_a_far_trim:.1f}pt"
                        
                        if far_bound_limit_tbl is None:
                            has_evidence, suggested_limit = _detect_far_side_text_evidence(
                                base_clip, text_lines_all, side,
                                edge_zone=40.0, min_width_ratio=0.30
                            )
                            if has_evidence:
                                far_bound_limit_tbl = suggested_limit
                                far_bound_reason_tbl = "far-side text evidence detected"
                    else:
                        # 远端是底部
                        if clip_after_A is not None:
                            phase_a_far_trim = base_clip.y1 - clip_after_A.y1
                            if phase_a_far_trim > 2.0:
                                far_bound_limit_tbl = clip_after_A.y1
                                far_bound_reason_tbl = f"Phase A trimmed {phase_a_far_trim:.1f}pt"
                        
                        if far_bound_limit_tbl is None:
                            has_evidence, suggested_limit = _detect_far_side_text_evidence(
                                base_clip, text_lines_all, side,
                                edge_zone=40.0, min_width_ratio=0.30
                            )
                            if has_evidence:
                                far_bound_limit_tbl = suggested_limit
                                far_bound_reason_tbl = "far-side text evidence detected"
                    
                    # 应用远端边界约束
                    if far_bound_limit_tbl is not None:
                        if side == 'above':
                            if tight.y0 < far_bound_limit_tbl:
                                if debug_captions:
                                    logger.debug(f"Table {ident}: [P0-1 FAR BOUND] Limiting top from {tight.y0:.1f} to {far_bound_limit_tbl:.1f} ({far_bound_reason_tbl})")
                                tight = fitz.Rect(tight.x0, far_bound_limit_tbl, tight.x1, tight.y1)
                        else:
                            if tight.y1 > far_bound_limit_tbl:
                                if debug_captions:
                                    logger.debug(f"Table {ident}: [P0-1 FAR BOUND] Limiting bottom from {tight.y1:.1f} to {far_bound_limit_tbl:.1f} ({far_bound_reason_tbl})")
                                tight = fitz.Rect(tight.x0, tight.y0, tight.x1, far_bound_limit_tbl)
                    
                    # 远端边缘保护：表格通常需要保留页眉线等细要素
                    # 【重要】保护扩展不能超过 far_bound_limit_tbl
                    far_pad_pt = max(0.0, protect_far_edge_px / scale)
                    if far_pad_pt > 0:
                        if side == 'above':
                            new_y0 = max(page_rect.y0, tight.y0 - far_pad_pt)
                            if far_bound_limit_tbl is not None:
                                new_y0 = max(new_y0, far_bound_limit_tbl)
                            tight = fitz.Rect(tight.x0, new_y0, tight.x1, tight.y1)
                        else:
                            new_y1 = min(page_rect.y1, tight.y1 + far_pad_pt)
                            if far_bound_limit_tbl is not None:
                                new_y1 = min(new_y1, far_bound_limit_tbl)
                            tight = fitz.Rect(tight.x0, tight.y0, tight.x1, new_y1)
                    if (autocrop_min_height_px or autocrop_shrink_limit is not None):
                        min_h_pt = max(0.0, (autocrop_min_height_px / scale))
                        if autocrop_shrink_limit is not None:
                            min_h_pt = max(min_h_pt, clip.height * (1.0 - autocrop_shrink_limit))
                        if side == 'above':
                            y1_new = max(tight.y1, min(clip.y1, clip.y0 + min_h_pt))
                            tight = fitz.Rect(tight.x0, tight.y0, tight.x1, y1_new)
                        else:
                            y0_new = min(tight.y0, max(clip.y0, clip.y1 - min_h_pt))
                            tight = fitz.Rect(tight.x0, y0_new, tight.x1, tight.y1)
                    
                    # Step 3.5: 在 autocrop 后再次应用版式引导，确保不切断文本块
                    if layout_model is not None:
                        clip_before_post_layout = fitz.Rect(tight)
                        tight = _adjust_clip_with_layout(
                            clip_rect=tight,
                            caption_rect=cap_rect,
                            layout_model=layout_model,
                            page_num=pno,  # 0-based
                            direction=side,
                            debug=debug_captions
                        )
                        if debug_captions and tight != clip_before_post_layout:
                            logger.debug(f"Table {ident}: Post-autocrop layout adjustment applied")
                    
                    # ============================================================
                    # 【P0-3】Phase D 后轻量去正文后处理（表格）
                    # ============================================================
                    tight_before_post = fitz.Rect(tight)
                    tight, was_post_trimmed = _trim_far_side_text_post_autocrop(
                        tight, text_lines_all, side,
                        typical_line_h=typical_lh,
                        scan_lines=3,
                        min_width_ratio=0.30,
                        min_text_len=15,
                        gap=6.0,
                    )
                    if was_post_trimmed and debug_captions:
                        if side == 'above':
                            logger.debug(f"Table {ident}: [P0-3 POST TRIM] y0 pushed from {tight_before_post.y0:.1f} to {tight.y0:.1f}")
                        else:
                            logger.debug(f"Table {ident}: [P0-3 POST TRIM] y1 pushed from {tight_before_post.y1:.1f} to {tight.y1:.1f}")
                    
                    pix = page.get_pixmap(matrix=mat, clip=tight, alpha=False)
                    clip = tight
                except Exception as e:
                    logger.warning(f"Autocrop failed: {e}", extra={'page': pno+1, 'kind': 'table', 'id': ident, 'stage': 'phase_d'})

            if refine_safe:
                refined = fitz.Rect(clip)
                r_height = max(1.0, refined.height)
                r_area = max(1.0, refined.width * refined.height)
                r_comp = comp_count(refined)
                r_text = text_line_count(refined)
                r_ink = 0.0
                try:
                    pix_small2 = page.get_pixmap(matrix=fitz.Matrix(1,1), clip=refined, alpha=False)
                    r_ink = estimate_ink_ratio(pix_small2)
                except Exception as e:
                    logger.warning(
                        f"Failed to render refined clip for ink estimation: {e}",
                        extra={'page': pno + 1, 'kind': 'table', 'id': ident, 'stage': 'validation'}
                    )
                # P1-07: 动态计算表格验收阈值（基于基线高度和远侧覆盖率）
                far_cov_tbl = 0.0
                try:
                    near_is_top = (side == 'below')
                    far_is_top = not near_is_top
                    far_lines_tbl: List[fitz.Rect] = []
                    for (lb, fs, tx) in text_lines_all:
                        if not tx.strip():
                            continue
                        inter = lb & base_clip
                        if inter.width <= 0 or inter.height <= 0:
                            continue
                        width_ok = (inter.width / max(1.0, base_clip.width)) >= max(0.35, text_trim_width_ratio * 0.7)
                        size_ok = (text_trim_font_min <= fs <= text_trim_font_max)
                        if not (width_ok and size_ok):
                            continue
                        if far_is_top:
                            in_far = (lb.y0 < base_clip.y0 + 0.5 * base_clip.height)
                        else:
                            in_far = (lb.y1 > base_clip.y0 + 0.5 * base_clip.height)
                        if in_far:
                            far_lines_tbl.append(lb)
                    if far_lines_tbl:
                        if far_is_top:
                            region_h_tbl = max(1.0, (base_clip.y0 + 0.5 * base_clip.height) - base_clip.y0)
                        else:
                            region_h_tbl = max(1.0, base_clip.y1 - (base_clip.y0 + 0.5 * base_clip.height))
                        far_cov_tbl = sum(lb.height for lb in far_lines_tbl) / region_h_tbl
                except Exception as e:
                    logger.warning(
                        f"Failed to estimate far-side text coverage: {e}",
                        extra={'page': pno + 1, 'kind': 'table', 'id': ident, 'stage': 'validation'}
                    )
                
                # P1-07: 使用动态阈值函数（表格模式）
                thresholds_tbl = _adaptive_acceptance_thresholds(
                    base_height=base_height,
                    is_table=True,
                    far_cov=far_cov_tbl
                )
                relax_h = thresholds_tbl.relax_h
                relax_a = thresholds_tbl.relax_a
                relax_ink = thresholds_tbl.relax_ink
                relax_text = thresholds_tbl.relax_text
                ok_h = (r_height >= relax_h * base_height)
                ok_a = (r_area >= relax_a * base_area)
                
                # ============================================================
                # P2-1: 从密度比转向 mass/保留量指标（表格）
                # ============================================================
                base_ink_mass_tbl = base_ink * base_area
                r_ink_mass_tbl = r_ink * r_area
                
                ok_ink_mass_tbl = (r_ink_mass_tbl >= relax_ink * base_ink_mass_tbl) if base_ink_mass_tbl > 1e-9 else True
                
                significant_shrink_tbl = (r_area < 0.70 * base_area)
                if significant_shrink_tbl:
                    ok_ink_density_tbl = (r_ink >= 0.60 * base_ink) if base_ink > 1e-9 else True
                else:
                    ok_ink_density_tbl = True
                
                ok_i = ok_ink_mass_tbl and ok_ink_density_tbl
                ok_t = (r_text >= max(1, int(relax_text * base_text))) if base_text > 0 else True
                ok_comp = (r_comp >= min(2, base_comp)) if base_comp >= 2 else True
                if not (ok_h and ok_a and ok_i and ok_t and ok_comp):
                    # 表格验收失败日志（P2-1 增强）
                    reasons = []
                    if not ok_h: reasons.append(f"height={r_height/base_height:.1%}")
                    if not ok_a: reasons.append(f"area={r_area/base_area:.1%}")
                    if not ok_i:
                        if not ok_ink_mass_tbl:
                            reasons.append(f"ink_mass={r_ink_mass_tbl/base_ink_mass_tbl:.1%}" if base_ink_mass_tbl > 1e-9 else "ink_mass=low")
                        if significant_shrink_tbl and not ok_ink_density_tbl:
                            reasons.append(f"ink_density={r_ink/base_ink:.1%}" if base_ink > 1e-9 else "ink_density=low")
                    if not ok_t: reasons.append(f"text_lines={r_text}/{base_text}")
                    if not ok_comp: reasons.append(f"comp={r_comp}/{base_comp}")
                    logger.warning(f"Refinement rejected ({', '.join(reasons)}), using A-only fallback", extra={'page': pno+1, 'kind': 'table', 'id': ident, 'stage': 'validation'})
                    log_event(
                        "refine_rejected",
                        level="warning",
                        pdf=pdf_name,
                        page=pno + 1,
                        kind="table",
                        id=str(ident),
                        stage="validation",
                        message="Refinement rejected; trying fallback",
                        reasons=reasons,
                        side=side,
                        far_cov=round(float(far_cov_tbl), 4),
                        thresholds={
                            "description": thresholds_tbl.description,
                            "relax_h": round(float(relax_h), 4),
                            "relax_a": round(float(relax_a), 4),
                            "relax_text": round(float(relax_text), 4),
                            "relax_ink": round(float(relax_ink), 4),
                        },
                        metrics={
                            "base": {
                                "height": round(float(base_height), 2),
                                "area": round(float(base_area), 2),
                                "ink": round(float(base_ink), 6),
                                "text_lines": int(base_text),
                                "comp": int(base_comp),
                            },
                            "refined": {
                                "height": round(float(r_height), 2),
                                "area": round(float(r_area), 2),
                                "ink": round(float(r_ink), 6),
                                "text_lines": int(r_text),
                                "comp": int(r_comp),
                            },
                        },
                        clips={
                            "base": _rect_to_list(base_clip),
                            "refined": _rect_to_list(refined),
                        },
                    )
                    typical_lh_fallback_tbl = line_metrics.get('typical_line_height') if (adaptive_line_height and 'line_metrics' in locals()) else None
                    # 2025-12-30 修复：表格使用更高的宽度阈值（70%）来识别正文段落
                    table_width_ratio_fallback = max(text_trim_width_ratio, table_far_side_width_ratio)
                    clip_A = _trim_clip_head_by_text_v2(
                        base_clip, page_rect, cap_rect, side, text_lines_all,
                        width_ratio=table_width_ratio_fallback,  # 表格使用更高的宽度阈值
                        font_min=text_trim_font_min,
                        font_max=text_trim_font_max,
                        gap=text_trim_gap,
                        adjacent_th=adjacent_th,
                        far_text_th=far_text_th,
                        far_text_para_min_ratio=far_text_para_min_ratio,
                        far_text_trim_mode=far_text_trim_mode,
                        far_side_min_dist=far_side_min_dist,
                        far_side_para_min_ratio=far_side_para_min_ratio,
                        typical_line_h=typical_lh_fallback_tbl,
                        skip_adjacent_sweep=True,  # 2025-12-30 修复：表格跳过 adjacent sweep（保护表头）
                        debug=debug_captions,
                    ) if text_trim else base_clip
                    # P1-07: 二次门槛也使用动态阈值
                    rA_h, rA_a = max(1.0, clip_A.height), max(1.0, clip_A.width * clip_A.height)
                    # P1-07: A-only fallback 也使用动态阈值（必须沿用同页 far_cov_tbl，否则会误拒绝并回退到 baseline）
                    fallback_th_tbl = _adaptive_acceptance_thresholds(base_height, is_table=True, far_cov=far_cov_tbl)
                    if (rA_h >= fallback_th_tbl.relax_h * base_height) and (rA_a >= fallback_th_tbl.relax_a * base_area):
                        clip = clip_A
                        log_event(
                            "refine_fallback_a_only",
                            level="info",
                            pdf=pdf_name,
                            page=pno + 1,
                            kind="table",
                            id=str(ident),
                            stage="validation",
                            message="Using A-only fallback after refinement rejection",
                            side=side,
                            fallback_thresholds={
                                "description": fallback_th_tbl.description,
                                "relax_h": round(float(fallback_th_tbl.relax_h), 4),
                                "relax_a": round(float(fallback_th_tbl.relax_a), 4),
                            },
                            metrics={
                                "fallback_a": {
                                    "height": round(float(rA_h), 2),
                                    "area": round(float(rA_a), 2),
                                }
                            },
                            clips={
                                "fallback_a": _rect_to_list(clip_A),
                                "final": _rect_to_list(clip),
                            },
                        )
                    else:
                        clip = base_clip
                        log_event(
                            "refine_revert_baseline",
                            level="warning",
                            pdf=pdf_name,
                            page=pno + 1,
                            kind="table",
                            id=str(ident),
                            stage="validation",
                            message="Reverted to baseline after refinement rejection (A-only fallback also rejected)",
                            side=side,
                            fallback_thresholds={
                                "description": fallback_th_tbl.description,
                                "relax_h": round(float(fallback_th_tbl.relax_h), 4),
                                "relax_a": round(float(fallback_th_tbl.relax_a), 4),
                            },
                            metrics={
                                "fallback_a": {
                                    "height": round(float(rA_h), 2),
                                    "area": round(float(rA_a), 2),
                                }
                            },
                            clips={
                                "baseline": _rect_to_list(base_clip),
                                "fallback_a": _rect_to_list(clip_A),
                                "final": _rect_to_list(clip),
                            },
                        )
                        if debug_visual:
                            debug_stages_tbl.append(DebugStageInfo(
                                name="Fallback (Reverted to Baseline)",
                                rect=fitz.Rect(clip),
                                color=(255, 255, 0),  # 黄色
                                description="Refinement rejected, reverted to baseline"
                            ))
                    try:
                        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                    except Exception as e:
                        logger.error(
                            f"Failed to render fallback clip for Table {ident} on page {pno + 1}: {e}",
                            extra={'page': pno + 1, 'kind': 'table', 'id': ident, 'stage': 'render'}
                        )
                        continue

            # Debug: 标记最终结果（成功的精炼或 A-only 回退），并保存可视化
            if debug_visual:
                # 若最终结果源自 D（autocrop）阶段，则标红
                if autocrop and (clip != base_clip) and (clip != clip_after_A):
                    debug_stages_tbl.append(DebugStageInfo(
                        name="Phase D (Final - Autocrop)",
                        rect=fitz.Rect(clip),
                        color=(255, 0, 0),  # 红色
                        description="Final result after A+B+D refinement"
                    ))
                elif clip == clip_after_A and text_trim:
                    # A-only 回退（B/D 未改变边界）
                    debug_stages_tbl.append(DebugStageInfo(
                        name="Final (A-only Fallback)",
                        rect=fitz.Rect(clip),
                        color=(255, 200, 0),  # 金黄色
                        description="A-only fallback result (B/D rejected)"
                    ))
                try:
                    artifacts = save_debug_visualization(
                        page=page,
                        out_dir=out_dir,
                        fig_no=ident,
                        page_num=pno + 1,
                        stages=debug_stages_tbl,
                        caption_rect=cap_rect,
                        kind='table',
                        layout_model=layout_model  # V2 Architecture
                    )
                    if artifacts:
                        debug_artifacts.extend(artifacts)
                except Exception as e:
                    logger.warning(f"Debug visualization failed: {e}", extra={'page': pno+1, 'kind': 'table', 'id': ident})

            base_name = build_output_basename('Table', ident, caption, max_chars=max_caption_chars, max_words=max_caption_words)
            count_prev = seen_counts.get(ident, 0)
            cont = False
            if count_prev >= 1 and not allow_continued:
                continue
            if count_prev >= 1 and allow_continued:
                base_name = f"{base_name}_continued_p{pno+1}"
                cont = True
            out_path = os.path.join(out_dir, base_name + ".png")
            # P0-07: 文件名碰撞处理
            out_path, had_collision = get_unique_path(out_path)
            pix.save(out_path)
            seen_counts[ident] = count_prev + 1
            records.append(AttachmentRecord('table', ident, pno + 1, caption, out_path, continued=cont, debug_artifacts=debug_artifacts))
            logger.info(f"Table {ident} page {pno+1} -> {out_path}")

    records.sort(key=lambda r: (r.page, r.num_key(), r.ident))
    return records


# ============================================================================
# 版式驱动提取（V2 Architecture - Layout-Driven Extraction）
# ============================================================================

def _classify_text_types(
    all_units: Dict[int, List[EnhancedTextUnit]],
    typical_font_size: float,
    typical_font_name: str,
    page_width: float,
    debug: bool = False
) -> Dict[int, List[EnhancedTextUnit]]:
    """
    基于规则的文本类型分类器（Step 3增强版）
    
    分类规则：
    1. Caption（图注/表注）: 匹配正则 + 字号略小于正文
    2. Title（标题）: 加粗 + 字号大
    3. List（列表）: bullet点或编号
    4. In-Figure Text（图表内文字）: 字体不同 or 字号小 or 短文本
    5. Paragraph（段落）: 默认类型
    """
    import re
    
    if debug:
        print("\n[DEBUG] Text Type Classification (Step 3 Enhanced)")
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
                    # 加粗但字号不大，需要进一步判断
                    # 特殊规则：如果是短文本（如 "3.5 Positional Encoding"），可能是小标题
                    text_len = len(text_stripped)
                    # 检测是否是编号标题（如 "3.5 Something"、"4.2.1 Title"）
                    import re
                    is_numbered_title = bool(re.match(r'^\d+(\.\d+)*\s+[A-Z]', text_stripped))
                    
                    if is_numbered_title or (text_len < 60 and text_len > 5):
                        # 短加粗文本，很可能是标题
                        unit.text_type = 'title_h3'
                        unit.confidence = 0.75
                    else:
                        # 长加粗文本，可能是段落强调或图表内文字
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
            
            # 规则4: Equation检测（简化）
            special_chars = set('∫∑∏√±≈≠≤≥∞αβγδθλμσΔΩ')
            if len(set(text_stripped) & special_chars) > 0 and unit.bbox.width < 0.6 * page_width:
                unit.text_type = 'equation'
                unit.confidence = 0.75
                continue
            
            # 规则5（新增）: In-Figure Text（图表内文字）检测
            # 特征：
            # - 字体与正文不同（font family不同）
            # - 字号明显小于正文（< 0.85×typical）
            # - 短文本（< 30字符）且独立成行
            # - 宽度小于页面的40%
            is_different_font = (typical_font_name.lower() not in unit.font_name.lower() and 
                                unit.font_name.lower() not in typical_font_name.lower())
            is_small_font = unit.font_size < 0.85 * typical_font_size
            is_short_text = len(text_stripped) < 30
            is_narrow = unit.bbox.width < 0.4 * page_width
            
            # 组合判断：如果满足多个特征，可能是图表内文字
            infig_score = 0
            if is_different_font:
                infig_score += 2  # 字体不同是强特征
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


def _detect_columns(
    all_units: Dict[int, List[EnhancedTextUnit]],
    page_width: float,
    debug: bool = False
) -> Tuple[int, float, Dict[int, List[EnhancedTextUnit]]]:
    """
    检测文档是单栏还是双栏
    
    方法：统计段落文本的x0分布，检测双峰
    
    返回: (num_columns, column_gap, updated_units)
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
    
    # 使用numpy进行直方图分析
    try:
        import numpy as np
        x0_array = np.array(x0_values)
        hist, bins = np.histogram(x0_array, bins=20)
        
        # 简单的峰值检测：找到直方图中的两个主要峰值
        # 峰值定义：该bin的计数高于平均值的1.5倍
        threshold = np.mean(hist) * 1.5
        peaks_idx = np.where(hist > threshold)[0]
        
        if len(peaks_idx) >= 2:
            # 选择最高的两个峰
            top_peaks = sorted(peaks_idx, key=lambda i: hist[i], reverse=True)[:2]
            top_peaks.sort()  # 按位置排序
            
            peak1_x = bins[top_peaks[0]]
            peak2_x = bins[top_peaks[1]]
            
            # 双栏
            num_columns = 2
            column_gap = peak2_x - peak1_x - (page_width - peak2_x)
            mid_x = (peak1_x + peak2_x) / 2
            
            if debug:
                print(f"Detected TWO columns:")
                print(f"  Left column x0 ≈ {peak1_x:.1f}pt")
                print(f"  Right column x0 ≈ {peak2_x:.1f}pt")
                print(f"  Column gap ≈ {column_gap:.1f}pt")
            
            # 标注每个单元所在栏
            for units in all_units.values():
                for unit in units:
                    unit.column = 0 if unit.bbox.x0 < mid_x else 1
        else:
            # 单栏
            num_columns = 1
            column_gap = 0.0
            
            if debug:
                print(f"Detected SINGLE column")
            
            for units in all_units.values():
                for unit in units:
                    unit.column = -1
    except ImportError:
        # numpy未安装，默认单栏
        if debug:
            print("NumPy not available, assuming single column")
        num_columns = 1
        column_gap = 0.0
        for units in all_units.values():
            for unit in units:
                unit.column = -1
    
    return num_columns, column_gap, all_units


def _build_text_blocks(
    all_units: Dict[int, List[EnhancedTextUnit]],
    typical_line_height: float,
    debug: bool = False
) -> Dict[int, List[TextBlock]]:
    """
    将相邻的文本单元聚合成文本区块（Step 3增强版）
    
    聚合规则：
    1. 同类型（如都是paragraph）
    2. 垂直距离 < 2×typical_line_height
    3. 同一栏
    
    新增：
    - 为标题创建单独的TextBlock（用于debug可视化）
    - 排除in_figure_text（图表内文字）
    """
    if debug:
        print("\n[DEBUG] Building Text Blocks (Step 3 Enhanced)")
        print("=" * 70)
        print(f"Typical line height: {typical_line_height:.1f}pt")
    
    all_blocks: Dict[int, List[TextBlock]] = {}
    
    for pno, units in all_units.items():
        if not units:
            all_blocks[pno] = []
            continue
        
        # 按y坐标排序
        sorted_units = sorted(units, key=lambda u: u.bbox.y0)
        
        blocks: List[TextBlock] = []
        current_block_units = [sorted_units[0]]
        current_type = sorted_units[0].text_type
        current_column = sorted_units[0].column
        
        for i in range(1, len(sorted_units)):
            unit = sorted_units[i]
            prev_unit = sorted_units[i-1]
            
            # 检查是否应该聚合
            same_type = unit.text_type == current_type
            same_column = unit.column == current_column
            vertical_distance = unit.bbox.y0 - prev_unit.bbox.y1
            close_distance = vertical_distance < 2 * typical_line_height
            
            if same_type and same_column and close_distance:
                current_block_units.append(unit)
            else:
                # 创建新区块
                # 1. 段落/列表：聚合多行（>=2）
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
                # 2. 标题：创建单独的block（用于debug可视化）
                elif current_type.startswith('title_') and len(current_block_units) >= 1:
                    merged_bbox = fitz.Rect()
                    for u in current_block_units:
                        merged_bbox |= u.bbox
                    blocks.append(TextBlock(
                        bbox=merged_bbox,
                        units=current_block_units,
                        block_type=current_type,  # 保留原始类型（title_h1/h2/h3）
                        page=pno,
                        column=current_column
                    ))
                # 3. in_figure_text：跳过，不创建block
                # 4. caption/equation：跳过
                
                # 开始新区块
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
            for i, block in enumerate(blocks[:5]):  # 显示前5个
                print(f"  Block {i+1}: {block.block_type}, {len(block.units)} units, bbox={block.bbox}")
    
    return all_blocks


def _detect_vacant_regions(
    all_blocks: Dict[int, List[TextBlock]],
    doc: "fitz.Document",
    debug: bool = False
) -> Dict[int, List[fitz.Rect]]:
    """
    识别页面中的留白区域（可能包含图表）
    
    方法：
    1. 将页面划分为网格（50×50pt）
    2. 标记被文本区块覆盖的格子
    3. 连通未覆盖的格子，形成留白区域
    4. 过滤小区域（< 0.05 × page_area）
    """
    if debug:
        print("\n[DEBUG] Detecting Vacant Regions")
        print("=" * 70)
    
    grid_size = 50  # pt
    all_vacant: Dict[int, List[fitz.Rect]] = {}
    
    for pno in range(len(doc)):
        page = doc[pno]
        page_rect = page.rect
        
        # 创建网格
        nx = int(page_rect.width / grid_size) + 1
        ny = int(page_rect.height / grid_size) + 1
        
        try:
            import numpy as np
            grid = np.zeros((ny, nx), dtype=bool)  # True = 被文本覆盖
            
            # 标记文本区块
            blocks = all_blocks.get(pno, [])
            for block in blocks:
                if block.block_type in ['paragraph_group', 'list_group']:
                    # 计算区块覆盖的网格范围
                    x0_idx = max(0, int(block.bbox.x0 / grid_size))
                    y0_idx = max(0, int(block.bbox.y0 / grid_size))
                    x1_idx = min(nx, int(block.bbox.x1 / grid_size) + 1)
                    y1_idx = min(ny, int(block.bbox.y1 / grid_size) + 1)
                    
                    grid[y0_idx:y1_idx, x0_idx:x1_idx] = True
            
            # 连通分量分析
            from scipy.ndimage import label as scipy_label
            labeled_grid, num_features = scipy_label(~grid)
            
            vacant_rects = []
            for region_id in range(1, num_features + 1):
                # 提取该区域的格子坐标
                coords = np.argwhere(labeled_grid == region_id)
                if len(coords) == 0:
                    continue
                
                # 转换为pdf坐标
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
                
                # 过滤小区域
                area_ratio = (rect.width * rect.height) / (page_rect.width * page_rect.height)
                if area_ratio > 0.05:  # 至少占5%页面面积
                    vacant_rects.append(rect)
            
            all_vacant[pno] = vacant_rects
            
            if debug and pno == 0:
                print(f"[Page {pno+1}] Found {len(vacant_rects)} vacant regions")
                for i, rect in enumerate(vacant_rects[:3]):
                    area_ratio = (rect.width * rect.height) / (page_rect.width * page_rect.height)
                    print(f"  Region {i+1}: {rect}, area={area_ratio:.1%}")
        
        except ImportError:
            # numpy或scipy未安装，跳过留白检测
            if debug and pno == 0:
                print(f"[Page {pno+1}] NumPy/SciPy not available, skipping vacant region detection")
            all_vacant[pno] = []
    
    return all_vacant


def _adjust_clip_with_layout(
    clip_rect: fitz.Rect,
    caption_rect: fitz.Rect,
    layout_model: DocumentLayoutModel,
    page_num: int,  # 0-based
    direction: str,  # 'above' or 'below'
    debug: bool = False
) -> fitz.Rect:
    """
    使用版式信息优化图表裁剪边界（Step 3核心功能）
    
    策略：
    1. 检测clip_rect与正文段落的重叠
    2. 如果重叠过多，调整边界以贴合文本区块边界
    3. 使用文本区块边界作为"软约束"
    
    参数:
        clip_rect: 候选窗口
        caption_rect: 图注边界框
        layout_model: 版式模型
        page_num: 页码（0-based）
        direction: 图注方向（'above' = 图在上方，'below' = 图在下方）
        debug: 调试模式
    
    返回:
        调整后的边界框
    """
    text_blocks = layout_model.text_blocks.get(page_num, [])
    if not text_blocks:
        return clip_rect  # 无文本区块，直接返回
    
    # 筛选出正文段落区块和标题（标题也需要保护，避免被误包含）
    protected_blocks = [b for b in text_blocks if b.block_type in ['paragraph_group', 'list_group'] or b.block_type.startswith('title_')]
    if not protected_blocks:
        return clip_rect
    
    # 区分"内容区块"（图表内部）和"外部区块"（需要排除）
    # 内容区块：位于 caption 和 clip 之间且与 clip 有显著重叠的文本块
    # 外部区块：远离 clip 边界或重叠度低的文本块
    content_blocks = []
    external_blocks = []
    
    for block in protected_blocks:
        # 计算重叠度
        inter = clip_rect & block.bbox
        if inter.is_empty:
            external_blocks.append(block)
            continue
        
        overlap_with_clip = (inter.width * inter.height) / (block.bbox.width * block.bbox.height)
        
        # 【关键修复】：区分"章节标题"和"表头行"
        # 章节标题（如 "5.1.2 Performance of Audio→Text" 或 "3.5 Positional Encoding"）应该被排除
        # 表头行（如 "Gemini-2.5-Flash Qwen3-235B-A22B..."）应该被保留为表格内容
        # 
        # 判断标准：
        # 1. 章节标题通常以数字编号开头（如 "5.1.2"、"3.5"、"4 "）
        # 2. 章节标题通常距离 caption 较远（>50pt）
        # 3. 章节标题通常靠近 clip 的远端边界（与 caption 相对的一侧）
        # 4. 表头行通常紧邻 caption 且不以数字编号开头
        if block.block_type.startswith('title_'):
            import re

            block_text = block.units[0].text.strip() if block.units else ""

            def _looks_like_table_numeric_cell(s: str) -> bool:
                t = (s or "").strip()
                # 允许纯编号（如 "4"、"6"）继续走标题逻辑
                if len(t) <= 4 and re.fullmatch(r"[\d\.]+", t):
                    return False

                t_norm = (
                    t.replace("×", "x")
                    .replace("·", "x")
                    .replace("−", "-")
                )
                t_compact = re.sub(r"\s+", "", t_norm)

                # 科学计数法：3.3x10^18 / 2.3e19
                if re.match(r"^\d+(?:\.\d+)?x10(?:\^?\d+)?$", t_compact, re.IGNORECASE):
                    return True
                if re.match(r"^\d+(?:\.\d+)?e[+-]?\d+$", t_compact, re.IGNORECASE):
                    return True

                # 纯数字/符号（常见于表格单元格）
                if re.fullmatch(r"[\d\.,\-\+\%/]+", t_compact):
                    return True

                return False

            # 检查是否是章节标题（编号 + 空格 + 真实标题文本）
            # 避免把表格中的科学计数法/数字单元格误判为章节标题（如 "3.3 × 10^18"）
            is_section_title = False
            if block_text and not _looks_like_table_numeric_cell(block_text):
                m = re.match(r"^(\d+(?:\.\d+)*)\s+(.*)$", block_text)
                if m:
                    after = (m.group(2) or "").strip()
                    if after:
                        after_norm = after.replace("×", "x").replace("·", "x")
                        after_compact = re.sub(r"\s+", "", after_norm)
                        # 过滤 "x10^18" 这类科学计数法后缀
                        if not re.match(r"^[xX]10(?:\^?\d+)?", after_compact):
                            is_section_title = after[0].isalpha()
            
            # 计算与 caption 的距离
            if direction == 'below':
                dist_from_caption = block.bbox.y0 - caption_rect.y1
                # 检查是否靠近 clip 的底部（远端）
                dist_from_clip_far_edge = clip_rect.y1 - block.bbox.y0
                is_near_far_edge = dist_from_clip_far_edge < 50  # 距离 clip 底部 <50pt
            else:
                dist_from_caption = caption_rect.y0 - block.bbox.y1
                # 检查是否靠近 clip 的顶部（远端）
                dist_from_clip_far_edge = block.bbox.y1 - clip_rect.y0
                is_near_far_edge = dist_from_clip_far_edge < 50  # 距离 clip 顶部 <50pt
            
            # 排除条件（满足任一）：
            # 1. 以数字编号开头且距离 caption 较远（>50pt）
            # 2. 靠近 clip 远端边界（<50pt）且距离 caption 较远（>100pt）
            #    这可以捕获不以数字开头的章节标题（如 "Performance of Audio→Text"）
            should_exclude = False
            exclude_reason = ""
            
            if is_section_title and dist_from_caption > 50:
                should_exclude = True
                exclude_reason = "section title (numbered)"
            elif (not _looks_like_table_numeric_cell(block_text)) and is_near_far_edge and dist_from_caption > 100:
                should_exclude = True
                exclude_reason = "title near clip far edge"
            
            if should_exclude:
                external_blocks.append(block)
                if debug:
                    print(f"  [SECTION TITLE EXCLUDED: {exclude_reason}] {block.block_type}: '{block_text[:50]}...' at y={block.bbox.y0:.1f}, dist_caption={dist_from_caption:.1f}pt, dist_far_edge={dist_from_clip_far_edge:.1f}pt")
                continue
            elif debug and (is_section_title or is_near_far_edge):
                print(f"  [TITLE KEPT] {block.block_type}: '{block_text[:50]}...' at y={block.bbox.y0:.1f}, dist_caption={dist_from_caption:.1f}pt, dist_far_edge={dist_from_clip_far_edge:.1f}pt")
        
        if direction == 'below':
            # 图在下方，caption在上方
            # 内容区块：在 caption 下方且与 clip 重叠度>50%
            if block.bbox.y0 >= caption_rect.y1 - 5 and overlap_with_clip > 0.5:
                content_blocks.append(block)
            else:
                external_blocks.append(block)
        else:  # direction == 'above'
            # 图在上方，caption在下方
            # 内容区块：在 caption 上方且与 clip 重叠度>50%
            if block.bbox.y1 <= caption_rect.y0 + 5 and overlap_with_clip > 0.5:
                content_blocks.append(block)
            else:
                external_blocks.append(block)
    
    # 只考虑外部区块的重叠（内容区块是应该保留的）
    total_overlap_area = 0.0
    clip_area = clip_rect.width * clip_rect.height
    
    overlapping_blocks = []
    for block in external_blocks:
        inter = clip_rect & block.bbox
        if not inter.is_empty:
            overlap_area = inter.width * inter.height
            total_overlap_area += overlap_area
            overlap_ratio = overlap_area / clip_area
            # 标题类区块：即使重叠度小也要记录（降低阈值到1%）
            # 段落类区块：需要重叠度>5%才记录
            threshold = 0.01 if block.block_type.startswith('title_') else 0.05
            if overlap_ratio > threshold:
                overlapping_blocks.append((block, inter, overlap_ratio))
    
    overlap_ratio_total = total_overlap_area / clip_area if clip_area > 0 else 0
    
    if debug:
        print(f"\n[DEBUG] Layout-Guided Clipping Adjustment")
        print(f"  Direction: {direction}")
        print(f"  Original clip: {clip_rect}")
        print(f"  Content blocks (inside): {len(content_blocks)}")
        print(f"  External blocks (outside): {len(external_blocks)}")
        print(f"  Total overlap (external only): {overlap_ratio_total:.1%}")
        print(f"  Overlapping blocks: {len(overlapping_blocks)}")
    
    # 初始化调整后的边界
    adjusted_clip = fitz.Rect(clip_rect)
    
    # ===== 优先处理：内容区块边界保护（即使外部重叠度低也要执行） =====
    # 特殊处理：如果有内容区块被部分切断，扩展clip以包含完整内容
    # 这主要解决表格内文字被切断的问题（如 Table 4 的表头）
    # 
    # P0-1 约束（2025-12-30）：
    # 只允许向 **近端**（靠近 caption）方向扩展，禁止向 **远端** 扩展
    # - direction='above'（caption在clip下方）：近端=bottom，远端=top
    # - direction='below'（caption在clip上方）：近端=top，远端=bottom
    content_adjusted = False
    for block in content_blocks:
        if direction == 'below':
            # 图在下方，caption在上方
            # 近端 = top (y0)，远端 = bottom (y1)
            # 只允许向上扩展（近端），禁止向下扩展（远端）
            if block.bbox.y0 < adjusted_clip.y0 < block.bbox.y1:
                adjusted_clip.y0 = block.bbox.y0 - 2  # 向上扩展（近端）✅ 允许
                content_adjusted = True
                if debug:
                    print(f"  -> Expanding top boundary (near-side) to include content block at {block.bbox.y0:.1f}pt")
            # 检查下边界 - P0-1: 禁止向远端扩展
            # if block.bbox.y0 < adjusted_clip.y1 < block.bbox.y1:
            #     # 远端扩展被禁止，跳过
            #     if debug:
            #         print(f"  -> [P0-1] Skipped expanding bottom boundary (far-side) at {block.bbox.y1:.1f}pt")
        else:  # direction == 'above'
            # 图在上方，caption在下方
            # 近端 = bottom (y1)，远端 = top (y0)
            # 只允许向下扩展（近端），禁止向上扩展（远端）
            if block.bbox.y0 < adjusted_clip.y1 < block.bbox.y1:
                adjusted_clip.y1 = block.bbox.y1 + 2  # 向下扩展（近端）✅ 允许
                content_adjusted = True
                if debug:
                    print(f"  -> Expanding bottom boundary (near-side) to include content block at {block.bbox.y1:.1f}pt")
            # 检查上边界 - P0-1: 禁止向远端扩展
            # if block.bbox.y0 < adjusted_clip.y0 < block.bbox.y1:
            #     # 远端扩展被禁止，跳过
            #     if debug:
            #         print(f"  -> [P0-1] Skipped expanding top boundary (far-side) at {block.bbox.y0:.1f}pt")
    
    # 如果进行了内容区块调整，直接返回（不需要检查外部重叠）
    if content_adjusted:
        if debug:
            print(f"  Adjusted clip (content protection): {adjusted_clip}")
            print(f"  Height change: {clip_rect.height:.1f}pt -> {adjusted_clip.height:.1f}pt ({(adjusted_clip.height/clip_rect.height - 1)*100:+.1f}%)")
        return adjusted_clip
    
    # ===== 外部区块处理：只有在没有内容区块调整时才执行 =====
    # 特殊处理：即使重叠度低，如果有标题与clip边界接触，也要调整
    has_title_overlap = False
    for block, inter, ratio in overlapping_blocks:
        if block.block_type.startswith('title_'):
            has_title_overlap = True
            break
    
    # ============================================================
    # P1-2: “边缘敏感”裁剪（Edge-Sensitive Layout Guidance）
    # ============================================================
    # 背景：有些页面只多 1-2 行正文，整体 overlap < 20% 会导致完全不触发，
    #       但这 1-2 行往往刚好贴在 clip 的远端边缘，影响最终 PNG 观感。
    #
    # 策略：在 clip 远端边缘建立一个 strip（≈ 3×行高，至少 30pt），
    #       若 strip 内命中正文/列表/标题文本块（宽度覆盖足够大），
    #       则直接把远端边界向内推，哪怕总 overlap 不到 20%。
    #
    # 注意：这里不能依赖 content/external 的初始分类（可能误把正文当 content），
    #       因此使用 protected_blocks 全量扫描，但通过宽度+位置门槛控风险。
    # ============================================================
    try:
        typical_lh = getattr(layout_model, "typical_line_height", None)
        edge_strip_h = max(30.0, (3.0 * typical_lh) if (typical_lh and typical_lh > 0) else 45.0)
    except Exception:
        edge_strip_h = 45.0

    # 远端 strip（只用于 y 向裁剪）
    if direction == 'above':
        # 图在上方，caption 在下方，远端 = 顶部
        far_strip = fitz.Rect(adjusted_clip.x0, adjusted_clip.y0, adjusted_clip.x1, min(adjusted_clip.y1, adjusted_clip.y0 + edge_strip_h))
        # 远端在上方：需要把顶部向下推
        candidate_blocks = []
        for b in protected_blocks:
            inter = b.bbox & far_strip
            if inter.is_empty:
                continue
            # 宽度覆盖足够大才认为是“正文/标题行干扰”
            w_ratio = inter.width / max(1.0, adjusted_clip.width)
            if w_ratio >= 0.35:
                candidate_blocks.append((b, w_ratio))
        if candidate_blocks:
            # 推到最下方那条文本块之后（+gap）
            new_y0 = max(b.bbox.y1 for (b, _) in candidate_blocks) + 6.0
            if new_y0 > adjusted_clip.y0 + 1e-3:
                if debug:
                    sample = candidate_blocks[0][0].units[0].text.strip()[:50] if candidate_blocks[0][0].units else ""
                    print(f"  [P1-2 EDGE] Trim top by far-strip blocks (n={len(candidate_blocks)}), sample='{sample}...' -> y0 {adjusted_clip.y0:.1f} -> {new_y0:.1f}")
                adjusted_clip.y0 = min(new_y0, adjusted_clip.y1 - 10.0)
    else:
        # direction == 'below'
        # 图在下方，caption 在上方，远端 = 底部
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
                if debug:
                    sample = candidate_blocks[0][0].units[0].text.strip()[:50] if candidate_blocks[0][0].units else ""
                    print(f"  [P1-2 EDGE] Trim bottom by far-strip blocks (n={len(candidate_blocks)}), sample='{sample}...' -> y1 {adjusted_clip.y1:.1f} -> {new_y1:.1f}")
                adjusted_clip.y1 = max(new_y1, adjusted_clip.y0 + 10.0)

    # 如果 P1-2 已经做了边缘收缩，则继续走“合理性检查”，不必强依赖 overlap>=20%
    edge_changed = (abs(adjusted_clip.y0 - clip_rect.y0) > 1e-3) or (abs(adjusted_clip.y1 - clip_rect.y1) > 1e-3)

    # 如果重叠不严重（<20%）且没有标题重叠且没有边缘变化，直接返回
    if overlap_ratio_total < 0.20 and not has_title_overlap and not edge_changed:
        if debug:
            print(f"  -> No adjustment needed (overlap < 20%, no title overlap, no edge change)")
        return clip_rect
    
    if direction == 'above':
        # 图在上方，图注在下方
        # 调整策略：向上收缩clip的下边界，避开下方的外部区块
        blocks_below = [b for b in external_blocks if b.bbox.y0 > caption_rect.y1]
        if blocks_below:
            # 最近的下方区块（不应该被包含）
            nearest_below = min(blocks_below, key=lambda b: b.bbox.y0 - caption_rect.y1)
            # 如果clip包含了这个区块，裁剪到caption上方
            if adjusted_clip.y1 > nearest_below.bbox.y0:
                adjusted_clip.y1 = min(adjusted_clip.y1, nearest_below.bbox.y0 - 5)  # 留5pt间隙
        
        # 同时检查是否包含了caption上方的外部区块
        blocks_above_caption = [b for b in external_blocks if b.bbox.y1 < caption_rect.y0]
        if blocks_above_caption:
            # 找到最近的上方区块
            nearest_above = max(blocks_above_caption, key=lambda b: b.bbox.y1)
            # 如果clip顶部超出了这个区块很多，可能误包含了更上方的文字
            if adjusted_clip.y0 < nearest_above.bbox.y0 - 50:  # 超出50pt
                # 调整顶部，贴合区块底部
                adjusted_clip.y0 = max(adjusted_clip.y0, nearest_above.bbox.y1 + 5)
    
    elif direction == 'below':
        # 图在下方，图注在上方
        # 调整策略：向下收缩clip的上边界，避开上方的外部区块
        blocks_above = [b for b in external_blocks if b.bbox.y1 < caption_rect.y0]
        if blocks_above:
            # 最近的上方区块（不应该被包含）
            nearest_above = max(blocks_above, key=lambda b: caption_rect.y0 - b.bbox.y1)
            # 如果clip包含了这个区块，裁剪到caption下方
            if adjusted_clip.y0 < nearest_above.bbox.y1:
                adjusted_clip.y0 = max(adjusted_clip.y0, nearest_above.bbox.y1 + 5)  # 留5pt间隙
        
        # 同时检查是否包含了caption下方的外部区块
        blocks_below_caption = [b for b in external_blocks if b.bbox.y0 > caption_rect.y1]
        if blocks_below_caption:
            # 找到最近的下方区块
            nearest_below = min(blocks_below_caption, key=lambda b: b.bbox.y0)
            # 如果是标题，只要clip包含了它（哪怕一点点），就要调整
            # 如果是段落，clip要超出很多才调整
            is_title = nearest_below.block_type.startswith('title_')
            threshold = 5 if is_title else 50
            if adjusted_clip.y1 > nearest_below.bbox.y1 + threshold:
                # 调整底部，贴合区块顶部
                adjusted_clip.y1 = min(adjusted_clip.y1, nearest_below.bbox.y0 - 5)
            elif is_title and adjusted_clip.y1 > nearest_below.bbox.y0:
                # 标题被部分包含，收缩到标题上方
                adjusted_clip.y1 = min(adjusted_clip.y1, nearest_below.bbox.y0 - 5)
    
    # 验证调整后的窗口仍然合理（高度至少保留50%）
    if adjusted_clip.height < 0.5 * clip_rect.height or adjusted_clip.height < 80:
        if debug:
            print(f"  -> Adjustment too aggressive, keeping original")
        return clip_rect
    
    if debug:
        print(f"  Adjusted clip: {adjusted_clip}")
        print(f"  Height change: {clip_rect.height:.1f}pt -> {adjusted_clip.height:.1f}pt ({(adjusted_clip.height/clip_rect.height - 1)*100:+.1f}%)")
    
    return adjusted_clip


# P1-01: Auto-detect whether to enable layout-driven extraction
def _should_enable_layout_driven(pdf_path: str, debug: bool = False) -> Tuple[bool, str]:
    """
    快速预扫描 PDF，判断是否需要启用版式驱动提取。
    
    检测标准（满足任一即启用）：
    1. 双栏布局
    2. 图表附近存在密集正文段落
    3. 页面文本区块复杂度高
    
    P2-3 改进：采样策略从"前5页"改为"含 Figure/Table 的页面 + 首/中/尾分布"
    
    Args:
        pdf_path: PDF 文件路径
        debug: 调试模式
    
    Returns:
        (enable, reason): 是否启用，以及原因说明
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return False, f"cannot open PDF: {e}"
    
    try:
        # P2-3 改进：智能采样策略
        # 1. 首先找出含 Figure/Table 的页面
        # 2. 加上首/中/尾各 1 页
        # 3. 去重后采样
        figure_table_pages = set()
        total_pages = len(doc)
        
        # 快速扫描找出含图表的页面
        fig_table_pattern = re.compile(r'(Figure|Fig\.?|Table|Tab\.?)\s*\d', re.IGNORECASE)
        for pno in range(total_pages):
            page = doc[pno]
            text = page.get_text("text")[:2000]  # 只检查前 2000 字符
            if fig_table_pattern.search(text):
                figure_table_pages.add(pno)
        
        # 构建采样页面集合
        sample_pages = set()
        # 加入首/中/尾
        sample_pages.add(0)  # 首页
        sample_pages.add(total_pages // 2)  # 中间页
        sample_pages.add(total_pages - 1)  # 末页
        # 加入最多 3 个含图表的页面
        for pno in sorted(figure_table_pages)[:3]:
            sample_pages.add(pno)
        # 确保不超过 total_pages
        sample_pages = {p for p in sample_pages if 0 <= p < total_pages}
        
        sample_count = len(sample_pages)
        sample_pages_list = sorted(sample_pages)
        
        if debug:
            print(f"[P2-3] Sampling pages: {sample_pages_list} (figure/table pages: {sorted(figure_table_pages)[:5]})")
        
        # 统计双栏特征
        dual_column_pages = 0
        dense_text_pages = 0
        figure_with_dense_text = 0
        
        # P2-3: 使用智能采样的页面列表
        for pno in sample_pages_list:
            page = doc[pno]
            page_rect = page.rect
            page_width = page_rect.width
            
            # 获取文本块
            blocks = page.get_text("dict")["blocks"]
            text_blocks = [b for b in blocks if b.get("type") == 0]  # type=0 是文本块
            
            if not text_blocks:
                continue
            
            # 检测双栏布局：统计文本块的 x 中心分布
            x_centers = []
            for block in text_blocks:
                bbox = block.get("bbox", (0, 0, 0, 0))
                x_center = (bbox[0] + bbox[2]) / 2
                x_centers.append(x_center)
            
            if x_centers:
                # 双栏检测：x 中心是否明显分布在页面左右两侧
                left_count = sum(1 for x in x_centers if x < page_width * 0.4)
                right_count = sum(1 for x in x_centers if x > page_width * 0.6)
                if left_count >= 3 and right_count >= 3:
                    dual_column_pages += 1
            
            # 检测文本密度
            total_text_area = sum(
                (b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1])
                for b in text_blocks
            )
            page_area = page_rect.width * page_rect.height
            text_density = total_text_area / page_area if page_area > 0 else 0
            
            if text_density > 0.4:  # 文本覆盖超过 40%
                dense_text_pages += 1
            
            # 检测图表附近是否有密集文本
            # 简单启发：如果页面有图片且文本密度高，认为是复杂布局
            images = page.get_images(full=True)
            if images and text_density > 0.3:
                figure_with_dense_text += 1
        
        doc.close()
        
        # 判定逻辑
        if dual_column_pages >= sample_count * 0.5:
            return True, f"dual-column layout detected ({dual_column_pages}/{sample_count} pages)"
        
        if figure_with_dense_text >= 2:
            return True, f"dense text near figures ({figure_with_dense_text} pages)"
        
        if dense_text_pages >= sample_count * 0.6:
            return True, f"high text density ({dense_text_pages}/{sample_count} pages)"
        
        return False, "simple layout, layout-driven not needed"
        
    except Exception as e:
        doc.close()
        return False, f"detection error: {e}"


# P1-02: Gathering 阶段 - 结构化文本提取
@dataclass
class GatheredParagraph:
    """结构化段落"""
    text: str                    # 段落文本
    page: int                    # 页码（1-based）
    bbox: Tuple[float, float, float, float]  # 边界框 (x0, y0, x1, y1)
    paragraph_type: str          # 'heading' | 'body' | 'caption' | 'list' | 'equation'
    column: int                  # 栏位（0=左栏/单栏，1=右栏）
    order: int                   # 阅读顺序


@dataclass
class GatheredText:
    """结构化文本结果"""
    paragraphs: List[GatheredParagraph]
    headers_removed: List[str]   # 被剔除的页眉
    footers_removed: List[str]   # 被剔除的页脚
    is_dual_column: bool         # 是否双栏
    page_count: int              # 总页数


def gather_structured_text(
    pdf_path: str,
    out_json: Optional[str] = None,
    debug: bool = False
) -> GatheredText:
    """
    P1-02: 结构化文本提取（Gathering 阶段）
    
    功能：
    1. Header/footer 检测与剔除（基于重复行/位置）
    2. 双栏顺序重排（基于 x 坐标与列检测）
    3. 段落分类（标题/正文/图注/列表）
    
    Args:
        pdf_path: PDF 文件路径
        out_json: 输出 JSON 路径（可选）
        debug: 调试模式
    
    Returns:
        GatheredText 结构化结果
    """
    import json
    from collections import Counter
    
    doc = fitz.open(pdf_path)
    page_count = len(doc)
    
    # Step 1: 收集所有页面的文本块
    all_blocks: List[Dict[str, Any]] = []
    header_candidates: List[str] = []
    footer_candidates: List[str] = []
    
    for pno in range(page_count):
        page = doc[pno]
        page_rect = page.rect
        page_height = page_rect.height
        
        # 获取文本块
        blocks = page.get_text("dict")["blocks"]
        
        for block in blocks:
            if block.get("type") != 0:  # 只处理文本块
                continue
            
            bbox = block.get("bbox", (0, 0, 0, 0))
            lines = block.get("lines", [])
            
            # 提取文本
            block_text = ""
            for line in lines:
                for span in line.get("spans", []):
                    block_text += span.get("text", "")
                block_text += "\n"
            block_text = block_text.strip()
            
            if not block_text:
                continue
            
            # 检测页眉页脚候选（顶部/底部 5% 区域的短文本）
            y_center = (bbox[1] + bbox[3]) / 2
            if y_center < page_height * 0.05:  # 顶部 5%
                if len(block_text) < 100:
                    header_candidates.append(block_text)
            elif y_center > page_height * 0.95:  # 底部 5%
                if len(block_text) < 100:
                    footer_candidates.append(block_text)
            
            all_blocks.append({
                "page": pno + 1,
                "bbox": bbox,
                "text": block_text,
                "lines": lines
            })
    
    # Step 2: 检测重复的页眉页脚
    header_counter = Counter(header_candidates)
    footer_counter = Counter(footer_candidates)
    
    # 出现超过 30% 页数的文本视为页眉/页脚
    threshold = max(2, page_count * 0.3)
    headers_to_remove = {text for text, count in header_counter.items() if count >= threshold}
    footers_to_remove = {text for text, count in footer_counter.items() if count >= threshold}
    
    if debug:
        print(f"[DEBUG] Detected headers to remove: {headers_to_remove}")
        print(f"[DEBUG] Detected footers to remove: {footers_to_remove}")
    
    # Step 3: 检测双栏布局
    # 统计文本块的 x 中心分布
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
    
    # Step 4: 构建段落列表（剔除页眉页脚，按阅读顺序排序）
    paragraphs: List[GatheredParagraph] = []
    
    for block in all_blocks:
        text = block["text"]
        
        # 剔除页眉页脚
        if text in headers_to_remove or text in footers_to_remove:
            continue
        
        bbox = block["bbox"]
        page = block["page"]
        
        # 确定栏位
        if is_dual_column:
            page_width = doc[0].rect.width
            x_center = (bbox[0] + bbox[2]) / 2
            column = 0 if x_center < page_width * 0.5 else 1
        else:
            column = 0
        
        # 简单段落类型检测
        lines = block.get("lines", [])
        first_span = lines[0]["spans"][0] if lines and lines[0].get("spans") else {}
        font_size = first_span.get("size", 10)
        font_flags = first_span.get("flags", 0)
        is_bold = bool(font_flags & 2 ** 4)  # bit 4 = bold
        
        # 启发式分类
        if len(text) < 50 and is_bold:
            para_type = "heading"
        elif text.lower().startswith(("figure", "fig.", "table", "图", "表")):
            para_type = "caption"
        elif text.startswith(("•", "-", "*", "1.", "2.", "(1)", "(a)")):
            para_type = "list"
        else:
            para_type = "body"
        
        paragraphs.append(GatheredParagraph(
            text=text,
            page=page,
            bbox=bbox,
            paragraph_type=para_type,
            column=column,
            order=0  # 稍后计算
        ))
    
    # Step 5: 计算阅读顺序
    # 排序规则：页码 → 栏位 → y 坐标
    paragraphs.sort(key=lambda p: (p.page, p.column, p.bbox[1]))
    for i, para in enumerate(paragraphs):
        para.order = i
    
    doc.close()
    
    result = GatheredText(
        paragraphs=paragraphs,
        headers_removed=list(headers_to_remove),
        footers_removed=list(footers_to_remove),
        is_dual_column=is_dual_column,
        page_count=page_count
    )
    
    # 输出 JSON
    if out_json:
        output = {
            "version": "1.0",
            "is_dual_column": result.is_dual_column,
            "page_count": result.page_count,
            "headers_removed": result.headers_removed,
            "footers_removed": result.footers_removed,
            "paragraphs": [
                {
                    "text": p.text,
                    "page": p.page,
                    "bbox": list(p.bbox),
                    "type": p.paragraph_type,
                    "column": p.column,
                    "order": p.order
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


# ========== P1-09: 图表正文上下文锚点 ==========
@dataclass
class FigureMention:
    """图表在正文中的提及位置"""
    page: int                    # 页码（1-based）
    paragraph_order: int         # 段落阅读顺序
    text_window: str             # 提及位置附近的文本窗口（1-2段）
    bbox: Tuple[float, float, float, float]  # 提及所在段落的边界框
    is_first: bool               # 是否为首次提及


@dataclass
class FigureContext:
    """单个图表的正文上下文信息"""
    ident: str                   # 图表标识符（如 "1", "S1", "I"）
    kind: str                    # "figure" 或 "table"
    caption: str                 # 图注文本
    caption_page: int            # 图注所在页码
    first_mention: Optional[FigureMention]  # 首次提及位置
    all_mentions: List[FigureMention]       # 所有提及位置
    caption_page_text_window: str           # 图注所在页附近的正文窗口


def build_figure_contexts(
    pdf_path: str,
    records: List[AttachmentRecord],
    gathered_text: Optional[GatheredText] = None,
    out_json: Optional[str] = None,
    debug: bool = False
) -> List[FigureContext]:
    """
    P1-09: 为每个 Figure/Table 建立正文上下文锚点。
    
    功能：
    1. 搜索每个图表在正文中的所有提及位置
    2. 提取首次提及位置附近的文本窗口
    3. 提取图注所在页附近的正文窗口
    
    Args:
        pdf_path: PDF 文件路径
        records: 提取的图表记录列表
        gathered_text: 结构化文本（如果已有）
        out_json: 输出 JSON 路径（可选）
        debug: 调试模式
    
    Returns:
        List[FigureContext] 图表上下文列表
    """
    import json
    
    if debug:
        print(f"\n{'='*60}")
        print("P1-09: Building Figure Contexts")
        print(f"{'='*60}")
    
    # 如果没有提供结构化文本，先生成
    if gathered_text is None:
        gathered_text = gather_structured_text(pdf_path, debug=debug)
    
    paragraphs = gathered_text.paragraphs
    
    # 构建图表提及的正则模式
    # 支持：Figure 1, Fig. 1, Figure S1, Table 1, 图1, 图 1, 表1, 表 1
    # 以及罗马数字：Figure I, Figure II
    mention_patterns = {
        'figure': re.compile(
            r"(?:Figure|Fig\.?|图|附图)\s*(S(?:\d+|[IVX]{1,6})|\d+|[IVX]{1,6})",
            re.IGNORECASE
        ),
        'table': re.compile(
            r"(?:Table|Tab\.?|表)\s*(S(?:\d+|[IVX]{1,6})|\d+|[A-Z]\d+|[IVX]{1,6})",
            re.IGNORECASE
        )
    }
    
    # 为每个段落建立索引
    para_by_page: Dict[int, List[GatheredParagraph]] = {}
    for para in paragraphs:
        if para.page not in para_by_page:
            para_by_page[para.page] = []
        para_by_page[para.page].append(para)
    
    contexts: List[FigureContext] = []
    
    for rec in records:
        ident = rec.ident
        kind = rec.kind.lower()
        caption = rec.caption
        caption_page = rec.page
        
        if debug:
            print(f"\n[DEBUG] Processing {kind} {ident} (page {caption_page})")
        
        # 搜索所有提及
        pattern = mention_patterns.get(kind)
        if not pattern:
            continue
        
        all_mentions: List[FigureMention] = []
        first_mention: Optional[FigureMention] = None
        
        for para in paragraphs:
            # 跳过图注本身（caption 类型）
            if para.paragraph_type == 'caption':
                continue
            
            # 搜索提及
            matches = pattern.findall(para.text)
            for match in matches:
                # 标准化标识符进行比较
                match_ident = match.upper().strip()
                rec_ident = ident.upper().strip()
                
                # 检查是否匹配当前图表
                if match_ident == rec_ident:
                    # 提取文本窗口（当前段落 + 上下各一段）
                    window_paras = [para]
                    
                    # 查找同页的上一段和下一段
                    page_paras = [p for p in paragraphs 
                                  if p.page == para.page and p.paragraph_type != 'caption']
                    page_paras.sort(key=lambda p: p.order)
                    
                    para_idx = None
                    for i, p in enumerate(page_paras):
                        if p.order == para.order:
                            para_idx = i
                            break
                    
                    if para_idx is not None:
                        if para_idx > 0:
                            window_paras.insert(0, page_paras[para_idx - 1])
                        if para_idx < len(page_paras) - 1:
                            window_paras.append(page_paras[para_idx + 1])
                    
                    text_window = "\n\n".join(p.text for p in window_paras)
                    
                    mention = FigureMention(
                        page=para.page,
                        paragraph_order=para.order,
                        text_window=text_window[:1000],  # 限制长度
                        bbox=para.bbox,
                        is_first=(first_mention is None)
                    )
                    
                    all_mentions.append(mention)
                    
                    if first_mention is None:
                        first_mention = mention
                        if debug:
                            print(f"  First mention: page {para.page}, order {para.order}")
        
        # 提取图注所在页附近的正文窗口
        caption_page_paras = para_by_page.get(caption_page, [])
        caption_page_window = "\n\n".join(
            p.text for p in caption_page_paras 
            if p.paragraph_type in ('body', 'heading') and len(p.text) > 20
        )[:1500]  # 限制长度
        
        ctx = FigureContext(
            ident=ident,
            kind=kind,
            caption=caption,
            caption_page=caption_page,
            first_mention=first_mention,
            all_mentions=all_mentions,
            caption_page_text_window=caption_page_window
        )
        contexts.append(ctx)
        
        if debug:
            print(f"  Total mentions: {len(all_mentions)}")
            if first_mention:
                print(f"  First mention window: {first_mention.text_window[:100]}...")
    
    # 输出 JSON
    if out_json:
        output = {
            "version": "1.0",
            "pdf": os.path.basename(pdf_path),
            "generated_at": __import__('datetime').datetime.now().isoformat(),
            "contexts": [
                {
                    "ident": ctx.ident,
                    "kind": ctx.kind,
                    "caption": ctx.caption,
                    "caption_page": ctx.caption_page,
                    "first_mention": {
                        "page": ctx.first_mention.page,
                        "paragraph_order": ctx.first_mention.paragraph_order,
                        "text_window": ctx.first_mention.text_window,
                        "bbox": list(ctx.first_mention.bbox),
                    } if ctx.first_mention else None,
                    "all_mentions_count": len(ctx.all_mentions),
                    "all_mentions": [
                        {
                            "page": m.page,
                            "paragraph_order": m.paragraph_order,
                            "is_first": m.is_first,
                        }
                        for m in ctx.all_mentions
                    ],
                    "caption_page_text_window": ctx.caption_page_text_window,
                }
                for ctx in contexts
            ]
        }
        
        out_dir = os.path.dirname(out_json)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        
        if debug:
            print(f"\n[INFO] Wrote figure contexts: {out_json} ({len(contexts)} items)")
    
    return contexts


def extract_text_with_format(
    pdf_path: str,
    out_json: Optional[str] = None,
    sample_pages: Optional[int] = None,
    debug: bool = False
) -> DocumentLayoutModel:
    """
    提取文本并保留完整格式信息，构建版式模型
    
    参数:
        pdf_path: PDF文件路径
        out_json: 输出JSON路径（可选）
        sample_pages: 采样页数（None表示全部）
        debug: 调试模式
    
    返回:
        DocumentLayoutModel: 版式模型对象
    """
    import json
    
    if debug:
        print("\n" + "=" * 70)
        print("LAYOUT-DRIVEN EXTRACTION: Building Document Layout Model")
        print("=" * 70)
    
    doc = fitz.open(pdf_path)
    
    # 1. 统计全局属性
    page_rect = doc[0].rect
    page_size = (page_rect.width, page_rect.height)
    
    # 使用现有的行高统计函数
    typical_metrics = _estimate_document_line_metrics(doc, sample_pages=5, debug=debug)
    typical_font_size = typical_metrics['typical_font_size']
    typical_line_height = typical_metrics['typical_line_height']
    typical_line_gap = typical_metrics['typical_line_gap']
    
    # 1b. 统计典型字体名（用于识别图表内文字）
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
                    # 仅统计正文字号范围内的字体（8-14pt）
                    if 8 <= font_size <= 14:
                        font_name_counts[font_name] = font_name_counts.get(font_name, 0) + 1
    
    # 取出现最频繁的字体名作为典型字体
    if font_name_counts:
        typical_font_name = max(font_name_counts, key=font_name_counts.get)
    else:
        typical_font_name = "Times"  # 默认值
    
    if debug:
        logger.debug(f"Typical font name: {typical_font_name}")
    
    # 2. 提取每页的增强文本单元
    all_units: Dict[int, List[EnhancedTextUnit]] = {}
    num_pages = len(doc) if sample_pages is None else min(sample_pages, len(doc))
    
    for pno in range(num_pages):
        page = doc[pno]
        dict_data = page.get_text("dict")
        
        units = []
        for blk_idx, blk in enumerate(dict_data.get("blocks", [])):
            if blk.get("type") != 0:  # 仅文本块
                continue
            for ln_idx, ln in enumerate(blk.get("lines", [])):
                spans = ln.get("spans", [])
                if not spans:
                    continue
                
                # 合并span级信息
                text = "".join(sp.get("text", "") for sp in spans)
                bbox = fitz.Rect(ln["bbox"])
                
                # 字体信息（取主要span）
                main_span = max(spans, key=lambda s: len(s.get("text", "")))
                font_name = main_span.get("font", "unknown")
                font_size = main_span.get("size", 10.0)
                font_flags = main_span.get("flags", 0)
                color = main_span.get("color", 0)
                
                # 判断加粗（flags的bit 4表示bold）
                font_weight = 'bold' if (font_flags & (1 << 4)) else 'regular'
                
                # RGB颜色
                if isinstance(color, int):
                    color_rgb = ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)
                else:
                    color_rgb = (0, 0, 0)
                
                # 创建增强文本单元
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
    
    # 3. 文本类型分类（Step 3增强：传递typical_font_name）
    all_units = _classify_text_types(all_units, typical_font_size, typical_font_name, page_size[0], debug=debug)
    
    # 4. 双栏检测
    num_columns, column_gap, all_units = _detect_columns(all_units, page_size[0], debug=debug)
    
    # 5. 构建文本区块
    all_blocks = _build_text_blocks(all_units, typical_line_height, debug=debug)
    
    # 6. 识别留白区域
    vacant_regions = _detect_vacant_regions(all_blocks, doc, debug=debug)
    
    # 7. 创建版式模型
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
    
    # 8. 保存为JSON（可选）
    if out_json:
        # 确保目录存在（只在dirname非空时创建，修复P1 review的bug）
        out_dir = os.path.dirname(out_json)
        if out_dir:  # 只在有目录路径时才创建
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


# 命令行参数解析：保持最小 API，同时提供关键裁剪与渲染调优项
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract text and figures/tables from a PDF")
    p.add_argument("--pdf", required=True, help="Path to PDF file")
    p.add_argument("--out-text", default=None, help="Path to output extracted text (.txt). If omitted, writes to <pdf_dir>/text/<pdf_name>.txt")
    p.add_argument("--out-dir", default=None, help="Directory for output image PNGs. If omitted, writes to <pdf_dir>/images/")
    p.add_argument("--manifest", default=None, help="Path to CSV manifest of extracted items (figures/tables)")
    p.add_argument("--index-json", default=None, help="Path to JSON index (default: <pdf_dir>/images/index.json)")
    # P0-06: 默认启用输出隔离，避免旧 PNG 混入新结果
    p.add_argument("--prune-images", action="store_true", default=True, help="After extraction, remove Figure_*/Table_* PNGs in out-dir that are not referenced by the written index.json (default: enabled)")
    p.add_argument("--no-prune-images", action="store_false", dest="prune_images", help="Disable automatic pruning of unindexed images")
    p.add_argument("--dpi", type=int, default=300, help="Render DPI for figure images")
    p.add_argument("--clip-height", type=float, default=650.0, help="Clip window height above caption (pt)")
    p.add_argument("--margin-x", type=float, default=20.0, help="Horizontal page margin (pt)")
    p.add_argument("--caption-gap", type=float, default=5.0, help="Gap between caption and crop bottom (pt)")
    p.add_argument("--max-caption-chars", type=int, default=160, help="Max characters for caption-based filename")
    p.add_argument("--max-caption-words", type=int, default=12, help="Max words after figure/table number in filename (default: 12)")
    p.add_argument("--min-figure", type=int, default=1, help="Minimum figure number to extract")
    p.add_argument("--max-figure", type=int, default=999, help="Maximum figure number to extract")
    # Autocrop related (default OFF). --autocrop enables trimming white margins.
    p.add_argument("--autocrop", action="store_true", help="Enable auto-cropping of white margins")
    p.add_argument("--autocrop-pad", type=int, default=30, help="Padding (pixels) to keep around detected content when autocrop is ON")
    p.add_argument("--autocrop-white-th", type=int, default=250, help="White threshold (0-255) for autocrop ink detection")
    p.add_argument("--below", default="", help="Comma-separated figure numbers to crop BELOW their captions (default ABOVE)")
    p.add_argument("--above", default="", help="Comma-separated figure numbers to crop ABOVE their captions (forces above)")
    p.add_argument("--allow-continued", action="store_true", help="Allow exporting multiple pages for the same figure number (continued)")
    p.add_argument("--preset", default=None, choices=["robust"], help="Parameter preset. 'robust' applies recommended safe settings")
    # Anchor mode & scanning (V2)
    p.add_argument("--anchor-mode", default="v2", choices=["v1", "v2"], help="Caption-anchoring strategy: v2 uses multi-scale scanning around captions (default)")
    p.add_argument("--scan-step", type=float, default=14.0, help="Vertical scan step (pt) for anchor v2")
    p.add_argument("--scan-heights", default="240,320,420,520,640,720,820,920", help="Comma-separated window heights (pt) for anchor v2")
    p.add_argument("--scan-dist-lambda", type=float, default=0.12, help="Penalty weight for distance of candidate window to caption (anchor v2, recommend 0.10-0.15)")
    p.add_argument("--scan-topk", type=int, default=3, help="Keep top-k candidates during anchor v2 (for debugging)")
    p.add_argument("--dump-candidates", action="store_true", help="Dump page-level candidate boxes for debugging (anchor v2)")
    p.add_argument("--caption-mid-guard", type=float, default=6.0, help="Guard (pt) around midline between adjacent captions to avoid cross-anchoring")
    # Smart caption detection (NEW)
    p.add_argument("--smart-caption-detection", action="store_true", default=True, help="Enable smart caption detection to distinguish real captions from in-text references (default: enabled)")
    p.add_argument("--no-smart-caption-detection", action="store_false", dest="smart_caption_detection", help="Disable smart caption detection (use simple pattern matching)")
    p.add_argument("--debug-captions", action="store_true", help="Print detailed caption candidate scoring information for debugging")
    # Visual debug mode (NEW)
    p.add_argument("--debug-visual", action="store_true", help="Enable visual debugging mode: save multi-stage boundary boxes overlaid on full page (output to images/debug/)")
    
    # Layout-driven extraction (V2 Architecture - NEW)
    # P1-01: Layout-driven extraction with three-state control (auto|on|off)
    # 2025-12-29: 默认改为 'on'，因为 layout-driven 对于正确排除章节标题等非常重要
    # nargs='?' + const='on' 保持向后兼容：--layout-driven (无值) 等价于 --layout-driven on
    p.add_argument("--layout-driven", nargs='?', const="on", default="on", choices=["auto", "on", "off"],
                   help="Layout-driven extraction mode (V2): 'on'=always enable (default), 'auto'=enable for complex layouts, 'off'=disable; flag-style '--layout-driven' equals '--layout-driven on'")
    p.add_argument("--layout-json", default=None, help="Path to save/load layout model JSON (default: <out_dir>/layout_model.json)")
    
    # Adaptive line height
    p.add_argument("--adaptive-line-height", action="store_true", default=True, help="Enable adaptive line height: auto-adjust parameters based on document's typical line height (default: enabled)")
    p.add_argument("--no-adaptive-line-height", action="store_false", dest="adaptive_line_height", help="Disable adaptive line height (use fixed default parameters)")
    
    # A) text trimming options
    p.add_argument("--text-trim", action="store_true", default=False, help="Trim paragraph-like text near caption side inside chosen clip")
    p.add_argument("--no-text-trim", action="store_false", dest="text_trim", help="Disable text trimming (overrides --text-trim and preset defaults)")
    p.add_argument("--text-trim-width-ratio", type=float, default=0.5, help="Min horizontal overlap ratio to treat a line as paragraph text")
    p.add_argument("--text-trim-font-min", type=float, default=7.0, help="Min font size for paragraph detection")
    p.add_argument("--text-trim-font-max", type=float, default=16.0, help="Max font size for paragraph detection")
    p.add_argument("--text-trim-gap", type=float, default=6.0, help="Gap between trimmed text and new clip boundary (pt)")
    p.add_argument("--adjacent-th", type=float, default=24.0, help="Adjacency threshold to caption to treat text as body (pt)")
    # A+) far-text trim options (dual-threshold)
    p.add_argument("--far-text-th", type=float, default=300.0, help="Maximum distance to detect far text (pt)")
    p.add_argument("--far-text-para-min-ratio", type=float, default=0.30, help="Minimum paragraph coverage ratio to trigger far-text trim")
    p.add_argument("--far-text-trim-mode", type=str, default="aggressive", choices=["aggressive", "conservative"], help="Far-text trim mode")
    p.add_argument("--far-side-min-dist", type=float, default=50.0, help="P1-1: Minimum distance to detect far-side text (pt)")
    p.add_argument("--far-side-para-min-ratio", type=float, default=0.12, help="P1-1: Minimum paragraph coverage ratio to trigger far-side trim")
    # B) object connectivity options
    p.add_argument("--object-pad", type=float, default=8.0, help="Padding (pt) added around chosen object component")
    p.add_argument("--object-min-area-ratio", type=float, default=0.012, help="Min area ratio of object region within clip to be considered (lower=more sensitive to small panels)")
    p.add_argument("--object-merge-gap", type=float, default=6.0, help="Gap (pt) when merging nearby object rects")
    # D) text-mask assisted autocrop
    p.add_argument("--autocrop-mask-text", action="store_true", help="Mask paragraph-like text when estimating autocrop bbox")
    p.add_argument("--mask-font-max", type=float, default=14.0, help="Max font size to be masked as text")
    p.add_argument("--mask-width-ratio", type=float, default=0.5, help="Min width ratio of text line to be masked")
    p.add_argument("--mask-top-frac", type=float, default=0.6, help="Near-side fraction of clip used for text mask (top for below; bottom for above)")
    p.add_argument("--text-trim-min-para-ratio", type=float, default=0.18, help="Min paragraph ratio in near-side strip to enable text-trim (A)")
    p.add_argument("--protect-far-edge-px", type=int, default=14, help="Extra pixels to keep on the far edge during autocrop to avoid over-trim")
    p.add_argument("--near-edge-pad-px", type=int, default=32, help="Extra pixels to expand towards caption side after autocrop (avoid missing axes/labels)")
    # Global anchor consistency
    p.add_argument("--global-anchor", default="auto", choices=["off", "auto"], help="Choose a single anchor side (above/below) for figures via a prescan")
    p.add_argument("--global-anchor-margin", type=float, default=0.02, help="Margin ratio to decide global side for figures: below > above*(1+margin) or vice versa")
    p.add_argument("--global-anchor-table", default="auto", choices=["off", "auto"], help="Choose a single anchor side (above/below) for tables via a prescan (default: auto)")
    p.add_argument("--global-anchor-table-margin", type=float, default=0.03, help="Margin ratio to decide global side for tables (default: 0.03, more lenient than figures)")
    # Safety & integration
    p.add_argument("--no-refine", default="", help="Comma-separated figure numbers to disable B/D refinements (keep baseline or A)")
    p.add_argument("--refine-near-edge-only", action="store_true", default=True, help="Refinements only adjust near-caption edge (default ON)")
    p.add_argument("--no-refine-near-edge-only", action="store_true", help="Disable near-edge-only behavior (for debugging)")
    p.add_argument("--no-refine-safe", action="store_true", help="Disable safety gates and fallback to baseline")
    p.add_argument("--autocrop-shrink-limit", type=float, default=0.30, help="Max area shrink ratio allowed during autocrop (0.30 = shrink up to 30%%, lower=more conservative)")
    p.add_argument("--autocrop-min-height-px", type=int, default=80, help="Minimal height in pixels after autocrop (at render DPI)")
    # Tables
    p.add_argument("--include-tables", dest="include_tables", action="store_true", help="Also extract tables as images")
    p.add_argument("--no-tables", dest="include_tables", action="store_false", help="Disable table extraction")
    p.set_defaults(include_tables=True)
    p.add_argument("--table-clip-height", type=float, default=520.0, help="Table clip window height (pt)")
    p.add_argument("--table-margin-x", type=float, default=26.0, help="Table horizontal page margin (pt)")
    p.add_argument("--table-caption-gap", type=float, default=6.0, help="Gap between table caption and crop boundary (pt)")
    p.add_argument("--t-below", default="", help="Comma-separated table ids to crop BELOW captions (e.g., '1,3,S1')")
    p.add_argument("--t-above", default="", help="Comma-separated table ids to crop ABOVE captions")
    p.add_argument("--table-object-min-area-ratio", type=float, default=0.005, help="Min area ratio for table object components")
    p.add_argument("--table-object-merge-gap", type=float, default=4.0, help="Merge gap (pt) for table object components")
    p.add_argument("--table-autocrop", action="store_true", default=True, help="Enable auto-cropping for tables")
    p.add_argument("--no-table-autocrop", dest="table_autocrop", action="store_false", help="Disable table autocrop")
    p.add_argument("--table-autocrop-pad", type=int, default=20, help="Padding (px) around detected content for table autocrop")
    p.add_argument("--table-autocrop-white-th", type=int, default=250, help="White threshold for table autocrop")
    p.add_argument("--table-mask-text", action="store_true", default=False, help="Mask text when estimating table autocrop bbox (default OFF)")
    p.add_argument("--no-table-mask-text", dest="table_mask_text", action="store_false", help="Disable table text mask (default)")
    p.add_argument("--table-adjacent-th", type=float, default=28.0, help="Adjacency threshold to caption for table text-trim")
    
    # QA-02: 日志相关参数
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging level (default: INFO)")
    p.add_argument("--log-file", default=None, help="Path to log file (text format, optional)")
    p.add_argument("--log-jsonl", default=None, help="Path to structured log file (JSONL format, optional)")
    
    return p.parse_args(argv)


# 入口：解析参数 → 文本提取（可选）→ 图像提取 → 写出清单（可选）
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    pdf_path = args.pdf
    pdf_basename = os.path.basename(pdf_path)

    # Resolve defaults relative to PDF dir（在配置日志前计算，便于默认 log_jsonl 落盘到 out_dir）
    pdf_dir = os.path.dirname(os.path.abspath(pdf_path))
    pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = args.out_dir or os.path.join(pdf_dir, "images")
    out_text = args.out_text or os.path.join(pdf_dir, "text", pdf_stem + ".txt")

    # 创建输出目录（结构化日志默认写入 out_dir，需要目录先存在）
    os.makedirs(out_dir, exist_ok=True)
    text_dir = os.path.dirname(out_text)
    if text_dir:
        os.makedirs(text_dir, exist_ok=True)

    # QA-04: 默认开启结构化日志文件（可用 --log-jsonl 覆盖）
    if getattr(args, "log_jsonl", None) is None:
        args.log_jsonl = os.path.join(out_dir, "run.log.jsonl")

    # QA-02: 配置日志系统
    run_id = configure_logging(
        level=args.log_level,
        log_file=args.log_file,
        log_jsonl=args.log_jsonl,
    )

    if not os.path.exists(pdf_path):
        logger.error(f"PDF not found: {pdf_path}")
        log_event(
            "fatal_error",
            level="error",
            pdf=pdf_basename,
            stage="main",
            message="PDF not found",
            pdf_path=os.path.abspath(pdf_path).replace("\\", "/"),
        )
        return 2

    # P3 fix: layout_driven 是三态字符串 ("on"|"auto"|"off")，不能用 bool() 转换
    # bool("off") == True，会导致日志中 layout_driven 始终记录为 True
    # 正确做法：直接记录原始字符串值，或转换为实际启用状态
    layout_driven_value = getattr(args, "layout_driven", "off")
    layout_driven_enabled = layout_driven_value != "off"  # 只有 "off" 才是禁用
    
    log_event(
        "run_start",
        level="info",
        pdf=pdf_basename,
        stage="main",
        message="Extraction run started",
        run_id=run_id,
        out_dir=os.path.abspath(out_dir).replace("\\", "/"),
        out_text=os.path.abspath(out_text).replace("\\", "/"),
        preset=getattr(args, "preset", None),
        anchor_mode=getattr(args, "anchor_mode", None),
        layout_driven=layout_driven_value,  # 记录原始三态值：on/auto/off
        layout_driven_enabled=layout_driven_enabled,  # 记录实际启用状态
        debug_captions=bool(getattr(args, "debug_captions", False)),
        debug_visual=bool(getattr(args, "debug_visual", False)),
    )

    # P1-03: PDF 预验证阶段
    validation = pre_validate_pdf(pdf_path)
    logger.info(f"PDF Validation: {validation}")
    if validation.warnings:
        for warn in validation.warnings:
            logger.warning(warn)
    if not validation.is_valid:
        for err in validation.errors:
            logger.error(err)
        log_event(
            "fatal_error",
            level="error",
            pdf=pdf_basename,
            stage="pre_validate_pdf",
            message="PDF validation failed",
            errors=list(validation.errors),
            warnings=list(validation.warnings),
        )
        return 3

    # --- P0-01：辅助 - 显式参数检测（支持 main(argv=...) 程序化调用） ---
    argv_for_cli_check = list(argv) if argv is not None else sys.argv[1:]

    def _cli_has_arg(arg_name: str) -> bool:
        """检查 argv 中是否包含指定的参数名（支持 --kebab-case / --snake_case / --arg=val）"""
        kebab = arg_name.replace('_', '-')
        snake = arg_name.replace('-', '_')
        variants = {f'--{kebab}', f'--{snake}'}
        for a in argv_for_cli_check:
            for v in variants:
                if a == v or a.startswith(v + '='):
                    return True
        return False

    # Extract text by default（若指定 out-text，默认尝试提取文本；使用 PyMuPDF）
    try_extract_text(pdf_path, out_text)
    
    # P1-02: Gathering 阶段 - 生成结构化文本
    gathered_text_path = os.path.join(os.path.dirname(out_text), "gathered_text.json") if out_text else None
    gathered_text = None
    try:
        if gathered_text_path:
            gathered_text = gather_structured_text(
                pdf_path=pdf_path,
                out_json=gathered_text_path,
                debug=args.debug_captions
            )
            logger.info(f"Gathered text: {len(gathered_text.paragraphs)} paragraphs, "
                  f"dual_column={gathered_text.is_dual_column}, "
                  f"headers_removed={len(gathered_text.headers_removed)}, "
                  f"footers_removed={len(gathered_text.footers_removed)}")
    except Exception as e:
        logger.warning(f"Gathering failed: {e}")

    # Apply presets if requested
    if getattr(args, "preset", None) == "robust":
        args.dpi = 300
        args.clip_height = 520.0
        args.margin_x = 26.0
        args.caption_gap = 6.0
        # P0-05: 允许 --no-text-trim 覆盖 robust 预设的默认开启
        if not _cli_has_arg("no-text-trim"):
            args.text_trim = True
        args.autocrop = True
        args.autocrop_pad = 30
        args.autocrop_white_th = 250
        args.autocrop_mask_text = True
        args.mask_font_max = 14.0
        args.mask_width_ratio = 0.5
        args.mask_top_frac = 0.6
        args.refine_near_edge_only = True
        args.no_refine_near_edge_only = False
        args.no_refine_safe = False
        args.autocrop_shrink_limit = 0.30
        args.autocrop_min_height_px = 80
        # Heuristics tuning for over-trim prevention
        args.text_trim_min_para_ratio = 0.18
        args.protect_far_edge_px = 18
        args.near_edge_pad_px = 32
        # 表格预设（特化）
        args.table_clip_height = 520.0
        args.table_margin_x = 26.0
        args.table_caption_gap = 6.0
        args.table_autocrop = True
        args.table_autocrop_pad = 20
        args.table_autocrop_white_th = 250
        args.table_mask_text = False
        args.table_object_min_area_ratio = 0.005
        args.table_object_merge_gap = 4.0

    # --- P0-01 修复：环境变量优先级（CLI > ENV > 默认值）---
    # 辅助函数：设置环境变量，实现 CLI > ENV > 默认值 优先级
    def _set_env_with_priority(env_key: str, cli_arg_name: str, cli_val, default_val):
        """
        优先级：CLI 参数（显式传递）> 环境变量 > 默认值
        
        逻辑：
        - 如果用户显式传递了 CLI 参数，使用 CLI 值
        - 否则，检查环境变量是否已设置
        - 最后使用默认值
        """
        if _cli_has_arg(cli_arg_name):
            # 用户显式传递了 CLI 参数，使用 CLI 值
            os.environ[env_key] = str(cli_val)
        else:
            # 用户未传 CLI 参数，使用 setdefault 保留已存在的环境变量
            os.environ.setdefault(env_key, str(default_val))
    
    # 设置环境变量（保留现有环境变量值，除非 CLI 显式覆盖）
    _set_env_with_priority('EXTRACT_ANCHOR_MODE', 'anchor-mode', args.anchor_mode, 'v2')
    _set_env_with_priority('SCAN_STEP', 'scan-step', args.scan_step, 14.0)
    _set_env_with_priority('SCAN_HEIGHTS', 'scan-heights', args.scan_heights, '240,320,420,520,640,720,820,920')
    _set_env_with_priority('SCAN_DIST_LAMBDA', 'scan-dist-lambda', getattr(args, 'scan_dist_lambda', 0.12), 0.12)
    _set_env_with_priority('CAPTION_MID_GUARD', 'caption-mid-guard', getattr(args, 'caption_mid_guard', 6.0), 6.0)
    _set_env_with_priority('GLOBAL_ANCHOR', 'global-anchor', args.global_anchor, 'auto')
    _set_env_with_priority('GLOBAL_ANCHOR_MARGIN', 'global-anchor-margin', getattr(args, 'global_anchor_margin', 0.02), 0.02)
    _set_env_with_priority('GLOBAL_ANCHOR_TABLE', 'global-anchor-table', getattr(args, 'global_anchor_table', 'auto'), 'auto')
    _set_env_with_priority('GLOBAL_ANCHOR_TABLE_MARGIN', 'global-anchor-table-margin', getattr(args, 'global_anchor_table_margin', 0.03), 0.03)
    
    # 打印最终生效的关键参数值（便于调试与复现）
    logger.info(f"Effective parameters:")
    print(f"       anchor_mode={os.environ['EXTRACT_ANCHOR_MODE']}, "
          f"global_anchor={os.environ['GLOBAL_ANCHOR']}, "
          f"global_anchor_table={os.environ['GLOBAL_ANCHOR_TABLE']}")

    # 控制调试导出
    if getattr(args, 'dump_candidates', False):
        os.environ['DUMP_CANDIDATES'] = '1'

    # Build layout model if --layout-driven is enabled (V2 Architecture)
    # P1-01: Build layout model with three-state control (auto|on|off)
    # 2025-12-29: 默认改为 'on'，因为 layout-driven 对于正确排除章节标题等非常重要
    layout_model: Optional[DocumentLayoutModel] = None
    layout_driven_mode = getattr(args, 'layout_driven', 'on')  # 默认启用
    enable_layout_driven = False
    
    if layout_driven_mode == 'on':
        enable_layout_driven = True
        # 不再打印冗余信息，默认启用是常态
    elif layout_driven_mode == 'off':
        enable_layout_driven = False
        print("[INFO] Layout-driven extraction: DISABLED (--layout-driven off)")
    elif layout_driven_mode == 'auto':
        # P1-01: Auto-detect whether to enable layout-driven extraction
        # Criteria: dual-column layout, or dense text near figure/table areas
        enable_layout_driven, auto_reason = _should_enable_layout_driven(pdf_path, debug=args.debug_captions)
        if enable_layout_driven:
            logger.info(f"Layout-driven extraction: AUTO-ENABLED ({auto_reason})")
        else:
            logger.info(f"Layout-driven extraction: AUTO-SKIPPED ({auto_reason})")
    
    if enable_layout_driven:
        print("\n" + "=" * 70)
        print("LAYOUT-DRIVEN EXTRACTION (V2 Architecture)")
        print("=" * 70)
        
        # Determine layout JSON path
        layout_json_path = args.layout_json or os.path.join(out_dir, "layout_model.json")
        
        # Build layout model
        layout_model = extract_text_with_format(
            pdf_path=pdf_path,
            out_json=layout_json_path,
            sample_pages=None,  # Analyze 全部页面
            debug=args.debug_captions  # 复用 debug_captions 开关
        )
        
        logger.info(f"Layout model built successfully")
        print(f"  - Columns: {layout_model.num_columns} ({'single' if layout_model.num_columns == 1 else 'double'})")
        print(f"  - Text blocks: {sum(len(v) for v in layout_model.text_blocks.values())}")
        print(f"  - Vacant regions: {sum(len(v) for v in layout_model.vacant_regions.values())}")
        print("=" * 70 + "\n")

    # Extract figures
    # --- P0-03 修复：改为返回 List[str] 以支持 "S1" 等附录编号 ---
    def parse_fig_list(s: str) -> List[str]:
        """解析图表编号列表，支持 "1,2,S1,S2" 等格式"""
        out: List[str] = []
        for part in (s or "").split(","):
            part = part.strip()
            if not part:
                continue
            # 保留原始字符串标识符（如 "1", "S1", "A1"）
            out.append(part)
        return out

    fig_records = extract_figures(
        pdf_path=pdf_path,
        out_dir=out_dir,
        dpi=args.dpi,
        clip_height=args.clip_height,
        margin_x=args.margin_x,
        caption_gap=args.caption_gap,
        max_caption_chars=args.max_caption_chars,
        max_caption_words=getattr(args, 'max_caption_words', 12),
        min_figure=args.min_figure,
        max_figure=args.max_figure,
        autocrop=args.autocrop,
        autocrop_pad_px=args.autocrop_pad,
        autocrop_white_threshold=args.autocrop_white_th,
        below_figs=parse_fig_list(args.below),
        above_figs=parse_fig_list(args.above),
        text_trim=args.text_trim,
        text_trim_width_ratio=args.text_trim_width_ratio,
        text_trim_font_min=args.text_trim_font_min,
        text_trim_font_max=args.text_trim_font_max,
        text_trim_gap=args.text_trim_gap,
        adjacent_th=args.adjacent_th,
        far_text_th=getattr(args, 'far_text_th', 300.0),
        far_text_para_min_ratio=getattr(args, 'far_text_para_min_ratio', 0.30),
        far_text_trim_mode=getattr(args, 'far_text_trim_mode', 'aggressive'),
        far_side_min_dist=getattr(args, 'far_side_min_dist', 50.0),  # P1-1
        far_side_para_min_ratio=getattr(args, 'far_side_para_min_ratio', 0.12),  # P1-1
        object_pad=args.object_pad,
        object_min_area_ratio=args.object_min_area_ratio,
        object_merge_gap=args.object_merge_gap,
        autocrop_mask_text=args.autocrop_mask_text,
        mask_font_max=args.mask_font_max,
        mask_width_ratio=args.mask_width_ratio,
        mask_top_frac=args.mask_top_frac,
        refine_near_edge_only=(False if args.no_refine_near_edge_only else args.refine_near_edge_only),
        no_refine_figs=parse_fig_list(args.no_refine),
        refine_safe=(False if args.no_refine_safe else True),
        autocrop_shrink_limit=args.autocrop_shrink_limit,
        autocrop_min_height_px=args.autocrop_min_height_px,
        text_trim_min_para_ratio=getattr(args, 'text_trim_min_para_ratio', 0.18),
        protect_far_edge_px=getattr(args, 'protect_far_edge_px', 14),
        near_edge_pad_px=getattr(args, 'near_edge_pad_px', 18),
        allow_continued=args.allow_continued,
        smart_caption_detection=getattr(args, 'smart_caption_detection', True),
        debug_captions=getattr(args, 'debug_captions', False),
        debug_visual=getattr(args, 'debug_visual', False),
        adaptive_line_height=getattr(args, 'adaptive_line_height', True),
        layout_model=layout_model,  # V2 Architecture
    )

    # 汇总记录
    all_records: List[AttachmentRecord] = list(fig_records)

    # Extract tables if enabled
    def parse_str_list(s: str) -> List[str]:
        return [t.strip() for t in (s or "").split(',') if t.strip()]

    if getattr(args, 'include_tables', True):
        tbl_records = extract_tables(
            pdf_path=pdf_path,
            out_dir=out_dir,
            dpi=args.dpi,
            table_clip_height=args.table_clip_height,
            table_margin_x=args.table_margin_x,
            table_caption_gap=args.table_caption_gap,
            max_caption_chars=args.max_caption_chars,
            max_caption_words=getattr(args, 'max_caption_words', 12),
            autocrop=getattr(args, 'table_autocrop', True),
            autocrop_pad_px=getattr(args, 'table_autocrop_pad', 20),
            autocrop_white_threshold=getattr(args, 'table_autocrop_white_th', 250),
            t_below=parse_str_list(getattr(args, 't_below', '')),
            t_above=parse_str_list(getattr(args, 't_above', '')),
            # --- P0-05 修复：正确传递 text_trim 参数 ---
            text_trim=args.text_trim,
            text_trim_width_ratio=max(0.35, getattr(args, 'text_trim_width_ratio', 0.5)),
            text_trim_font_min=getattr(args, 'text_trim_font_min', 7.0),
            text_trim_font_max=getattr(args, 'text_trim_font_max', 16.0),
            text_trim_gap=getattr(args, 'text_trim_gap', 6.0),
            adjacent_th=getattr(args, 'table_adjacent_th', 28.0),
            far_text_th=getattr(args, 'far_text_th', 300.0),
            far_text_para_min_ratio=getattr(args, 'far_text_para_min_ratio', 0.30),
            far_text_trim_mode=getattr(args, 'far_text_trim_mode', 'aggressive'),
            far_side_min_dist=getattr(args, 'far_side_min_dist', 50.0),  # P1-1
            far_side_para_min_ratio=getattr(args, 'far_side_para_min_ratio', 0.12),  # P1-1
            object_pad=getattr(args, 'object_pad', 8.0),
            object_min_area_ratio=getattr(args, 'table_object_min_area_ratio', 0.005),
            object_merge_gap=getattr(args, 'table_object_merge_gap', 4.0),
            autocrop_mask_text=getattr(args, 'table_mask_text', False),
            mask_font_max=getattr(args, 'mask_font_max', 14.0),
            mask_width_ratio=getattr(args, 'mask_width_ratio', 0.5),
            mask_top_frac=getattr(args, 'mask_top_frac', 0.6),
            refine_near_edge_only=(False if args.no_refine_near_edge_only else args.refine_near_edge_only),
            refine_safe=(False if args.no_refine_safe else True),
            autocrop_shrink_limit=getattr(args, 'autocrop_shrink_limit', 0.35),
            autocrop_min_height_px=getattr(args, 'autocrop_min_height_px', 80),
            allow_continued=args.allow_continued,
            protect_far_edge_px=getattr(args, 'protect_far_edge_px', 12),
            smart_caption_detection=getattr(args, 'smart_caption_detection', True),
            debug_captions=getattr(args, 'debug_captions', False),
            debug_visual=getattr(args, 'debug_visual', False),
            adaptive_line_height=getattr(args, 'adaptive_line_height', True),
            layout_model=layout_model,  # V2 Architecture
        )
        all_records.extend(tbl_records)

    # 统一排序：按页码 → Figure 优先 → 编号/标识
    all_records.sort(key=lambda r: (r.page, 0 if r.kind == 'figure' else 1, r.num_key(), r.ident))

    # P1-04: 独立质量检查阶段（在写入 index.json 之前执行，以便写入 QC 结果）
    qc_issues = quality_check(all_records, pdf_path, out_text)

    # 写出 index.json（默认 images/index.json）
    index_json_path = args.index_json or os.path.join(out_dir, 'index.json')
    try:
        # P1-06: 使用扩展版 index.json，包含元数据和 QC 结果
        write_index_json(
            all_records,
            index_json_path,
            pdf_path=pdf_path,
            preset=getattr(args, 'preset', None),
            run_id=run_id,
            log_jsonl=getattr(args, "log_jsonl", None),
            layout_model=layout_model,
            validation=validation,
            qc_issues=qc_issues  # 包含 QC 结果
        )
    except Exception as e:
        print(f"[WARN] Write index.json failed: {e}")

    # 可选：清理 out_dir 中未被本次 index.json 引用的旧图，避免混入旧结果
    if getattr(args, "prune_images", False):
        removed = prune_unindexed_images(out_dir=out_dir, index_json_path=index_json_path)
        logger.info(f"Pruned unindexed images: {removed}")
    
    # P1-09: 生成图表正文上下文锚点
    try:
        figure_contexts_path = os.path.join(out_dir, 'figure_contexts.json')
        figure_contexts = build_figure_contexts(
            pdf_path=pdf_path,
            records=all_records,
            gathered_text=gathered_text,
            out_json=figure_contexts_path,
            debug=getattr(args, 'debug_captions', False)
        )
        mentions_found = sum(1 for ctx in figure_contexts if ctx.first_mention is not None)
        logger.info(f"Figure contexts: {len(figure_contexts)} items, {mentions_found} with mentions")
    except Exception as e:
        logger.warning(f"Figure contexts failed: {e}")
    
    if qc_issues:
        print("\n" + "=" * 50)
        print("QUALITY CHECK RESULTS")
        print("=" * 50)
        errors = [i for i in qc_issues if i.level == 'error']
        warnings = [i for i in qc_issues if i.level == 'warning']
        infos = [i for i in qc_issues if i.level == 'info']
        
        for issue in errors:
            print(f"[ERROR] {issue.message}")
        for issue in warnings:
            print(f"[WARN] {issue.message}")
        for issue in infos:
            logger.info(f"{issue.message}")
        
        print(f"\nQC Summary: {len(errors)} errors, {len(warnings)} warnings, {len(infos)} info")
        print("=" * 50 + "\n")
    else:
        print("[INFO] Quality check passed: no issues found")

    # Manifest：若用户指定 --manifest，则将记录写入 CSV
    write_manifest(all_records, args.manifest)

    # 质量汇总与弱对齐统计
    try:
        fig_cnt = sum(1 for r in all_records if r.kind == 'figure')
        tbl_cnt = sum(1 for r in all_records if r.kind == 'table')
        print(f"[QC] Extracted: figures={fig_cnt}, tables={tbl_cnt}, total={len(all_records)}")
        txt_path = out_text
        text_counts = {}
        if os.path.exists(txt_path):
            with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
                txt = f.read()
            text_counts['Figure'] = len(re.findall(r"\bFigure\s+[SIVXivx\d]+", txt))
            text_counts['Table'] = len(re.findall(r"\bTable\s+[SIVXivx\d]+", txt))
            text_counts['图'] = len(re.findall(r"图\s*[\d０-９一二三四五六七八九十百千]", txt))
            text_counts['表'] = len(re.findall(r"表\s*[\d０-９一二三四五六七八九十百千]", txt))
            print(f"[QC] Text counts (rough): Figure={text_counts['Figure']} Table={text_counts['Table']} 图={text_counts['图']} 表={text_counts['表']}")
    except Exception as e:
        print(f"[WARN] QC summary failed: {e}")
    
    # P1-11: 结构化输入合同验证与摘要
    print("\n" + "=" * 60)
    print("P1-11: STRUCTURED INPUT CONTRACT FOR SUMMARY GENERATION")
    print("=" * 60)
    
    contract_files = {
        "index.json": index_json_path,
        "gathered_text.json": gathered_text_path if out_text else None,
        "figure_contexts.json": os.path.join(out_dir, 'figure_contexts.json'),
        "plain_text.txt": out_text,
    }
    
    contract_status = []
    all_present = True
    
    for name, path in contract_files.items():
        if path is None:
            status = "⚠️  NOT CONFIGURED"
            all_present = False
        elif os.path.exists(path):
            size = os.path.getsize(path)
            status = f"✅ PRESENT ({size:,} bytes)"
        else:
            status = "❌ MISSING"
            all_present = False
        contract_status.append((name, path or "N/A", status))
        print(f"  {name:25s} {status}")
    
    # 列出 PNG 文件数量
    png_files = [f for f in os.listdir(out_dir) if f.endswith('.png') and (f.startswith('Figure_') or f.startswith('Table_'))]
    print(f"  {'PNG files':25s} ✅ {len(png_files)} files")
    
    print()
    if all_present:
        print("[CONTRACT] ✅ All required files present - ready for summary generation")
        print()
        print("NEXT STEPS:")
        print(f"  1. Review extracted content in: {out_dir}")
        print(f"  2. (Optional) Rename files: python scripts/generate_rename_plan.py {pdf_dir}")
        print(f"  3. Generate summary using: index.json + gathered_text.json + figure_contexts.json + PNG files")
    else:
        print("[CONTRACT] ⚠️  Some files missing - summary generation may be incomplete")
        print()
        print("MISSING FILES:")
        for name, path, status in contract_status:
            if "MISSING" in status or "NOT CONFIGURED" in status:
                print(f"  - {name}: {path}")
    
    print("=" * 60)

    log_event(
        "run_end",
        level="info",
        pdf=pdf_basename,
        stage="main",
        message="Extraction run completed",
        run_id=run_id,
        figures=sum(1 for r in all_records if r.kind == "figure"),
        tables=sum(1 for r in all_records if r.kind == "table"),
        out_dir=os.path.abspath(out_dir).replace("\\", "/"),
        index_json=os.path.abspath(index_json_path).replace("\\", "/"),
        log_jsonl=os.path.abspath(getattr(args, "log_jsonl", "")).replace("\\", "/"),
    )
    
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
