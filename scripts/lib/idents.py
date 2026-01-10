#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 03: 标识符与正则表达式

从 extract_pdf_assets.py 抽离的标识符解析和正则表达式相关代码。

包含：
- Figure/Table 正则表达式（支持罗马数字、S前缀、中文等）
- 标识符解析函数（_extract_figure_ident, _extract_table_ident）
- 罗马数字转换（_roman_to_int, _is_roman_numeral）
- 文件名生成（sanitize_filename_from_caption, build_output_basename）
- QC 引用检测正则

P1-08: 正则表达式覆盖扩展
- 支持 Figure I（罗马数字）
- 支持 Figure S1（S前缀）
- 支持 Figure 1a（子图标签）
- 支持 图1（中文无空格）
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Match, Optional, Set, Tuple


# ============================================================================
# Figure/Table 主匹配正则表达式（用于 caption 检测）
# ============================================================================

# P1-08: 增强的 Figure 正则表达式
# 支持：
# - Figure 1, Fig. 2, Fig 3
# - Figure I, Figure II（罗马数字）
# - Figure S1, Supplementary Figure 1
# - Figure 1a, Figure 2b（子图标签）
# - Extended Data Figure 1
# - 图1, 图 2（中文）

FIGURE_LINE_RE = re.compile(
    r"(?P<label>(?:Extended\s+Data\s+)?(?:Supplementary\s+)?(?:Figure|Fig\.?)\s*)"
    r"(?:"
    r"(?P<s_prefix>S\s*)(?P<s_id>\d+|[IVX]{1,6})"  # S前缀 + 数字/罗马
    r"|"
    r"(?P<roman>[IVX]{1,6})"                        # 纯罗马数字
    r"|"
    r"(?P<num>\d+)"                                 # 普通数字
    r")"
    r"(?P<sublabel>[a-z])?",                        # 子图标签（可选）
    re.IGNORECASE,
)

# 中文图正则
FIGURE_CN_RE = re.compile(
    r"图\s*(?P<num>\d+)",
)

# P1-08: 增强的 Table 正则表达式
# 支持：
# - Table 1, Tab. 2
# - Table I, Table II（罗马数字）
# - Table S1, Supplementary Table 1
# - Table A1, Table B2（附录表）
# - Extended Data Table 1
# - 表1, 表 2（中文）

TABLE_LINE_RE = re.compile(
    r"(?P<label>(?:Extended\s+Data\s+)?(?:Supplementary\s+)?(?:Table|Tab\.?)\s*)"
    r"(?:"
    r"(?P<s_prefix>S\s*)(?P<s_id>\d+|[IVX]{1,6})"  # S前缀 + 数字/罗马
    r"|"
    r"(?P<letter_id>[A-Z]\d+)"                     # 附录表（如 A1, B2）
    r"|"
    r"(?P<roman>[IVX]{1,6})"                       # 纯罗马数字
    r"|"
    r"(?P<num>\d+)"                                # 普通数字
    r")",
    re.IGNORECASE,
)

# 中文表正则
TABLE_CN_RE = re.compile(
    r"表\s*(?P<num>\d+)",
)


# ============================================================================
# QC 引用检测正则（QA-06）
# ============================================================================

# QC 引用检测：英文 Figure
# 注意：使用 \b 边界避免 "figures in" 被误匹配为 "figures i"
QC_FIGURE_REF_EN_RE = re.compile(
    r"(?:Extended\s+Data\s+)?(?:Supplementary\s+)?(?:Figures?|Figs?\.?)\s*"
    r"(?:S\s*)?(\d+|[IVX]{1,6})\b",
    re.IGNORECASE,
)

# QC 引用检测：中文图
QC_FIGURE_REF_CN_RE = re.compile(
    r"图\s*(\d+)",
)

# QC 引用检测：英文 Table
QC_TABLE_REF_EN_RE = re.compile(
    r"(?:Extended\s+Data\s+)?(?:Supplementary\s+)?(?:Tables?|Tab\.?)\s*"
    r"(?:S\s*)?([A-Z]?\d+|[IVX]{1,6})\b",
    re.IGNORECASE,
)

# QC 引用检测：中文表
QC_TABLE_REF_CN_RE = re.compile(
    r"表\s*(\d+)",
)

# S 前缀辅助正则
QC_S_PREFIX_RE = re.compile(r"\bS\s*(\d+|[IVX]{1,6})", re.IGNORECASE)


# ============================================================================
# 罗马数字转换
# ============================================================================

