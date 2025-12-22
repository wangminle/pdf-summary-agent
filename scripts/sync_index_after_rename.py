#!/usr/bin/env python3
"""
Sync images/index.json after manual/AI renaming of Figure_/Table_ PNGs.

Typical workflow:
  1) extract -> images/index.json + temporary filenames
  2) rename PNGs to final descriptive names (keep Figure_N_/Table_N_ prefix)
  3) run this script to update index.json "file" fields
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


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

    items = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        print(f"[ERROR] index.json is not a list: {index_path}")
        return 2

    changed = 0
    missing = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        resolved = resolve_image_file(images_dir, it)
        if not resolved:
            missing += 1
            continue
        new_rel = resolved.relative_to(images_dir).as_posix()
        old_rel = (it.get("file") or "").replace("\\", "/")
        if new_rel != old_rel:
            changed += 1
            print(f"[UPDATE] {it.get('type')} {it.get('id')} p{it.get('page')}: '{old_rel}' -> '{new_rel}'")
            it["file"] = new_rel

    if args.dry_run:
        print(f"[DRY-RUN] Would update {changed} entries, missing={missing}")
        return 0

    index_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] Synced index: {index_path} (updated={changed}, missing={missing})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
