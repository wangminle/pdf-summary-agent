#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 05: 输出与索引管理

从 extract_pdf_assets.py 抽离的输出和索引相关代码。

包含：
- write_manifest: 写入 CSV 清单
- load_index_json_items: 加载 index.json 中的 items
- prune_unindexed_images: 清理未索引的图片
- get_unique_path: 获取唯一文件路径（处理文件名碰撞）
- write_index_json: 写入扩展版 index.json
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

# 避免循环导入
if TYPE_CHECKING:
    from .models import (
        AttachmentRecord,
        DocumentLayoutModel,
        PDFValidationResult,
        QualityIssue,
    )

# 模块日志器
logger = logging.getLogger(__name__)


# ============================================================================
# CSV 清单输出
# ============================================================================

def write_manifest(
    records: List["AttachmentRecord"],
    manifest_path: Optional[str]
) -> Optional[str]:
    """
    将导出的图/表信息写入 CSV 清单。
    
    Args:
        records: 图/表提取记录列表
        manifest_path: 清单输出路径（为 None 则不输出）
    
    Returns:
        写入的文件路径，如果未写入则返回 None
    """
    if not manifest_path:
        return None
    
    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    if base_dir:
        os.makedirs(base_dir, exist_ok=True)
    
    with open(manifest_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        # 统一为 (type, id, page, caption, file, continued)
        w.writerow(["type", "id", "page", "caption", "file", "continued"])
        for r in records:
            rel = os.path.relpath(os.path.abspath(r.out_path), base_dir).replace('\\', '/')
            w.writerow([r.kind, r.ident, r.page, r.caption, rel, int(r.continued)])
    
    logger.info(f"Wrote manifest: {manifest_path} (items={len(records)})")
    return manifest_path


# ============================================================================
# index.json 读写
# ============================================================================

def load_index_json_items(index_json_path: str) -> List[Dict[str, Any]]:
    """
    兼容层：从 index.json 中加载 items 列表，同时支持旧格式（list）和新格式（dict）。
    
    旧格式: [{"type": ..., "id": ..., "file": ...}, ...]
    新格式: {"version": "2.0", "items": [...], "figures": [...], "tables": [...], ...}
    
    Args:
        index_json_path: index.json 文件路径
    
    Returns:
        items 列表
    """
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
    """
    删除 out_dir 中未被 index.json 引用的 Figure_*/Table_* PNG 文件。
    
    Args:
        out_dir: 图片输出目录
        index_json_path: index.json 文件路径
    
    Returns:
        删除的文件数量
    """
    try:
        base_dir = os.path.dirname(os.path.abspath(index_json_path))
        items = load_index_json_items(index_json_path)
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
                logger.warning(
                    f"Failed to remove file during prune: {abs_path}: {e}",
                    extra={'stage': 'prune_images'}
                )
        return removed
    except Exception as e:
        logger.warning(f"Prune failed: {e}", extra={'stage': 'prune_images'})
        return 0


# ============================================================================
# 文件名碰撞处理
# ============================================================================

def get_unique_path(base_path: str) -> Tuple[str, bool]:
    """
    检查文件路径是否存在，如果存在则追加后缀 _1, _2, ... 直到找到唯一路径。
    
    Args:
        base_path: 基础文件路径
    
    Returns:
        (unique_path, had_collision) 元组：
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


# ============================================================================
# 扩展版 index.json 写入（P1-06）
# ============================================================================

# 全局运行 ID（用于关联日志和输出）
_RUN_ID: Optional[str] = None


def get_run_id() -> str:
    """获取当前运行的唯一 ID"""
    global _RUN_ID
    if _RUN_ID is None:
        _RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _RUN_ID


def set_run_id(run_id: str) -> None:
    """设置运行 ID（用于测试或外部指定）"""
    global _RUN_ID
    _RUN_ID = run_id


def write_index_json(
    records: List["AttachmentRecord"],
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
    
    Args:
        records: 图/表提取记录列表
        index_path: index.json 输出路径
        pdf_path: 源 PDF 文件路径
        preset: 使用的预设名称
        run_id: 运行 ID
        log_jsonl: 结构化日志文件路径
        layout_model: 版式模型
        validation: PDF 验证结果
        qc_issues: 质量问题列表
        extractor_version: 提取器版本
    
    Returns:
        写入的文件路径
    """
    base_dir = os.path.dirname(os.path.abspath(index_path))
    os.makedirs(base_dir, exist_ok=True)
    
    # 计算 PDF 文件哈希（用于可复现性验证）
    pdf_hash = ""
    pdf_pages = 0
    if pdf_path and os.path.exists(pdf_path):
        try:
            with open(pdf_path, 'rb') as f:
                pdf_hash = f"sha256:{hashlib.sha256(f.read()).hexdigest()[:16]}"
            # 延迟导入以避免循环依赖
            from .pdf_backend import open_pdf
            with open_pdf(pdf_path) as doc:
                pdf_pages = len(doc)
        except Exception as e:
            logger.warning(
                f"Failed to compute PDF hash/pages: {e}",
                extra={'stage': 'write_index_json'}
            )
    
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


# ============================================================================
# 便捷导出（向后兼容）
# ============================================================================

# 向后兼容的别名
_load_index_json_items = load_index_json_items
