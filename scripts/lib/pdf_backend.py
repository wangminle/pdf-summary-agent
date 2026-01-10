#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Commit 01B: PDF 后端适配层（薄适配 + 可选 pdfplumber）

设计原则（MVP）：
1. 不改变任何算法/阈值/默认值/输出字段（仍以现有 PyMuPDF 行为为准）
2. 主路径只用 PyMuPDF；pdfplumber 仅在显式调用 helper 时按需 import
3. MVP 阶段不做"统一数据结构转换"，直接透传 PyMuPDF 原始返回
4. 封装层保留 .raw（doc/page）以便渐进迁移

坐标约定：
- 内部统一使用 pt 单位的 (x0, y0, x1, y1)，原点在页面左上（与 PyMuPDF Rect 一致）
- pdfplumber 的 top/bottom 同样以页面顶部为 0，可直接映射

使用示例：
    from lib.pdf_backend import open_pdf, try_extract_tables_with_pdfplumber
    
    with open_pdf("paper.pdf") as doc:
        print(f"Pages: {doc.page_count}")
        page = doc[0]
        text_dict = page.get_text_dict()
        images = page.get_images()
        drawings = page.get_drawings()
        
        # 渲染区域
        pix = page.get_pixmap(dpi=300, clip=(0, 0, 500, 500))
        pix.save("region.png")
    
    # 可选：表格结构分析（需要 pdfplumber）
    tables = try_extract_tables_with_pdfplumber("paper.pdf", page_number=1)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union
from pathlib import Path

# PyMuPDF 导入（保持与现有代码一致的入口名 'fitz'）
try:
    import fitz  # PyMuPDF
except ImportError as e:
    raise ImportError(
        "PyMuPDF (pymupdf) is required: pip install pymupdf"
    ) from e


# ============================================================================
# 核心数据结构（MVP: 薄封装，透传 PyMuPDF 原始结构）
# ============================================================================

@dataclass
class PDFDocument:
    """
    PDF 文档封装。
    
    Attributes:
        raw: 底层 PyMuPDF 文档对象 (fitz.Document)
        path: PDF 文件路径
    """
    raw: Any  # fitz.Document
    path: str
    
    @property
    def page_count(self) -> int:
        """文档页数"""
        return self.raw.page_count
    
    @property
    def metadata(self) -> Dict[str, str]:
        """文档元数据"""
        return dict(self.raw.metadata or {})
    
    def __getitem__(self, index: int) -> "PDFPage":
        """获取页面（0-based 索引）"""
        return PDFPage(raw=self.raw[index], doc=self)
    
    def __iter__(self) -> Iterator["PDFPage"]:
        """迭代所有页面"""
        for i in range(self.page_count):
            yield self[i]
    
    def __len__(self) -> int:
        """文档页数"""
        return self.page_count
    
    def extract_image(self, xref: int) -> Dict[str, Any]:
        """
        提取嵌入图像内容。
        
        Args:
            xref: 图像内部引用 ID
        
        Returns:
            包含 'image' (bytes), 'ext' (str), 等字段的字典
        """
        return self.raw.extract_image(xref)
    
    def close(self) -> None:
        """关闭文档"""
        self.raw.close()
    
    def __enter__(self) -> "PDFDocument":
        return self
    
    def __exit__(self, *exc_info) -> None:
        self.close()


