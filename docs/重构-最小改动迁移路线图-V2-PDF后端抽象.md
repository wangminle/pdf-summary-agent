# 重构路线图 V2：PDF 后端抽象层（PyMuPDF + pdfplumber 双引擎）

> 基于 2026-01-05 最新开源库调研，在原有重构方案基础上新增 **PDF 后端抽象层**，支持 PyMuPDF 和 pdfplumber 双引擎切换。

---

## 0. 核心库能力对比

### 0.0 结论与边界（先说结论）

- 在“最小改动 + 保持现有输出合同”前提下，**pdfplumber 不适合作为完整后端替换 PyMuPDF**：它缺少“提取嵌入图像内容/高质量渲染/与现有算法强绑定的 API（如 `get_drawings()`）”等能力，且解析性能显著更慢。
- 合理落地方式是：**PyMuPDF 继续作为唯一主引擎**（文本/渲染/矢量/图像），新增一个**薄适配层**统一资源管理与调用点；pdfplumber 仅作为**可选的表格结构分析/调试组件**按需调用（不进入主路径，不影响现有 PNG 裁剪产物）。
- 若引入抽象层的真实动机是“规避 PyMuPDF 的 AGPL”，需要额外引入**可替代渲染与图像提取**的引擎（例如 `pypdfium2`、`pdf2image+poppler` 等）；**仅靠 pdfplumber 无法覆盖本项目当前能力面**。

### 0.1 PyMuPDF (fitz) — 当前主力引擎

