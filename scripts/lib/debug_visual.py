#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 06: 调试可视化

从 extract_pdf_assets.py 抽离的调试可视化相关代码。

包含：
- draw_rects_on_pix: 在位图上绘制矩形边框
- dump_page_candidates: 保存候选区域调试图
- save_debug_visualization: 保存多阶段边界框可视化
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

# 尝试导入 fitz
try:
    import fitz
except ImportError:
    fitz = None  # type: ignore

# 避免循环导入
if TYPE_CHECKING:
    from .models import DebugStageInfo, DocumentLayoutModel

# 模块日志器
logger = logging.getLogger(__name__)


# ============================================================================
# 颜色定义（用于调试可视化）
# ============================================================================

# 阶段颜色方案
STAGE_COLORS = {
    'baseline': (0, 102, 255),      # 蓝色 - 锚点选择阶段的原始窗口
    'phase_a': (0, 200, 0),         # 绿色 - 文本裁切后
    'phase_b': (255, 165, 0),       # 橙色 - 对象对齐后
    'phase_d': (255, 0, 0),         # 红色 - Autocrop 最终窗口
    'fallback': (255, 255, 0),      # 黄色 - 验收失败回退
    'caption': (148, 0, 211),       # 紫色 - 图注位置
    'title': (255, 105, 180),       # 粉红 - 章节标题
    'paragraph': (255, 105, 180),   # 粉红 - 正文段落
}


# ============================================================================
# 位图绘制
# ============================================================================

def draw_rects_on_pix(
    pix: "fitz.Pixmap",
    rects: List[Tuple[Any, Tuple[int, int, int]]],
    *,
    scale: float,
    line_width: int = 1
) -> None:
    """
    在位图上原地绘制彩色矩形边框。
    
    Args:
        pix: PyMuPDF 位图对象
        rects: 矩形列表，每个元素为 (rect, (r, g, b)) 颜色元组
        scale: 缩放比例（pt -> px）
        line_width: 边框线宽（默认 1）
    """
    if fitz is None:
        logger.warning("PyMuPDF not available, skipping rect drawing")
        return
    
    # 确保无 alpha 通道
    if pix.alpha:
        tmp = fitz.Pixmap(fitz.csRGB, pix)
        pix = tmp
    
    w, h = pix.width, pix.height
    n = pix.n
    
    # 转换为可变的 bytearray 以便修改像素
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
        
        # 绘制带有线宽的边框
        for offset in range(line_width):
            # 顶部和底部边缘
            for x in range(lx, rx + 1):
                set_px(x, ty + offset, col)
                set_px(x, by - offset, col)
            # 左侧和右侧边缘
            for y in range(ty, by + 1):
                set_px(lx + offset, y, col)
                set_px(rx - offset, y, col)
    
    # 将修改后的 samples 写回位图
    pix.set_samples(bytes(samples))


def dump_page_candidates(
    page: "fitz.Page",
    out_path: str,
    *,
    candidates: List[Tuple[float, str, Any]],
    best: Tuple[float, str, Any],
    caption_rect: Any,
) -> Optional[str]:
    """
    调试：保存页面上的候选区域可视化图。
    
    Args:
        page: PyMuPDF 页面对象
        out_path: 输出图片路径
        candidates: 候选列表 [(score, side, rect), ...]
        best: 最佳候选 (score, side, rect)
        caption_rect: 图注边界框
    
    Returns:
        保存的文件路径，失败返回 None
    """
    if fitz is None:
        logger.warning("PyMuPDF not available, skipping candidate dump")
        return None
    
    try:
        scale = 1.0
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        rects: List[Tuple[Any, Tuple[int, int, int]]] = []
        
        # Caption 用蓝色
        rects.append((caption_rect, (0, 102, 255)))
        
        # 候选区域用红色（显示前 10 个）
        for sc, side, r in candidates[:10]:
            rects.append((r, (255, 85, 85)))
        
        # 最佳候选用绿色（最后绘制覆盖其他）
        rects.append((best[2], (0, 200, 0)))
        
        draw_rects_on_pix(pix, rects, scale=scale, line_width=1)
        pix.save(out_path)
        return out_path
    
    except Exception as e:
        page_no = getattr(page, "number", None)
        extra = {'stage': 'dump_page_candidates'}
        if isinstance(page_no, int):
            extra['page'] = page_no + 1
        logger.warning(f"Failed to dump page candidates: {e}", extra=extra)
        return None


# ============================================================================
# 多阶段可视化
# ============================================================================

