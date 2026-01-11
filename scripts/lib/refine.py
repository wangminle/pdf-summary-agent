#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 07+: 精裁与验收

从 extract_pdf_assets.py 抽离的精裁和验收相关代码。

V0.4.0 更新：迁移完整的文本裁切逻辑 (Phase A/B/C)

包含：
- detect_content_bbox_pixels: 像素级内容包围盒检测
- estimate_ink_ratio: 墨迹密度估算
- merge_rects: 合并重叠矩形
- refine_clip_by_objects: 基于对象的裁剪优化
- build_text_masks_px: 构建文本遮罩
- detect_far_side_text_evidence: 远端正文检测
- trim_far_side_text_post_autocrop: 远端正文后处理裁切
- adaptive_acceptance_thresholds: 动态验收阈值
- snap_clip_edges: 对齐裁剪边缘到绘图线
- is_caption_text: 检查文本行是否属于图注
- detect_exact_n_lines_of_text: 检测精确行数
- trim_clip_head_by_text: 文本裁切（Phase A 基础版）
- trim_clip_head_by_text_v2: 增强文本裁切（Phase A/B/C 完整版）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

# 尝试导入 fitz
try:
    import fitz
except ImportError:
    fitz = None  # type: ignore

# 避免循环导入
if TYPE_CHECKING:
    from .models import AcceptanceThresholds, DrawItem

# 模块日志器
logger = logging.getLogger(__name__)


# ============================================================================
# 像素级内容检测
# ============================================================================

def detect_content_bbox_pixels(
    pix: "fitz.Pixmap",
    white_threshold: int = 250,
    pad: int = 30,
    mask_rects_px: Optional[List[Tuple[int, int, int, int]]] = None,
) -> Tuple[int, int, int, int]:
    """
    在像素级估计非白色区域包围盒（带少量 padding），用于 autocrop 去除白边。
    
    Args:
        pix: PyMuPDF 位图对象
        white_threshold: 白色阈值（0-255）
        pad: 边界 padding（像素）
        mask_rects_px: 可选的掩码矩形列表（像素坐标），这些区域将被忽略
    
    Returns:
        (left, top, right, bottom) 像素坐标的边界框
    """
    if fitz is None:
        return (0, 0, pix.width, pix.height) if pix else (0, 0, 0, 0)
    
    w, h = pix.width, pix.height
    n = pix.n
    
    # 转换为 RGB 避免 alpha 复杂性
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


def estimate_ink_ratio(pix: "fitz.Pixmap", white_threshold: int = 250) -> float:
    """
    估计位图中"有墨迹"的像素比例（0~1），通过子采样快速近似。
    值越大表示内容越密集。
    
    Args:
        pix: PyMuPDF 位图对象
        white_threshold: 白色阈值（0-255）
    
    Returns:
        墨迹比例（0.0~1.0）
    """
    if fitz is None:
        return 0.0
    
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


# ============================================================================
# 矩形合并
# ============================================================================

def merge_rects(rects: List[Any], merge_gap: float = 6.0) -> List[Any]:
    """
    合并重叠的矩形。
    
    通过先扩展再合并相交框的方式迭代处理。
    
    Args:
        rects: fitz.Rect 列表
        merge_gap: 合并间隙（pt）
    
    Returns:
        合并后的矩形列表
    """
    if not rects or fitz is None:
        return []
    
    # 扩展后合并相交框
    expanded = [fitz.Rect(r.x0 - merge_gap, r.y0 - merge_gap, r.x1 + merge_gap, r.y1 + merge_gap) for r in rects]
    changed = True
    while changed:
        changed = False
        out: List[Any] = []
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
    return expanded


# ============================================================================
# 基于对象的裁剪优化
# ============================================================================

def refine_clip_by_objects(
    clip: Any,
    caption_rect: Any,
    direction: str,
    image_rects: List[Any],
    vector_rects: List[Any],
    *,
    object_pad: float = 8.0,
    min_area_ratio: float = 0.015,
    merge_gap: float = 6.0,
    near_edge_only: bool = True,
    use_axis_union: bool = True,
    use_horizontal_union: bool = False,
) -> Any:
    """
    使用对象组件优化裁剪区域。
    
    Args:
        clip: 当前裁剪区域
        caption_rect: 图注边界框
        direction: 方向 ('above' | 'below')
        image_rects: 图像边界框列表
        vector_rects: 矢量图形边界框列表
        object_pad: 对象 padding
        min_area_ratio: 最小面积比
        merge_gap: 合并间隙
        near_edge_only: 是否只调整靠近图注的边界
        use_axis_union: 是否使用垂直轴联合
        use_horizontal_union: 是否使用水平轴联合
    
    Returns:
        优化后的裁剪区域
    """
    if fitz is None:
        return clip
    
    area = max(1.0, clip.width * clip.height)
    cand: List[Any] = []
    
    for r in image_rects + vector_rects:
        inter = r & clip
        if inter.width > 0 and inter.height > 0:
            if (inter.width * inter.height) / area >= min_area_ratio:
                cand.append(inter)
    
    if not cand:
        return clip

    comps = merge_rects(cand, merge_gap=merge_gap)
    if not comps:
        return clip

    # 选择最靠近图注的组件
    def comp_score(r: Any) -> float:
        if direction == 'above':
            dist = max(0.0, caption_rect.y0 - r.y1)
        else:
            dist = max(0.0, r.y0 - caption_rect.y1)
        return dist + (-0.0001 * r.width * r.height)

    comps.sort(key=comp_score)
    chosen = comps[0]
    
    # 垂直堆叠组件联合
    if use_axis_union and len(comps) >= 2:
        overlaps = []
        for r in comps:
            inter_w = max(0.0, min(r.x1, chosen.x1) - max(r.x0, chosen.x0))
            overlaps.append(inter_w / max(1.0, min(r.width, chosen.width)))
        if sum(1 for v in overlaps if v >= 0.6) >= 2:
            union = comps[0]
            for r in comps[1:]:
                union = union | r
            chosen = union

    # 水平并列组件联合
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

    # 应用 padding
    chosen = fitz.Rect(
        chosen.x0 - object_pad,
        chosen.y0 - object_pad,
        chosen.x1 + object_pad,
        chosen.y1 + object_pad,
    )

    # 非对称更新：只调整靠近图注的边界
    result = fitz.Rect(clip)
    if near_edge_only:
        if direction == 'above':
            result.y1 = min(clip.y1, max(chosen.y1, clip.y0 + 40.0))
        else:
            result.y0 = max(clip.y0, min(chosen.y0, clip.y1 - 40.0))
        result.x0 = min(result.x0, chosen.x0)
        result.x1 = max(result.x1, chosen.x1)
        result = result & clip
        return result if result.height >= 40 else clip
    else:
        result = (chosen & clip)
        return result if result.height >= 40 else clip


