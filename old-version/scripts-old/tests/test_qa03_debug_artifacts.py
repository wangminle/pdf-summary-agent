#!/usr/bin/env python3
"""
QA-03 回归：debug 输出与 index.json 关联（debug_artifacts 字段）

目标：
1) 当 AttachmentRecord.debug_artifacts 非空时，write_index_json() 会写入到 items/figures/tables 中
2) 路径保持为相对 images(out_dir) 的相对路径
"""

import json
import os
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


def test_debug_artifacts_written() -> None:
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td) / "images"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 模拟导出的 PNG
        png_path = out_dir / "Figure_1_Test.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n")

        record = AttachmentRecord(
            kind="figure",
            ident="1",
            page=1,
            caption="Figure 1: Test",
            out_path=str(png_path),
            continued=False,
            debug_artifacts=["debug/Figure_1_p1_debug_stages.png", "debug/Figure_1_p1_legend.txt"],
        )

        index_path = out_dir / "index.json"
        write_index_json([record], str(index_path), pdf_path=None, preset="robust")

        data = json.loads(index_path.read_text(encoding="utf-8"))
        assert "items" in data and isinstance(data["items"], list) and len(data["items"]) == 1
        item = data["items"][0]
        assert item.get("type") == "figure"
        assert item.get("id") == "1"
        assert item.get("debug_artifacts") == record.debug_artifacts


def main() -> int:
    tests = [test_debug_artifacts_written]
    passed = 0
    failed = 0

    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"❌ 失败: {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ 错误: {t.__name__}: {e}")
            failed += 1

    print(f"\n测试结果: {passed} 通过, {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

