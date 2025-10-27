#!/usr/bin/env python3
# ----------------------------------------
# 脚本中文说明
#
# 目标：
# - 从 PDF 中提取正文文本与图像（基于图注定位），并将图像导出为 PNG；
# - 可选写出 TXT 文本与 CSV 清单。
#
# 方法概述：
# - 文本提取：使用 pdfminer.six（若未安装则跳过，不会中断脚本）。
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
- Text extraction via pdfminer.six (optional: skip if unavailable)
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
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Iterable, Any

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


# 文本提取：若提供 out_text 路径且安装了 pdfminer.six，则将 PDF 全文提取为 UTF-8 文本文件
# 返回写入路径或 None（未提取/失败）。
def try_extract_text(pdf_path: str, out_text: Optional[str]) -> Optional[str]:
    if out_text is None:
        # 未指定输出路径：直接跳过文本提取
        return None
    try:
        from pdfminer.high_level import extract_text  # type: ignore
    except Exception:
        print("[WARN] pdfminer.six not installed; skip text extraction.")
        return None
    try:
        # 调用 pdfminer 的高层 API 提取整文文本
        txt = extract_text(pdf_path)
        with open(out_text, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"[INFO] Wrote text: {out_text} (chars={len(txt)})")
        return out_text
    except Exception as e:
        print(f"[WARN] Text extraction failed: {e}")
        return None


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


