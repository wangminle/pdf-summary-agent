#!/usr/bin/env python3
"""
P1-10: 重命名工作流半自动化与可验收闭环

功能：
1. 读取 images/index.json，为每个图表生成重命名建议
2. 检测文件名碰撞（同名、sanitize 后同名、大小写冲突）
3. 生成平台特定的重命名脚本：
   - macOS/Linux: rename_plan.sh
   - Windows/PowerShell: rename_plan.ps1
4. 脚本执行后自动联动 sync_index_after_rename.py

用法：
    # 生成重命名计划（不执行）
    python scripts/generate_rename_plan.py <PDF_DIR>
    
    # 生成并执行（需要手动确认）
    python scripts/generate_rename_plan.py <PDF_DIR> --execute
    
    # 只检查碰撞（dry-run）
    python scripts/generate_rename_plan.py <PDF_DIR> --dry-run

输出文件：
    <PDF_DIR>/rename_plan.sh (macOS/Linux)
    <PDF_DIR>/rename_plan.ps1 (Windows/PowerShell)
    <PDF_DIR>/images/rename_mapping.json (映射记录)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class RenameEntry:
    """重命名条目"""
    kind: str                  # "figure" 或 "table"
    ident: str                 # 图表标识符
    page: int                  # 页码
    caption: str               # 原始图注
    original_file: str         # 原始文件名
    suggested_file: str        # 建议的新文件名
    final_file: Optional[str]  # 最终文件名（碰撞消歧后）
    has_collision: bool        # 是否有碰撞


def _load_index_json(index_path: Path) -> Tuple[List[Dict], Dict | None]:
    """
    兼容层：加载 index.json，同时支持旧格式（list）和新格式（dict）。
    """
    data = json.loads(index_path.read_text(encoding="utf-8"))
    
    if isinstance(data, list):
        return data, None
    elif isinstance(data, dict):
        if "items" in data:
            items = data["items"]
        else:
            items = data.get("figures", []) + data.get("tables", [])
        return items, data
    else:
        return [], None


def sanitize_for_filename(s: str, max_words: int = 12) -> str:
    """
    将字符串清理为安全的文件名部分。
    
    规则：
    - 只保留字母、数字、下划线
    - 移除多余空白，用下划线连接
    - 限制单词数量
    """
    # 规范化 Unicode
    s = unicodedata.normalize('NFKC', s)
    
    # 移除非法字符
    s = re.sub(r'[^\w\s-]', ' ', s)
    
    # 分词并限制数量
    words = s.split()
    words = [w for w in words if w][:max_words]
    
    # 连接并返回
    return '_'.join(words)


def suggest_new_filename(
    kind: str,
    ident: str,
    caption: str,
    original_file: str,
    max_words: int = 12
) -> str:
    """
    生成建议的新文件名。
    
    规则：
    - 保留原有的 Figure_N_ 或 Table_N_ 前缀
    - 基于图注生成描述性后缀
    - 限制单词数量
    """
    prefix = "Figure" if kind.lower() == "figure" else "Table"
    
    # 从图注中提取描述部分
    # 跳过 "Figure 1:" 或 "Table 1." 等开头
    desc = caption
    
    # 移除常见的图注开头模式
    patterns = [
        r'^(?:Figure|Fig\.?|Table|Tab\.?|图|表)\s*[S]?\d+[a-zA-Z]?\s*[:\.。：]?\s*',
        r'^\s*[:\.。：]\s*',
    ]
    for pat in patterns:
        desc = re.sub(pat, '', desc, flags=re.IGNORECASE)
    
    # 清理描述
    desc_clean = sanitize_for_filename(desc, max_words)
    
    if not desc_clean:
        # 如果描述为空，使用原文件名的描述部分
        original_stem = Path(original_file).stem
        # 尝试提取原文件名中的描述部分
        match = re.match(r'(?:Figure|Table)_[S]?\w+_(.+)', original_stem)
        if match:
            desc_clean = match.group(1)
        else:
            desc_clean = "Unnamed"
    
    return f"{prefix}_{ident}_{desc_clean}.png"


def check_collisions(entries: List[RenameEntry]) -> Dict[str, List[RenameEntry]]:
    """
    检测文件名碰撞。
    
    返回：碰撞文件名 -> 涉及的条目列表
    """
    by_name: Dict[str, List[RenameEntry]] = {}
    
    for entry in entries:
        name = entry.suggested_file.lower()  # 大小写不敏感比较
        if name not in by_name:
            by_name[name] = []
        by_name[name].append(entry)
    
    # 只返回有碰撞的
    return {k: v for k, v in by_name.items() if len(v) > 1}


def resolve_collisions(entries: List[RenameEntry]) -> None:
    """
    解决文件名碰撞，通过追加 _1, _2 等后缀。
    """
    collisions = check_collisions(entries)
    
    for name, conflicting in collisions.items():
        for i, entry in enumerate(conflicting):
            entry.has_collision = True
            stem = Path(entry.suggested_file).stem
            entry.final_file = f"{stem}_{i+1}.png"
            print(f"[WARN] Collision detected: {entry.suggested_file} -> {entry.final_file}")
    
    # 没有碰撞的条目
    for entry in entries:
        if entry.final_file is None:
            entry.final_file = entry.suggested_file


def generate_bash_script(entries: List[RenameEntry], pdf_dir: Path, sync_script_path: str) -> str:
    """生成 bash 重命名脚本（macOS/Linux）"""
    lines = [
        "#!/bin/bash",
        "# P1-10: 自动生成的重命名脚本",
        "# 生成时间: " + __import__('datetime').datetime.now().isoformat(),
        "",
        f'PDF_DIR="{pdf_dir}"',
        f'IMAGES_DIR="$PDF_DIR/images"',
        "",
        "# 切换到 images 目录",
        'cd "$IMAGES_DIR" || exit 1',
        "",
        "# 重命名文件",
    ]
    
    renamed_count = 0
    for entry in entries:
        if entry.original_file != entry.final_file:
            renamed_count += 1
            # 转义文件名中的特殊字符
            orig = entry.original_file.replace('"', '\\"')
            new = entry.final_file.replace('"', '\\"')
            lines.append(f'[ -f "{orig}" ] && mv "{orig}" "{new}" && echo "Renamed: {orig} -> {new}"')
    
    if renamed_count == 0:
        lines.append("echo 'No files need renaming.'")
    
    lines.extend([
        "",
        "# 返回 PDF 目录",
        'cd "$PDF_DIR"',
        "",
        "# 同步 index.json",
        f'python3 "{sync_script_path}" "$PDF_DIR"',
        "",
        "echo 'Rename completed. Please verify images/index.json.'",
    ])
    
    return '\n'.join(lines)


def generate_powershell_script(entries: List[RenameEntry], pdf_dir: Path, sync_script_path: str) -> str:
    """生成 PowerShell 重命名脚本（Windows）"""
    lines = [
        "# P1-10: 自动生成的重命名脚本",
        "# 生成时间: " + __import__('datetime').datetime.now().isoformat(),
        "",
        f'$PDF_DIR = "{pdf_dir}"',
        f'$IMAGES_DIR = "$PDF_DIR\\images"',
        "",
        "# 切换到 images 目录",
        'Set-Location $IMAGES_DIR',
        "",
        "# 重命名文件",
    ]
    
    renamed_count = 0
    for entry in entries:
        if entry.original_file != entry.final_file:
            renamed_count += 1
            orig = entry.original_file.replace('"', '`"')
            new = entry.final_file.replace('"', '`"')
            lines.append(f'if (Test-Path "{orig}") {{ Move-Item "{orig}" "{new}"; Write-Host "Renamed: {orig} -> {new}" }}')
    
    if renamed_count == 0:
        lines.append('Write-Host "No files need renaming."')
    
    lines.extend([
        "",
        "# 返回 PDF 目录",
        'Set-Location $PDF_DIR',
        "",
        "# 同步 index.json",
        f'python "{sync_script_path}" $PDF_DIR',
        "",
        'Write-Host "Rename completed. Please verify images\\index.json."',
    ])
    
    return '\n'.join(lines)


def save_rename_mapping(entries: List[RenameEntry], out_path: Path) -> None:
    """保存重命名映射为 JSON"""
    mapping = {
        "version": "1.0",
        "generated_at": __import__('datetime').datetime.now().isoformat(),
        "mappings": [
            {
                "kind": e.kind,
                "ident": e.ident,
                "page": e.page,
                "original_file": e.original_file,
                "suggested_file": e.suggested_file,
                "final_file": e.final_file,
                "has_collision": e.has_collision,
            }
            for e in entries
        ]
    }
    
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="P1-10: 生成重命名计划脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    ap.add_argument("pdf_dir", help="PDF 目录（包含 images/index.json）")
    ap.add_argument("--index", default=None, help="index.json 路径（默认: <pdf_dir>/images/index.json）")
    ap.add_argument("--dry-run", action="store_true", help="只检查碰撞，不生成脚本")
    ap.add_argument("--execute", action="store_true", help="生成脚本后立即执行")
    ap.add_argument("--max-words", type=int, default=12, help="文件名最大单词数（默认: 12）")
    args = ap.parse_args()
    
    pdf_dir = Path(args.pdf_dir).resolve()
    index_path = Path(args.index) if args.index else (pdf_dir / "images" / "index.json")
    images_dir = pdf_dir / "images"
    
    if not index_path.exists():
        print(f"[ERROR] index.json not found: {index_path}")
        return 2
    
    # 加载 index.json
    items, _ = _load_index_json(index_path)
    if not items:
        print(f"[ERROR] index.json is empty or invalid: {index_path}")
        return 2
    
    print(f"[INFO] Loaded {len(items)} items from index.json")
    
    # 构建重命名条目
    entries: List[RenameEntry] = []
    
    for item in items:
        if not isinstance(item, dict):
            continue
        
        kind = (item.get("type") or "").lower()
        ident = str(item.get("id") or "").strip()
        page = int(item.get("page") or 0)
        caption = item.get("caption") or ""
        original_file = (item.get("file") or "").replace("\\", "/")
        
        if not kind or not ident or not original_file:
            continue
        
        # 检查文件是否存在
        original_path = images_dir / original_file
        if not original_path.exists():
            print(f"[WARN] File not found: {original_file}")
            continue
        
        # 生成建议的新文件名
        suggested = suggest_new_filename(kind, ident, caption, original_file, args.max_words)
        
        entry = RenameEntry(
            kind=kind,
            ident=ident,
            page=page,
            caption=caption,
            original_file=original_file,
            suggested_file=suggested,
            final_file=None,
            has_collision=False
        )
        entries.append(entry)
    
    if not entries:
        print("[INFO] No items to rename")
        return 0
    
    # 检测并解决碰撞
    resolve_collisions(entries)
    
    # 统计
    need_rename = sum(1 for e in entries if e.original_file != e.final_file)
    has_collision = sum(1 for e in entries if e.has_collision)
    
    print(f"\n[SUMMARY]")
    print(f"  Total items: {len(entries)}")
    print(f"  Need rename: {need_rename}")
    print(f"  Collisions resolved: {has_collision}")
    
    if args.dry_run:
        print("\n[DRY-RUN] Rename plan:")
        for entry in entries:
            if entry.original_file != entry.final_file:
                status = " [COLLISION]" if entry.has_collision else ""
                print(f"  {entry.original_file} -> {entry.final_file}{status}")
        return 0
    
    # 确定 sync_index_after_rename.py 的路径
    script_dir = Path(__file__).parent
    sync_script_path = script_dir / "sync_index_after_rename.py"
    if not sync_script_path.exists():
        print(f"[WARN] sync_index_after_rename.py not found: {sync_script_path}")
        sync_script_path = Path("scripts/sync_index_after_rename.py")
    
    # 生成脚本
    is_windows = platform.system() == "Windows"
    
    if is_windows:
        script_content = generate_powershell_script(entries, pdf_dir, str(sync_script_path))
        script_path = pdf_dir / "rename_plan.ps1"
    else:
        script_content = generate_bash_script(entries, pdf_dir, str(sync_script_path))
        script_path = pdf_dir / "rename_plan.sh"
    
    with open(script_path, 'w', encoding='utf-8', newline='\n' if not is_windows else None) as f:
        f.write(script_content)
    
    if not is_windows:
        os.chmod(script_path, 0o755)
    
    print(f"\n[OK] Generated rename script: {script_path}")
    
    # 保存映射记录
    mapping_path = images_dir / "rename_mapping.json"
    save_rename_mapping(entries, mapping_path)
    print(f"[OK] Saved rename mapping: {mapping_path}")
    
    # 执行脚本
    if args.execute:
        print(f"\n[EXEC] Running rename script...")
        if is_windows:
            ret = os.system(f'powershell -ExecutionPolicy Bypass -File "{script_path}"')
        else:
            ret = os.system(f'bash "{script_path}"')
        
        if ret == 0:
            print("[OK] Rename script executed successfully")
        else:
            print(f"[WARN] Rename script returned: {ret}")
        return ret
    
    # 提示用户手动执行
    print(f"\n[NEXT STEPS]")
    if is_windows:
        print(f"  1. Review the script: {script_path}")
        print(f'  2. Execute: powershell -ExecutionPolicy Bypass -File "{script_path}"')
    else:
        print(f"  1. Review the script: {script_path}")
        print(f'  2. Execute: bash "{script_path}"')
    print(f"  3. Verify: images/index.json")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

