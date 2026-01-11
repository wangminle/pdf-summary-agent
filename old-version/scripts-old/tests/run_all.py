#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QA-01 统一测试入口

单命令跑全套测试：
  python3 scripts/tests/run_all.py

可选参数：
  -v, --verbose     显示详细输出
  --json            以 JSON 格式输出结果
  --skip-regex      跳过正则测试
  --skip-golden     跳过 golden 对比测试
  --skip-p0         跳过 P0 测试
  --skip-p1         跳过 P1 测试
  --update-golden   更新 golden 基准文件

测试套件：
1. P0 环境变量优先级测试 (test_p0_env_priority.py)
2. P1 标识符解析测试 (test_p1_ident_parsing.py)
3. 正则表达式测试 (scripts/tests/test_regex_patterns.py)
4. Golden 对比测试 (scripts/tests/test_extraction_golden.py)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 项目根目录
# run_all.py 现在在 scripts/tests/ 下，所以需要向上两级
PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPTS_TESTS_DIR = PROJECT_ROOT / "scripts" / "tests"  # scripts/tests/ 目录
SCRIPTS_DIR = PROJECT_ROOT / "scripts"                   # scripts/ 目录


@dataclass
class TestSuiteResult:
    """测试套件结果"""
    name: str
    passed: int = 0
    failed: int = 0
    skipped: bool = False
    duration_ms: int = 0
    messages: List[str] = field(default_factory=list)
    
    @property
    def total(self) -> int:
        return self.passed + self.failed
    
    @property
    def success(self) -> bool:
        return self.failed == 0 and not self.skipped


def run_python_script(
    script_path: Path,
    args: Optional[List[str]] = None,
    cwd: Optional[Path] = None,
) -> Tuple[int, str, str]:
    """
    运行 Python 脚本并返回结果
    
    Returns:
        (exit_code, stdout, stderr)
    """
    cmd = [sys.executable, str(script_path)]
    if args:
        cmd.extend(args)
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd or PROJECT_ROOT),
    )
    
    return result.returncode, result.stdout, result.stderr


