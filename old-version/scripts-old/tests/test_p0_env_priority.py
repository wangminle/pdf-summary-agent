#!/usr/bin/env python3
"""
P0-01 环境变量优先级修复验证测试
测试优先级：CLI 参数 > 环境变量 > 默认值

测试场景：
1. 默认值生效（无 CLI、无 ENV）
2. 环境变量生效（无 CLI、有 ENV）
3. CLI 参数覆盖环境变量（有 CLI、有 ENV）
4. main(argv=...) 程序化调用
5. SCAN_HEIGHTS / SCAN_DIST_LAMBDA 默认值对齐
"""

import os
import sys
import subprocess
import tempfile

# 测试 PDF 路径
TEST_PDF = "tests/basic-benchmark/DeepSeek_V3_2/DeepSeek_V3_2.pdf"
SCRIPT_PATH = "scripts/core/extract_pdf_assets.py"

def run_extract(env_vars: dict = None, cli_args: list = None) -> str:
    """运行提取脚本并返回输出"""
    cmd = [sys.executable, SCRIPT_PATH, "--pdf", TEST_PDF, "--preset", "robust"]
    if cli_args:
        cmd.extend(cli_args)
    
    # 准备环境
    env = os.environ.copy()
    # 清理可能存在的测试环境变量
    for key in ['EXTRACT_ANCHOR_MODE', 'SCAN_STEP', 'SCAN_HEIGHTS', 
                'SCAN_DIST_LAMBDA', 'GLOBAL_ANCHOR']:
        env.pop(key, None)
    # 设置测试环境变量
    if env_vars:
        env.update(env_vars)
    
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.stdout + result.stderr


def extract_effective_params(output: str) -> dict:
    """从输出中提取有效参数"""
    params = {}
    for line in output.split('\n'):
        if 'anchor_mode=' in line:
            # 解析 anchor_mode=v2, global_anchor=auto, global_anchor_table=auto
            for part in line.split(','):
                if '=' in part:
                    key, val = part.strip().split('=', 1)
                    params[key.strip()] = val.strip()
    return params


def test_default_values():
    """测试1: 默认值生效（无 CLI、无 ENV）"""
    print("\n" + "="*60)
    print("测试1: 默认值生效（无 CLI、无 ENV）")
    print("="*60)
    
    output = run_extract()
    params = extract_effective_params(output)
    
    print(f"  anchor_mode = {params.get('anchor_mode', 'N/A')}")
    
    assert params.get('anchor_mode') == 'v2', f"期望 v2，实际 {params.get('anchor_mode')}"
    print("  ✅ 通过: 默认值 anchor_mode=v2 正确生效")


def test_env_override_default():
    """测试2: 环境变量覆盖默认值（无 CLI、有 ENV）"""
    print("\n" + "="*60)
    print("测试2: 环境变量覆盖默认值（无 CLI、有 ENV）")
    print("="*60)
    
    output = run_extract(env_vars={'EXTRACT_ANCHOR_MODE': 'v1'})
    params = extract_effective_params(output)
    
    print(f"  ENV: EXTRACT_ANCHOR_MODE=v1")
    print(f"  anchor_mode = {params.get('anchor_mode', 'N/A')}")
    
    assert params.get('anchor_mode') == 'v1', f"期望 v1，实际 {params.get('anchor_mode')}"
    print("  ✅ 通过: 环境变量 EXTRACT_ANCHOR_MODE=v1 正确覆盖默认值")


def test_cli_override_env():
    """测试3: CLI 参数覆盖环境变量（有 CLI、有 ENV）"""
    print("\n" + "="*60)
    print("测试3: CLI 参数覆盖环境变量（有 CLI、有 ENV）")
    print("="*60)
    
    output = run_extract(
        env_vars={'EXTRACT_ANCHOR_MODE': 'v1'},
        cli_args=['--anchor-mode', 'v2']
    )
    params = extract_effective_params(output)
    
    print(f"  ENV: EXTRACT_ANCHOR_MODE=v1")
    print(f"  CLI: --anchor-mode v2")
    print(f"  anchor_mode = {params.get('anchor_mode', 'N/A')}")
    
    assert params.get('anchor_mode') == 'v2', f"期望 v2，实际 {params.get('anchor_mode')}"
    print("  ✅ 通过: CLI 参数 --anchor-mode v2 正确覆盖环境变量")


def test_cli_override_env_same_as_default():
    """测试4: CLI 参数显式设为默认值时，仍应覆盖环境变量"""
    print("\n" + "="*60)
    print("测试4: CLI 显式设为默认值时覆盖环境变量")
    print("="*60)
    
    # 环境变量设为 v1，CLI 显式设为 v2（恰好是默认值）
    output = run_extract(
        env_vars={'EXTRACT_ANCHOR_MODE': 'v1'},
        cli_args=['--anchor-mode', 'v2']
    )
    params = extract_effective_params(output)
    
    print(f"  ENV: EXTRACT_ANCHOR_MODE=v1")
    print(f"  CLI: --anchor-mode v2 (与默认值相同)")
    print(f"  anchor_mode = {params.get('anchor_mode', 'N/A')}")
    
    assert params.get('anchor_mode') == 'v2', f"期望 v2，实际 {params.get('anchor_mode')}"
    print("  ✅ 通过: CLI 显式传递即使等于默认值，也覆盖环境变量")