@dataclass
class PDFPage:
    """
    PDF 页面封装。
    
    Attributes:
        raw: 底层 PyMuPDF 页面对象 (fitz.Page)
        doc: 所属文档
    """
    raw: Any  # fitz.Page
    doc: PDFDocument
    
    @property
    def page_number(self) -> int:
        """页码（1-based）"""
        return self.raw.number + 1
    
    @property
    def page_index(self) -> int:
        """页面索引（0-based）"""
        return self.raw.number
    
    @property
    def rect(self) -> "fitz.Rect":
        """
        页面边界框。
        
        Returns:
            fitz.Rect 对象，包含 (x0, y0, x1, y1)
        """
        return self.raw.rect
    
    @property
    def width(self) -> float:
        """页面宽度（pt）"""
        return self.raw.rect.width
    
    @property
    def height(self) -> float:
        """页面高度（pt）"""
        return self.raw.rect.height
    
    def get_text(self, mode: str = "text") -> Union[str, Dict, List]:
        """
        提取文本。
        
        Args:
            mode: 提取模式
                - "text": 纯文本字符串
                - "dict": 完整结构（blocks, lines, spans）
                - "blocks": 文本块列表
                - "words": 单词列表
                - "html": HTML 格式
        
        Returns:
            根据 mode 返回不同格式的文本内容
        """
        return self.raw.get_text(mode)
    
    def get_text_dict(self) -> Dict[str, Any]:
        """
        获取结构化文本（透传 PyMuPDF 的 get_text("dict")）。
        
        Returns:
            包含 'blocks', 'width', 'height' 等字段的字典
        """
        return self.raw.get_text("dict")
    
    def get_images(self, full: bool = True) -> List[Tuple]:
        """
        获取页面上的嵌入图像列表。
        
        Args:
            full: 是否返回完整信息
        
        Returns:
            图像信息元组列表，每个元组包含 (xref, smask, width, height, ...)
        """
        return self.raw.get_images(full=full)
    
    def get_image_rects(self, xref: int) -> List["fitz.Rect"]:
        """
        获取指定图像在页面上的位置。
        
        Args:
            xref: 图像引用 ID
        
        Returns:
            图像边界框列表
        """
        return self.raw.get_image_rects(xref)
    
    def get_drawings(self) -> List[Dict[str, Any]]:
        """
        获取页面上的矢量图形（路径、线条、矩形等）。
        
        Returns:
            矢量图形信息列表，每个元素包含 'rect', 'items', 'fill', 'color' 等字段
        """
        return self.raw.get_drawings()
    
    def get_pixmap(
        self,
        dpi: int = 150,
        clip: Optional[Union[Tuple[float, float, float, float], "fitz.Rect"]] = None,
        alpha: bool = False,
    ) -> "fitz.Pixmap":
        """
        渲染页面为位图。
        
        Args:
            dpi: 渲染分辨率
            clip: 裁剪区域 (x0, y0, x1, y1) 或 fitz.Rect
            alpha: 是否包含透明通道
        
        Returns:
            fitz.Pixmap 对象
        """
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        
        # 处理 clip 参数
        clip_rect = None
        if clip is not None:
            if isinstance(clip, (tuple, list)):
                clip_rect = fitz.Rect(clip)
            else:
                clip_rect = clip
        
        return self.raw.get_pixmap(matrix=mat, clip=clip_rect, alpha=alpha)
    
    def get_bboxlog(self) -> List[Tuple[str, Tuple[float, float, float, float]]]:
        """
        获取页面元素边界框日志（PyMuPDF 新 API）。
        
        Returns:
            元素类型和边界框的列表
        """
        if hasattr(self.raw, "get_bboxlog"):
            return self.raw.get_bboxlog()
        return []
    
    def draw_rect(
        self,
        rect: Union[Tuple[float, float, float, float], "fitz.Rect"],
        color: Tuple[float, float, float] = (1, 0, 0),
        width: float = 1.0,
        fill: Optional[Tuple[float, float, float]] = None,
    ) -> None:
        """
        在页面上绘制矩形（用于调试可视化）。
        
        Args:
            rect: 矩形边界框
            color: 边框颜色 (R, G, B)，范围 0-1
            width: 线宽
            fill: 填充颜色（可选）
        """
        if isinstance(rect, (tuple, list)):
            rect = fitz.Rect(rect)
        
        shape = self.raw.new_shape()
        shape.draw_rect(rect)
        shape.finish(color=color, width=width, fill=fill)
        shape.commit()


# ============================================================================
# 工厂函数
# ============================================================================

def open_pdf(pdf_path: Union[str, Path]) -> PDFDocument:
    """
    打开 PDF 文档。
    
    Args:
        pdf_path: PDF 文件路径
    
    Returns:
        PDFDocument 实例
    
    Raises:
        FileNotFoundError: 文件不存在
        RuntimeError: PDF 打开失败
    
    Example:
        with open_pdf("paper.pdf") as doc:
            for page in doc:
                print(page.get_text())
    """
    path_str = str(pdf_path)
    
    if not Path(path_str).exists():
        raise FileNotFoundError(f"PDF not found: {path_str}")
    
    try:
        raw_doc = fitz.open(path_str)
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF: {path_str}") from e
    
    return PDFDocument(raw=raw_doc, path=path_str)