| 维度 | 能力 |
|------|------|
| **官方仓库** | [pymupdf/PyMuPDF](https://github.com/pymupdf/PyMuPDF) |
| **底层引擎** | MuPDF（C 语言，Artifex 维护） |
| **Python 版本** | 本项目目标 Python 3.12+（库自身支持范围以官方为准） |
| **许可证** | **AGPL**（商用需购买许可） |
| **性能** | 高（C 引擎；更适合主路径大规模处理） |
| **文本提取** | `page.get_text()` 多种模式（text/dict/blocks/words/html/xhtml） |
| **带坐标文本** | `page.get_text("dict")` 返回 bbox、fontname、size、color |
| **图像提取** | `page.get_images()` + `doc.extract_image()` 支持提取嵌入图像内容 |
| **矢量图形** | `page.get_drawings()` 返回路径、rect、fill/stroke |
| **页面渲染** | `page.get_pixmap()` 渲染为 PNG/JPEG |
| **表格提取** | 原生不提供“单元格级表格”；本项目当前做的是“表格截图”，不依赖该能力 |
| **可视化调试** | 需要手动绘制（`page.draw_rect()` 等），但可控性强 |
| **PDF 修改** | 支持添加注释、合并、拆分、加密等 |
| **OCR 支持** | 可集成外部 OCR |

**关键 API 示例**：
```python
import pymupdf

doc = pymupdf.open("paper.pdf")
page = doc[0]

# 1. 文本提取（带坐标）
text_dict = page.get_text("dict")
for block in text_dict["blocks"]:
    if block["type"] == 0:  # 文本块
        bbox = block["bbox"]  # (x0, y0, x1, y1)
        for line in block["lines"]:
            for span in line["spans"]:
                print(f"Text: {span['text']}, Font: {span['font']}, Size: {span['size']}")

# 2. 图像提取
images = page.get_images(full=True)
for img_index, img in enumerate(images):
    xref = img[0]
    base_image = doc.extract_image(xref)
    image_bytes = base_image["image"]
    image_ext = base_image["ext"]

# 3. 矢量图形提取
drawings = page.get_drawings()
for path in drawings:
    rect = path["rect"]  # 边界框
    items = path["items"]  # 路径命令

# 4. 页面渲染为 PNG
pix = page.get_pixmap(dpi=300)
pix.save("page.png")

# 5. 边界框日志（新 API）
bboxlog = page.get_bboxlog()  # [(type, (x0,y0,x1,y1)), ...]
```

### 0.2 pdfplumber — 表格提取专家

| 维度 | 能力 |
|------|------|
| **官方仓库** | [jsvine/pdfplumber](https://github.com/jsvine/pdfplumber) |
| **底层引擎** | pdfminer.six（纯 Python） |
| **Python 版本** | 本项目目标 Python 3.12+（库自身支持范围以官方为准） |
| **许可证** | **MIT**（商用友好） |
| **性能** | 中-低（纯 Python 解析；不建议放在主路径） |
| **文本提取** | `page.extract_text()` 支持 layout 模式 |
| **带坐标文本** | `page.chars` 提供字符级信息 |
| **图像提取** | 仅提供图像位置（不提供嵌入图像 bytes），无法直接替代 `doc.extract_image()` |
| **矢量图形** | `page.lines`/`page.rects`/`page.curves` |
| **页面渲染** | `page.to_image()`（质量/性能依赖底层与参数） |
| **表格提取** | 强项：`page.extract_tables()`/`TableFinder` |
| **可视化调试** | 强项：`im.debug_tablefinder()` |
| **PDF 修改** | 不支持 |
| **OCR 支持** | 不支持 |

**关键 API 示例**：
```python
import pdfplumber

with pdfplumber.open("paper.pdf") as pdf:
    page = pdf.pages[0]
    
    # 1. 文本提取（带布局）
    text = page.extract_text(layout=True)
    
    # 2. 字符级信息
    for char in page.chars:
        print(f"Char: {char['text']}, Bbox: ({char['x0']}, {char['top']}, {char['x1']}, {char['bottom']})")
        print(f"  Font: {char['fontname']}, Size: {char['size']}")
    
    # 3. 表格提取（核心优势）
    tables = page.extract_tables(table_settings={
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "snap_tolerance": 3,
    })
    
    # 4. 可视化调试
    im = page.to_image(resolution=150)
    im.draw_rects(page.chars)  # 绘制字符边界框
    im.debug_tablefinder()     # 调试表格检测
    im.save("debug.png")
    
    # 5. 裁剪页面
    cropped = page.crop((0, 100, page.width, 400))
    cropped_text = cropped.extract_text()
    
    # 6. 矢量对象
    lines = page.lines      # 线段
    rects = page.rects      # 矩形
    curves = page.curves    # 曲线
    images = page.images    # 图像位置（不含内容）
```

### 0.3 pymupdf4llm — LLM 专用扩展

| 维度 | 能力 |
|------|------|
| **官方仓库** | [pymupdf/pymupdf4llm](https://github.com/pymupdf/pymupdf4llm) |
| **底层引擎** | PyMuPDF |
| **核心功能** | PDF → Markdown（针对 LLM 优化） |
| **多栏处理** | 自动处理多栏布局 |
| **表格输出** | Markdown 表格格式 |
| **图像处理** | 可提取并内联或保存 |
| **分块输出** | `page_chunks=True` 按页分块 |

```python
import pymupdf4llm

# 转换为 Markdown（适合喂给 LLM）
md_text = pymupdf4llm.to_markdown("paper.pdf", write_images=True)

# 按页分块
chunks = pymupdf4llm.to_markdown("paper.pdf", page_chunks=True)
for chunk in chunks:
    page_num = chunk["metadata"]["page"]
    content = chunk["text"]
```

---

## 1. 推荐架构：双引擎混合策略

基于能力对比，推荐采用 **PyMuPDF 为主 + pdfplumber 补充** 的混合架构：

```
┌─────────────────────────────────────────────────────────────┐
│                    PDF Backend Abstraction                   │
│                      scripts/lib/pdf_backend.py              │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│   ┌─────────────────┐         ┌─────────────────┐          │
│   │   PyMuPDF (主)   │         │  pdfplumber (辅) │          │
│   ├─────────────────┤         ├─────────────────┤          │
│   │ • 文本/坐标提取   │         │ • 表格结构分析   │          │
│   │ • 图像提取/渲染   │         │ • 表格调试可视化 │          │
│   │ • 矢量图形/绘图   │         │ • （可选依赖）   │          │
│   │ • 主路径高性能    │         │                 │          │
│   └─────────────────┘         └─────────────────┘          │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     Unified Interface                        │
│                                                              │
│  • PDFDocument          - 文档封装/资源管理                 │
│  • PDFPage              - 页面封装/统一调用点               │
│  • (MVP) 透传 PyMuPDF 原始结构（如 get_text('dict') 的 dict）│
│  • (可选) pdfplumber：表格结构分析/调试辅助                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 1.1 功能分工矩阵

| 功能模块 | 主引擎 | 备选/增强 | 说明 |
|---------|--------|----------|------|
| 文本提取 | PyMuPDF | - | 速度快，信息全 |
| 带格式文本 | PyMuPDF | - | `get_text("dict")` |
| 图像提取 | PyMuPDF | - | 原生支持提取图像内容 |
| 页面渲染 | PyMuPDF | - | `get_pixmap()` 高质量 |
| 矢量图形 | PyMuPDF | pdfplumber | PyMuPDF 更详细 |
| 表格提取 | pdfplumber | - | **pdfplumber 专长** |
| 表格调试 | pdfplumber | - | `debug_tablefinder()` |
| 可视化调试 | pdfplumber | PyMuPDF | pdfplumber 更易用 |
| Caption 检测 | PyMuPDF | - | 速度优先 |
| 版式分析 | PyMuPDF | - | 性能关键 |

---

## 2. 新增重构模块：PDF 后端适配层（薄适配 + 可选 pdfplumber）

在原有重构计划（Commit 00-14）基础上，建议在 **Commit 01 与 Commit 02** 之间插入一个**可选提交**（本文记为 **Commit 01B**）。

### Commit 01B：新增 `scripts/lib/pdf_backend.py`（薄适配层）

**目标**：把“打开/关闭 PDF、获取 page、渲染、提取 text/drawings/images”等调用点集中到单一模块，避免后续引擎替换或增强时在全仓库散落改动。

**新增文件**：
- `scripts/lib/pdf_backend.py`

**设计原则**：
1. 不改变任何算法/阈值/默认值/输出字段（仍以现有 PyMuPDF 行为为准）
2. 主路径只用 PyMuPDF；pdfplumber 仅在显式调用 helper 时按需 import（缺依赖时返回 `None`，不影响主流程）
3. MVP 阶段不做“统一数据结构转换”，直接透传 PyMuPDF 原始返回（例如 `page.get_text("dict")` 的 dict），避免牵一发而动全身
4. 封装层保留 `.raw`（doc/page）以便渐进迁移：先集中调用点，再逐步替换局部实现

**坐标约定**：
- 内部统一使用 pt 单位的 `(x0, y0, x1, y1)`，原点在页面左上（与 PyMuPDF `Rect` 一致）
- pdfplumber 的 `top/bottom` 同样以页面顶部为 0，可直接映射到 `y0/y1`（无需做翻转）

**MVP（推荐，最小改动）**：

```python
# scripts/lib/pdf_backend.py (MVP)
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Optional

import fitz  # PyMuPDF（保持与现有代码一致的入口名）


@dataclass
class PDFDocument:
    raw: Any
    path: str

    @property
    def page_count(self) -> int:
        return self.raw.page_count

    @property
    def metadata(self) -> dict:
        return dict(self.raw.metadata or {})

    def __getitem__(self, index: int) -> "PDFPage":
        return PDFPage(raw=self.raw[index], doc=self)

    def __iter__(self) -> Iterator["PDFPage"]:
        for i in range(self.page_count):
            yield self[i]

    def extract_image(self, xref: int) -> dict:
        return self.raw.extract_image(xref)

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> "PDFDocument":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


@dataclass
class PDFPage:
    raw: Any
    doc: PDFDocument

    @property
    def page_number(self) -> int:
        return self.raw.number + 1

    @property
    def rect(self):
        return self.raw.rect

    def get_text_dict(self) -> dict:
        return self.raw.get_text(\"dict\")

    def get_images(self, full: bool = True):
        return self.raw.get_images(full=full)

    def get_drawings(self):
        return self.raw.get_drawings()

    def get_pixmap(self, dpi: int, clip=None):
        return self.raw.get_pixmap(dpi=dpi, clip=clip)


def open_pdf(pdf_path: str) -> PDFDocument:
    return PDFDocument(raw=fitz.open(pdf_path), path=pdf_path)


def try_extract_tables_with_pdfplumber(
    pdf_path: str,
    page_number: int,
    table_settings: Optional[dict] = None,
):
    \"\"\"可选：仅用于“表格结构分析/调试”，不影响现有表格截图主流程。\"\"\"
    try:
        import pdfplumber  # type: ignore
    except Exception:
        return None

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number - 1]
        return page.extract_tables(table_settings=table_settings or {})
```

**备选：类型化接口草案（非最小改动；仅作为长期参考）**：

```python
# scripts/lib/pdf_backend.py
"""
PDF 后端抽象层 - 支持 PyMuPDF 和 pdfplumber 双引擎
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Iterator, Union, Literal
from pathlib import Path
import logging

# ============================================================
# 统一数据结构
# ============================================================

@dataclass
class TextSpan:
    """文本片段（最小文本单元）"""
    text: str
    bbox: Tuple[float, float, float, float]  # (x0, y0, x1, y1)
    font: str = ""
    size: float = 0.0
    flags: int = 0  # bold=1, italic=2, etc.
    color: Optional[Tuple[float, ...]] = None
    
    @property
    def is_bold(self) -> bool:
        return bool(self.flags & 1)
    
    @property
    def is_italic(self) -> bool:
        return bool(self.flags & 2)


@dataclass
class TextLine:
    """文本行"""
    spans: List[TextSpan] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    
    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class TextBlock:
    """文本块"""
    lines: List[TextLine] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    block_type: Literal["text", "image"] = "text"
    
    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass
class ImageInfo:
    """图像信息"""
    bbox: Tuple[float, float, float, float]
    width: int
    height: int
    colorspace: str = ""
    bpc: int = 8  # bits per component
    xref: int = 0  # PyMuPDF 内部引用
    name: str = ""
    # 图像内容（可选，按需加载）
    data: Optional[bytes] = None
    ext: str = "png"


@dataclass
class DrawingPath:
    """矢量路径"""
    rect: Tuple[float, float, float, float]  # 边界框
    items: List[Tuple] = field(default_factory=list)  # 路径命令
    fill: Optional[Tuple[float, ...]] = None
    stroke: Optional[Tuple[float, ...]] = None
    width: float = 1.0
    closePath: bool = False


@dataclass
class LineSegment:
    """线段（简化矢量）"""
    x0: float
    y0: float
    x1: float
    y1: float
    linewidth: float = 1.0
    stroking_color: Optional[Tuple[float, ...]] = None


@dataclass 
class RectInfo:
    """矩形"""
    x0: float
    y0: float
    x1: float
    y1: float
    linewidth: float = 1.0
    stroking_color: Optional[Tuple[float, ...]] = None
    non_stroking_color: Optional[Tuple[float, ...]] = None  # fill


@dataclass
class TableCell:
    """表格单元格"""
    text: str
    bbox: Tuple[float, float, float, float]
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1


@dataclass
class TableResult:
    """表格提取结果"""
    bbox: Tuple[float, float, float, float]
    cells: List[TableCell] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)  # 二维文本数组
    page_number: int = 0


# ============================================================
# 抽象基类
# ============================================================

class PDFPageBase(ABC):
    """页面抽象基类"""
    
    @property
    @abstractmethod
    def page_number(self) -> int:
        """页码（1-based）"""
        pass
    
    @property
    @abstractmethod
    def width(self) -> float:
        """页面宽度（pt）"""
        pass
    
    @property
    @abstractmethod
    def height(self) -> float:
        """页面高度（pt）"""
        pass
    
    @property
    def rect(self) -> Tuple[float, float, float, float]:
        """页面边界框"""
        return (0, 0, self.width, self.height)
    
    @abstractmethod
    def get_text(self, mode: str = "text") -> str:
        """提取文本
        
        Args:
            mode: "text"=纯文本, "blocks"=按块, "dict"=完整结构
        """
        pass
    
    @abstractmethod
    def get_text_blocks(self) -> List[TextBlock]:
        """获取文本块（带坐标）"""
        pass
    
    @abstractmethod
    def get_images(self) -> List[ImageInfo]:
        """获取页面上的图像信息"""
        pass
    
    @abstractmethod
    def get_drawings(self) -> List[DrawingPath]:
        """获取矢量图形"""
        pass
    
    @abstractmethod
    def get_lines(self) -> List[LineSegment]:
        """获取线段"""
        pass
    
    @abstractmethod
    def get_rects(self) -> List[RectInfo]:
        """获取矩形"""
        pass
    
    @abstractmethod
    def get_pixmap(self, dpi: int = 150, clip: Optional[Tuple[float, float, float, float]] = None) -> bytes:
        """渲染页面为 PNG
        
        Args:
            dpi: 分辨率
            clip: 裁剪区域 (x0, y0, x1, y1)
        
        Returns:
            PNG 图像字节
        """
        pass
    
    def extract_tables(self, settings: Optional[dict] = None) -> List[TableResult]:
        """提取表格（默认使用 pdfplumber）"""
        raise NotImplementedError("Table extraction requires pdfplumber backend")


class PDFDocumentBase(ABC):
    """文档抽象基类"""
    
    @property
    @abstractmethod
    def page_count(self) -> int:
        """页数"""
        pass
    
    @property
    @abstractmethod
    def metadata(self) -> dict:
        """元数据"""
        pass
    
    @abstractmethod
    def __getitem__(self, index: int) -> PDFPageBase:
        """获取页面（0-based）"""
        pass
    
    @abstractmethod
    def __iter__(self) -> Iterator[PDFPageBase]:
        """迭代所有页面"""
        pass
    
    @abstractmethod
    def close(self):
        """关闭文档"""
        pass
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()
    
    def extract_image(self, xref: int) -> Optional[Tuple[bytes, str]]:
        """提取嵌入图像
        
        Args:
            xref: 图像引用 ID
        
        Returns:
            (image_bytes, extension) 或 None
        """
        raise NotImplementedError("Image extraction not supported by this backend")


# ============================================================
# PyMuPDF 实现
# ============================================================

class PyMuPDFPage(PDFPageBase):
    """PyMuPDF 页面实现"""
    
    def __init__(self, page, doc: "PyMuPDFDocument"):
        self._page = page
        self._doc = doc
    
    @property
    def page_number(self) -> int:
        return self._page.number + 1
    
    @property
    def width(self) -> float:
        return self._page.rect.width
    
    @property
    def height(self) -> float:
        return self._page.rect.height
    
    def get_text(self, mode: str = "text") -> str:
        return self._page.get_text(mode)
    
    def get_text_blocks(self) -> List[TextBlock]:
        blocks = []
        text_dict = self._page.get_text("dict")
        
        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:  # 文本块
                lines = []
                for line in block.get("lines", []):
                    spans = []
                    for span in line.get("spans", []):
                        spans.append(TextSpan(
                            text=span.get("text", ""),
                            bbox=tuple(span.get("bbox", (0, 0, 0, 0))),
                            font=span.get("font", ""),
                            size=span.get("size", 0),
                            flags=span.get("flags", 0),
                            color=span.get("color"),
                        ))
                    lines.append(TextLine(
                        spans=spans,
                        bbox=tuple(line.get("bbox", (0, 0, 0, 0))),
                    ))
                blocks.append(TextBlock(
                    lines=lines,
                    bbox=tuple(block.get("bbox", (0, 0, 0, 0))),
                    block_type="text",
                ))
        return blocks
    
    def get_images(self) -> List[ImageInfo]:
        images = []
        for img in self._page.get_images(full=True):
            xref = img[0]
            # 获取图像在页面上的位置
            img_rects = self._page.get_image_rects(xref)
            bbox = img_rects[0] if img_rects else (0, 0, 0, 0)
            
            images.append(ImageInfo(
                bbox=tuple(bbox),
                width=img[2],
                height=img[3],
                colorspace=str(img[5]),
                bpc=img[6] if len(img) > 6 else 8,
                xref=xref,
                name=img[7] if len(img) > 7 else "",
            ))
        return images
    
    def get_drawings(self) -> List[DrawingPath]:
        paths = []
        for path in self._page.get_drawings():
            paths.append(DrawingPath(
                rect=tuple(path.get("rect", (0, 0, 0, 0))),
                items=path.get("items", []),
                fill=path.get("fill"),
                stroke=path.get("color"),
                width=path.get("width", 1.0),
                closePath=path.get("closePath", False),
            ))
        return paths
    
    def get_lines(self) -> List[LineSegment]:
        """从 drawings 中提取线段"""
        lines = []
        for path in self._page.get_drawings():
            for item in path.get("items", []):
                if item[0] == "l":  # line
                    p1, p2 = item[1], item[2]
                    lines.append(LineSegment(
                        x0=p1.x, y0=p1.y,
                        x1=p2.x, y1=p2.y,
                        linewidth=path.get("width", 1.0),
                        stroking_color=path.get("color"),
                    ))
        return lines
    
    def get_rects(self) -> List[RectInfo]:
        """从 drawings 中提取矩形"""
        rects = []
        for path in self._page.get_drawings():
            # 检查是否是矩形路径
            items = path.get("items", [])
            if len(items) >= 4:
                # 简化：使用边界框
                r = path.get("rect", (0, 0, 0, 0))
                rects.append(RectInfo(
                    x0=r[0], y0=r[1], x1=r[2], y1=r[3],
                    linewidth=path.get("width", 1.0),
                    stroking_color=path.get("color"),
                    non_stroking_color=path.get("fill"),
                ))
        return rects
    
    def get_pixmap(self, dpi: int = 150, clip: Optional[Tuple[float, float, float, float]] = None) -> bytes:
        import pymupdf
        mat = pymupdf.Matrix(dpi / 72, dpi / 72)
        clip_rect = pymupdf.Rect(clip) if clip else None
        pix = self._page.get_pixmap(matrix=mat, clip=clip_rect)
        return pix.tobytes("png")
    
    def extract_tables(self, settings: Optional[dict] = None) -> List[TableResult]:
        """使用 pdfplumber 提取表格"""
        return _extract_tables_with_pdfplumber(
            self._doc._path, 
            self.page_number - 1,  # 0-based
            settings
        )


class PyMuPDFDocument(PDFDocumentBase):
    """PyMuPDF 文档实现"""
    
    def __init__(self, path: Union[str, Path]):
        import pymupdf
        self._path = str(path)
        self._doc = pymupdf.open(self._path)
    
    @property
    def page_count(self) -> int:
        return len(self._doc)
    
    @property
    def metadata(self) -> dict:
        return dict(self._doc.metadata)
    
    def __getitem__(self, index: int) -> PyMuPDFPage:
        return PyMuPDFPage(self._doc[index], self)
    
    def __iter__(self) -> Iterator[PyMuPDFPage]:
        for i in range(len(self._doc)):
            yield self[i]
    
    def close(self):
        self._doc.close()
    
    def extract_image(self, xref: int) -> Optional[Tuple[bytes, str]]:
        try:
            img = self._doc.extract_image(xref)
            return (img["image"], img["ext"])
        except Exception:
            return None


# ============================================================
# pdfplumber 表格提取辅助
# ============================================================

_pdfplumber_cache = {}

def _extract_tables_with_pdfplumber(
    pdf_path: str, 
    page_index: int, 
    settings: Optional[dict] = None
) -> List[TableResult]:
    """使用 pdfplumber 提取表格
    
    按需加载 pdfplumber，避免强制依赖
    """
    try:
        import pdfplumber
    except ImportError:
        logging.warning("pdfplumber not installed. Run: pip install pdfplumber")
        return []
    
    # 缓存打开的 PDF（同一 PDF 多页提取时复用）
    if pdf_path not in _pdfplumber_cache:
        _pdfplumber_cache[pdf_path] = pdfplumber.open(pdf_path)
    
    pdf = _pdfplumber_cache[pdf_path]
    page = pdf.pages[page_index]
    
    # 默认表格设置
    default_settings = {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "snap_tolerance": 3,
        "join_tolerance": 3,
    }
    if settings:
        default_settings.update(settings)
    
    results = []
    tables = page.find_tables(table_settings=default_settings)
    
    for table in tables:
        rows = table.extract()
        cells = []
        for row_idx, row in enumerate(rows):
            for col_idx, cell_text in enumerate(row):
                cells.append(TableCell(
                    text=cell_text or "",
                    bbox=(0, 0, 0, 0),  # pdfplumber 表格不提供单元格 bbox
                    row=row_idx,
                    col=col_idx,
                ))
        
        results.append(TableResult(
            bbox=tuple(table.bbox),
            cells=cells,
            rows=rows,
            page_number=page_index + 1,
        ))
    
    return results


def close_pdfplumber_cache():
    """关闭缓存的 pdfplumber 文档"""
    global _pdfplumber_cache
    for pdf in _pdfplumber_cache.values():
        pdf.close()
    _pdfplumber_cache.clear()


# ============================================================
# 工厂函数
# ============================================================

def open_pdf(
    path: Union[str, Path], 
    backend: Literal["pymupdf", "pdfplumber", "auto"] = "auto"
) -> PDFDocumentBase:
    """打开 PDF 文档
    
    Args:
        path: PDF 文件路径
        backend: 后端选择
            - "pymupdf": 强制使用 PyMuPDF
            - "pdfplumber": 强制使用 pdfplumber（仅限表格场景）
            - "auto": 自动选择（默认 PyMuPDF）
    
    Returns:
        PDFDocumentBase 实例
    """
    if backend == "pdfplumber":
        raise NotImplementedError("Pure pdfplumber backend not implemented. Use PyMuPDF with table extraction.")
    
    return PyMuPDFDocument(path)


# ============================================================
# 便捷函数（兼容现有代码）
# ============================================================

def extract_text_with_format(page: PDFPageBase) -> List[TextBlock]:
    """提取带格式的文本（兼容现有 extract_text_with_format）"""
    return page.get_text_blocks()


def get_page_images(page: PDFPageBase) -> List[ImageInfo]:
    """获取页面图像（兼容现有 get_page_images）"""
    return page.get_images()


def get_page_drawings(page: PDFPageBase) -> List[DrawingPath]:
    """获取页面矢量图形（兼容现有 get_page_drawings）"""
    return page.get_drawings()
```

---

## 3. 更新后的完整提交序列

```
Commit 00:  补齐 scripts/core/ 兼容入口
Commit 01:  抽离 ENV 优先级 + 有效参数打印 → lib/env_priority.py
Commit 01B:  新增 PDF 后端适配层 → lib/pdf_backend.py
Commit 02:  集中数据结构到 lib/models.py
Commit 03:  抽离 ident/正则 → lib/idents.py
Commit 04:  抽离 QC 与预验证 → lib/qc.py
Commit 05:  抽离输出与索引 → lib/output.py
Commit 06:  抽离调试可视化 → lib/debug_visual.py
Commit 07:  抽离精裁与验收 → lib/refine.py
Commit 08:  抽离 Caption Detection → lib/caption_detection.py
Commit 09:  抽离 Layout Model → lib/layout_model.py
Commit 10:  抽离文本相关 → lib/text_extract.py
Commit 11:  抽离 Figure Context → lib/figure_contexts.py
Commit 12:  拆出提取主循环 → lib/extract_figures.py + lib/extract_tables.py
Commit 13:  入口瘦身
Commit 14:  清理与文档化
```

### Commit 01B 详细变更

**新增文件**：
- `scripts/lib/pdf_backend.py`（如上所示）

**修改文件**：
- `scripts/requirements.txt`：保持仅包含 `pymupdf`（核心依赖）；`pdfplumber` 作为可选依赖不写入默认清单，避免主路径被“慢引擎”拖累

**回归验证**：
```bash
# 快速回归
python scripts/tests/run_all.py --skip-golden

# 验证 pdf_backend 模块可用
python -c "from lib.pdf_backend import open_pdf; print('OK')"
```

---

## 4. 迁移策略：渐进式替换

### 阶段 1：引入适配层（Commit 01B）
- 新增 `pdf_backend.py`
- 不修改现有代码
- 仅作为备选路径

### 阶段 2：逐步迁移（Commit 08-12）
- 在 `caption_detection.py` 中开始使用 `pdf_backend`
- 在 `extract_figures.py` 中使用 `pdf_backend`
- 保留现有 `fitz` 直接调用作为回退

**迁移示例**：
```python
# 旧代码
import fitz
doc = fitz.open(pdf_path)
page = doc[0]
text = page.get_text("dict")

# 新代码（兼容层）
from lib.pdf_backend import open_pdf
with open_pdf(pdf_path) as doc:
    page = doc[0]
    text_dict = page.get_text_dict()  # 透传 PyMuPDF: page.get_text("dict")
```

### 阶段 3：表格提取增强（可选后续）
- 若后续确实要“单元格级表格结构”（而不是表格截图），再引入 `pdfplumber`：
  - 首期只提供 `try_extract_tables_with_pdfplumber()` 作为离线分析/调试辅助
  - 不进入主提取路径，不改变 `images/Table_*.png` 的输出逻辑

---

## 5. 依赖管理

### `scripts/requirements.txt`（核心依赖）
```txt
pymupdf
```

### 可选依赖安装
```bash
# 仅安装核心依赖（默认）
pip install -r scripts/requirements.txt

# 安装全部依赖（含表格增强）
pip install pymupdf pdfplumber

# 安装 LLM 优化工具（可选）
pip install pymupdf4llm
```

---

## 6. 性能对比参考

> 说明：这里给的是“定性结论”，避免把不同时机器/文档/参数下的数字写死在路线图里。

| 操作 | PyMuPDF | pdfplumber | 备注 |
|------|---------|------------|------|
| 打开/遍历 PDF | 快 | 慢 | pdfplumber 纯 Python 解析 |
| 提取全文文本 | 快 | 慢 | 主路径应优先 PyMuPDF |
| 带坐标文本 | 快 | 慢 | PyMuPDF `get_text("dict")` 信息更直接 |
| 表格结构分析 | - | 强 | 仅在需要“单元格级结构”时启用 |
| 调试可视化（表格） | - | 强 | `debug_tablefinder()` 很有用 |
| 渲染 PNG | 强 | 取决于底层 | 本项目当前依赖 PyMuPDF 渲染能力 |

**结论**：PyMuPDF 在大多数操作上有显著性能优势，pdfplumber 的价值在于表格提取和可视化调试。

---

## 7. 许可证注意事项

| 库 | 许可证 | 商用影响 |
|----|--------|---------|
| PyMuPDF | AGPL | 需开源或购买商业许可 |
| pdfplumber | MIT | 无限制 |
| pymupdf4llm | AGPL | 同 PyMuPDF |

**建议**：
- 内部工具/开源项目：可自由使用
- 商用闭源项目：联系 Artifex 获取 PyMuPDF 商业许可，或评估“替换渲染/图像引擎”的路线（仅靠 pdfplumber 不足以覆盖本项目当前能力面）

---

## 8. 后续扩展点

1. **添加 OCR 支持**：集成 Tesseract 或 PaddleOCR
2. **添加 pdf2docx**：支持 PDF→Word 转换
3. **添加 pymupdf4llm**：直接输出 LLM 友好的 Markdown
4. **添加 camelot**：针对扫描版 PDF 的表格提取

---

## 附录：快速验证脚本

```python
#!/usr/bin/env python3
"""验证 PDF 后端适配层（MVP）"""
import sys
sys.path.insert(0, "scripts")

from lib.pdf_backend import open_pdf, try_extract_tables_with_pdfplumber

def test_backend(pdf_path: str):
    print(f"Testing: {pdf_path}")
    
    with open_pdf(pdf_path) as doc:
        print(f"  Pages: {doc.page_count}")
        print(f"  Metadata: {doc.metadata.get('title', 'N/A')}")
        
        page = doc[0]
        print(f"  Page 1 size: {page.rect.width:.1f} x {page.rect.height:.1f} pt")
        
        # 文本（dict 结构）
        text_dict = page.get_text_dict()
        blocks = text_dict.get(\"blocks\", [])
        print(f\"  Text blocks: {len(blocks)}\")
        
        # 图像
        images = page.get_images()
        print(f"  Images: {len(images)}")
        
        # 矢量图形
        drawings = page.get_drawings()
        print(f"  Drawings: {len(drawings)}")
        
        # 表格结构（可选：需要 pdfplumber）
        tables = try_extract_tables_with_pdfplumber(pdf_path, page_number=1)
        if tables is None:
            print(\"  Tables: N/A (pdfplumber not installed)\")
        else:
            print(f\"  Tables: {len(tables)}\")

    print("  Test passed")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_backend.py <pdf_path>")
        sys.exit(1)
    test_backend(sys.argv[1])
```
