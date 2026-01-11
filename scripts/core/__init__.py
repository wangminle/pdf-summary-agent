#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/core/ - 核心入口模块

Commit 13: 入口瘦身

本包提供 extract_pdf_assets 的核心入口点：
- extract_pdf_assets: 主入口模块（parse_args, main）

架构说明：
- scripts/lib/: 模块化组件库（数据结构、算法、工具函数）
- scripts/core/: 核心入口（CLI 解析、主流程）
- scripts/extract_pdf_assets.py: 兼容导出层

使用方式：
    # 方式 1：直接使用 core 模块
    from scripts.core.extract_pdf_assets import main, parse_args
    
    # 方式 2：通过兼容层（推荐，保持向后兼容）
    from scripts.extract_pdf_assets import main, parse_args
    
    # 方式 3：命令行运行
    python scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust
"""

from __future__ import annotations

# 从 extract_pdf_assets 导入核心入口
from .extract_pdf_assets import (
    main,
    parse_args,
    # 重新导出的常用符号
    AttachmentRecord,
    DocumentLayoutModel,
    GatheredText,
    write_index_json,
    write_manifest,
    prune_unindexed_images,
    pre_validate_pdf,
    try_extract_text,
    gather_structured_text,
    build_figure_contexts,
)

__all__ = [
    "main",
    "parse_args",
    "AttachmentRecord",
    "DocumentLayoutModel",
    "GatheredText",
    "write_index_json",
    "write_manifest",
    "prune_unindexed_images",
    "pre_validate_pdf",
    "try_extract_text",
    "gather_structured_text",
    "build_figure_contexts",
]
