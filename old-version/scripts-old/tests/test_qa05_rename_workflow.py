#!/usr/bin/env python3
"""
QA-05 回归：重命名工作流联动

验证点：
1) write_index_json() 写入 original_file / current_file（初始等于 file）
2) 手工/AI 重命名 PNG 后，sync_index_after_rename.py 能更新 file/current_file
3) original_file 保留为初始文件名（便于审计与回滚）
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# 支持从项目根目录运行或从 scripts/tests 目录运行
import os
_scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
# 兼容旧路径
if "scripts" not in sys.path:
    sys.path.insert(0, "scripts")

from extract_pdf_assets import AttachmentRecord, write_index_json  # noqa: E402


def test_rename_sync_preserves_original_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        images_dir = root / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        old_name = "Figure_1_Overview.png"
        new_name = "Figure_1_Multimodal_Transformer_Architecture.png"
        (images_dir / old_name).write_bytes(b"\x89PNG\r\n\x1a\n")

        record = AttachmentRecord(
            kind="figure",
            ident="1",
            page=1,
            caption="Figure 1: Overview",
            out_path=str(images_dir / old_name),
            continued=False,
        )

        index_path = images_dir / "index.json"
        write_index_json([record], str(index_path), pdf_path=None, preset="robust")

        data0 = json.loads(index_path.read_text(encoding="utf-8"))
        assert data0.get("items") and len(data0["items"]) == 1
        item0 = data0["items"][0]
        assert item0.get("file") == old_name
        assert item0.get("original_file") == old_name
        assert item0.get("current_file") == old_name

        # 重命名文件
        (images_dir / old_name).rename(images_dir / new_name)

        # 同步 index.json
        cmd = [sys.executable, str(Path("scripts") / "core" / "sync_index_after_rename.py"), str(root)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr[-1000:]

        data1 = json.loads(index_path.read_text(encoding="utf-8"))
        assert data1.get("items") and len(data1["items"]) == 1
        item1 = data1["items"][0]
        assert item1.get("file") == new_name
        assert item1.get("current_file") == new_name
        assert item1.get("original_file") == old_name


def main() -> int:
    tests = [test_rename_sync_preserves_original_file]
    passed = 0
    failed = 0

    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"? 失败: {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"? 错误: {t.__name__}: {e}")
            failed += 1

    print(f"\n测试结果: {passed} 通过, {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