# ============================================================================
# 文本遮罩构建
# ============================================================================

def build_text_masks_px(
    clip: Any,
    text_lines: List[Tuple[Any, float, str]],
    *,
    scale: float,
    direction: str = 'above',
    near_frac: float = 0.6,
    width_ratio: float = 0.5,
    font_max: float = 14.0,
    mask_mode: str = 'auto',
    far_edge_zone: float = 40.0,
) -> List[Tuple[int, int, int, int]]:
    """
    将选定的文本行转换为像素空间遮罩。
    
    Args:
        clip: 裁剪区域
        text_lines: 文本行列表 [(rect, font_size, text), ...]
        scale: 缩放比例（pt -> px）
        direction: 方向 ('above' | 'below')
        near_frac: 近端区域比例
        width_ratio: 宽度比例阈值
        font_max: 最大字号
        mask_mode: 遮罩模式 ('near' | 'both' | 'auto')
        far_edge_zone: 远端边缘检测区域（pt）
    
    Returns:
        像素坐标的遮罩矩形列表 [(left, top, right, bottom), ...]
    """
    if fitz is None:
        return []
    
    masks: List[Tuple[int, int, int, int]] = []
    y_thresh_top = clip.y0 + near_frac * clip.height
    y_thresh_bot = clip.y1 - near_frac * clip.height
    
    mask_near = True
    mask_far = (mask_mode == 'both')
    
    # 'auto' 模式：检测远端是否有正文行
    far_side_lines: List[Tuple[Any, float, str]] = []
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
            if (inter.width / max(1.0, clip.width)) < width_ratio:
                continue
            if len(txt) < 10:
                continue
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
        
        in_near_side = False
        in_far_side = False
        
        if direction == 'above':
            if inter.y0 >= y_thresh_bot:
                in_near_side = True
            if inter.y1 <= y_thresh_top:
                in_far_side = True
        else:
            if inter.y1 <= y_thresh_top:
                in_near_side = True
            if inter.y0 >= y_thresh_bot:
                in_far_side = True
        
        should_mask = False
        if mask_near and in_near_side:
            should_mask = True
        if mask_far and in_far_side:
            should_mask = True
        
        if not should_mask:
            continue
        
        # 转换为像素坐标
        l = int(max(0, (inter.x0 - clip.x0) * scale))
        t = int(max(0, (inter.y0 - clip.y0) * scale))
        r = int(min((clip.x1 - clip.x0) * scale, (inter.x1 - clip.x0) * scale))
        b = int(min((clip.y1 - clip.y0) * scale, (inter.y1 - clip.y0) * scale))
        if r - l > 1 and b - t > 1:
            masks.append((l, t, r, b))
    
    return masks


# ============================================================================
# 远端正文检测
# ============================================================================

def detect_far_side_text_evidence(
    clip: Any,
    text_lines: List[Tuple[Any, float, str]],
    direction: str,
    edge_zone: float = 40.0,
    min_width_ratio: float = 0.30,
    font_min: float = 7.0,
    font_max: float = 16.0,
) -> Tuple[bool, float]:
    """
    检测远端边缘附近是否有正文行证据。
    
    用于单调性约束：当远端附近有正文行时，Phase D 不应该扩展到这些行的区域。
    
    Args:
        clip: 当前裁剪区域
        text_lines: 文本行列表 [(rect, font_size, text), ...]
        direction: 方向 ('above' | 'below')
        edge_zone: 远端边缘检测范围（pt）
        min_width_ratio: 正文行最小宽度比例
        font_min/font_max: 正文字号范围
    
    Returns:
        (has_evidence, suggested_limit):
        - has_evidence: 是否检测到正文证据
        - suggested_limit: 建议的边界限制
    """
    if fitz is None or clip.height <= 1 or clip.width <= 1:
        return False, 0.0
    
    far_is_top = (direction == 'above')
    evidence_lines: List[Any] = []
    
    for (lb, fs, text) in text_lines:
        txt = text.strip()
        if not txt:
            continue
        
        inter = lb & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        
        width_ratio = inter.width / max(1.0, clip.width)
        if width_ratio < min_width_ratio:
            continue
        
        if not (font_min <= fs <= font_max):
            continue
        
        if len(txt) < 10:
            continue
        
        if far_is_top:
            dist_to_far_edge = lb.y0 - clip.y0
            if dist_to_far_edge < edge_zone:
                evidence_lines.append(lb)
        else:
            dist_to_far_edge = clip.y1 - lb.y1
            if dist_to_far_edge < edge_zone:
                evidence_lines.append(lb)
    
    if evidence_lines:
        gap = 6.0
        if far_is_top:
            suggested_limit = max(lb.y1 for lb in evidence_lines) + gap
        else:
            suggested_limit = min(lb.y0 for lb in evidence_lines) - gap
        return True, suggested_limit
    
    return False, 0.0