def save_debug_visualization(
    page: "fitz.Page",
    out_dir: str,
    fig_no: int,
    page_num: int,
    *,
    stages: List["DebugStageInfo"],
    caption_rect: Any,
    kind: str = 'figure',
    layout_model: Optional["DocumentLayoutModel"] = None,
    run_id: Optional[str] = None,
) -> Optional[List[str]]:
    """
    保存带多色线框的调试可视化图片。
    
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
    if fitz is None:
        logger.warning("PyMuPDF not available, skipping debug visualization")
        return None
    
    try:
        # QA-03: 使用 run_id 创建隔离的 debug 目录
        if run_id:
            debug_dir = os.path.join(out_dir, "debug", run_id)
        else:
            debug_dir = os.path.join(out_dir, "debug")
        os.makedirs(debug_dir, exist_ok=True)
        
        # 创建临时 PDF 文档用于绘图
        src_doc = page.parent
        temp_doc = fitz.open()
        temp_page = temp_doc.new_page(width=page.rect.width, height=page.rect.height)
        
        # 先渲染原始页面内容（2x 分辨率）
        scale_render = 2.0
        pix = page.get_pixmap(matrix=fitz.Matrix(scale_render, scale_render), alpha=False)
        
        # 在临时页面上插入原始页面的图像
        temp_page.insert_image(temp_page.rect, pixmap=pix)
        
        # 绘制边界框（按从大到小排序，确保小的框在上面）
        sorted_stages = sorted(stages, key=lambda s: s.bbox.width * s.bbox.height, reverse=True)
        
        shape = temp_page.new_shape()
        
        # 绘制所有阶段的边界框
        for stage in sorted_stages:
            r = stage.bbox
            color_normalized = tuple(c / 255.0 for c in stage.color)
            shape.draw_rect(r)
            shape.finish(color=color_normalized, width=3)
        
        # 绘制文本区块（如果提供了 layout_model）
        text_blocks_drawn = []
        if layout_model is not None:
            pno_zero_based = page_num - 1
            text_blocks = layout_model.text_blocks.get(pno_zero_based, [])
            pink_color = (255/255.0, 105/255.0, 180/255.0)  # Hot Pink
            
            for block in text_blocks:
                if block.block_type in ['paragraph_group', 'list_group']:
                    # 段落/列表：粉红色虚线
                    shape.draw_rect(block.bbox)
                    shape.finish(color=pink_color, width=2, dashes=[3, 3])
                    text_blocks_drawn.append(block)
                elif block.block_type.startswith('title_'):
                    # 标题：粉红色实线
                    shape.draw_rect(block.bbox)
                    shape.finish(color=pink_color, width=2)
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
        _write_legend_file(
            legend_path,
            prefix,
            fig_no,
            page_num,
            caption_rect,
            stages,
            text_blocks_drawn
        )
        
        print(f"[DEBUG] Saved visualization: {vis_path}")
        print(f"[DEBUG] Saved legend: {legend_path}")

        # QA-03: 返回相对 out_dir 的稳定路径
        rel_vis = os.path.relpath(os.path.abspath(vis_path), os.path.abspath(out_dir)).replace('\\', '/')
        rel_legend = os.path.relpath(os.path.abspath(legend_path), os.path.abspath(out_dir)).replace('\\', '/')
        return [rel_vis, rel_legend]
    
    except Exception as e:
        logger.warning(f"Debug visualization failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def _write_legend_file(
    legend_path: str,
    prefix: str,
    fig_no: int,
    page_num: int,
    caption_rect: Any,
    stages: List["DebugStageInfo"],
    text_blocks_drawn: List[Any]
) -> None:
    """
    写入调试图例文件。
    
    Args:
        legend_path: 图例文件路径
        prefix: 前缀（Figure/Table）
        fig_no: 图/表编号
        page_num: 页码
        caption_rect: 图注边界框
        stages: 阶段信息列表
        text_blocks_drawn: 已绘制的文本块列表
    """
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
                # 显示前 80 个字符
                sample_text = " ".join(u.text for u in block.units[:2])
                if len(sample_text) > 80:
                    sample_text = sample_text[:77] + "..."
                f.write(f"  Sample: {sample_text}\n\n")
            
            f.write("=" * 70 + "\n\n")
        
        # 写入阶段信息
        for stage in stages:
            r = stage.bbox
            f.write(f"{stage.name}:\n")
            f.write(f"  Position: {r.x0:.1f},{r.y0:.1f} -> {r.x1:.1f},{r.y1:.1f}\n")
            f.write(f"  Size: {r.width:.1f}×{r.height:.1f}pt ({r.width * r.height / 72.0 / 72.0:.2f} sq.in)\n")
            f.write(f"  Color: RGB{stage.color}\n")
            f.write(f"  Description: {stage.description}\n\n")


# ============================================================================
# 向后兼容别名
# ============================================================================

_draw_rects_on_pix = draw_rects_on_pix
