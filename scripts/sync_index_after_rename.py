#!/usr/bin/env python3
"""
Sync images/index.json after manual/AI renaming of Figure_/Table_ PNGs.

P1-10 + QA-05 增强：支持记录 original_file 和 current_file 映射关系，便于审计与回滚。

Typical workflow:
  1) extract -> images/index.json + temporary filenames
  2) rename PNGs to final descriptive names (keep Figure_N_/Table_N_ prefix)
  3) run this script to update index.json "file" fields

New features (P1-10):
  - Records original_file and current_file in index.json
  - Validates all files exist after sync
  - Warns on missing files
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_index_json(index_path: Path) -> tuple[list[dict], dict | None]:
    """
    兼容层：加载 index.json，同时支持旧格式（list）和新格式（dict）。
    
    Returns:
        (items 列表, 原始 dict 结构 或 None)
        - 如果是旧格式（list），返回 (items, None)
        - 如果是新格式（dict），返回 (items, original_dict)
    """
    data = json.loads(index_path.read_text(encoding="utf-8"))
    
    if isinstance(data, list):
        # 旧格式：直接是 items 列表
        return data, None
    elif isinstance(data, dict):
        # 新格式：从 "items" 字段获取，或合并 "figures" + "tables"
        if "items" in data:
            items = data["items"]
        else:
            items = data.get("figures", []) + data.get("tables", [])
        return items, data
    else:
        return [], None


def _save_index_json(index_path: Path, items: list[dict], original_dict: dict | None) -> None:
    """
    兼容层：保存 index.json，保持原有格式。
    
    Args:
        items: 更新后的 items 列表
        original_dict: 原始 dict 结构（新格式）或 None（旧格式）
    """
    if original_dict is None:
        # 旧格式：直接写 list
        output: Any = items
    else:
        # 新格式：更新 items 和 figures/tables
        output = original_dict.copy()
        output["items"] = items
        
        # 同步更新 figures 和 tables（如果存在）
        if "figures" in output or "tables" in output:
            figures = [it for it in items if (it.get("type") or "").lower() == "figure"]
            tables = [it for it in items if (it.get("type") or "").lower() == "table"]
            output["figures"] = figures
            output["tables"] = tables
    
    index_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_image_file(images_dir: Path, item: dict) -> Path | None:
    rel = (item.get("file") or "").replace("\\", "/")
    if rel:
        p = images_dir / rel
        if p.exists():
            return p

    kind = (item.get("type") or "").lower()
    ident = str(item.get("id") or "").strip()
    page = int(item.get("page") or 0)
    continued = bool(item.get("continued"))
    if kind not in {"figure", "table"} or not ident:
        return None

    prefix = "Figure" if kind == "figure" else "Table"
    candidates = list(images_dir.glob(f"{prefix}_{ident}_*.png"))
    if not candidates:
        return None

    if continued:
        continued_tag = f"continued_p{page}"
        cand2 = [c for c in candidates if continued_tag in c.name]
        if cand2:
            candidates = cand2

    if len(candidates) == 1:
        return candidates[0]

    if not continued:
        cand2 = [c for c in candidates if "continued_p" not in c.name]
        if cand2:
            candidates = cand2

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync images/index.json after renaming Figure_/Table_ PNGs")
    ap.add_argument("pdf_dir", help="PDF directory containing images/index.json (and images/*.png)")
    ap.add_argument("--index", default=None, help="Path to index.json (default: <pdf_dir>/images/index.json)")
    ap.add_argument("--dry-run", action="store_true", help="Only print changes, do not write files")
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir)
    index_path = Path(args.index) if args.index else (pdf_dir / "images" / "index.json")
    images_dir = index_path.parent

    if not index_path.exists():
        print(f"[ERROR] index.json not found: {index_path}")
        return 2

    # 兼容新旧格式读取
    items, original_dict = _load_index_json(index_path)
    if not items and original_dict is None:
        print(f"[ERROR] index.json is empty or invalid: {index_path}")
        return 2

    changed = 0
    missing = 0
    missing_items = []
    
    for it in items:
        if not isinstance(it, dict):
            continue
        resolved = resolve_image_file(images_dir, it)
        if not resolved:
            missing += 1
            missing_items.append(f"{it.get('type')} {it.get('id')} p{it.get('page')}")
            continue
        new_rel = resolved.relative_to(images_dir).as_posix()
        old_rel = (it.get("file") or "").replace("\\", "/")
        
        if new_rel != old_rel:
            changed += 1
            print(f"[UPDATE] {it.get('type')} {it.get('id')} p{it.get('page')}: '{old_rel}' -> '{new_rel}'")
            
            # P1-10 + QA-05: 记录 original_file（仅首次重命名时记录）
            if "original_file" not in it:
                it["original_file"] = old_rel
            
            it["file"] = new_rel
            it["current_file"] = new_rel  # 当前生效文件名

    if args.dry_run:
        print(f"[DRY-RUN] Would update {changed} entries, missing={missing}")
        if missing_items:
            print(f"[WARN] Missing files for: {', '.join(missing_items)}")
        return 0

    # 兼容新旧格式写入
    _save_index_json(index_path, items, original_dict)
    print(f"[OK] Synced index: {index_path} (updated={changed}, missing={missing})")
    
    # 验证一致性
    if missing > 0:
        print(f"[WARN] {missing} items have missing files:")
        for item in missing_items:
            print(f"  - {item}")
        print("[HINT] These entries may need manual attention or re-extraction")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
