#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 02: 集中数据结构定义

所有从 extract_pdf_assets.py 抽离的数据结构（dataclass）集中在此模块。

包含：
- PDFValidationResult: PDF 预验证结果
- QualityIssue: 质量问题记录
- AttachmentRecord: 图/表提取记录
- DrawItem: 矢量绘图元素
- CaptionCandidate: 图注候选项
- CaptionIndex: 全文图注索引
- EnhancedTextUnit: 增强文本单元
- TextBlock: 文本块
- DocumentLayoutModel: 文档版式模型
- GatheredParagraph: 结构化段落
- GatheredText: 结构化文本
- FigureMention: 图表提及
- FigureContext: 图表上下文
- DebugStageInfo: 调试阶段信息
- AcceptanceThresholds: 验收阈值
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 尝试导入 fitz 类型（用于类型注解）
try:
    import fitz
except ImportError:
    fitz = None  # type: ignore


# ============================================================================
# P1-03: PDF 预验证结果
# ============================================================================

@dataclass
class PDFValidationResult:
    """
    PDF 预验证结果，用于在提取前检测潜在问题。
    
    检测内容：
    - 文件是否存在且可读
    - 是否加密
    - 是否有文本层
    - 页数和文件大小
    """
    is_valid: bool               # 是否可以正常处理
    page_count: int              # 页数
    has_text_layer: bool         # 是否有文本层
    text_layer_ratio: float      # 有文本层的页面占比（0.0~1.0）
    is_encrypted: bool           # 是否加密
    pdf_version: str             # PDF 版本
    file_size_mb: float          # 文件大小（MB）
    warnings: List[str]          # 警告列表
    errors: List[str]            # 错误列表
    
    def __str__(self) -> str:
        status = "VALID" if self.is_valid else "INVALID"
        return (f"PDFValidationResult({status}, pages={self.page_count}, "
                f"text_ratio={self.text_layer_ratio:.1%}, encrypted={self.is_encrypted})")


# ============================================================================
# P1-04: 质量控制相关
# ============================================================================

@dataclass
class QualityIssue:
    """
    质量问题记录。
    
    Attributes:
        level: 问题级别 ('error' | 'warning' | 'info')
        category: 问题类别 ('count_mismatch' | 'size_anomaly' | 'numbering_gap' | 'continued_incomplete')
        message: 问题描述
        details: 详细信息（可选）
    """
    level: str        # 'error' | 'warning' | 'info'
    category: str     # 'count_mismatch' | 'size_anomaly' | 'numbering_gap' | 'continued_incomplete'
    message: str      # 问题描述
    details: Dict[str, Any] = None  # type: ignore  # 详细信息（可选）
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}


# ============================================================================
# 图/表提取记录
# ============================================================================

@dataclass
class AttachmentRecord:
    """
    统一记录：图（figure）或表（table）的提取结果。
    
    Attributes:
        kind: 类型 ('figure' | 'table')
        ident: 标识符（保留原样，如 '1', 'S1', 'III'）
        page: 页码（1-based）
        caption: 图注文本
        out_path: 输出文件路径
        continued: 是否为续页
        debug_artifacts: QA-03 调试输出文件列表
    """
    kind: str              # 'figure' | 'table'
    ident: str             # 标识：图/表号（保留原样，如 '1'/'S1'/'III'）
    page: int              # 1-based
    caption: str
    out_path: str
    continued: bool = False
    # QA-03: 将 debug 输出与 index 关联（相对 out_dir / images 目录的相对路径）
    debug_artifacts: List[str] = field(default_factory=list)
    
    def num_key(self) -> float:
        """用于排序的数值键：尽量将可解析的数字排在前面。"""
        try:
            return float(int(self.ident))
        except ValueError:
            return 1e9


# ============================================================================
# 矢量绘图相关
# ============================================================================

@dataclass
class DrawItem:
    """
    矢量绘图元素。
    
    Attributes:
        rect: 边界框（fitz.Rect）
        orient: 方向 ('H'=水平, 'V'=垂直, 'O'=其他)
    """
    rect: Any  # fitz.Rect
    orient: str  # 'H' | 'V' | 'O'


# ============================================================================
# 智能 Caption 检测相关
# ============================================================================