def roman_to_int(roman: str) -> int:
    """
    罗马数字转阿拉伯数字。
    
    Args:
        roman: 罗马数字字符串（如 "I", "IV", "X"）
    
    Returns:
        对应的阿拉伯数字
    
    Examples:
        >>> roman_to_int("I")
        1
        >>> roman_to_int("IV")
        4
        >>> roman_to_int("X")
        10
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


def is_roman_numeral(s: str) -> bool:
    """
    检查字符串是否为有效的罗马数字。
    
    Args:
        s: 待检查字符串
    
    Returns:
        True 如果是有效罗马数字
    
    Examples:
        >>> is_roman_numeral("IV")
        True
        >>> is_roman_numeral("ABC")
        False
    """
    if not s:
        return False
    return bool(re.match(r'^[IVXLCDMivxlcdm]+$', s))


def is_roman_in_range(s: str, min_val: int = 1, max_val: int = 20) -> bool:
    """
    检查罗马数字是否在指定范围内（I~XX）。
    
    Args:
        s: 罗马数字字符串
        min_val: 最小值
        max_val: 最大值
    
    Returns:
        True 如果在范围内
    """
    if not is_roman_numeral(s):
        return False
    val = roman_to_int(s)
    return min_val <= val <= max_val


# ============================================================================
# 标识符提取函数
# ============================================================================

def extract_figure_ident(match: Match) -> str:
    """
    从 figure_line_re 的匹配结果中提取完整的图表标识符。
    
    支持两种捕获结构：
    1) 命名分组结构（推荐）：label/s_prefix/s_id/roman/num
    2) 旧的分组结构（group 1..4）作为回退
    
    Args:
        match: 正则匹配对象
    
    Returns:
        完整标识符，如 "S1", "1", "S2", "I", "II", "SIV" 等
    
    Examples:
        >>> m = FIGURE_LINE_RE.search("Figure S1: Overview")
        >>> extract_figure_ident(m)
        'S1'
        >>> m = FIGURE_LINE_RE.search("Figure III shows")
        >>> extract_figure_ident(m)
        'III'
    """
    # --- 命名分组（优先）---
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
        
        # "Supplementary Figure IV" 强制补齐 S 前缀
        if is_supp_kw and ident and (not ident.upper().startswith("S")):
            ident = f"S{ident}".upper()
        return ident.strip()
    
    # --- 旧结构：按 group(1..4) 回退 ---
    try:
        s_prefix = match.group(1) or ""
        s_number = match.group(2) or ""
        if s_prefix and s_number:
            return (s_prefix + s_number).strip()
    except IndexError:
        pass
    
    try:
        roman = match.group(3) or ""
        if roman:
            return roman.strip().upper()
    except IndexError:
        pass
    
    try:
        number = match.group(4) or ""
        return number.strip()
    except IndexError:
        return ""


def extract_table_ident(match: Match) -> str:
    """
    从 table_line_re 的匹配结果中提取完整的表格标识符。
    
    兼容：
    - 命名分组结构（label/s_prefix/s_id/letter_id/roman/num）
    - 旧的分组结构（group 1..3）
    
    Args:
        match: 正则匹配对象
    
    Returns:
        表格标识符，如 "1", "S1", "A1", "IV", "SIV" 等
    """
    # --- 命名分组（优先）---
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


def parse_figure_ident(ident: str) -> Tuple[bool, int]:
    """
    解析图表标识符，返回 (is_supplementary, numeric_part)。
    
    支持罗马数字（P1-08 扩展）。
    
    Args:
        ident: 标识符字符串
    
    Returns:
        (is_supplementary, numeric_value) 元组
    
    Examples:
        >>> parse_figure_ident("S1")
        (True, 1)
        >>> parse_figure_ident("1")
        (False, 1)
        >>> parse_figure_ident("III")
        (False, 3)
    """
    if ident.upper().startswith('S') and len(ident) > 1:
        rest = ident[1:].strip()
        if rest.isdigit():
            return True, int(rest)
        if is_roman_numeral(rest):
            return True, roman_to_int(rest)
        return True, 0
    elif is_roman_numeral(ident):
        return False, roman_to_int(ident)
    else:
        try:
            return False, int(ident)
        except ValueError:
            return False, 0


def ident_in_range(ident: str, min_val: int, max_val: int) -> bool:
    """
    检查标识符的数字部分是否在指定范围内。
    对于 S1 等附录编号，总是返回 True（不过滤附录图）。
    
    Args:
        ident: 标识符
        min_val: 最小值
        max_val: 最大值
    
    Returns:
        True 如果在范围内或是附录编号
    """
    is_supp, num = parse_figure_ident(ident)
    if is_supp:
        return True  # 附录图不受过滤
    return min_val <= num <= max_val


# ============================================================================
# QC 引用统计
# ============================================================================

def is_qc_roman_numeral(s: str) -> bool:
    """判断字符串是否为罗马数字（I~XX范围）"""
    if not s:
        return False
    return bool(re.fullmatch(r"[IVX]{1,6}", s.upper()))


def count_text_references(text: str) -> Dict[str, Set[str]]:
    """
    QA-06: 统计正文中的图表引用。
    
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
    for m in QC_FIGURE_REF_EN_RE.finditer(text):
        ident = m.group(1)
        full_match = m.group(0)
        
        # 检查是否有 S 前缀
        s_match = QC_S_PREFIX_RE.search(full_match)
        if s_match:
            ident = f"S{s_match.group(1).upper()}"
        elif is_qc_roman_numeral(ident):
            ident = ident.upper()
        
        figures.add(ident)
    
    # 查找所有中文图引用
    for m in QC_FIGURE_REF_CN_RE.finditer(text):
        figures.add(m.group(1))
    
    # 查找所有英文 Table 引用
    for m in QC_TABLE_REF_EN_RE.finditer(text):
        ident = m.group(1)
        full_match = m.group(0)
        
        # 检查是否有 S 前缀
        s_match = QC_S_PREFIX_RE.search(full_match)
        if s_match:
            ident = f"S{s_match.group(1).upper()}"
        elif is_qc_roman_numeral(ident):
            ident = ident.upper()
        elif ident and ident[0].isalpha() and len(ident) > 1:
            # 附录表编号如 A1, B2
            ident = ident.upper()
        
        tables.add(ident)
    
    # 查找所有中文表引用
    for m in QC_TABLE_REF_CN_RE.finditer(text):
        tables.add(m.group(1))
    
    return {"figures": figures, "tables": tables}