def trim_far_side_text_post_autocrop(
    clip: Any,
    text_lines: List[Tuple[Any, float, str]],
    direction: str,
    *,
    typical_line_h: Optional[float] = None,
    scan_lines: int = 3,
    min_width_ratio: float = 0.30,
    min_text_len: int = 15,
    font_min: float = 7.0,
    font_max: float = 16.0,
    gap: float = 6.0,
) -> Tuple[Any, bool]:
    """
    Phase D 后的轻量去正文后处理。
    
    在 autocrop 完成后，扫描远端边缘附近的正文行，如果检测到明确的正文，
    向内推 y0/y1（只动 y，不动 x）。
    
    Args:
        clip: 当前裁剪区域
        text_lines: 文本行列表
        direction: 方向 ('above' | 'below')
        typical_line_h: 典型行高
        scan_lines: 扫描行数
        min_width_ratio: 正文最小宽度比例
        min_text_len: 正文最小长度
        font_min/font_max: 正文字号范围
        gap: 裁剪后的间隙
    
    Returns:
        (new_clip, was_trimmed): 新的裁剪区域和是否进行了裁剪
    """
    if fitz is None or clip.height <= 1 or clip.width <= 1:
        return clip, False
    
    if typical_line_h and typical_line_h > 0:
        scan_range = typical_line_h * scan_lines
    else:
        scan_range = 45.0
    
    far_is_top = (direction == 'above')
    text_to_trim: List[Any] = []
    
    for (lb, fs, text) in text_lines:
        txt = text.strip()
        if not txt:
            continue
        
        inter = lb & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        
        width_ratio = inter.width / max(1.0, clip.width)
        if width_ratio < min_width_ratio:
            continue
        if len(txt) < min_text_len:
            continue
        if not (font_min <= fs <= font_max):
            continue
        
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
    
    new_clip = fitz.Rect(clip)
    if far_is_top:
        max_y1 = max(lb.y1 for lb in text_to_trim)
        new_y0 = max_y1 + gap
        if new_y0 < clip.y0 + 0.5 * clip.height:
            new_clip = fitz.Rect(clip.x0, new_y0, clip.x1, clip.y1)
    else:
        min_y0 = min(lb.y0 for lb in text_to_trim)
        new_y1 = min_y0 - gap
        if new_y1 > clip.y0 + 0.5 * clip.height:
            new_clip = fitz.Rect(clip.x0, clip.y0, clip.x1, new_y1)
    
    was_trimmed = (new_clip != clip)
    return new_clip, was_trimmed


# ============================================================================
# 动态验收阈值
# ============================================================================

def adaptive_acceptance_thresholds(
    base_height: float,
    *,
    is_table: bool = False,
    far_cov: float = 0.0,
) -> "AcceptanceThresholds":
    """
    根据基线高度和远侧覆盖率动态计算验收阈值。
    
    策略：
    - 大图（>400pt）：允许更激进的精裁
    - 中等图（200-400pt）：使用默认阈值
    - 小图（<200pt）：更保守
    - 远侧文字覆盖率越高，允许缩小得越多
    
    Args:
        base_height: 基线窗口高度（pt）
        is_table: 是否为表格
        far_cov: 远侧文字覆盖率（0.0-1.0）
    
    Returns:
        AcceptanceThresholds 对象
    """
    from .models import AcceptanceThresholds as AT
    
    # 基础阈值（根据尺寸分层）
    if base_height > 400:
        base_h, base_a = (0.50, 0.45) if is_table else (0.55, 0.50)
        base_ink, base_cov, base_text = 0.85, 0.80, 0.70
        desc = "large"
    elif base_height > 200:
        base_h, base_a = (0.50, 0.45) if is_table else (0.60, 0.55)
        base_ink, base_cov, base_text = 0.90, 0.85, 0.75
        desc = "medium"
    else:
        base_h, base_a = (0.65, 0.60) if is_table else (0.70, 0.65)
        base_ink, base_cov, base_text = 0.92, 0.88, 0.80
        desc = "small"
    
    # 根据远侧覆盖率进一步调整
    if far_cov >= 0.60:
        base_h = min(base_h, 0.35)
        base_a = min(base_a, 0.25)
        base_ink = min(base_ink, 0.70)
        base_cov = min(base_cov, 0.70)
        base_text = min(base_text, 0.55)
        desc += "+high_far_cov"
    elif far_cov >= 0.30:
        base_h = min(base_h, 0.45)
        base_a = min(base_a, 0.35)
        base_ink = min(base_ink, 0.75)
        base_cov = min(base_cov, 0.75)
        base_text = min(base_text, 0.60)
        desc += "+med_far_cov"
    elif far_cov >= 0.18:
        base_h = min(base_h, 0.50)
        base_a = min(base_a, 0.40)
        base_ink = min(base_ink, 0.80)
        base_cov = min(base_cov, 0.80)
        base_text = min(base_text, 0.65)
        desc += "+low_far_cov"
    
    return AT(
        height_ratio=base_h,
        area_ratio=base_a,
        object_coverage=base_cov,
        ink_density=base_ink,
    )


