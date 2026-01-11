#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QA-02 + QA-04: 统一日志系统

提供：
1. 全局 logger 配置（支持 --log-level）
2. 结构化日志事件（用于 QA-04 失败分级与可解释日志）
3. 上下文感知的错误记录
4. run_id 管理（用于 QA-03 可视化调试统一化）

使用方式：
    from extraction_logger import get_logger, configure_logging, log_event
    
    # 在 main() 开头配置
    configure_logging(level="INFO", log_file="run.log.jsonl", run_id="20251223-180000")
    
    # 获取 logger
    logger = get_logger(__name__)
    logger.info("Starting extraction")
    
    # 记录结构化事件
    log_event("anchor_selection", kind="figure", id="1", page=3, side="above", score=0.85)
"""

import json
import logging
import os
import sys
import uuid
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from pathlib import Path

# ============================================================================
# 全局状态
# ============================================================================

_RUN_ID: Optional[str] = None
_LOG_FILE_HANDLER: Optional[logging.FileHandler] = None
_JSONL_FILE: Optional[Path] = None
_EVENTS: List[Dict[str, Any]] = []  # 内存中的事件列表（用于写入 run.log.jsonl）


# ============================================================================
# 日志级别映射
# ============================================================================

LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


# ============================================================================
# 结构化事件定义 (QA-04)
# ============================================================================

@dataclass
class ExtractionEvent:
    """结构化提取事件"""
    event: str                    # 事件类型（如 "anchor_selection", "refine_fallback"）
    ts: str = ""                  # 时间戳
    run_id: str = ""              # 运行 ID
    level: str = "info"           # 级别：debug/info/warning/error
    
    # 上下文字段
    pdf: str = ""                 # PDF 文件名
    page: int = 0                 # 页码（1-based）
    kind: str = ""                # "figure" | "table"
    id: str = ""                  # 图表标识符
    stage: str = ""               # 处理阶段（如 "baseline", "phase_a", "phase_b"）
    
    # 事件详情
    details: Dict[str, Any] = field(default_factory=dict)
    message: str = ""             # 可读消息
    
    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.now().isoformat()
        if not self.run_id and _RUN_ID:
            self.run_id = _RUN_ID
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于 JSON 序列化）"""
        d = asdict(self)
        # 移除空字段
        return {k: v for k, v in d.items() if v or k in ("ts", "event", "level")}


# ============================================================================
# 自定义 Formatter（带颜色和上下文）
# ============================================================================

class ColoredFormatter(logging.Formatter):
    """带颜色的控制台日志格式化器"""
    
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
        'RESET': '\033[0m',       # Reset
    }
    
    def format(self, record: logging.LogRecord) -> str:
        # 添加颜色
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.COLORS['RESET']}"
        
        # 格式化消息
        formatted = super().format(record)
        
        # 恢复原始 levelname（避免影响其他 handler）
        record.levelname = levelname
        
        return formatted


class ContextFormatter(logging.Formatter):
    """带上下文的日志格式化器（用于文件日志）"""
    
    def format(self, record: logging.LogRecord) -> str:
        # 添加上下文信息
        context_parts = []
        for key in ('pdf', 'page', 'kind', 'id', 'stage'):
            val = getattr(record, key, None)
            if val:
                context_parts.append(f"{key}={val}")
        
        if context_parts:
            record.context = f"[{', '.join(context_parts)}]"
        else:
            record.context = ""
        
        return super().format(record)


# ============================================================================
# Logger 配置函数
# ============================================================================

def generate_run_id() -> str:
    """生成唯一的运行 ID"""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{timestamp}-{short_uuid}"


def get_run_id() -> str:
    """获取当前运行 ID"""
    global _RUN_ID
    if _RUN_ID is None:
        _RUN_ID = generate_run_id()
    return _RUN_ID