def parse_test_output(output: str) -> Tuple[int, int]:
    """
    从测试输出中解析通过/失败数量
    
    查找格式：
    - "测试结果: X 通过, Y 失败"
    - "X 通过, Y 失败"
    """
    import re
    
    # 尝试匹配中文格式
    m = re.search(r'(\d+)\s*通过.*?(\d+)\s*失败', output)
    if m:
        return int(m.group(1)), int(m.group(2))
    
    # 尝试匹配英文格式
    m = re.search(r'(\d+)\s*passed.*?(\d+)\s*failed', output, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    
    return 0, 0


def run_test_suite(
    name: str,
    script_path: Path,
    args: Optional[List[str]] = None,
    verbose: bool = False,
) -> TestSuiteResult:
    """运行单个测试套件"""
    result = TestSuiteResult(name=name)
    
    if not script_path.exists():
        result.skipped = True
        result.messages.append(f"脚本不存在: {script_path}")
        return result
    
    start_time = time.time()
    
    exit_code, stdout, stderr = run_python_script(script_path, args)
    
    result.duration_ms = int((time.time() - start_time) * 1000)
    
    # 合并输出
    output = stdout + stderr
    
    # 解析结果
    passed, failed = parse_test_output(output)
    result.passed = passed
    result.failed = failed
    
    # 如果无法解析，根据退出码判断
    if passed == 0 and failed == 0:
        if exit_code == 0:
            result.passed = 1
        else:
            result.failed = 1
    
    if verbose:
        result.messages.append(output)
    elif failed > 0:
        # 只保留失败信息
        for line in output.split('\n'):
            if '❌' in line or '失败' in line or 'failed' in line.lower():
                result.messages.append(line.strip())
    
    return result


def main(argv: Optional[List[str]] = None) -> int:
    """主函数"""
    parser = argparse.ArgumentParser(
        description="QA-01 统一测试入口"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果"
    )
    parser.add_argument(
        "--skip-regex",
        action="store_true",
        help="跳过正则测试"
    )
    parser.add_argument(
        "--skip-golden",
        action="store_true",
        help="跳过 golden 对比测试"
    )
    parser.add_argument(
        "--skip-p0",
        action="store_true",
        help="跳过 P0 测试"
    )
    parser.add_argument(
        "--skip-p1",
        action="store_true",
        help="跳过 P1 测试"
    )
    parser.add_argument(
        "--update-golden",
        action="store_true",
        help="更新 golden 基准文件"
    )
    
    args = parser.parse_args(argv)
    
    print("\n" + "#"*70)
    print("# QA-01 统一测试入口 - 单命令跑全套")
    print("#"*70)
    
    results: List[TestSuiteResult] = []
    
    # 测试套件列表
    test_suites = []
    
    if not args.skip_p0:
        test_suites.append({
            "name": "P0 环境变量优先级测试",
            "path": SCRIPTS_TESTS_DIR / "test_p0_env_priority.py",
            "args": [],
        })
    
    if not args.skip_p1:
        test_suites.append({
            "name": "P1 标识符解析测试",
            "path": SCRIPTS_TESTS_DIR / "test_p1_ident_parsing.py",
            "args": [],
        })

        # QA-03: debug 输出与 index 关联
        test_suites.append({
            "name": "QA-03 debug_artifacts 写入测试",
            "path": SCRIPTS_TESTS_DIR / "test_qa03_debug_artifacts.py",
            "args": [],
        })

        # QA-04: 失败分级与结构化日志
        test_suites.append({
            "name": "QA-04 结构化日志 run.log.jsonl 测试",
            "path": SCRIPTS_TESTS_DIR / "test_qa04_structured_log.py",
            "args": [],
        })

        # QA-05: 重命名工作流联动
        test_suites.append({
            "name": "QA-05 重命名工作流联动测试",
            "path": SCRIPTS_TESTS_DIR / "test_qa05_rename_workflow.py",
            "args": [],
        })
    
    if not args.skip_regex:
        test_suites.append({
            "name": "正则表达式测试",
            "path": SCRIPTS_TESTS_DIR / "test_regex_patterns.py",
            "args": ["-v"] if args.verbose else [],
        })
    
    if not args.skip_golden:
        golden_args = []
        if args.verbose:
            golden_args.append("-v")
        if args.update_golden:
            golden_args.append("--update-golden")
        test_suites.append({
            "name": "Golden 对比测试",
            "path": SCRIPTS_TESTS_DIR / "test_extraction_golden.py",
            "args": golden_args,
        })
    
    # 运行所有测试套件
    for suite in test_suites:
        print(f"\n{'='*60}")
        print(f"运行: {suite['name']}")
        print('='*60)
        
        result = run_test_suite(
            name=suite["name"],
            script_path=suite["path"],
            args=suite["args"],
            verbose=args.verbose,
        )
        results.append(result)
        
        if result.skipped:
            print(f"  ⏭️  跳过: {result.messages[0] if result.messages else '未知原因'}")
        elif result.failed == 0:
            print(f"  ✅ 通过: {result.passed} 测试 ({result.duration_ms}ms)")
        else:
            print(f"  ❌ 失败: {result.passed} 通过, {result.failed} 失败 ({result.duration_ms}ms)")
            for msg in result.messages[:5]:  # 只显示前5条
                print(f"      {msg}")
    
    # 汇总结果
    total_passed = sum(r.passed for r in results)
    total_failed = sum(r.failed for r in results)
    total_skipped = sum(1 for r in results if r.skipped)
    total_duration = sum(r.duration_ms for r in results)
    
    print("\n" + "="*70)
    print("测试汇总")
    print("="*70)
    
    for r in results:
        if r.skipped:
            status = "⏭️  跳过"
        elif r.failed == 0:
            status = "✅ 通过"
        else:
            status = "❌ 失败"
        print(f"  {status}  {r.name}: {r.passed}/{r.total} ({r.duration_ms}ms)")
    
    print("-"*70)
    print(f"  总计: {total_passed} 通过, {total_failed} 失败, {total_skipped} 跳过")
    print(f"  耗时: {total_duration}ms")
    print("="*70)
    
    if args.json:
        output = {
            "summary": {
                "passed": total_passed,
                "failed": total_failed,
                "skipped": total_skipped,
                "duration_ms": total_duration,
            },
            "suites": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "failed": r.failed,
                    "skipped": r.skipped,
                    "duration_ms": r.duration_ms,
                }
                for r in results
            ]
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
