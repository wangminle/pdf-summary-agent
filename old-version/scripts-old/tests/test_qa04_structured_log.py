#!/usr/bin/env python3
"""
QA-04 回归：结构化日志 run.log.jsonl

验证点：
1) 未显式传 --log-jsonl 时，默认在 out_dir 下生成 run.log.jsonl
2) 至少写入 run_start / run_end 事件（便于批量复盘）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _make_minimal_pdf(pdf_path: Path) -> None:
    try:
        import fitz  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"PyMuPDF not available: {e}")

    doc = fitz.open()
    doc.new_page(width=595, height=842)  # A4
    doc.save(str(pdf_path))
    doc.close()


def test_default_run_log_jsonl_and_events() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pdf_path = root / "mini.pdf"
        out_dir = root / "images"
        out_text = root / "text" / "mini.txt"

        _make_minimal_pdf(pdf_path)

        cmd = [
            sys.executable,
            str(Path("scripts") / "core" / "extract_pdf_assets.py"),
            "--pdf",
            str(pdf_path),
            "--out-dir",
            str(out_dir),
            "--out-text",
            str(out_text),
            "--no-tables",
            "--max-figure",
            "0",
            "--log-level",
            "ERROR",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr[-1000:]

        log_path = out_dir / "run.log.jsonl"
        assert log_path.exists(), f"run.log.jsonl 未生成: {log_path}"

        events = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events.append(json.loads(line))

        names = [e.get("event") for e in events]
        assert "run_start" in names, f"缺少 run_start 事件: {names}"
        assert "run_end" in names, f"缺少 run_end 事件: {names}"


def main() -> int:
    tests = [test_default_run_log_jsonl_and_events]
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