# ============================================================================
# 边缘对齐
# ============================================================================

def snap_clip_edges(
    clip: Any,
    draw_items: List["DrawItem"],
    *,
    snap_px: float = 14.0,
) -> Any:
    """
    将裁剪区域的上下边缘对齐到最近的水平线。
    
    Args:
        clip: 裁剪区域
        draw_items: 绘图元素列表
        snap_px: 对齐距离阈值（pt）
    
    Returns:
        对齐后的裁剪区域
    """
    if fitz is None:
        return clip
    
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
        
        d_top = abs(y_mid - top)
        if d_top <= snap_px and d_top < best_top_dist:
            best_top_dist = d_top
            best_top = y_mid
        
        d_bot = abs(y_mid - bottom)
        if d_bot <= snap_px and d_bot < best_bot_dist:
            best_bot_dist = d_bot
            best_bot = y_mid
    
    if best_bot - best_top >= 40.0:
        return fitz.Rect(clip.x0, best_top, clip.x1, best_bot)
    return clip


# ============================================================================
# 文本裁切辅助函数
# ============================================================================

def is_caption_text(
    lines: List[Any],
    caption_rect: Any,
    tolerance: float = 10.0
) -> bool:
    """
    检查给定的文本行是否与图注 caption_rect 重叠或非常接近。
    
    用于防止"两行检测"误裁图注本身（尤其是长标题换行的情况）。
    
    Args:
        lines: 待检查的文本行边界框列表 (fitz.Rect)
        caption_rect: 图注的边界框
        tolerance: 容差（pt），行与图注距离小于此值视为图注的一部分
    
    Returns:
        True 如果任何一行被判定为属于图注
    """
    if fitz is None:
        return False
    
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


def detect_exact_n_lines_of_text(
    clip_rect: Any,
    text_lines: List[Tuple[Any, float, str]],
    typical_line_h: float,
    n: int = 2,
    tolerance: float = 0.35
) -> Tuple[bool, List[Any]]:
    """
    检测 clip_rect 中是否恰好包含 n 行文字。
    
    Args:
        clip_rect: 待检测的矩形区域
        text_lines: 文本行列表 (bbox, font_size, text)
        typical_line_h: 典型行高
        n: 期望的行数
        tolerance: 容差（相对于期望值的比例）
    
    Returns:
        (is_exact_n_lines, matched_line_bboxes)
    """
    if fitz is None:
        return False, []
    
    # 筛选在区域内的文本行
    text_in_region = []
    for bbox, size_est, text in text_lines:
        if bbox.intersects(clip_rect) and bbox.height < typical_line_h * 1.5:
            text_in_region.append((bbox, size_est, text))
    
    if not text_in_region:
        return False, []
    
    # 按 y 坐标排序
    text_in_region.sort(key=lambda x: x[0].y0)
    
    # 计算实际行数（根据 y 间距判断是否为同一行）
    actual_lines: List[Any] = []
    current_line_bboxes = [text_in_region[0][0]]
    
    for i in range(1, len(text_in_region)):
        prev_bbox = text_in_region[i-1][0]
        curr_bbox = text_in_region[i][0]
        gap = curr_bbox.y0 - prev_bbox.y1
        
        if gap < typical_line_h * 0.8:  # 认为是同一行
            current_line_bboxes.append(curr_bbox)
        else:  # 新的一行
            # 合并当前行的所有 bbox
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
    
    # 检查总高度是否约等于 n 倍行高
    if len(actual_lines) > 0:
        total_height = actual_lines[-1].y1 - actual_lines[0].y0
        expected_height = n * typical_line_h
        
        if abs(total_height - expected_height) / expected_height > tolerance:
            return False, []
    
    return True, actual_lines


# ============================================================================
# Phase A: 基础文本裁切
# ============================================================================