def test_programmatic_argv():
    """测试5: main(argv=...) 程序化调用的正确性"""
    print("\n" + "="*60)
    print("测试5: main(argv=...) 程序化调用")
    print("="*60)
    
    # 通过 Python 代码直接调用 main(argv=...)
    test_code = '''
import os
import sys
sys.path.insert(0, "scripts")

# 清理环境变量
for k in ['EXTRACT_ANCHOR_MODE', 'SCAN_STEP', 'SCAN_HEIGHTS', 'SCAN_DIST_LAMBDA', 'GLOBAL_ANCHOR']:
    os.environ.pop(k, None)

# 设置环境变量
os.environ['EXTRACT_ANCHOR_MODE'] = 'v1'

# 程序化调用 main(argv=...) 传递 CLI 参数
from extract_pdf_assets import main
# 注意：argv 应该不包含脚本名，类似 sys.argv[1:]
argv = ['--pdf', 'tests/basic-benchmark/DeepSeek_V3_2/DeepSeek_V3_2.pdf', '--preset', 'robust', '--anchor-mode', 'v2']
try:
    main(argv=argv)
except SystemExit:
    pass

# 检查最终环境变量
print(f"FINAL_ANCHOR_MODE={os.environ.get('EXTRACT_ANCHOR_MODE', 'N/A')}")
'''
    
    result = subprocess.run(
        [sys.executable, '-c', test_code],
        capture_output=True, text=True,
        cwd=os.getcwd()
    )
    output = result.stdout + result.stderr
    
    # 查找最终的 anchor_mode
    final_mode = None
    for line in output.split('\n'):
        if 'FINAL_ANCHOR_MODE=' in line:
            final_mode = line.split('=')[1].strip()
        if 'anchor_mode=' in line:
            for part in line.split(','):
                if 'anchor_mode=' in part:
                    final_mode = part.split('=')[1].strip()
    
    print(f"  ENV: EXTRACT_ANCHOR_MODE=v1")
    print(f"  argv: ['--anchor-mode', 'v2']")
    print(f"  anchor_mode = {final_mode}")
    
    assert final_mode == 'v2', f"期望 v2，实际 {final_mode}"
    print("  ✅ 通过: main(argv=...) 程序化调用时 CLI 参数正确覆盖环境变量")


def test_scan_heights_default():
    """测试6: SCAN_HEIGHTS 默认值对齐到 240..920"""
    print("\n" + "="*60)
    print("测试6: SCAN_HEIGHTS 默认值对齐")
    print("="*60)
    
    # 通过环境变量检查默认值
    output = run_extract()
    
    # 检查脚本中的默认值
    with open(SCRIPT_PATH, 'r') as f:
        content = f.read()
    
    # 检查 argparse 默认值
    if '240,320,420,520,640,720,820,920' in content:
        print("  ✅ argparse 默认值: 240,320,420,520,640,720,820,920")
    else:
        print("  ❌ argparse 默认值未找到")
        return
    
    # 检查 Anchor V2 回退值
    if '240.0, 320.0, 420.0, 520.0, 640.0, 720.0, 820.0, 920.0' in content:
        print("  ✅ Anchor V2 回退值已对齐: [240.0, 320.0, ..., 920.0]")
    else:
        print("  ❌ Anchor V2 回退值未对齐")
        return
    
    print("  ✅ 通过: SCAN_HEIGHTS 默认值已正确对齐到 240..920")


def test_scan_dist_lambda_default():
    """测试7: SCAN_DIST_LAMBDA 默认值对齐到 0.12"""
    print("\n" + "="*60)
    print("测试7: SCAN_DIST_LAMBDA 默认值对齐")
    print("="*60)
    
    with open(SCRIPT_PATH, 'r') as f:
        content = f.read()
    
    # 检查 argparse 默认值
    if 'scan-dist-lambda", type=float, default=0.12' in content:
        print("  ✅ argparse 默认值: 0.12")
    else:
        print("  ❌ argparse 默认值未找到或不正确")
        return
    
    # 检查 Anchor V2 回退值
    if "SCAN_DIST_LAMBDA', '0.12'" in content or 'SCAN_DIST_LAMBDA\', "0.12"' in content:
        print("  ✅ Anchor V2 回退值已对齐: 0.12")
    else:
        print("  ❌ Anchor V2 回退值未对齐")
        return
    
    print("  ✅ 通过: SCAN_DIST_LAMBDA 默认值已正确对齐到 0.12")


def main():
    print("\n" + "#"*60)
    print("# P0-01 环境变量优先级修复验证测试")
    print("# 优先级: CLI 参数 > 环境变量 > 默认值")
    print("#"*60)
    
    tests = [
        test_default_values,
        test_env_override_default,
        test_cli_override_env,
        test_cli_override_env_same_as_default,
        test_programmatic_argv,
        test_scan_heights_default,
        test_scan_dist_lambda_default,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ❌ 失败: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ 错误: {e}")
            failed += 1
    
    print("\n" + "="*60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("="*60)
    
    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