@dataclass
class CaptionCandidate:
    """
    图注候选项（可能是真实图注，也可能是正文引用）。
    
    Attributes:
        rect: 文本行的边界框
        text: 完整文本内容
        number: 提取的编号（如 '1', '2', 'S1'）
        kind: 类型 ('figure' | 'table')
        page: 页码（0-based）
        block_idx: 所在 block 索引
        line_idx: 在 block 中的 line 索引
        spans: spans 信息（字体、flags 等）
        block: 所在 block 的完整信息
        score: 评分（越高越可能是真实图注）
    """
    rect: Any  # fitz.Rect
    text: str
    number: str
    kind: str                # 'figure' | 'table'
    page: int                # 页码（0-based）
    block_idx: int
    line_idx: int
    spans: List[Dict]
    block: Dict
    score: float = 0.0
    
    def __repr__(self):
        return f"CaptionCandidate({self.kind} {self.number}, page={self.page}, score={self.score:.1f}, y={self.rect.y0:.1f})"


@dataclass
class CaptionIndex:
    """
    全文 caption 索引，记录每个编号的所有出现位置。
    
    Attributes:
        candidates: 字典，key 为 'figure_1' | 'table_2' 格式
    """
    candidates: Dict[str, List[CaptionCandidate]]  # key: 'figure_1' | 'table_2'
    
    def get_candidates(self, kind: str, number: str) -> List[CaptionCandidate]:
        """获取指定编号的所有候选项"""
        key = f"{kind}_{number}"
        return self.candidates.get(key, [])


# ============================================================================
# V2 架构：版式驱动提取相关
# ============================================================================

@dataclass
class EnhancedTextUnit:
    """
    增强的文本单元（行级），保留完整格式信息。
    
    Attributes:
        bbox: 边界框
        text: 文本内容
        page: 页码（0-based）
        font_name: 字体名称
        font_size: 字号（pt）
        font_weight: 'bold' | 'regular'
        font_flags: PyMuPDF flags (bit flags)
        color: RGB 颜色 (R, G, B)
        text_type: 文本类型（title_h1/h2/h3/paragraph/caption_figure/caption_table/list/equation/unknown）
        confidence: 类型分类的置信度（0~1）
        column: 所在栏（0=左栏, 1=右栏, -1=单栏）
        indent: 左边界（用于检测缩进）
        block_idx: 所在 block 索引
        line_idx: 所在 line 索引
    """
    bbox: Any  # fitz.Rect
    text: str
    page: int                            # 页码（0-based）
    
    # 格式信息
    font_name: str                       # 字体名称
    font_size: float                     # 字号（pt）
    font_weight: str                     # 'bold' | 'regular'
    font_flags: int                      # PyMuPDF flags
    color: Tuple[int, int, int]          # RGB 颜色
    
    # 类型标注
    text_type: str                       # 'title_h1' | 'title_h2' | 'title_h3' | 'paragraph' | 
                                         # 'caption_figure' | 'caption_table' | 'list' | 'equation' | 'unknown'
    confidence: float                    # 类型分类置信度（0~1）
    
    # 排版信息
    column: int                          # 所在栏（0=左栏, 1=右栏, -1=单栏）
    indent: float                        # 左边界
    
    # 层级关系
    block_idx: int
    line_idx: int


@dataclass
class TextBlock:
    """
    文本密集区域的聚合单元。
    
    Attributes:
        bbox: 聚合后的边界框
        units: 包含的文本单元列表
        block_type: 块类型 ('paragraph_group' | 'caption' | 'title' | 'list')
        page: 页码
        column: 所在栏
    """
    bbox: Any  # fitz.Rect
    units: List[EnhancedTextUnit]
    block_type: str                      # 'paragraph_group' | 'caption' | 'title' | 'list'
    page: int
    column: int