def configure_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    log_jsonl: Optional[str] = None,
    run_id: Optional[str] = None,
    use_color: bool = True,
) -> str:
    """
    配置全局日志系统
    
    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）
        log_file: 文本日志文件路径（可选）
        log_jsonl: JSONL 结构化日志文件路径（可选）
        run_id: 运行 ID（可选，不提供则自动生成）
        use_color: 是否在控制台使用颜色
    
    Returns:
        运行 ID
    """
    global _RUN_ID, _LOG_FILE_HANDLER, _JSONL_FILE
    
    # 设置运行 ID
    _RUN_ID = run_id or generate_run_id()
    
    # 解析日志级别
    log_level = LOG_LEVELS.get(level.upper(), logging.INFO)
    
    # 获取根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # 清除现有 handler
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)
    
    if use_color and sys.stderr.isatty():
        console_format = "%(levelname)s %(message)s"
        console_handler.setFormatter(ColoredFormatter(console_format))
    else:
        console_format = "[%(levelname)s] %(message)s"
        console_handler.setFormatter(logging.Formatter(console_format))
    
    root_logger.addHandler(console_handler)
    
    # 文件 handler（可选）
    if log_file:
        _LOG_FILE_HANDLER = logging.FileHandler(log_file, encoding='utf-8')
        _LOG_FILE_HANDLER.setLevel(logging.DEBUG)  # 文件记录更详细
        file_format = "%(asctime)s [%(levelname)s] %(name)s %(context)s %(message)s"
        _LOG_FILE_HANDLER.setFormatter(ContextFormatter(file_format))
        root_logger.addHandler(_LOG_FILE_HANDLER)
    
    # JSONL 文件路径（用于结构化事件）
    if log_jsonl:
        _JSONL_FILE = Path(log_jsonl)
        try:
            _JSONL_FILE.parent.mkdir(parents=True, exist_ok=True)
            # 触发创建文件（保证路径可写；不写入事件，避免噪声）
            _JSONL_FILE.open("a", encoding="utf-8").close()
        except Exception:
            # JSONL 日志不可用不应中断主流程
            _JSONL_FILE = None
    
    return _RUN_ID


def get_logger(name: str = "extract_pdf_assets") -> logging.Logger:
    """获取命名的 logger"""
    return logging.getLogger(name)


# ============================================================================
# 上下文感知的日志记录
# ============================================================================

class LogContext:
    """日志上下文管理器（用于设置当前处理的 PDF/页面/图表）"""
    
    def __init__(
        self,
        pdf: str = "",
        page: int = 0,
        kind: str = "",
        id: str = "",
        stage: str = "",
    ):
        self.pdf = pdf
        self.page = page
        self.kind = kind
        self.id = id
        self.stage = stage
        self._old_factory = None
    
    def __enter__(self):
        # 保存当前的 record factory
        self._old_factory = logging.getLogRecordFactory()
        
        # 创建新的 factory，添加上下文
        context = self
        old_factory = self._old_factory
        
        def record_factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)
            record.pdf = context.pdf
            record.page = context.page
            record.kind = context.kind
            record.id = context.id
            record.stage = context.stage
            return record
        
        logging.setLogRecordFactory(record_factory)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        # 恢复原始 factory
        if self._old_factory:
            logging.setLogRecordFactory(self._old_factory)
        return False


def log_with_context(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    pdf: str = "",
    page: int = 0,
    kind: str = "",
    id: str = "",
    stage: str = "",
    exc_info: bool = False,
    **kwargs
):
    """带上下文的日志记录"""
    extra = {
        'pdf': pdf,
        'page': page,
        'kind': kind,
        'id': id,
        'stage': stage,
    }
    extra.update(kwargs)
    logger.log(level, message, extra=extra, exc_info=exc_info)


# ============================================================================
# 结构化事件记录 (QA-04)
# ============================================================================

def log_event(
    event: str,
    *,
    level: str = "info",
    pdf: str = "",
    page: int = 0,
    kind: str = "",
    id: str = "",
    stage: str = "",
    message: str = "",
    **details
) -> ExtractionEvent:
    """
    记录结构化事件（用于 run.log.jsonl）
    
    Args:
        event: 事件类型（如 "anchor_selection", "refine_fallback"）
        level: 级别（debug/info/warning/error）
        pdf: PDF 文件名
        page: 页码（1-based）
        kind: "figure" | "table"
        id: 图表标识符
        stage: 处理阶段
        message: 可读消息
        **details: 其他详情字段
    
    Returns:
        创建的事件对象
    """
    evt = ExtractionEvent(
        event=event,
        level=level,
        pdf=pdf,
        page=page,
        kind=kind,
        id=id,
        stage=stage,
        message=message,
        details=details,
    )
    
    # 添加到内存列表
    _EVENTS.append(evt.to_dict())
    
    # 如果有 JSONL 文件，追加写入
    if _JSONL_FILE:
        try:
            with open(_JSONL_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(evt.to_dict(), ensure_ascii=False) + '\n')
        except Exception:
            pass  # 日志写入失败不应中断主流程
    
    return evt