def trim_clip_head_by_text(
    clip: Any,
    page_rect: Any,
    caption_rect: Any,
    direction: str,
    text_lines: List[Tuple[Any, float, str]],
    *,
    width_ratio: float = 0.5,
    font_min: float = 7.0,
    font_max: float = 16.0,
    gap: float = 6.0,
    adjacent_th: float = 24.0,
) -> Any:
    """
    Phase A 基础版：裁切靠近图注侧的段落类文本。
    
    只调整靠近图注的边缘：
    - 'above': 近端是 BOTTOM (y1)
    - 'below': 近端是 TOP (y0)
    
    Args:
        clip: 当前裁剪区域 (fitz.Rect)
        page_rect: 页面边界 (fitz.Rect)
        caption_rect: 图注边界 (fitz.Rect)
        direction: 方向 ('above' | 'below')
        text_lines: 文本行列表 [(rect, font_size, text), ...]
        width_ratio: 段落判定宽度比（默认 0.5）
        font_min: 正文最小字号
        font_max: 正文最大字号
        gap: 裁切后保留的间隙
        adjacent_th: 相邻判定阈值（pt）
    
    Returns:
        裁切后的 clip (fitz.Rect)
    """
    if fitz is None:
        return clip
    
    if clip.height <= 1 or clip.width <= 1:
        return clip

    # 哪个边缘靠近图注？above: 近端底部; below: 近端顶部
    near_is_top = (direction == 'below')
    frac = 0.35
    new_top, new_bottom = clip.y0, clip.y1
    
    for (lb, size_est, text) in text_lines:
        if not text.strip():
            continue
        # 仅考虑水平重叠且在 clip 头部区域内的行
        inter = lb & clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        # 段落启发式过滤
        width_ok = (inter.width / max(1.0, clip.width)) >= width_ratio
        size_ok = (font_min <= size_est <= font_max)
        if not (width_ok and size_ok):
            continue
        # 近端判定：'below' 只考虑顶部区域，'above' 只考虑底部区域
        if near_is_top:
            top_thresh = clip.y0 + max(40.0, frac * clip.height)
            if lb.y1 > top_thresh:
                continue
        else:
            bot_thresh = clip.y1 - max(40.0, frac * clip.height)
            if lb.y0 < bot_thresh:
                continue
        # 邻接图注判定：靠近图注的文本很可能是正文
        near_caption = False
        if near_is_top:
            dist = caption_rect.y0 - lb.y1
            if 0 <= dist <= adjacent_th:
                near_caption = True
        else:
            dist = lb.y0 - caption_rect.y1
            if 0 <= dist <= adjacent_th:
                near_caption = True
        if not near_caption:
            # 即使不相邻，如果行紧贴页边距，也考虑裁切
            if abs(lb.x0 - page_rect.x0) < 6.5 or abs(page_rect.x1 - lb.x1) < 6.5:
                near_caption = True
        if not near_caption:
            continue

        if near_is_top:
            new_top = max(new_top, lb.y1 + gap)
        else:
            new_bottom = min(new_bottom, lb.y0 - gap)

    # 强制最小高度
    min_h = 40.0
    max_trim_ratio = 0.25
    base_h = clip.height
    if near_is_top and new_top > clip.y0:
        new_top = min(new_top, clip.y0 + max(min_h, max_trim_ratio * base_h))
        if new_bottom - new_top >= min_h:
            clip = fitz.Rect(clip.x0, new_top, clip.x1, clip.y1)
    if (not near_is_top) and new_bottom < clip.y1:
        new_bottom = max(new_bottom, clip.y1 - max(min_h, max_trim_ratio * base_h))
        if new_bottom - new_top >= min_h:
            clip = fitz.Rect(clip.x0, clip.y0, clip.x1, new_bottom)
    # 限制在页面范围内
    clip = fitz.Rect(clip.x0, max(page_rect.y0, clip.y0), clip.x1, min(page_rect.y1, clip.y1))
    return clip


# ============================================================================
# Phase A/B/C: 增强文本裁切
# ============================================================================