# ============================================================================
# 可选：pdfplumber 表格提取辅助（按需加载，不影响主流程）
# ============================================================================

def try_extract_tables_with_pdfplumber(
    pdf_path: str,
    page_number: int,
    table_settings: Optional[Dict[str, Any]] = None,
) -> Optional[List[List[List[str]]]]:
    """
    可选：使用 pdfplumber 提取表格结构。
    
    仅用于"表格结构分析/调试"，不影响现有表格截图主流程。
    
    Args:
        pdf_path: PDF 文件路径
        page_number: 页码（1-based）
        table_settings: pdfplumber 表格设置（可选）
    
    Returns:
        表格数据列表，每个表格是二维字符串列表；
        如果 pdfplumber 不可用或提取失败，返回 None
    
    Example:
        tables = try_extract_tables_with_pdfplumber("paper.pdf", page_number=3)
        if tables:
            for table in tables:
                for row in table:
                    print(row)
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        # pdfplumber 不可用，静默返回 None
        return None
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_number < 1 or page_number > len(pdf.pages):
                return None
            
            page = pdf.pages[page_number - 1]
            
            # 默认设置
            settings = {
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "snap_tolerance": 3,
            }
            if table_settings:
                settings.update(table_settings)
            
            return page.extract_tables(table_settings=settings)
    except Exception:
        return None


def try_debug_tablefinder_with_pdfplumber(
    pdf_path: str,
    page_number: int,
    output_path: str,
    table_settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    可选：使用 pdfplumber 生成表格调试可视化图像。
    
    Args:
        pdf_path: PDF 文件路径
        page_number: 页码（1-based）
        output_path: 输出图像路径
        table_settings: pdfplumber 表格设置（可选）
    
    Returns:
        True 如果成功生成，False 如果失败或 pdfplumber 不可用
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        return False
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_number < 1 or page_number > len(pdf.pages):
                return False
            
            page = pdf.pages[page_number - 1]
            im = page.to_image(resolution=150)
            
            settings = {
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
            }
            if table_settings:
                settings.update(table_settings)
            
            im.debug_tablefinder(table_settings=settings)
            im.save(output_path)
            return True
    except Exception:
        return False


# ============================================================================
# 便捷函数（兼容现有代码）
# ============================================================================

def rect_to_list(r: "fitz.Rect") -> List[float]:
    """
    将 fitz.Rect 转换为列表。
    
    Args:
        r: fitz.Rect 对象
    
    Returns:
        [x0, y0, x1, y1] 列表，保留一位小数
    """
    return [round(float(r.x0), 1), round(float(r.y0), 1),
            round(float(r.x1), 1), round(float(r.y1), 1)]


def create_rect(
    x0: float, y0: float, x1: float, y1: float
) -> "fitz.Rect":
    """
    创建 fitz.Rect 对象。
    
    Args:
        x0, y0: 左上角坐标
        x1, y1: 右下角坐标
    
    Returns:
        fitz.Rect 对象
    """
    return fitz.Rect(x0, y0, x1, y1)


def intersect_rects(r1: "fitz.Rect", r2: "fitz.Rect") -> "fitz.Rect":
    """
    计算两个矩形的交集。
    
    Args:
        r1, r2: fitz.Rect 对象
    
    Returns:
        交集 fitz.Rect（可能为空）
    """
    return r1 & r2


def union_rects(r1: "fitz.Rect", r2: "fitz.Rect") -> "fitz.Rect":
    """
    计算两个矩形的并集。
    
    Args:
        r1, r2: fitz.Rect 对象
    
    Returns:
        并集 fitz.Rect
    """
    return r1 | r2


def is_rect_empty(r: "fitz.Rect") -> bool:
    """
    检查矩形是否为空。
    
    Args:
        r: fitz.Rect 对象
    
    Returns:
        True 如果矩形为空或无效
    """
    return r.is_empty or r.is_infinite
