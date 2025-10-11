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

# 运行时版本检查：要求 Python 3.12+
if sys.version_info < (3, 12):  # pragma: no cover
    print(f"[ERROR] Python 3.12+ is required; found {sys.version.split()[0]}", file=sys.stderr)
    raise SystemExit(3)

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


# 从图注文本生成安全的文件名：
# - 规范化分隔符与 Unicode；
# - 限制可用字符集合；
# - 压缩多余下划线并限制最大长度；
# - 确保以 Figure_<no> 开头，避免重复与歧义。
def sanitize_filename_from_caption(caption: str, figure_no: int, max_chars: int = 160) -> str:
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
    
    # === Phase B: Detect and trim far-distance text ===
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
    
    # Phase B trimming (near-side far text)
    if far_para_lines and para_coverage_ratio >= far_text_para_min_ratio:
        pass  # Will be handled below
    
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
        
        # Must be far from caption (>100pt)
        if dist > 100.0:
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
        
        # Decision: trim far-side if coverage >= 20% (covers edge cases at 0.25)
        if far_side_para_coverage >= 0.20:
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
        print(f"Text: {candidate.text[:60]}...")
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
                    base = 0.55 * ink + 0.25 * obj - 0.2 * para + comp_bonus
                    # 距离罚项：候选窗离 caption 越远，得分越低
                    if cap_rect:
                        if clip.y1 <= cap_rect.y0:  # above
                            dist = abs(cap_rect.y0 - clip.y1)
                        else:  # below
                            dist = abs(clip.y0 - cap_rect.y1)
                        base -= dist_lambda * (dist / max(1.0, page_rect.height))
                    return base

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

            # clip 已选定（V1/V2）

            # Baseline metrics for acceptance gating
            base_clip = fitz.Rect(clip)
            base_height = max(1.0, base_clip.height)
            base_area = max(1.0, base_clip.width * base_clip.height)
            base_cov = object_area_ratio(base_clip)
            base_ink = ink_ratio_small(base_clip)
            base_comp = comp_count(base_clip)

            # A) 文本邻接裁切：增加“段落占比”门槛，防止误剪图边
            if text_trim:
                # Always run Phase C (far-side trim) regardless of para_ratio
                # This handles cases where large paragraphs are far from caption
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
                )

            # B) 对象连通域引导（可按图号禁用）
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

            # 额外：若远端边（非靠 caption 一侧）仍有大量对象紧贴，尝试向远端外扩，避免“半幅”
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
                ok_h = (r_height >= 0.60 * base_height)
                ok_a = (r_area >= 0.55 * base_area)
                ok_c = (r_cov >= (0.85 * base_cov) if base_cov > 0 else True)
                ok_i = (r_ink >= (0.90 * base_ink) if base_ink > 0 else True)
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
                    ) if text_trim else base_clip
                    rA_h, rA_a = max(1.0, clip_A.height), max(1.0, clip_A.width * clip_A.height)
                    if (rA_h >= 0.60 * base_height) and (rA_a >= 0.55 * base_area):
                        clip = clip_A
                        print(f"[INFO] Fig {fig_no} p{pno+1}: using A-only fallback")
                    else:
                        clip = base_clip
                        print(f"[INFO] Fig {fig_no} p{pno+1}: reverted to baseline")

            # 生成安全文件名；若同名已存在（例如多页同名），则附加页码后缀
            base = sanitize_filename_from_caption(caption, fig_no, max_chars=max_caption_chars)
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
def build_output_basename(kind: str, ident: str, caption: str, max_chars: int = 160) -> str:
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


def _draw_rects_on_pix(pix: "fitz.Pixmap", rects: List[Tuple[fitz.Rect, Tuple[int, int, int]]], *, scale: float) -> None:
    """Draw rectangle edges on a pixmap in-place with RGB colors.
    rects: list of (rect, (r,g,b))
    """
    # Ensure no alpha
    if pix.alpha:
        tmp = fitz.Pixmap(fitz.csRGB, pix)
        pix = tmp
    w, h = pix.width, pix.height
    n = pix.n
    samples = pix.samples  # bytes-like
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
        for x in range(lx, rx + 1):
            set_px(x, ty, col)
            set_px(x, by, col)
        for y in range(ty, by + 1):
            set_px(lx, y, col)
            set_px(rx, y, col)


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
        _draw_rects_on_pix(pix, rects, scale=scale)
        pix.save(out_path)
        return out_path
    except Exception:
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
                # 组合分：墨迹/列对齐/线密度/对象占比 - 段落惩罚
                base = 0.5 * ink + 0.2 * cols_norm + 0.15 * line_d + 0.15 * obj - 0.25 * para
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

            if text_trim:
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
                )

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
                ok_h = (r_height >= 0.50 * base_height)
                ok_a = (r_area >= 0.45 * base_area)
                ok_i = (r_ink >= (0.85 * base_ink) if base_ink > 0 else True)
                ok_t = (r_text >= max(1, int(0.75 * base_text))) if base_text > 0 else True
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
                    ) if text_trim else base_clip
                    clip = clip_A
                    try:
                        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                    except Exception:
                        pass

            base_name = build_output_basename('Table', ident, caption, max_chars=max_caption_chars)
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
    p.add_argument("--scan-heights", default="240,320,420,520,640,720,820", help="Comma-separated window heights (pt) for anchor v2")
    p.add_argument("--scan-dist-lambda", type=float, default=0.12, help="Penalty weight for distance of candidate window to caption (anchor v2, recommend 0.10-0.15)")
    p.add_argument("--scan-topk", type=int, default=3, help="Keep top-k candidates during anchor v2 (for debugging)")
    p.add_argument("--dump-candidates", action="store_true", help="Dump page-level candidate boxes for debugging (anchor v2)")
    p.add_argument("--caption-mid-guard", type=float, default=6.0, help="Guard (pt) around midline between adjacent captions to avoid cross-anchoring")
    # Smart caption detection (NEW)
    p.add_argument("--smart-caption-detection", action="store_true", default=True, help="Enable smart caption detection to distinguish real captions from in-text references (default: enabled)")
    p.add_argument("--no-smart-caption-detection", action="store_false", dest="smart_caption_detection", help="Disable smart caption detection (use simple pattern matching)")
    p.add_argument("--debug-captions", action="store_true", help="Print detailed caption candidate scoring information for debugging")
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
    p.add_argument("--autocrop-shrink-limit", type=float, default=0.30, help="Max area shrink ratio allowed during autocrop (0.30 = shrink up to 30%, lower=more conservative)")
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
    os.makedirs(os.path.dirname(out_text), exist_ok=True)

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