def trim_clip_head_by_text_v2(
    clip: Any,
    page_rect: Any,
    caption_rect: Any,
    direction: str,
    text_lines: List[Tuple[Any, float, str]],
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
    far_side_min_dist: float = 50.0,
    far_side_para_min_ratio: float = 0.12,
    # Adaptive line height
    typical_line_h: Optional[float] = None,
    # 表格保护 - 跳过 adjacent sweep
    skip_adjacent_sweep: bool = False,
    # Debug
    debug: bool = False,
) -> Any:
    """
    Phase A/B/C 完整版：增强双阈值文本裁切。
    
    Phase A: 裁切相邻文本 (<adjacent_th, 默认 24pt)
    Phase B: 检测并移除远距离文本块 (adjacent_th ~ far_text_th)
    Phase C: 检测并移除远端大段落
    
    Args:
        clip: 当前裁剪区域
        page_rect: 页面边界
        caption_rect: 图注边界
        direction: 方向 ('above' | 'below')
        text_lines: 文本行列表 [(rect, font_size, text), ...]
        width_ratio: 段落判定宽度比
        font_min/font_max: 正文字号范围
        gap: 裁切后保留的间隙
        adjacent_th: 相邻判定阈值
        far_text_th: 远距离文本检测最大距离
        far_text_para_min_ratio: 触发远距离裁切的最小段落覆盖率
        far_text_trim_mode: 'aggressive' 或 'conservative'
        far_side_min_dist: 远端段落最小距离
        far_side_para_min_ratio: 远端段落最小覆盖率
        typical_line_h: 典型行高（用于自适应检测）
        skip_adjacent_sweep: 跳过相邻扫描（表格保护）
        debug: 调试输出
    
    Returns:
        裁切后的 clip
    """
    if fitz is None:
        return clip
    
    if clip.height <= 1 or clip.width <= 1:
        return clip
    
    # 保存原始 clip 用于后续检测
    original_clip = fitz.Rect(clip)
    
    # === Phase A: 应用基础相邻文本裁切 ===
    clip = trim_clip_head_by_text(
        clip, page_rect, caption_rect, direction, text_lines,
        width_ratio=width_ratio, font_min=font_min, font_max=font_max,
        gap=gap, adjacent_th=adjacent_th
    )
    
    # === Phase A+: 增强"精确两行"检测 ===
    if typical_line_h is not None and typical_line_h > 0:
        near_is_top_a = (direction == 'below')
        # 定义近端检测条带
        if near_is_top_a:
            check_strip = fitz.Rect(
                original_clip.x0,
                original_clip.y0,
                original_clip.x1,
                min(original_clip.y1, original_clip.y0 + 3.5 * typical_line_h)
            )
        else:
            check_strip = fitz.Rect(
                original_clip.x0,
                max(original_clip.y0, original_clip.y1 - 3.5 * typical_line_h),
                original_clip.x1,
                original_clip.y1
            )
        
        # 检测是否恰好有 2 行文字
        is_exact_two, matched_lines = detect_exact_n_lines_of_text(
            check_strip, text_lines, typical_line_h, n=2, tolerance=0.35
        )
        
        if is_exact_two and len(matched_lines) == 2:
            # 检查匹配到的文字是否属于图注本身
            if is_caption_text(matched_lines, caption_rect, tolerance=10.0):
                pass  # 跳过裁切，保留图注
            else:
                # 使用更激进的裁切
                if near_is_top_a:
                    new_y0 = matched_lines[-1].y1 + gap
                    clip = fitz.Rect(clip.x0, max(clip.y0, new_y0), clip.x1, clip.y1)
                else:
                    new_y1 = matched_lines[0].y0 - gap
                    clip = fitz.Rect(clip.x0, clip.y0, clip.x1, min(clip.y1, new_y1))
    
    # === Phase B: 检测并裁切远距离文本 ===
    near_is_top = (direction == 'below')
    
    # 收集远距离段落行（使用原始 clip）
    far_para_lines: List[Tuple[Any, float, str]] = []
    for (lb, size_est, text) in text_lines:
        if not text.strip():
            continue
        inter = lb & original_clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        width_ok = (inter.width / max(1.0, original_clip.width)) >= width_ratio
        size_ok = (font_min <= size_est <= font_max)
        if not (width_ok and size_ok):
            continue
        
        # 到图注的距离（远距离范围：adjacent_th ~ far_text_th）
        if near_is_top:
            dist = caption_rect.y0 - lb.y1
        else:
            dist = lb.y0 - caption_rect.y1
        
        if adjacent_th < dist <= far_text_th:
            if near_is_top:
                top_thresh = original_clip.y0 + max(40.0, 0.5 * original_clip.height)
                if lb.y1 <= top_thresh:
                    far_para_lines.append((lb, size_est, text))
            else:
                bot_thresh = original_clip.y1 - max(40.0, 0.5 * original_clip.height)
                if lb.y0 >= bot_thresh:
                    far_para_lines.append((lb, size_est, text))
    
    # 计算近端段落覆盖率
    para_coverage_ratio = 0.0
    if far_para_lines:
        if near_is_top:
            region_start = original_clip.y0
            region_end = original_clip.y0 + max(40.0, 0.5 * original_clip.height)
            region_h = max(1.0, region_end - region_start)
            para_h = sum(lb.height for (lb, _, _) in far_para_lines)
            para_coverage_ratio = para_h / region_h
        else:
            region_start = original_clip.y1 - max(40.0, 0.5 * original_clip.height)
            region_end = original_clip.y1
            region_h = max(1.0, region_end - region_start)
            para_h = sum(lb.height for (lb, _, _) in far_para_lines)
            para_coverage_ratio = para_h / region_h
    
    # === Phase C: 检测并裁切远端大段落 ===
    far_is_top = not near_is_top
    far_side_para_lines: List[Tuple[Any, float, str]] = []
    
    for (lb, size_est, text) in text_lines:
        if not text.strip():
            continue
        inter = lb & original_clip
        if inter.width <= 0 or inter.height <= 0:
            continue
        width_ok = (inter.width / max(1.0, original_clip.width)) >= width_ratio
        size_ok = (font_min <= size_est <= font_max)
        if not (width_ok and size_ok):
            continue
        
        if far_is_top:
            dist = caption_rect.y0 - lb.y1
        else:
            dist = lb.y0 - caption_rect.y1
        
        if dist > far_side_min_dist:
            if far_is_top:
                mid_point = original_clip.y0 + 0.5 * original_clip.height
                if lb.y0 < mid_point:
                    far_side_para_lines.append((lb, size_est, text))
            else:
                mid_point = original_clip.y0 + 0.5 * original_clip.height
                if lb.y1 > mid_point:
                    far_side_para_lines.append((lb, size_est, text))
    
    # 处理远端段落
    if far_side_para_lines:
        far_side_para_lines.sort(key=lambda x: x[0].y0)
        if far_is_top:
            far_side_region_start = original_clip.y0
            far_side_region_end = original_clip.y0 + 0.5 * original_clip.height
        else:
            far_side_region_start = original_clip.y0 + 0.5 * original_clip.height
            far_side_region_end = original_clip.y1
        
        far_side_region_height = max(1.0, far_side_region_end - far_side_region_start)
        far_side_total_para_height = sum(lb.height for (lb, _, _) in far_side_para_lines)
        far_side_para_coverage = far_side_total_para_height / far_side_region_height
        
        if far_side_para_coverage >= far_side_para_min_ratio:
            if debug:
                print(f"[DBG] Far-side trim: coverage={far_side_para_coverage:.3f} th={far_side_para_min_ratio}")
            
            trim_ratio = 0.65 if far_side_para_coverage >= 0.15 else 0.50
            
            if far_is_top:
                last_para_y1 = max(lb.y1 for (lb, _, _) in far_side_para_lines)
                new_y0 = last_para_y1 + gap
                max_trim = original_clip.y0 + trim_ratio * original_clip.height
                clip = fitz.Rect(clip.x0, min(new_y0, max_trim), clip.x1, clip.y1)
                
                # 邻近短行清扫
                if not skip_adjacent_sweep:
                    adjacent_zone = max(40.0, 4.0 * (typical_line_h or 12.0))
                    for (lb, size_est, txt) in text_lines:
                        if not txt.strip() or len(txt.strip()) < 3:
                            continue
                        inter = lb & clip
                        if inter.width <= 0 or inter.height <= 0:
                            continue
                        if lb.y0 >= clip.y0 and lb.y0 < clip.y0 + adjacent_zone:
                            w_ok = (inter.width / max(1.0, clip.width)) >= 0.05
                            s_ok = (font_min <= size_est <= font_max)
                            if w_ok and s_ok:
                                candidate_y0 = lb.y1 + gap
                                if candidate_y0 > clip.y0 and candidate_y0 <= max_trim:
                                    clip = fitz.Rect(clip.x0, candidate_y0, clip.x1, clip.y1)
            else:
                first_para_y0 = min(lb.y0 for (lb, _, _) in far_side_para_lines)
                new_y1 = first_para_y0 - gap
                min_trim = original_clip.y1 - trim_ratio * original_clip.height
                clip = fitz.Rect(clip.x0, clip.y0, clip.x1, max(new_y1, min_trim))
                
                if not skip_adjacent_sweep:
                    adjacent_zone = max(40.0, 4.0 * (typical_line_h or 12.0))
                    for (lb, size_est, txt) in text_lines:
                        if not txt.strip() or len(txt.strip()) < 3:
                            continue
                        inter = lb & clip
                        if inter.width <= 0 or inter.height <= 0:
                            continue
                        if lb.y1 <= clip.y1 and lb.y1 > clip.y1 - adjacent_zone:
                            w_ok = (inter.width / max(1.0, clip.width)) >= 0.05
                            s_ok = (font_min <= size_est <= font_max)
                            if w_ok and s_ok:
                                candidate_y1 = lb.y0 - gap
                                if candidate_y1 < clip.y1 and candidate_y1 >= min_trim:
                                    clip = fitz.Rect(clip.x0, clip.y0, clip.x1, candidate_y1)
            
            # 迭代扫描短行文字
            if not skip_adjacent_sweep:
                max_iterations = 5
                for _iter in range(max_iterations):
                    _extra_short_lines: List[Any] = []
                    for (lb, size_est, text) in text_lines:
                        txt = text.strip()
                        if not txt or len(txt) < 5:
                            continue
                        inter = lb & clip
                        if inter.width <= 0 or inter.height <= 0:
                            continue
                        if far_is_top:
                            far_region_end = clip.y0 + 0.5 * clip.height
                            in_far = (lb.y0 < far_region_end)
                        else:
                            far_region_start = clip.y1 - 0.5 * clip.height
                            in_far = (lb.y1 > far_region_start)
                        if not in_far:
                            continue
                        w_ratio_extra = inter.width / max(1.0, clip.width)
                        if w_ratio_extra < 0.08:
                            continue
                        if not (font_min <= size_est <= font_max):
                            continue
                        _extra_short_lines.append(lb)
                    
                    if not _extra_short_lines:
                        break
                    
                    if far_is_top:
                        new_y0 = max(lb.y1 for lb in _extra_short_lines) + gap
                        max_trim2 = original_clip.y0 + trim_ratio * original_clip.height
                        if new_y0 > clip.y0 + 1e-3:
                            clip = fitz.Rect(clip.x0, min(new_y0, max_trim2), clip.x1, clip.y1)
                        else:
                            break
                    else:
                        new_y1 = min(lb.y0 for lb in _extra_short_lines) - gap
                        min_trim2 = original_clip.y1 - trim_ratio * original_clip.height
                        if new_y1 < clip.y1 - 1e-3:
                            clip = fitz.Rect(clip.x0, clip.y0, clip.x1, max(new_y1, min_trim2))
                        else:
                            break
        else:
            # Fallback: 处理散落的远端文字
            fallback_lines: List[Any] = []
            for (lb, size_est, text) in text_lines:
                if not text.strip():
                    continue
                inter = lb & original_clip
                if inter.width <= 0 or inter.height <= 0:
                    continue
                txt = text.strip()
                has_bullet = txt.startswith('•') or txt.startswith('·') or txt.startswith('- ') or txt.startswith('○') or txt.startswith('–')
                is_very_long_line = len(txt) > 60
                is_long_line = len(txt) > 30
                
                if has_bullet or is_very_long_line:
                    pass
                else:
                    width_ok_small = (inter.width / max(1.0, original_clip.width)) >= max(0.10, width_ratio * 0.3)
                    size_ok = (font_min <= size_est <= font_max)
                    if not (width_ok_small and size_ok):
                        continue
                
                if far_is_top:
                    dist = caption_rect.y0 - lb.y1
                    in_far_region = (lb.y0 < original_clip.y0 + 0.50 * original_clip.height)
                else:
                    dist = lb.y0 - caption_rect.y1
                    in_far_region = (lb.y1 > original_clip.y0 + 0.50 * original_clip.height)
                
                should_trim = False
                if has_bullet:
                    should_trim = (dist > 15.0 and in_far_region)
                elif is_very_long_line:
                    should_trim = (dist > 18.0 and in_far_region)
                elif is_long_line:
                    should_trim = (dist > 20.0 and in_far_region)
                else:
                    should_trim = (dist > max(25.0, far_side_min_dist * 0.7) and in_far_region)
                
                if should_trim:
                    fallback_lines.append(lb)
            
            if fallback_lines:
                if far_is_top:
                    new_y0 = max(lb.y1 for lb in fallback_lines) + gap
                    max_trim = original_clip.y0 + 0.5 * original_clip.height
                    clip = fitz.Rect(clip.x0, min(new_y0, max_trim), clip.x1, clip.y1)
                else:
                    new_y1 = min(lb.y0 for lb in fallback_lines) - gap
                    min_trim = original_clip.y1 - 0.5 * original_clip.height
                    clip = fitz.Rect(clip.x0, clip.y0, clip.x1, max(new_y1, min_trim))
    
    # 处理 Phase B（近端远距离文本）
    if far_para_lines and para_coverage_ratio >= far_text_para_min_ratio:
        if far_text_trim_mode == "aggressive":
            if near_is_top:
                last_para_y1 = max(lb.y1 for (lb, _, _) in far_para_lines)
                new_y0 = last_para_y1 + gap
                max_trim = original_clip.y0 + 0.6 * original_clip.height
                clip = fitz.Rect(clip.x0, min(new_y0, max_trim), clip.x1, clip.y1)
            else:
                first_para_y0 = min(lb.y0 for (lb, _, _) in far_para_lines)
                new_y1 = first_para_y0 - gap
                min_trim = original_clip.y1 - 0.6 * original_clip.height
                clip = fitz.Rect(clip.x0, clip.y0, clip.x1, max(new_y1, min_trim))
        elif far_text_trim_mode == "conservative":
            is_continuous = True
            for i in range(len(far_para_lines) - 1):
                gap_between = far_para_lines[i+1][0].y0 - far_para_lines[i][0].y1
                if gap_between > 20.0:
                    is_continuous = False
                    break
            if is_continuous:
                if near_is_top:
                    last_para_y1 = max(lb.y1 for (lb, _, _) in far_para_lines)
                    new_y0 = last_para_y1 + gap
                    max_trim = original_clip.y0 + 0.6 * original_clip.height
                    clip = fitz.Rect(clip.x0, min(new_y0, max_trim), clip.x1, clip.y1)
                else:
                    first_para_y0 = min(lb.y0 for (lb, _, _) in far_para_lines)
                    new_y1 = first_para_y0 - gap
                    min_trim = original_clip.y1 - 0.6 * original_clip.height
                    clip = fitz.Rect(clip.x0, clip.y0, clip.x1, max(new_y1, min_trim))
    
    # 强制最小高度
    min_h = 40.0
    if clip.height < min_h:
        return trim_clip_head_by_text(
            fitz.Rect(page_rect.x0, caption_rect.y0 - 600, page_rect.x1, caption_rect.y1 + 600) & page_rect,
            page_rect, caption_rect, direction, text_lines,
            width_ratio=width_ratio, font_min=font_min, font_max=font_max,
            gap=gap, adjacent_th=adjacent_th
        )
    
    # 限制在页面范围内
    clip = fitz.Rect(clip.x0, max(page_rect.y0, clip.y0), clip.x1, min(page_rect.y1, clip.y1))
    return clip


