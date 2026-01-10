#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 01: ENV 优先级与有效参数打印

统一参数优先级规则：CLI 显式传参 > 环境变量 > 默认值

主要功能：
1. get_env_with_cli_priority: 统一获取参数值（CLI > ENV > default）
2. print_effective_params: 打印最终生效的参数（调试用）
3. parse_comma_list: 解析逗号分隔的列表字符串

P0-01: 避免环境变量导致"参数静默失效/不可复现"
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar, Union

T = TypeVar("T")


def get_env_str(key: str, default: str = "") -> str:
    """
    获取环境变量字符串值。
    
    Args:
        key: 环境变量名
        default: 默认值
    
    Returns:
        环境变量值或默认值
    """
    return os.environ.get(key, default)


def get_env_int(key: str, default: int = 0) -> int:
    """
    获取环境变量整数值。
    
    Args:
        key: 环境变量名
        default: 默认值
    
    Returns:
        环境变量值（转换为整数）或默认值
    """
    val = os.environ.get(key, "")
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def get_env_float(key: str, default: float = 0.0) -> float:
    """
    获取环境变量浮点值。
    
    Args:
        key: 环境变量名
        default: 默认值
    
    Returns:
        环境变量值（转换为浮点数）或默认值
    """
    val = os.environ.get(key, "")
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def get_env_bool(key: str, default: bool = False) -> bool:
    """
    获取环境变量布尔值。
    
    支持的 True 值：'1', 'true', 'yes', 'on'（不区分大小写）
    支持的 False 值：'0', 'false', 'no', 'off'（不区分大小写）
    
    Args:
        key: 环境变量名
        default: 默认值
    
    Returns:
        环境变量值（转换为布尔值）或默认值
    """
    val = os.environ.get(key, "").lower().strip()
    if not val:
        return default
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def parse_comma_list(value: str) -> List[str]:
    """
    解析逗号分隔的列表字符串。
    
    Args:
        value: 逗号分隔的字符串，如 "1,2,S1,S2"
    
    Returns:
        去除空白的字符串列表
    
    Examples:
        >>> parse_comma_list("1,2,3")
        ['1', '2', '3']
        >>> parse_comma_list("S1, S2")
        ['S1', 'S2']
        >>> parse_comma_list("")
        []
    """
    if not value or not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_comma_set(value: str) -> Set[str]:
    """
    解析逗号分隔的列表字符串为集合。
    
    Args:
        value: 逗号分隔的字符串
    
    Returns:
        去除空白的字符串集合
    """
    return set(parse_comma_list(value))


def get_env_with_cli_priority(
    cli_value: Optional[T],
    env_key: str,
    default: T,
    cli_was_explicit: bool,
    converter: Callable[[str], T] = str,  # type: ignore
) -> T:
    """
    统一参数优先级获取。
    
    优先级：CLI 显式传参 > 环境变量 > 默认值
    
    P0-01 修复：只有当 CLI 参数被显式传递时，才使用 CLI 值；
    否则检查环境变量，最后使用默认值。
    
    Args:
        cli_value: CLI 传入的值（可能是 None 或默认值）
        env_key: 环境变量名
        default: 默认值
        cli_was_explicit: CLI 是否显式传了这个参数
        converter: 类型转换函数（默认 str）
    
    Returns:
        最终生效的值
    
    Examples:
        >>> # CLI 显式传了 --dpi 200
        >>> get_env_with_cli_priority(200, "EXTRACT_DPI", 300, True, int)
        200
        >>> # CLI 没传，ENV 有值
        >>> os.environ["EXTRACT_DPI"] = "400"
        >>> get_env_with_cli_priority(300, "EXTRACT_DPI", 300, False, int)
        400
        >>> # CLI 没传，ENV 没值，用默认
        >>> get_env_with_cli_priority(300, "EXTRACT_DPI_NOT_SET", 300, False, int)
        300
    """
    # 优先级 1: CLI 显式传参
    if cli_was_explicit:
        return cli_value  # type: ignore
    
    # 优先级 2: 环境变量
    env_val = os.environ.get(env_key, "")
    if env_val:
        try:
            return converter(env_val)
        except (ValueError, TypeError):
            pass
    
    # 优先级 3: 默认值
    return default


def was_arg_explicitly_passed(arg_name: str, argv: Optional[List[str]] = None) -> bool:
    """
    检测 argparse 参数是否被用户显式传递。
    
    这是一个简单的启发式检测：检查 argv 中是否包含对应的参数标志。
    
    Args:
        arg_name: 参数名（如 'dpi', 'clip-height'）
        argv: 命令行参数列表（默认使用 sys.argv）
    
    Returns:
        True 如果参数在命令行中被显式传递
    
    Examples:
        >>> was_arg_explicitly_passed('dpi', ['--pdf', 'a.pdf', '--dpi', '300'])
        True
        >>> was_arg_explicitly_passed('dpi', ['--pdf', 'a.pdf'])
        False
    """
    if argv is None:
        argv = sys.argv
    
    # 转换参数名为可能的命令行形式
    flag_forms = [
        f"--{arg_name}",
        f"--{arg_name.replace('_', '-')}",
    ]
    
    for flag in flag_forms:
        for arg in argv:
            if arg == flag or arg.startswith(f"{flag}="):
                return True
    
    return False