def categorize_idents(idents: Set[str]) -> Dict[str, Set[str]]:
    """
    QA-06: 对标识符进行分类。
    
    Args:
        idents: 标识符集合
    
    Returns:
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
        elif is_qc_roman_numeral(ident):
            result["roman"].add(ident)
        elif ident and ident[0].isalpha():
            result["appendix"].add(ident)
        else:
            result["numeric"].add(ident)
    
    return result


# ============================================================================
# 文件名生成
# ============================================================================

def sanitize_filename_from_caption(
    caption: str,
    figure_no: int,
    max_chars: int = 160,
    max_words: int = 12
) -> str:
    """
    从图注文本生成安全的文件名。
    
    规则：
    - 规范化分隔符与 Unicode
    - 限制可用字符集合
    - 压缩多余下划线并限制最大长度
    
    Args:
        caption: 图注文本
        figure_no: 图号（未使用，保留兼容）
        max_chars: 最大字符数
        max_words: 最大单词数
    
    Returns:
        安全的文件名（不含扩展名）
    """
    # 规范化 Unicode
    caption = unicodedata.normalize("NFC", caption)
    
    # 替换常见分隔符为下划线
    for sep in (" ", "-", "–", "—", "/", "\\", ":", ";", ",", ".", "(", ")", "[", "]"):
        caption = caption.replace(sep, "_")
    
    # 只保留安全字符
    safe_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    result = "".join(c if c in safe_chars else "_" for c in caption)
    
    # 压缩多个下划线
    while "__" in result:
        result = result.replace("__", "_")
    
    # 去除首尾下划线
    result = result.strip("_")
    
    # 限制单词数
    words = result.split("_")
    if len(words) > max_words:
        words = words[:max_words]
        result = "_".join(words)
    
    # 限制长度
    if len(result) > max_chars:
        result = result[:max_chars].rstrip("_")
    
    return result


def limit_words_after_prefix(
    filename: str,
    prefix_pattern: str,
    max_words: int = 12
) -> str:
    """
    限制文件名中前缀之后的单词数量。
    
    Args:
        filename: 文件名（如 "Figure_1_This_is_a_long_caption"）
        prefix_pattern: 前缀模式（如 r"^(Figure_\\d+|Table_\\d+)_"）
        max_words: 最大单词数
    
    Returns:
        限制后的文件名
    """
    match = re.match(prefix_pattern, filename)
    if not match:
        return filename
    
    prefix = match.group(0)
    rest = filename[len(prefix):]
    
    if not rest:
        return filename.rstrip("_")
    
    desc_parts = rest.split("_")
    if len(desc_parts) <= max_words:
        return filename
    
    prefix_parts = prefix.rstrip("_").split("_")
    desc_parts = desc_parts[:max_words]
    
    return "_".join(prefix_parts + desc_parts)


def build_output_basename(
    kind: str,
    ident: str,
    caption: str,
    max_chars: int = 160,
    max_words: int = 12
) -> str:
    """
    构建输出文件的基础名（不含扩展名）。
    
    格式：{kind}_{ident}_{sanitized_caption}
    
    Args:
        kind: 类型 ('figure' | 'table')
        ident: 标识符
        caption: 图注文本
        max_chars: 最大字符数
        max_words: 描述部分最大单词数
    
    Returns:
        输出文件基础名
    
    Examples:
        >>> build_output_basename("figure", "1", "Overview of the system")
        'Figure_1_Overview_of_the_system'
    """
    # 类型前缀
    type_prefix = "Figure" if kind == "figure" else "Table"
    prefix = f"{type_prefix}_{ident}_"
    
    # 清理图注
    sanitized = sanitize_filename_from_caption(caption, 0, max_chars - len(prefix), max_words)
    
    # 去除图注开头可能重复的 Figure/Table 标识
    patterns = [
        rf"^{type_prefix}_{re.escape(ident)}_",
        rf"^{type_prefix.lower()}_{re.escape(ident)}_",
        rf"^{type_prefix}\s*{re.escape(ident)}_",
    ]
    for pattern in patterns:
        sanitized = re.sub(pattern, "", sanitized, flags=re.IGNORECASE)
    
    result = f"{prefix}{sanitized}".rstrip("_")
    
    # 最终长度检查
    if len(result) > max_chars:
        result = result[:max_chars].rstrip("_")
    
    return result