@dataclass
class AttachmentRecord:
    # 统一记录：图（figure）或表（table）
    kind: str              # 'figure' | 'table'
    ident: str             # 标识：图/表号（保留原样，如 '1'/'S1'/'III'）
    page: int              # 1-based
    caption: str
    out_path: str
    continued: bool = False

    def num_key(self) -> float:
        """用于排序的数值键：尽量将可解析的数字排在前面。"""
        try:
            return float(int(self.ident))
        except Exception:
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
    
    def to_dict(self) -> Dict:
        """转换为可序列化的字典"""
        return {
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
    except Exception:
        pass
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
    far_side_min_dist: float = 100.0,
    far_side_para_min_ratio: float = 0.20,
    # Adaptive line height
    typical_line_h: Optional[float] = None,
) -> fitz.Rect:
    """
    Enhanced dual-threshold text trimming.
    
    Phase A: Trim adjacent text (<adjacent_th, default 24pt) using original logic
    Phase B: Detect and remove far-distance text blocks (adjacent_th ~ far_text_th)
    
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
            try:
                print(f"[DBG] Far-side trim: direction={'above' if near_is_top else 'below'} far_is_top={far_is_top} coverage={far_side_para_coverage:.3f} th={far_side_para_min_ratio}")
            except Exception:
                pass
            if far_is_top:
                # Move clip.y0 down to after last far-side paragraph
                last_para_y1 = max(lb.y1 for (lb, _, _) in far_side_para_lines)
                new_y0 = last_para_y1 + gap
                # Safety: don't trim more than 50% of original clip height
                max_trim = original_clip.y0 + 0.5 * original_clip.height
                clip.y0 = min(new_y0, max_trim)
            else:
                # Move clip.y1 up to before first far-side paragraph
                first_para_y0 = min(lb.y0 for (lb, _, _) in far_side_para_lines)
                new_y1 = first_para_y0 - gap
                # Safety: don't trim more than 50% of original clip height
                min_trim = original_clip.y1 - 0.5 * original_clip.height
                clip.y1 = max(new_y1, min_trim)
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
                try:
                    print(f"[DBG] Far-side fallback trim: lines={len(fallback_lines)}")
                except Exception:
                    pass
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
) -> List[Tuple[int, int, int, int]]:
    """Convert selected text line rects to PIXEL-space masks relative to clip.
    Limit to NEAR-CAPTION portion: for 'above' use bottom fraction; for 'below' use top fraction.
    """
    masks: List[Tuple[int, int, int, int]] = []
    y_thresh_top = clip.y0 + near_frac * clip.height
    y_thresh_bot = clip.y1 - near_frac * clip.height
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
        if direction == 'above':
            # near side is bottom → keep only bottom portion
            if inter.y0 < y_thresh_bot:
                continue
        else:
            # near side is top → keep only top portion
            if inter.y1 > y_thresh_top:
                continue
        # convert to pixel coords
        l = int(max(0, (inter.x0 - clip.x0) * scale))
        t = int(max(0, (inter.y0 - clip.y0) * scale))
        r = int(min((clip.x1 - clip.x0) * scale, (inter.x1 - clip.x0) * scale))
        b = int(min((clip.y1 - clip.y0) * scale, (inter.y1 - clip.y0) * scale))
        if r - l > 1 and b - t > 1:
            masks.append((l, t, r, b))
    return masks


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
    except Exception:
        pass
    return images


def get_page_drawings(page: "fitz.Page") -> List[fitz.Rect]:
    """提取页面中所有绘图对象的边界框"""
    drawings: List[fitz.Rect] = []
    try:
        for dr in page.get_drawings():
            r = dr.get("rect")
            if r and isinstance(r, fitz.Rect):
                drawings.append(r)
    except Exception:
        pass
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
                    # 提取编号（假设第一个捕获组是编号）
                    number = match.group(1)
                    
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
        print(f"Warning: Failed to parse page {page_num + 1} for {kind} captions: {e}")
    
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
    
    # 获取页面中的图像和绘图对象
    images = get_page_images(page)
    drawings = get_page_drawings(page)
    
    # 为每个候选项评分
    scored_candidates: List[Tuple[float, CaptionCandidate]] = []
    for cand in candidates:
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
    # 默认 pattern
    if figure_pattern is None:
        figure_pattern = re.compile(
            r"^\s*(?:(?:Extended\s+Data\s+Figure|Supplementary\s+Figure|Figure|Fig\.?|图表|附图|图)\s*(?:S\s*)?(\d+))",
            re.IGNORECASE
        )
    
    if table_pattern is None:
        table_pattern = re.compile(
            r"^\s*(?:(?:Extended\s+Data\s+Table|Supplementary\s+Table|Table|表)\s*(?:S\s*)?(\d+|[IVX]+))",
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
    below_figs: Optional[List[int]] = None,
    above_figs: Optional[List[int]] = None,
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
    far_side_min_dist: float = 100.0,
    far_side_para_min_ratio: float = 0.20,
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
    no_refine_figs: Optional[List[int]] = None,
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
    # 打开 PDF 文档并准备输出目录
    doc = fitz.open(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    # 匹配 "Figure N" 或 "图 N" 的图注起始行（忽略大小写），支持续页标记
    figure_line_re = re.compile(
        r"^\s*(?:(?:Extended\s+Data\s+Figure|Supplementary\s+Figure|Figure|Fig\.?|图表|附图|图)\s*(?:S\s*)?(\d+))"
        r"(?:\s*\(continued\)|\s*续|\s*接上页)?",  # 可选的续页标记
        re.IGNORECASE,
    )
    seen: Dict[int, str] = {}
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
            far_text_th = 10.0 * typical_line_h
        if text_trim_gap == 6.0:  # 默认值
            text_trim_gap = 0.5 * typical_line_h
        if far_side_min_dist == 100.0:  # 默认值
            far_side_min_dist = 8.0 * typical_line_h
        
        if debug_captions:
            print(f"ADAPTIVE PARAMETERS (based on line_height={typical_line_h:.1f}pt):")
            print(f"  adjacent_th:      {adjacent_th:.1f} pt (2.0× line_height)")
            print(f"  far_text_th:      {far_text_th:.1f} pt (10.0× line_height)")
            print(f"  text_trim_gap:    {text_trim_gap:.1f} pt (0.5× line_height)")
            print(f"  far_side_min_dist:{far_side_min_dist:.1f} pt (8.0× line_height)")
            print()

    def _parse_fig_list(s: str) -> List[int]:
        out: List[int] = []
        for part in (s or "").split(','):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                pass
        return out

    anchor_mode = os.getenv('EXTRACT_ANCHOR_MODE', '').lower()
    # Global side prescan (figures)
    global_side: Optional[str] = None
    if os.getenv('GLOBAL_ANCHOR', 'auto').lower() == 'auto':
        try:
            ga_margin = float(os.getenv('GLOBAL_ANCHOR_MARGIN', '0.02'))
        except Exception:
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
            except Exception:
                pass
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
                except Exception:
                    ink_a = 0.0
                try:
                    pix_b = page_s.get_pixmap(matrix=fitz.Matrix(1,1), clip=clip_below, alpha=False)
                    ink_b = estimate_ink_ratio(pix_b)
                except Exception:
                    ink_b = 0.0
                above_total += 0.6 * ink_a + 0.4 * obj_ratio(clip_above)
                below_total += 0.6 * ink_b + 0.4 * obj_ratio(clip_below)
        if below_total > above_total * (1.0 + ga_margin):
            global_side = 'below'
        elif above_total > below_total * (1.0 + ga_margin):
            global_side = 'above'
        else:
            global_side = None
    # === 存储智能选择的结果（用于跨页查找）===
    smart_caption_cache: Dict[int, Tuple[fitz.Rect, str, int]] = {}  # {fig_no: (rect, caption, page_num)}
    
    for pno in range(len(doc)):
        # 遍历每一页，读取文本与对象布局
        page = doc[pno]
        page_rect = page.rect
        dict_data = page.get_text("dict")

        # 收集本页所有图注（line-level 聚合）：
        # 将连续的行在遇到下一处图注前合并为同一条 caption。
        captions_on_page: List[Tuple[int, fitz.Rect, str]] = []
        
        # === 智能 Caption 选择（如果启用）===
        if smart_caption_detection and caption_index:
            # 使用智能选择逻辑
            # 1. 找到本页所有潜在的 figure 编号
            page_fig_numbers = set()
            for blk in dict_data.get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                for ln in blk.get("lines", []):
                    text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
                    m = figure_line_re.match(text.strip())
                    if m:
                        try:
                            fig_no = int(m.group(1))
                            if min_figure <= fig_no <= max_figure:
                                page_fig_numbers.add(fig_no)
                        except Exception:
                            pass
            
            # 2. 对每个 figure 编号，从索引中获取候选项并选择最佳的
            for fig_no in sorted(page_fig_numbers):
                # 先检查是否已经在其他页找到过（智能选择可能跨页）
                if fig_no in smart_caption_cache:
                    cached_rect, cached_caption, cached_page = smart_caption_cache[fig_no]
                    # 如果缓存的是本页，则使用
                    if cached_page == pno:
                        captions_on_page.append((fig_no, cached_rect, cached_caption))
                    continue
                
                # 从索引中获取所有候选项
                candidates = caption_index.get_candidates('figure', str(fig_no))
                if not candidates:
                    continue
                
                # 选择最佳候选（优先选择本页的，但如果本页得分太低，可能选择其他页）
                best_candidate = select_best_caption(candidates, page, min_score_threshold=25.0, debug=debug_captions)
                
                if best_candidate:
                    # 收集完整 caption 文本（合并后续行）
                    full_caption = best_candidate.text
                    cap_rect = best_candidate.rect
                    
                    # 尝试合并后续行
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
                    
                    # 如果最佳候选是本页，则添加到本页列表
                    if best_candidate.page == pno:
                        captions_on_page.append((fig_no, cap_rect, full_caption))
                    
                    # 缓存结果
                    smart_caption_cache[fig_no] = (cap_rect, full_caption, best_candidate.page)
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
                    try:
                        fig_no = int(m.group(1))
                    except Exception:
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
                    if min_figure <= fig_no <= max_figure:
                        captions_on_page.append((fig_no, cap_rect, caption))
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
        except Exception:
            pass
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
            except Exception:
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
            except Exception:
                return 0.0
        # collect text lines once for this page (used by A / D)
        text_lines_all = _collect_text_lines(dict_data)

        for idx, (fig_no, cap_rect, caption) in enumerate(captions_on_page):
            if fig_no in seen:
                continue

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
                if crop_below or crop_above:
                    side, chosen_clip = 'below', clip_below
                else:
                    try:
                        ra = figure_score(clip_above)
                        rb = figure_score(clip_below)
                        if rb > ra * 1.02:
                            side, chosen_clip = 'below', clip_below
                        else:
                            side, chosen_clip = 'above', clip_above
                    except Exception:
                        side, chosen_clip = 'above', clip_above
                clip = chosen_clip
            else:
                # Anchor V2：多尺度滑窗
                scan_heights = os.getenv('SCAN_HEIGHTS', '')
                if scan_heights:
                    heights = [float(h) for h in scan_heights.split(',') if h.strip()]
                else:
                    heights = [240.0, 320.0, 420.0, 520.0, 640.0]
                step = 14.0
                try:
                    step = float(os.getenv('SCAN_STEP', '14'))
                except Exception:
                    pass

                dist_lambda = 0.0
                try:
                    dist_lambda = float(os.getenv('SCAN_DIST_LAMBDA', '0.15'))
                except Exception:
                    dist_lambda = 0.15

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

                def fig_score(clip: fitz.Rect) -> float:
                    # 小分辨率渲染估计墨迹
                    small_scale = 1.0
                    try:
                        pix = page.get_pixmap(matrix=fitz.Matrix(small_scale, small_scale), clip=clip, alpha=False)
                        ink = estimate_ink_ratio(pix)
                    except Exception:
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
                except Exception:
                    cap_mid_guard = 6.0
                y0_min_guard = top_bound
                if prev_cap is not None:
                    mid_prev = 0.5 * (prev_cap.y1 + cap_rect.y0)
                    y0_min_guard = max(y0_min_guard, mid_prev + cap_mid_guard)
                if global_side in (None, 'above'):
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
                            candidates.append((sc, 'above', c))
                            y0 -= step
                            if y0 < y0_min:
                                break
                # below scanning
                top2 = cap_rect.y1 + caption_gap
                bot2 = (next_cap.y0 - 8) if next_cap else page_rect.y1
                # 防跨：下方窗口不得越过当前/下一 caption 的中线
                y1_max_guard = min(bot2, page_rect.y1)
                if next_cap is not None:
                    mid_next = 0.5 * (cap_rect.y1 + next_cap.y0)
                    y1_max_guard = min(y1_max_guard, mid_next - cap_mid_guard)
                if global_side in (None, 'below'):
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
                            candidates.append((sc, 'below', c))
                            y0 += step
                            y1 = min(y1_max, y0 + h)
                            if y0 >= y1_max:
                                break
                if not candidates:
                    clip = fitz.Rect(x_left, max(page_rect.y0, cap_rect.y0 - 200), x_right, min(page_rect.y1, cap_rect.y1 + 200))
                    side = 'above'
                else:
                    candidates.sort(key=lambda t: t[0], reverse=True)
                    best = candidates[0]
                    if os.getenv('DUMP_CANDIDATES', '0') == '1':
                        dbg_dir = os.path.join(os.path.dirname(os.path.abspath(out_dir)), 'images')
                        os.makedirs(dbg_dir, exist_ok=True)
                        dump_page_candidates(
                            page,
                            os.path.join(dbg_dir, f"debug_candidates_fig_p{pno+1}.png"),
                            candidates=candidates,
                            best=best,
                            caption_rect=cap_rect,
                        )
                    side = best[1]
                    clip = snap_clip_edges(best[2], draw_items)
                    try:
                        print(f"[DBG] Select side={side} for Figure {fig_no} on page {pno+1}")
                    except Exception:
                        pass

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
                    print(f"[INFO] Figure {fig_no}: Layout-guided adjustment applied")
            
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
                print(f"[WARN] Render failed on page {pno+1}, Figure {fig_no}: {e}")
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
                    # 远端边缘保护：在远离 caption 的一侧向外扩 保护像素，避免轻微顶部/底部被裁
                    far_pad_pt = max(0.0, protect_far_edge_px / scale)
                    if far_pad_pt > 0:
                        if side == 'above':
                            # far edge = TOP
                            tight = fitz.Rect(tight.x0, max(page_rect.y0, tight.y0 - far_pad_pt), tight.x1, tight.y1)
                        else:
                            # far edge = BOTTOM
                            tight = fitz.Rect(tight.x0, tight.y0, tight.x1, min(page_rect.y1, tight.y1 + far_pad_pt))
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
                            print(f"[INFO] Figure {fig_no}: Post-autocrop layout adjustment applied")
                    
                    pix = page.get_pixmap(matrix=mat, clip=tight, alpha=False)
                    clip = tight
                except Exception as e:
                    print(f"[WARN] Autocrop failed (Figure {fig_no} page {pno+1}): {e}")

            # Safety gate & fallback: compare to baseline
            if refine_safe and not (no_refine_figs and (fig_no in no_refine_figs)):
                refined = fitz.Rect(clip)
                r_height = max(1.0, refined.height)
                r_area = max(1.0, refined.width * refined.height)
                r_cov = object_area_ratio(refined)
                r_ink = ink_ratio_small(refined)
                r_comp = comp_count(refined)
                # Adaptive relaxation: if the FAR side of the base clip contains
                # substantial paragraph text (likely headers/bullets), allow a
                # stronger shrink since we are intentionally removing that region.
                relax_h = 0.60
                relax_a = 0.55
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
                    far_cov = 0.0
                    if far_lines:
                        if far_is_top:
                            region_h = max(1.0, (base_clip.y0 + 0.5 * base_clip.height) - base_clip.y0)
                        else:
                            region_h = max(1.0, base_clip.y1 - (base_clip.y0 + 0.5 * base_clip.height))
                        far_cov = sum(lb.height for lb in far_lines) / region_h
                    # Relax thresholds if far-side paragraphs are present
                    # 分层策略：远侧文字越多，允许缩小得越多
                    # 同时调整 ink 和 coverage 的阈值
                    relax_ink = 0.90
                    relax_cov = 0.85
                    if far_cov >= 0.60:  # 极高覆盖率（>60%）：很可能是大段正文
                        relax_h = 0.35
                        relax_a = 0.25
                        relax_ink = 0.70  # 允许 ink 降到70%
                        relax_cov = 0.70  # 允许 coverage 降到70%
                    elif far_cov >= 0.30:  # 高覆盖率（30-60%）：可能是多行段落
                        relax_h = 0.45
                        relax_a = 0.35
                        relax_ink = 0.75
                        relax_cov = 0.75
                    elif far_cov >= 0.18:  # 中等覆盖率（18-30%）：少量文字
                        relax_h = 0.50
                        relax_a = 0.40
                        relax_ink = 0.80
                        relax_cov = 0.80
                except Exception:
                    pass
                ok_h = (r_height >= relax_h * base_height)
                ok_a = (r_area >= relax_a * base_area)
                ok_c = (r_cov >= (relax_cov * base_cov) if base_cov > 0 else True)
                ok_i = (r_ink >= (relax_ink * base_ink) if base_ink > 0 else True)
                # If stacked components shrink to 1, be cautious
                ok_comp = (r_comp >= min(2, base_comp)) if base_comp >= 2 else True
                if not (ok_h and ok_a and ok_c and ok_i and ok_comp):
                    # 收集失败原因用于调试
                    reasons = []
                    if not ok_h: reasons.append(f"height={r_height/base_height:.1%}")
                    if not ok_a: reasons.append(f"area={r_area/base_area:.1%}")
                    if not ok_c: reasons.append(f"cov={r_cov/base_cov:.1%}" if base_cov > 0 else "cov=low")
                    if not ok_i: reasons.append(f"ink={r_ink/base_ink:.1%}" if base_ink > 0 else "ink=low")
                    if not ok_comp: reasons.append(f"comp={r_comp}/{base_comp}")
                    print(f"[WARN] Fig {fig_no} p{pno+1}: refinement rejected ({', '.join(reasons)}), trying fallback")
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
                    ) if text_trim else base_clip
                    rA_h, rA_a = max(1.0, clip_A.height), max(1.0, clip_A.width * clip_A.height)
                    if (rA_h >= 0.60 * base_height) and (rA_a >= 0.55 * base_area):
                        clip = clip_A
                        print(f"[INFO] Fig {fig_no} p{pno+1}: using A-only fallback")
                    else:
                        clip = base_clip
                        print(f"[INFO] Fig {fig_no} p{pno+1}: reverted to baseline")
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
                    save_debug_visualization(
                        page=page,
                        out_dir=out_dir,
                        fig_no=fig_no,
                        page_num=pno + 1,
                        stages=debug_stages,
                        caption_rect=cap_rect,
                        kind='figure',
                        layout_model=layout_model  # V2 Architecture
                    )
                except Exception as e:
                    print(f"[WARN] Debug visualization failed for Figure {fig_no}: {e}")

            # 生成安全文件名；若同名已存在（例如多页同名），则附加页码后缀
            base = sanitize_filename_from_caption(caption, fig_no, max_chars=max_caption_chars, max_words=max_caption_words)
            # 同号多页：根据选项决定是否允许继续导出，并命名为 continued
            count_prev = seen_counts.get(fig_no, 0)
            if count_prev >= 1 and not allow_continued:
                # 已导出过且不允许 continued：跳过
                continue
            if count_prev >= 1 and allow_continued:
                base = f"{base}_continued_p{pno+1}"
            out_path = os.path.join(out_dir, base + ".png")
            if os.path.exists(out_path):
                out_path = os.path.join(out_dir, f"{base}_p{pno+1}.png")
            pix.save(out_path)
            # 记录当前图号的输出路径与次数
            if count_prev == 0:
                seen[fig_no] = out_path
            seen_counts[fig_no] = count_prev + 1
            records.append(AttachmentRecord('figure', str(fig_no), pno + 1, caption, out_path, continued=(count_prev>=1)))
            print(f"[INFO] Figure {fig_no} page {pno+1} -> {out_path}")

    # 按数字键排序，兼容新结构
    records.sort(key=lambda r: r.num_key())
    return records


# 将导出的图信息写入 CSV 清单（可选）
def write_manifest(records: List[AttachmentRecord], manifest_path: Optional[str]) -> Optional[str]:
    if not manifest_path:
        return None
    with open(manifest_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        # 统一为 (type,id,page,caption,file,continued)
        w.writerow(["type", "id", "page", "caption", "file", "continued"])
        for r in records:
            w.writerow([r.kind, r.ident, r.page, r.caption, r.out_path, int(r.continued)])
    print(f"[INFO] Wrote manifest: {manifest_path} (items={len(records)})")
    return manifest_path


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


# ---- JSON 索引：images/index.json ----
def write_index_json(records: List[AttachmentRecord], index_path: str) -> Optional[str]:
    import json
    base_dir = os.path.dirname(os.path.abspath(index_path))
    os.makedirs(base_dir, exist_ok=True)
    out: List[Dict[str, Any]] = []
    for r in records:
        rel = os.path.relpath(os.path.abspath(r.out_path), base_dir).replace('\\', '/')
        out.append({
            "type": r.kind,
            "id": r.ident,
            "page": r.page,
            "caption": r.caption,
            "file": rel,
            "continued": bool(r.continued),
        })
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Wrote index: {index_path} (items={len(out)})")
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
    except Exception:
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
) -> Optional[str]:
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
    
    Returns:
        输出文件路径
    """
    try:
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
        return vis_path
    except Exception as e:
        print(f"[WARN] Debug visualization failed: {e}")
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
    far_side_min_dist: float = 100.0,
    far_side_para_min_ratio: float = 0.20,
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
                r"^\s*(?:(?:Extended\s+Data\s+Table|Supplementary\s+Table|Table|Tab\.?|表)\s*"
                r"(?:S\s*)?"
                r"([A-Z]\d+|[IVX]{1,5}|\d+))",  # 单一捕获组，支持附录表/罗马数字/普通数字
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
            far_text_th = 10.0 * typical_line_h
        if text_trim_gap == 6.0:  # 默认值
            text_trim_gap = 0.5 * typical_line_h
        if far_side_min_dist == 100.0:  # 默认值
            far_side_min_dist = 8.0 * typical_line_h
        
        if debug_captions:
            print(f"ADAPTIVE TABLE PARAMETERS (based on line_height={typical_line_h:.1f}pt):")
            print(f"  adjacent_th:      {adjacent_th:.1f} pt (2.0× line_height)")
            print(f"  far_text_th:      {far_text_th:.1f} pt (10.0× line_height)")
            print(f"  text_trim_gap:    {text_trim_gap:.1f} pt (0.5× line_height)")
            print(f"  far_side_min_dist:{far_side_min_dist:.1f} pt (8.0× line_height)")
            print()

    # 改进：支持罗马数字、附录表、补充材料表、续页标记
    table_line_re = re.compile(
        r"^\s*(?:(?:Extended\s+Data\s+Table|Supplementary\s+Table|Table|Tab\.?|表)\s*"
        r"(?:S\s*)?"
        r"(?:"
        r"([A-Z]\d+)|"              # 附录表: A1, B2, C3
        r"([IVX]{1,5})|"            # 罗马数字: I, II, III, IV, V
        r"(\d+)"                    # 普通数字: 1, 2, 3
        r"))"
        r"(?:\s*\(continued\)|\s*续|\s*接上页)?",  # 可选的续页标记
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
        except Exception:
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
            except Exception:
                pass
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
                except Exception:
                    ink_a = 0.0
                try:
                    pix_b = page_s.get_pixmap(matrix=fitz.Matrix(1,1), clip=clip_below, alpha=False)
                    ink_b = estimate_ink_ratio(pix_b)
                except Exception:
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
        if below_total_tbl > above_total_tbl * (1.0 + ga_margin_tbl):
            global_side_table = 'below'
            print(f"[INFO] Global table anchor: BELOW (below={below_total_tbl:.2f} vs above={above_total_tbl:.2f})")
        elif above_total_tbl > below_total_tbl * (1.0 + ga_margin_tbl):
            global_side_table = 'above'
            print(f"[INFO] Global table anchor: ABOVE (above={above_total_tbl:.2f} vs below={below_total_tbl:.2f})")
        else:
            global_side_table = None
            print(f"[INFO] Global table anchor: AUTO (no clear preference)")
    
    # === Cache for smart-selected table captions ===
    smart_caption_cache_table: Dict[str, Tuple[fitz.Rect, str, int]] = {}
    
    if smart_caption_detection and caption_index_table:
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
                        ident = (m.group(1) or m.group(2) or m.group(3) or "").strip()
                        if ident:
                            page_table_ids.add(ident)
            
            # For each table ID, select best caption
            for table_id in page_table_ids:
                if table_id in smart_caption_cache_table:
                    continue  # Already cached
                candidates = caption_index_table.get_candidates('table', str(table_id))
                if candidates:
                    best = select_best_caption(candidates, page_pre, min_score_threshold=25.0, debug=debug_captions)
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
        except Exception:
            pass
        draw_items = collect_draw_items(page)

        captions_on_page: List[Tuple[str, fitz.Rect, str]] = []
        
        # === Use smart-selected captions if available ===
        if smart_caption_detection and caption_index_table:
            # Find all table IDs on this page from cache
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
                    ident = (m.group(1) or m.group(2) or m.group(3) or "").strip()
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

            try:
                dist_lambda = float(os.getenv('SCAN_DIST_LAMBDA', '0.12'))
            except Exception:
                dist_lambda = 0.12

            def score_table_clip(clip: fitz.Rect) -> float:
                small_scale = 1.0
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(small_scale, small_scale), clip=clip, alpha=False)
                    ink = estimate_ink_ratio(pix)
                except Exception:
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
                    except Exception:
                        side, chosen_clip = 'above', clip_above
                clip = chosen_clip
            else:
                # Anchor V2：多尺度滑窗 + 吸附
                scan_heights = os.getenv('SCAN_HEIGHTS', '')
                heights = [float(h) for h in scan_heights.split(',') if h.strip()] if scan_heights else [240.0, 320.0, 420.0, 520.0, 640.0]
                try:
                    step = float(os.getenv('SCAN_STEP', '14'))
                except Exception:
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

                # above (respect global anchor for tables)
                if global_side_table in (None, 'above'):
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
                # below (respect global anchor for tables)
                if global_side_table in (None, 'below'):
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
                        dbg_dir = os.path.join(os.path.dirname(os.path.abspath(out_dir)), 'images')
                        os.makedirs(dbg_dir, exist_ok=True)
                        dump_page_candidates(
                            page,
                            os.path.join(dbg_dir, f"debug_candidates_tbl_p{pno+1}.png"),
                            candidates=cands,
                            best=best,
                            caption_rect=cap_rect,
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
                    print(f"[INFO] Table {ident}: Layout-guided adjustment applied")

            base_clip = fitz.Rect(clip)
            base_height = max(1.0, base_clip.height)
            base_area = max(1.0, base_clip.width * base_clip.height)
            base_ink = 0.0
            try:
                pix_small = page.get_pixmap(matrix=fitz.Matrix(1,1), clip=base_clip, alpha=False)
                base_ink = estimate_ink_ratio(pix_small)
            except Exception:
                pass
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
            clip_after_A = fitz.Rect(clip)
            if text_trim:
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
                    far_side_min_dist=far_side_min_dist,
                    far_side_para_min_ratio=far_side_para_min_ratio,
                    typical_line_h=typical_lh,
                )
                clip_after_A = fitz.Rect(clip)
                if debug_visual and (clip_after_A != base_clip):
                    debug_stages_tbl.append(DebugStageInfo(
                        name="Phase A (Text Trimming)",
                        rect=fitz.Rect(clip_after_A),
                        color=(0, 200, 0),  # 绿色
                        description="After removing adjacent text (Phase A+B+C)"
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
                print(f"[WARN] Render failed on page {pno+1}, Table {ident}: {e}")
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
                    # 远端边缘保护：表格通常需要保留页眉线等细要素
                    far_pad_pt = max(0.0, protect_far_edge_px / scale)
                    if far_pad_pt > 0:
                        if side == 'above':
                            tight = fitz.Rect(tight.x0, max(page_rect.y0, tight.y0 - far_pad_pt), tight.x1, tight.y1)
                        else:
                            tight = fitz.Rect(tight.x0, tight.y0, tight.x1, min(page_rect.y1, tight.y1 + far_pad_pt))
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
                            print(f"[INFO] Table {ident}: Post-autocrop layout adjustment applied")
                    
                    pix = page.get_pixmap(matrix=mat, clip=tight, alpha=False)
                    clip = tight
                except Exception as e:
                    print(f"[WARN] Autocrop failed (Table {ident} page {pno+1}): {e}")

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
                except Exception:
                    pass
                # 表格也检测远侧文字覆盖率，分层放宽阈值
                relax_h = 0.50
                relax_a = 0.45
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
                    far_cov_tbl = 0.0
                    if far_lines_tbl:
                        if far_is_top:
                            region_h_tbl = max(1.0, (base_clip.y0 + 0.5 * base_clip.height) - base_clip.y0)
                        else:
                            region_h_tbl = max(1.0, base_clip.y1 - (base_clip.y0 + 0.5 * base_clip.height))
                        far_cov_tbl = sum(lb.height for lb in far_lines_tbl) / region_h_tbl
                    # 分层策略（与 figure 一致）
                    # 同时调整 ink 和 text_lines 的阈值
                    relax_ink = 0.85
                    relax_text = 0.75
                    if far_cov_tbl >= 0.60:
                        relax_h = 0.35
                        relax_a = 0.25
                        relax_ink = 0.70  # 极高覆盖率：允许 ink 降到70%
                        relax_text = 0.55  # 允许 text_lines 降到55%
                    elif far_cov_tbl >= 0.30:
                        relax_h = 0.45
                        relax_a = 0.35
                        relax_ink = 0.75
                        relax_text = 0.60
                    elif far_cov_tbl >= 0.18:
                        relax_h = 0.50
                        relax_a = 0.40
                        relax_ink = 0.80
                        relax_text = 0.65
                except Exception:
                    pass
                ok_h = (r_height >= relax_h * base_height)
                ok_a = (r_area >= relax_a * base_area)
                ok_i = (r_ink >= (relax_ink * base_ink) if base_ink > 0 else True)
                ok_t = (r_text >= max(1, int(relax_text * base_text))) if base_text > 0 else True
                ok_comp = (r_comp >= min(2, base_comp)) if base_comp >= 2 else True
                if not (ok_h and ok_a and ok_i and ok_t and ok_comp):
                    # 表格验收失败日志
                    reasons = []
                    if not ok_h: reasons.append(f"height={r_height/base_height:.1%}")
                    if not ok_a: reasons.append(f"area={r_area/base_area:.1%}")
                    if not ok_i: reasons.append(f"ink={r_ink/base_ink:.1%}" if base_ink > 0 else "ink=low")
                    if not ok_t: reasons.append(f"text_lines={r_text}/{base_text}")
                    if not ok_comp: reasons.append(f"comp={r_comp}/{base_comp}")
                    print(f"[WARN] Table {ident} p{pno+1}: refinement rejected ({', '.join(reasons)}), using A-only fallback")
                    typical_lh_fallback_tbl = line_metrics.get('typical_line_height') if (adaptive_line_height and 'line_metrics' in locals()) else None
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
                        typical_line_h=typical_lh_fallback_tbl,
                    ) if text_trim else base_clip
                    # 二次门槛：如 A-only 过小则回退到基线
                    rA_h, rA_a = max(1.0, clip_A.height), max(1.0, clip_A.width * clip_A.height)
                    if (rA_h >= 0.60 * base_height) and (rA_a >= 0.55 * base_area):
                        clip = clip_A
                    else:
                        clip = base_clip
                        if debug_visual:
                            debug_stages_tbl.append(DebugStageInfo(
                                name="Fallback (Reverted to Baseline)",
                                rect=fitz.Rect(clip),
                                color=(255, 255, 0),  # 黄色
                                description="Refinement rejected, reverted to baseline"
                            ))
                    try:
                        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                    except Exception:
                        pass

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
                    save_debug_visualization(
                        page=page,
                        out_dir=out_dir,
                        fig_no=ident,
                        page_num=pno + 1,
                        stages=debug_stages_tbl,
                        caption_rect=cap_rect,
                        kind='table',
                        layout_model=layout_model  # V2 Architecture
                    )
                except Exception as e:
                    print(f"[WARN] Debug visualization failed for Table {ident}: {e}")

            base_name = build_output_basename('Table', ident, caption, max_chars=max_caption_chars, max_words=max_caption_words)
            count_prev = seen_counts.get(ident, 0)
            cont = False
            if count_prev >= 1 and not allow_continued:
                continue
            if count_prev >= 1 and allow_continued:
                base_name = f"{base_name}_continued_p{pno+1}"
                cont = True
            out_path = os.path.join(out_dir, base_name + ".png")
            if os.path.exists(out_path):
                out_path = os.path.join(out_dir, f"{base_name}_p{pno+1}.png")
            pix.save(out_path)
            seen_counts[ident] = count_prev + 1
            records.append(AttachmentRecord('table', ident, pno + 1, caption, out_path, continued=cont))
            print(f"[INFO] Table {ident} page {pno+1} -> {out_path}")

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
    content_adjusted = False
    for block in content_blocks:
        if direction == 'below':
            # 图在下方，检查上边界是否切断了内容区块
            if block.bbox.y0 < adjusted_clip.y0 < block.bbox.y1:
                adjusted_clip.y0 = block.bbox.y0 - 2  # 向上扩展，留2pt间隙
                content_adjusted = True
                if debug:
                    print(f"  -> Expanding top boundary to include content block at {block.bbox.y0:.1f}pt")
            # 检查下边界
            if block.bbox.y0 < adjusted_clip.y1 < block.bbox.y1:
                adjusted_clip.y1 = block.bbox.y1 + 2  # 向下扩展
                content_adjusted = True
                if debug:
                    print(f"  -> Expanding bottom boundary to include content block at {block.bbox.y1:.1f}pt")
        else:  # direction == 'above'
            # 图在上方，检查边界是否切断了内容区块
            if block.bbox.y0 < adjusted_clip.y1 < block.bbox.y1:
                adjusted_clip.y1 = block.bbox.y1 + 2  # 向下扩展
                content_adjusted = True
                if debug:
                    print(f"  -> Expanding bottom boundary to include content block at {block.bbox.y1:.1f}pt")
            if block.bbox.y0 < adjusted_clip.y0 < block.bbox.y1:
                adjusted_clip.y0 = block.bbox.y0 - 2  # 向上扩展
                content_adjusted = True
                if debug:
                    print(f"  -> Expanding top boundary to include content block at {block.bbox.y0:.1f}pt")
    
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
    
    # 如果重叠不严重（<20%）且没有标题重叠，直接返回
    if overlap_ratio_total < 0.20 and not has_title_overlap:
        if debug:
            print(f"  -> No adjustment needed (overlap < 20%, no title overlap)")
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
        print(f"[INFO] Typical font name: {typical_font_name}")
    
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
    p.add_argument("--layout-driven", action="store_true", help="Enable layout-driven extraction (V2): build document layout model first, then use it to guide figure/table extraction (experimental)")
    p.add_argument("--layout-json", default=None, help="Path to save/load layout model JSON (default: <out_dir>/layout_model.json)")
    
    # Adaptive line height
    p.add_argument("--adaptive-line-height", action="store_true", default=True, help="Enable adaptive line height: auto-adjust parameters based on document's typical line height (default: enabled)")
    p.add_argument("--no-adaptive-line-height", action="store_false", dest="adaptive_line_height", help="Disable adaptive line height (use fixed default parameters)")
    
    # A) text trimming options
    p.add_argument("--text-trim", action="store_true", help="Trim paragraph-like text near caption side inside chosen clip")
    p.add_argument("--text-trim-width-ratio", type=float, default=0.5, help="Min horizontal overlap ratio to treat a line as paragraph text")
    p.add_argument("--text-trim-font-min", type=float, default=7.0, help="Min font size for paragraph detection")
    p.add_argument("--text-trim-font-max", type=float, default=16.0, help="Max font size for paragraph detection")
    p.add_argument("--text-trim-gap", type=float, default=6.0, help="Gap between trimmed text and new clip boundary (pt)")
    p.add_argument("--adjacent-th", type=float, default=24.0, help="Adjacency threshold to caption to treat text as body (pt)")
    # A+) far-text trim options (dual-threshold)
    p.add_argument("--far-text-th", type=float, default=300.0, help="Maximum distance to detect far text (pt)")
    p.add_argument("--far-text-para-min-ratio", type=float, default=0.30, help="Minimum paragraph coverage ratio to trigger far-text trim")
    p.add_argument("--far-text-trim-mode", type=str, default="aggressive", choices=["aggressive", "conservative"], help="Far-text trim mode")
    p.add_argument("--far-side-min-dist", type=float, default=100.0, help="Minimum distance to detect far-side text (pt)")
    p.add_argument("--far-side-para-min-ratio", type=float, default=0.20, help="Minimum paragraph coverage ratio to trigger far-side trim")
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
    return p.parse_args(argv)


# 入口：解析参数 → 文本提取（可选）→ 图像提取 → 写出清单（可选）
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    pdf_path = args.pdf
    if not os.path.exists(pdf_path):
        print(f"[ERROR] PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    # Resolve defaults relative to PDF dir
    pdf_dir = os.path.dirname(os.path.abspath(pdf_path))
    pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = args.out_dir or os.path.join(pdf_dir, "images")
    out_text = args.out_text or os.path.join(pdf_dir, "text", pdf_stem + ".txt")
    # 确保文本输出目录存在（只在dirname非空时创建）
    text_dir = os.path.dirname(out_text)
    if text_dir:
        os.makedirs(text_dir, exist_ok=True)

    # Extract text by default（若安装 pdfminer.six 且指定 out-text，默认尝试提取文本）
    try_extract_text(pdf_path, out_text)

    # Apply presets if requested
    if getattr(args, "preset", None) == "robust":
        args.dpi = 300
        args.clip_height = 520.0
        args.margin_x = 26.0
        args.caption_gap = 6.0
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
        args.include_tables = True
        args.table_clip_height = 520.0
        args.table_margin_x = 26.0
        args.table_caption_gap = 6.0
        args.table_autocrop = True
        args.table_autocrop_pad = 20
        args.table_autocrop_white_th = 250
        args.table_mask_text = False
        args.table_object_min_area_ratio = 0.005
        args.table_object_merge_gap = 4.0
        # 自适应行高（默认启用）
        args.adaptive_line_height = True

    # Anchor mode & scan params
    os.environ.setdefault('EXTRACT_ANCHOR_MODE', (args.anchor_mode or 'v2'))
    os.environ.setdefault('SCAN_STEP', str(args.scan_step))
    os.environ.setdefault('SCAN_HEIGHTS', args.scan_heights or '240,320,420,520,640')
    os.environ.setdefault('SCAN_DIST_LAMBDA', str(getattr(args, 'scan_dist_lambda', 0.15)))
    os.environ.setdefault('CAPTION_MID_GUARD', str(getattr(args, 'caption_mid_guard', 6.0)))
    os.environ.setdefault('GLOBAL_ANCHOR', (args.global_anchor or 'auto'))
    os.environ.setdefault('GLOBAL_ANCHOR_MARGIN', str(getattr(args, 'global_anchor_margin', 0.02)))
    os.environ.setdefault('GLOBAL_ANCHOR_TABLE', (getattr(args, 'global_anchor_table', 'auto') or 'auto'))
    os.environ.setdefault('GLOBAL_ANCHOR_TABLE_MARGIN', str(getattr(args, 'global_anchor_table_margin', 0.03)))

    # 控制调试导出
    if getattr(args, 'dump_candidates', False):
        os.environ['DUMP_CANDIDATES'] = '1'

    # Build layout model if --layout-driven is enabled (V2 Architecture)
    layout_model: Optional[DocumentLayoutModel] = None
    if args.layout_driven:
        print("\n" + "=" * 70)
        print("LAYOUT-DRIVEN EXTRACTION (V2 Architecture)")
        print("=" * 70)
        
        # Determine layout JSON path
        layout_json_path = args.layout_json or os.path.join(out_dir, "layout_model.json")
        
        # Build layout model
        layout_model = extract_text_with_format(
            pdf_path=pdf_path,
            out_json=layout_json_path,
            sample_pages=None,  # Analyze全部页面
            debug=args.debug_captions  # 复用debug_captions开关
        )
        
        print(f"[INFO] Layout model built successfully")
        print(f"  - Columns: {layout_model.num_columns} ({'single' if layout_model.num_columns == 1 else 'double'})")
        print(f"  - Text blocks: {sum(len(v) for v in layout_model.text_blocks.values())}")
        print(f"  - Vacant regions: {sum(len(v) for v in layout_model.vacant_regions.values())}")
        print("=" * 70 + "\n")

    # Extract figures
    def parse_fig_list(s: str) -> List[int]:
        out: List[int] = []
        for part in (s or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                pass
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
        far_side_min_dist=getattr(args, 'far_side_min_dist', 100.0),
        far_side_para_min_ratio=getattr(args, 'far_side_para_min_ratio', 0.20),
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
            text_trim=True if args.text_trim else True,
            text_trim_width_ratio=max(0.35, getattr(args, 'text_trim_width_ratio', 0.5)),
            text_trim_font_min=getattr(args, 'text_trim_font_min', 7.0),
            text_trim_font_max=getattr(args, 'text_trim_font_max', 16.0),
            text_trim_gap=getattr(args, 'text_trim_gap', 6.0),
            adjacent_th=getattr(args, 'table_adjacent_th', 28.0),
            far_text_th=getattr(args, 'far_text_th', 300.0),
            far_text_para_min_ratio=getattr(args, 'far_text_para_min_ratio', 0.30),
            far_text_trim_mode=getattr(args, 'far_text_trim_mode', 'aggressive'),
            far_side_min_dist=getattr(args, 'far_side_min_dist', 100.0),
            far_side_para_min_ratio=getattr(args, 'far_side_para_min_ratio', 0.20),
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

    # 写出 index.json（默认 images/index.json）
    index_json_path = args.index_json or os.path.join(out_dir, 'index.json')
    try:
        write_index_json(all_records, index_json_path)
    except Exception as e:
        print(f"[WARN] Write index.json failed: {e}")

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
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
