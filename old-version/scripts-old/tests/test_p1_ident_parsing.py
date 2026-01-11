#!/usr/bin/env python3
"""
P0-03 + P1-08 + P1-09 回归：Figure/Table 标识符解析

覆盖场景：
1) Figure/Tab 常规数字
2) 罗马数字（I/II/IV）
3) Supplementary 前缀（Supplementary Figure IV / Figure SIV / Table SIV）
4) 附录表编号（A1/B2）
"""

import re
import sys

# 支持从项目根目录运行或从 scripts/tests 目录运行
import os
_scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
# 兼容旧路径
if "scripts" not in sys.path:
    sys.path.insert(0, "scripts")

from extract_pdf_assets import _extract_figure_ident, _extract_table_ident, _parse_figure_ident


def test_figure_idents():
    figure_line_re = re.compile(
        r"^\s*(?P<label>Extended\s+Data\s+Figure|Supplementary\s+(?:Figure|Fig\.?)|Figure|Fig\.?|图表|附图|图)\s*"
        r"(?:(?P<s_prefix>S)\s*(?P<s_id>(?:\d+|[IVX]{1,6}))|(?P<roman>[IVX]{1,6})|(?P<num>\d+))"
        r"(?:\s*[-–]?\s*[A-Za-z]|\s*\([A-Za-z]\))?"
        r"(?:\s*\(continued\)|\s*续|\s*接上页)?",
        re.IGNORECASE,
    )

    cases = {
        "Figure 1: xxx": "1",
        "Fig. 2A xxx": "2",
        "Figure I": "I",
        "Figure IV": "IV",
        "Figure S1": "S1",
        "Figure S IV": "SIV",
        "Figure SIV": "SIV",
        "Supplementary Figure IV": "SIV",
        "Supplementary Figure 3": "S3",
    }

    for text, expect in cases.items():
        m = figure_line_re.match(text)
        assert m, f"should match: {text}"
        got = _extract_figure_ident(m)
        assert got == expect, f"{text}: expect {expect}, got {got}"


def test_table_idents():
    table_line_re = re.compile(
        r"^\s*(?P<label>Extended\s+Data\s+Table|Supplementary\s+Table|Table|Tab\.?|表)\s*"
        r"(?:(?P<s_prefix>S)\s*(?P<s_id>(?:\d+|[IVX]{1,6}))|(?P<letter_id>[A-Z]\d+)|(?P<roman>[IVX]{1,6})|(?P<num>\d+))"
        r"(?:\s*\(continued\)|\s*续|\s*接上页)?",
        re.IGNORECASE,
    )

    cases = {
        "Table 1: xxx": "1",
        "Table IV": "IV",
        "Table S1": "S1",
        "Table SIV": "SIV",
        "Supplementary Table IV": "SIV",
        "Supplementary Table 2": "S2",
        "Table A1": "A1",
    }

    for text, expect in cases.items():
        m = table_line_re.match(text)
        assert m, f"should match: {text}"
        got = _extract_table_ident(m)
        assert got == expect, f"{text}: expect {expect}, got {got}"


def test_parse_figure_ident_numeric_key():
    # 附录罗马数字应能解析出数值部分，避免与正文罗马冲突
    assert _parse_figure_ident("SIV") == (True, 4)
    assert _parse_figure_ident("IV") == (False, 4)
    assert _parse_figure_ident("S1") == (True, 1)
    assert _parse_figure_ident("1") == (False, 1)


def main() -> int:
    tests = [
        test_figure_idents,
        test_table_idents,
        test_parse_figure_ident_numeric_key,
    ]

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