@dataclass
class DocumentLayoutModel:
    """
    全文档的版式模型。
    
    Attributes:
        page_size: (width, height) in pt
        num_columns: 1=单栏, 2=双栏
        margin_left/right/top/bottom: 页面边距
        column_gap: 双栏时的栏间距
        typical_font_size: 正文字号
        typical_line_height: 行高
        typical_line_gap: 行距
        text_units: 文本单元（按页组织）
        text_blocks: 文本块（按页组织）
        vacant_regions: 留白区域（按页组织）
    """
    page_size: Tuple[float, float]       # (width, height) in pt
    num_columns: int                     # 1=单栏, 2=双栏
    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float
    column_gap: float                    # 双栏时的栏间距
    
    # 典型尺寸
    typical_font_size: float             # 正文字号
    typical_line_height: float           # 行高
    typical_line_gap: float              # 行距
    
    # 文本单元和区块（按页组织）
    text_units: Dict[int, List[EnhancedTextUnit]]   # key=page_num
    text_blocks: Dict[int, List[TextBlock]]         # key=page_num
    
    # 留白区域
    vacant_regions: Dict[int, List[Any]]            # key=page_num, value=List[fitz.Rect]
    
    def to_dict(self, include_details: bool = True) -> Dict[str, Any]:
        """
        转换为可序列化的字典。
        
        Args:
            include_details: 是否包含 text_blocks 的 bbox/type 细节
        """
        result = {
            'page_size': self.page_size,
            'num_columns': self.num_columns,
            'margins': {
                'left': self.margin_left,
                'right': self.margin_right,
                'top': self.margin_top,
                'bottom': self.margin_bottom
            },
            'column_gap': self.column_gap,
            'typical_metrics': {
                'font_size': self.typical_font_size,
                'line_height': self.typical_line_height,
                'line_gap': self.typical_line_gap
            },
            'text_units_count': {str(k): len(v) for k, v in self.text_units.items()},
            'text_blocks_count': {str(k): len(v) for k, v in self.text_blocks.items()},
            'vacant_regions_count': {str(k): len(v) for k, v in self.vacant_regions.items()}
        }
        
        # 可选：落盘 text_blocks 的 bbox/type 细节
        if include_details:
            text_blocks_detail = {}
            for page_num, blocks in self.text_blocks.items():
                page_blocks = []
                for block in blocks:
                    block_info = {
                        'type': block.block_type,
                        'bbox': [round(block.bbox.x0, 2), round(block.bbox.y0, 2), 
                                 round(block.bbox.x1, 2), round(block.bbox.y1, 2)],
                        'column': block.column,
                        'units_count': len(block.units),
                    }
                    # 只保存前 100 字符的文本样本
                    sample_text = ' '.join(u.text[:50] for u in block.units[:2]).strip()
                    if sample_text:
                        block_info['sample'] = sample_text[:100]
                    page_blocks.append(block_info)
                text_blocks_detail[str(page_num)] = page_blocks
            result['text_blocks'] = text_blocks_detail
        
        return result


# ============================================================================
# P1-02: Gathering 阶段结构
# ============================================================================

@dataclass
class GatheredParagraph:
    """
    结构化段落。
    
    Attributes:
        page: 页码
        text: 段落文本
        bbox: 边界框 (x0, y0, x1, y1)
        is_heading: 是否为标题
    """
    page: int
    text: str
    bbox: Tuple[float, float, float, float]
    is_heading: bool


@dataclass
class GatheredText:
    """
    结构化文本（Gathering 阶段输出）。
    
    Attributes:
        version: 格式版本
        is_dual_column: 是否双栏
        headers_removed: 移除的页眉列表
        footers_removed: 移除的页脚列表
        paragraphs: 段落列表
    """
    version: str
    is_dual_column: bool
    headers_removed: List[str]
    footers_removed: List[str]
    paragraphs: List[GatheredParagraph]


# ============================================================================
# P1-09: 图表上下文
# ============================================================================

@dataclass
class FigureMention:
    """
    图表在正文中的一次提及。
    
    Attributes:
        page: 页码
        para_idx: 段落索引
        text_window: 上下文文本窗口
    """
    page: int
    para_idx: int
    text_window: str


@dataclass
class FigureContext:
    """
    图表的正文上下文。
    
    Attributes:
        kind: 类型 ('figure' | 'table')
        ident: 标识符
        first_mention: 首次提及
        all_mentions: 所有提及列表
        caption_page_text_window: 图注所在页附近正文窗口
    """
    kind: str
    ident: str
    first_mention: Optional[FigureMention]
    all_mentions: List[FigureMention]
    caption_page_text_window: str


# ============================================================================
# 调试可视化相关
# ============================================================================

@dataclass
class DebugStageInfo:
    """
    调试阶段信息（用于可视化调试）。
    
    Attributes:
        name: 阶段名称
        bbox: 边界框
        color: RGB 颜色
        description: 描述
    """
    name: str
    bbox: Any  # fitz.Rect 或 Tuple
    color: Tuple[int, int, int]
    description: str


@dataclass
class AcceptanceThresholds:
    """
    验收阈值（用于精裁验收判断）。
    
    Attributes:
        height_ratio: 最小高度比
        area_ratio: 最小面积比
        object_coverage: 最小对象覆盖率
        ink_density: 最小墨迹密度
    """
    height_ratio: float
    area_ratio: float
    object_coverage: float
    ink_density: float
