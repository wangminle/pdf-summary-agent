#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向后兼容 shim：转发到 scripts/core/extract_pdf_assets.py

此文件是一个薄包装器，用于保持向后兼容性。
实际的提取逻辑已移动到 scripts/core/extract_pdf_assets.py

注意：此入口点仍然完全支持，但建议新脚本使用新路径：
    python3 scripts/core/extract_pdf_assets.py --pdf <file> --preset robust

用法（与旧版本完全相同）：
    python3 scripts/extract_pdf_assets.py --pdf <file> --preset robust
"""

import os
import sys

# 获取新脚本的路径
_current_dir = os.path.dirname(os.path.abspath(__file__))
_new_script = os.path.join(_current_dir, "core", "extract_pdf_assets.py")

if not os.path.exists(_new_script):
    print(f"[ERROR] 核心脚本不存在: {_new_script}", file=sys.stderr)
    print("[HINT] 请确保 scripts/core/extract_pdf_assets.py 存在", file=sys.stderr)
    sys.exit(1)

# 将 core 目录添加到 Python 路径（确保相对导入正常工作）
_core_dir = os.path.join(_current_dir, "core")
if _core_dir not in sys.path:
    sys.path.insert(0, _core_dir)

# 将 scripts 目录添加到 Python 路径（确保 lib 模块可导入）
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

# 导入并执行主函数
# 使用 runpy 来正确执行模块，保持 __name__ == "__main__" 的行为
if __name__ == "__main__":
    import runpy
    
    # 设置 sys.argv[0] 为新脚本路径（某些代码可能依赖此路径）
    # 但保留原始参数
    original_argv0 = sys.argv[0]
    sys.argv[0] = _new_script
    
    try:
        # 作为 __main__ 运行新脚本
        runpy.run_path(_new_script, run_name="__main__")
    finally:
        # 恢复原始 argv[0]（虽然脚本结束后这不重要）
        sys.argv[0] = original_argv0