def collect_explicit_args(argv: Optional[List[str]] = None) -> Set[str]:
    """
    收集命令行中所有显式传递的参数名。
    
    Args:
        argv: 命令行参数列表
    
    Returns:
        显式传递的参数名集合（统一为下划线形式）
    """
    if argv is None:
        argv = sys.argv
    
    explicit = set()
    for arg in argv:
        if arg.startswith("--"):
            # 去除前缀和可能的值
            name = arg[2:]
            if "=" in name:
                name = name.split("=")[0]
            # 统一为下划线形式
            name = name.replace("-", "_")
            explicit.add(name)
    
    return explicit


def print_effective_params(
    params: Dict[str, Any],
    prefix: str = "Effective parameters",
    logger: Optional[Any] = None,
    level: str = "DEBUG",
) -> None:
    """
    打印最终生效的参数（用于调试）。
    
    Args:
        params: 参数字典
        prefix: 打印前缀
        logger: 可选的 logger 对象
        level: 日志级别（默认 DEBUG）
    
    Example output:
        [DEBUG] Effective parameters:
          dpi: 300
          clip_height: 650.0
          margin_x: 20.0
          ...
    """
    lines = [f"{prefix}:"]
    for key, value in sorted(params.items()):
        lines.append(f"  {key}: {value}")
    
    msg = "\n".join(lines)
    
    if logger:
        log_func = getattr(logger, level.lower(), logger.debug)
        log_func(msg)
    else:
        print(f"[{level}] {msg}")


def apply_preset_robust(args: Any) -> None:
    """
    应用 'robust' 预设参数。
    
    这是 --preset robust 的实现，会设置一系列稳健参数。
    
    注意：只会覆盖未显式传递的参数。
    
    Args:
        args: argparse.Namespace 对象
    """
    # 预设参数映射
    robust_defaults = {
        "dpi": 300,
        "clip_height": 520,
        "margin_x": 26,
        "caption_gap": 6,
        "text_trim": True,
        "text_trim_width_ratio": 0.5,
        "text_trim_font_min": 7,
        "text_trim_font_max": 16,
        "text_trim_gap": 6,
        "adjacent_th": 24,
        "object_pad": 8,
        "object_min_area_ratio": 0.012,
        "object_merge_gap": 6,
        "autocrop": True,
        "autocrop_pad": 30,
        "autocrop_white_th": 250,
        "autocrop_mask_text": True,
        "mask_font_max": 14,
        "mask_width_ratio": 0.5,
        "mask_top_frac": 0.6,
        "near_edge_pad_px": 32,
        "protect_far_edge_px": 18,
        # 表格特化
        "table_clip_height": 520,
        "table_margin_x": 26,
        "table_caption_gap": 6,
        "table_object_min_area_ratio": 0.005,
        "table_object_merge_gap": 4,
        "table_autocrop": True,
        "table_autocrop_pad": 20,
        "table_adjacent_th": 28,
    }
    
    # 收集显式传递的参数
    explicit_args = collect_explicit_args()
    
    # 只设置未显式传递的参数
    for key, value in robust_defaults.items():
        if key not in explicit_args:
            setattr(args, key, value)


# ============================================================================
# 便捷函数：常用环境变量
# ============================================================================

def get_force_above_figures() -> Set[str]:
    """
    获取强制从上方取图的图号列表。
    
    环境变量：EXTRACT_FORCE_ABOVE
    
    Returns:
        图号集合，如 {'1', '4'}
    """
    return parse_comma_set(get_env_str("EXTRACT_FORCE_ABOVE", ""))


def get_force_below_figures() -> Set[str]:
    """
    获取强制从下方取图的图号列表。
    
    环境变量：EXTRACT_FORCE_BELOW
    
    Returns:
        图号集合
    """
    return parse_comma_set(get_env_str("EXTRACT_FORCE_BELOW", ""))


def get_force_above_tables() -> Set[str]:
    """
    获取强制从上方取表的表号列表。
    
    环境变量：EXTRACT_FORCE_TABLE_ABOVE
    
    Returns:
        表号集合
    """
    return parse_comma_set(get_env_str("EXTRACT_FORCE_TABLE_ABOVE", ""))


def get_force_below_tables() -> Set[str]:
    """
    获取强制从下方取表的表号列表。
    
    环境变量：EXTRACT_FORCE_TABLE_BELOW
    
    Returns:
        表号集合
    """
    return parse_comma_set(get_env_str("EXTRACT_FORCE_TABLE_BELOW", ""))