def get_events() -> List[Dict[str, Any]]:
    """获取所有已记录的事件"""
    return _EVENTS.copy()


def clear_events():
    """清除已记录的事件"""
    global _EVENTS
    _EVENTS = []


def flush_events(output_path: str) -> int:
    """
    将所有事件写入 JSONL 文件
    
    Args:
        output_path: 输出文件路径
    
    Returns:
        写入的事件数量
    """
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            for evt in _EVENTS:
                f.write(json.dumps(evt, ensure_ascii=False) + '\n')
        return len(_EVENTS)
    except Exception:
        return 0


# ============================================================================
# 错误分层策略 (QA-02)
# ============================================================================

class ExtractionError(Exception):
    """提取过程中的可恢复错误"""
    
    def __init__(
        self,
        message: str,
        *,
        pdf: str = "",
        page: int = 0,
        kind: str = "",
        id: str = "",
        stage: str = "",
        recoverable: bool = True,
    ):
        super().__init__(message)
        self.pdf = pdf
        self.page = page
        self.kind = kind
        self.id = id
        self.stage = stage
        self.recoverable = recoverable
    
    def __str__(self):
        parts = [super().__str__()]
        context = []
        if self.pdf:
            context.append(f"pdf={self.pdf}")
        if self.page:
            context.append(f"page={self.page}")
        if self.kind:
            context.append(f"kind={self.kind}")
        if self.id:
            context.append(f"id={self.id}")
        if self.stage:
            context.append(f"stage={self.stage}")
        if context:
            parts.append(f"[{', '.join(context)}]")
        return " ".join(parts)


class FatalExtractionError(ExtractionError):
    """致命错误（必须终止）"""
    
    def __init__(self, message: str, **kwargs):
        super().__init__(message, recoverable=False, **kwargs)


# 致命错误类型（必须终止）
FATAL_ERROR_TYPES = (
    "pdf_not_found",
    "pdf_open_failed",
    "output_dir_not_writable",
    "critical_dependency_missing",
)

# 可恢复错误类型（记录后继续）
RECOVERABLE_ERROR_TYPES = (
    "page_render_failed",
    "single_figure_extraction_failed",
    "drawing_read_failed",
    "autocrop_failed",
    "debug_visualization_failed",
)


def is_fatal_error(error_type: str) -> bool:
    """判断是否为致命错误"""
    return error_type in FATAL_ERROR_TYPES


# ============================================================================
# 便捷函数
# ============================================================================

def log_warning(
    logger: logging.Logger,
    message: str,
    *,
    pdf: str = "",
    page: int = 0,
    kind: str = "",
    id: str = "",
    stage: str = "",
    exc_info: bool = False,
):
    """记录警告（带上下文）"""
    context_parts = []
    if page:
        context_parts.append(f"page {page}")
    if kind and id:
        context_parts.append(f"{kind} {id}")
    elif kind:
        context_parts.append(kind)
    if stage:
        context_parts.append(f"stage={stage}")
    
    if context_parts:
        full_message = f"[{', '.join(context_parts)}] {message}"
    else:
        full_message = message
    
    logger.warning(full_message, exc_info=exc_info)
    
    # 同时记录结构化事件
    log_event(
        "warning",
        level="warning",
        pdf=pdf,
        page=page,
        kind=kind,
        id=id,
        stage=stage,
        message=message,
    )


def log_error(
    logger: logging.Logger,
    message: str,
    *,
    pdf: str = "",
    page: int = 0,
    kind: str = "",
    id: str = "",
    stage: str = "",
    exc_info: bool = True,
):
    """记录错误（带上下文）"""
    context_parts = []
    if page:
        context_parts.append(f"page {page}")
    if kind and id:
        context_parts.append(f"{kind} {id}")
    if stage:
        context_parts.append(f"stage={stage}")
    
    if context_parts:
        full_message = f"[{', '.join(context_parts)}] {message}"
    else:
        full_message = message
    
    logger.error(full_message, exc_info=exc_info)
    
    # 同时记录结构化事件
    log_event(
        "error",
        level="error",
        pdf=pdf,
        page=page,
        kind=kind,
        id=id,
        stage=stage,
        message=message,
    )


# ============================================================================
# 模块初始化
# ============================================================================

# 默认配置（可被 configure_logging 覆盖）
_default_logger = get_logger()
