#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 11: Figure Context 构建

从 text_extract.py 抽离的图表上下文构建代码。

功能：
- build_figure_contexts: 为每个 Figure/Table 建立正文上下文锚点
- 搜索图表在正文中的提及位置
- 提取首次提及附近的文本窗口

这个模块用于支持摘要生成时建立图表与正文的关联。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, List, Optional

# 避免循环导入
if TYPE_CHECKING:
    from .models import (
        AttachmentRecord,
        FigureContext,
        FigureMention,
        GatheredText,
    )

# 模块日志器
logger = logging.getLogger(__name__)


def build_figure_contexts(
    pdf_path: str,
    records: List["AttachmentRecord"],
    gathered_text: Optional["GatheredText"] = None,
    out_json: Optional[str] = None,
    debug: bool = False
) -> List["FigureContext"]:
    """
    为每个 Figure/Table 建立正文上下文锚点。
    
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
        FigureContext 列表
    """
    from .models import FigureContext, FigureMention
    from .text_extract import gather_structured_text
    
    if debug:
        print(f"\n{'='*60}")
        print("Building Figure Contexts")
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
    
    contexts: List[FigureContext] = []
    
    for rec in records:
        ident = rec.ident
        kind = rec.kind.lower()
        caption = rec.caption
        caption_page = rec.page
        
        if debug:
            print(f"\n[DEBUG] Processing {kind} {ident} (page {caption_page})")
        
        # 获取匹配模式
        pattern = mention_patterns.get(kind)
        if not pattern:
            continue
        
        all_mentions: List[FigureMention] = []
        first_mention: Optional[FigureMention] = None
        
        # 在所有段落中搜索提及
        for idx, para in enumerate(paragraphs):
            # 跳过标题段落
            if para.is_heading:
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
                    window_start = max(0, idx - 1)
                    window_end = min(len(paragraphs), idx + 2)
                    text_window = " ".join(p.text for p in paragraphs[window_start:window_end])
                    
                    mention = FigureMention(
                        page=para.page,
                        para_idx=idx,
                        text_window=text_window[:500]  # 限制长度
                    )
                    all_mentions.append(mention)
                    
                    if first_mention is None:
                        first_mention = mention
                        if debug:
                            print(f"  First mention: page {para.page}, para_idx {idx}")
        
        # 提取图注所在页附近的正文窗口
        caption_page_paras = [p for p in paragraphs if p.page == caption_page and not p.is_heading]
        caption_text_window = " ".join(p.text for p in caption_page_paras[:3])[:500] if caption_page_paras else ""
        
        contexts.append(FigureContext(
            kind=kind,
            ident=ident,
            first_mention=first_mention,
            all_mentions=all_mentions,
            caption_page_text_window=caption_text_window
        ))
        
        if debug:
            print(f"  Total mentions: {len(all_mentions)}")
    
    # 输出 JSON
    if out_json:
        output = [
            {
                "kind": ctx.kind,
                "ident": ctx.ident,
                "first_mention": {
                    "page": ctx.first_mention.page,
                    "para_idx": ctx.first_mention.para_idx,
                    "text_window": ctx.first_mention.text_window
                } if ctx.first_mention else None,
                "all_mentions_count": len(ctx.all_mentions),
                "caption_page_text_window": ctx.caption_page_text_window
            }
            for ctx in contexts
        ]
        out_dir = os.path.dirname(out_json)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        
        if debug:
            print(f"\n[INFO] Wrote figure contexts: {out_json} ({len(contexts)} items)")
    
    return contexts


# ============================================================================
# 向后兼容别名
# ============================================================================

# 保持与旧代码的兼容性
_build_figure_contexts = build_figure_contexts