# ============================================================================
# 向后兼容别名
# ============================================================================

_merge_rects = merge_rects
_refine_clip_by_objects = refine_clip_by_objects
_build_text_masks_px = build_text_masks_px
_detect_far_side_text_evidence = detect_far_side_text_evidence
_trim_far_side_text_post_autocrop = trim_far_side_text_post_autocrop
_adaptive_acceptance_thresholds = adaptive_acceptance_thresholds
_is_caption_text = is_caption_text
_detect_exact_n_lines_of_text = detect_exact_n_lines_of_text
_trim_clip_head_by_text = trim_clip_head_by_text
_trim_clip_head_by_text_v2 = trim_clip_head_by_text_v2


# ============================================================================
# 模块导出
# ============================================================================

__all__ = [
    # 像素级内容检测
    "detect_content_bbox_pixels",
    "estimate_ink_ratio",
    # 矩形合并
    "merge_rects",
    # 对象裁剪优化
    "refine_clip_by_objects",
    # 文本遮罩
    "build_text_masks_px",
    # 远端正文检测
    "detect_far_side_text_evidence",
    "trim_far_side_text_post_autocrop",
    # 验收阈值
    "adaptive_acceptance_thresholds",
    # 边缘对齐
    "snap_clip_edges",
    # 文本裁切辅助
    "is_caption_text",
    "detect_exact_n_lines_of_text",
    # Phase A 文本裁切
    "trim_clip_head_by_text",
    "trim_clip_head_by_text_v2",
]
