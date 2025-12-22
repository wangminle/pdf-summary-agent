下面给你一套“实战可落地”的 Python 方案，以及图片/表格抽取的常见流程要点（含官方文档出处）。

# 一、该用哪些库（各司其职）

* **文本**

  * `pdfminer.six`：经典、稳健，能拿到字符级坐标/字体信息；适合纯文本解析或需要版面细节时用。 ([pdfminersix.readthedocs.io][1])
  * `PyMuPDF (fitz)`：速度快，API统一；既能抽文本，也能拿到块/词坐标并配合图片/矢量对象信息，做“版面+元素”联合抽取很方便。 ([PyMuPDF 文档][2])
  * 备选：`pypdf`/`PyPDF2` 也可做文本抽取，但版面/图像能力不如上两者强。 ([pypdf.readthedocs.io][3])

* **图片 / 图表（含矢量渲染回退）**

  * **首选** `PyMuPDF`：直接枚举页面中显示的图片对象、解码、拿到 bbox 和哈希去重。 ([PyMuPDF 文档][4])
  * **渲染回退** `pypdfium2` 或 `pdf2image`：把整页渲染成位图，再二次分割；用于页面只有矢量图或需要统一像素坐标时。`pypdfium2` 绑定 PDFium；`pdf2image` 是 Poppler 的包装。 ([pypdfium2.readthedocs.io][5])

* **表格（结构化成 DataFrame/CSV）**

  * **Camelot**：文本型 PDF 表格；两种解析器——`lattice`（线框明显）与 `stream`（靠空白对齐）。提供准确率/空白度等指标。1.0 起默认图像后端改为 pdfium，安装更顺滑。 ([camelot-py.readthedocs.io][6])
  * **tabula-py**：Tabula 的 Python 包装，稳定好用，适合批量抽表直达 DataFrame/CSV。 ([tabula-py.readthedocs.io][7])
  * **pdfplumber**：基于 pdfminer.six，能读字符、线段、矩形；对“非标准表格”可自定义规则，兼顾文本与表格调试视图（但更偏“机生PDF”，对扫描件需先 OCR）。 ([GitHub][8])

* **OCR / 扫描件处理（当 PDF 是扫描或表格是图片时）**

  * 轻量：`pytesseract`（Tesseract 的 Python 包装）。 ([Tesseract OCR][9])
  * 深度学习：`docTR`（Mindee，检测+识别端到端）；需要更强鲁棒性时可选。 ([mindee.github.io][10])
  * **表格OCR结构化**：`PaddleOCR` 的 PP-Structure/表格结构识别，能从表格图片还原 HTML/CSV。 ([paddlepaddle.github.io][11])
  * **版面检测（可选增强）**：`layoutparser`（预训练 PubLayNet/DocLayNet 模型）先定位“表格/图/段落/标题”等，再分别处理。 ([GitHub][12])

---

# 二、图片与表格的“通用抽取流程”

## A. 图片/图表抽取（优先直接抽嵌入图像，必要时整页渲染）

1. **识别图片对象**：用 `PyMuPDF` 列出页面显示的所有图片（包括 xref、尺寸、bbox、可选 MD5 digest 便于去重）。 ([PyMuPDF 文档][4])
2. **解码与导出**：根据 xref 直接提取原始编码（JPEG、JPX、CCITT、JBIG2 等）或转为 PNG/TIFF；保留原始格式优先。 ([PyMuPDF 文档][13])
3. **位置与裁剪**：记录图片在页内的 bbox（后续可与图题匹配、或复原版面）。 ([PyMuPDF 文档][4])
4. **去重与命名**：用 `hashes=True` 生成 digest，同页/跨页去重。 ([PyMuPDF 文档][4])
5. **矢量图/绘图对象回退**：如果是矢量绘制而非嵌入图，或需要统一像素坐标，使用 `pypdfium2/pdf2image` 把页面渲染为位图，再做目标分割/裁切。 ([pypdfium2.readthedocs.io][5])

> 最小示例（思路级）：
>
> * PyMuPDF：`page.get_image_info()` → `doc.extract_image(xref)` 保存；
> * 若无嵌入图：用 `pypdfium2.PdfDocument(page).render()` 渲染整页，再识别/裁切。 ([PyMuPDF 文档][4])

## B. 表格抽取（按“文本型 vs 扫描/图片型”分流）

**① 判别页面类型**：若 `pdfminer.six`/`PyMuPDF` 能提取正常文本，优先走文本型流程；否则走 OCR 流程。 ([pdfminersix.readthedocs.io][1])

**② 文本型表格**

* **Camelot**：

  * 线框清晰 → `lattice`（默认）；线框不清晰/无线框 → `stream`；可按区域、页码、背景线等参数微调。 ([camelot-py.readthedocs.io][14])
* **tabula-py**：快速批处理到 DataFrame/CSV；可指定页码与区域模板。 ([tabula-py.readthedocs.io][7])
* **pdfplumber**：当表格不规整或要结合文本/线段细粒度信息时，用它写规则更灵活（但不自带 OCR）。 ([GitHub][8])

**③ 扫描/图片型表格**

* **OCR + 结构化识别**：

  1. `docTR`/`pytesseract` 做文字检测+识别；
  2. `PaddleOCR PP-Structure` 做表格结构（行列/单元格）解析，导出 HTML/CSV。 ([mindee.github.io][10])
* **可选：版面先验**：用 `layoutparser`（PubLayNet/DocLayNet 预训练）先找出“表格”区域，再送入表格结构模型，能显著提升多版面论文的定位稳定性。 ([layout-parser.readthedocs.io][15])

**④ 质量控制与修补**

* Camelot 自带 `accuracy/whitespace` 等指标；跨页表格用 pandas 合并；遇到合并单元格/多行头可二次规则处理。 ([camelot-py.readthedocs.io][16])
* 遇到“机生 PDF 里表格线被画成曲线/背景线”的棘手样例，切换 Camelot 解析器或转用 pdfplumber 自定义规则。 ([camelot-py.readthedocs.io][16])

---

# 三、几段最小可用代码（思路）

**1) 抽文本（两条线）**

```python
# pdfminer.six
from pdfminer.high_level import extract_text
text = extract_text("paper.pdf")  # 一步到位
```

或

```python
# PyMuPDF：拿词/块+坐标
import fitz
doc = fitz.open("paper.pdf")
page = doc[0]
words = page.get_text("words")  # [(x0,y0,x1,y1,"word", block_no, line_no, word_no), ...]
```

([pdfminersix.readthedocs.io][1])

**2) 抽嵌入图片（含 bbox 与去重）**

```python
import fitz, hashlib, pathlib
doc = fitz.open("paper.pdf")
out = pathlib.Path("images"); out.mkdir(exist_ok=True)
for pno, page in enumerate(doc):
    for info in page.get_image_info(hashes=True):  # 包含 xref、bbox、digest 等
        xref = info["xref"]
        img = doc.extract_image(xref)
        ext = img["ext"]; data = img["image"]
        name = (out / f"p{pno+1}_{info['digest'].hex()}.{ext}")
        name.write_bytes(data)
        # 位置：info["bbox"] 可用于回填图题或裁切
```

([PyMuPDF 文档][4])

**3) 文本型表格（两行起步）**

```python
import camelot
tables = camelot.read_pdf("paper.pdf", pages="all", flavor="lattice")  # 或 flavor="stream"
tables[0].to_csv("table1.csv")
```

([camelot-py.readthedocs.io][14])

**4) 扫描表格（OCR + 结构化）**
思路：`pdf2image` 渲染 → `docTR/pytesseract` 识别 → `PaddleOCR PP-Structure` 还原表格为 HTML/CSV（官方教程含示例脚本）。 ([pdf2image.readthedocs.io][17])

---

# 四、实践建议（踩坑避雷）

* **先判别类型，再选路径**：能选“直接抽取”就别“渲染+OCR”；OCR 做兜底。 ([pypdf2.readthedocs.io][18])
* **Camelot/Tabula 适合文本型**；扫描件请直走 OCR + 表格结构识别。 ([PyPI][19])
* **安装依赖**：Camelot 1.0+ 默认用 pdfium 后端，Linux 上 `pip install camelot-py[base]` 更省事（老文档里提到的 Ghostscript 现为可选）。 ([camelot-py.readthedocs.io][20])
* **矢量图与嵌入图的区别**：嵌入图可直接提取；纯矢量绘图需整页渲染再分割。用 `pypdfium2/pdf2image` 统一像素坐标便于后处理。 ([pypdfium2.readthedocs.io][5])
* **调参优先级**：Camelot `flavor`（lattice/stream）→ `table_areas`/`process_background` 等高阶参数；pdfplumber 则通过线段/矩形/字符坐标自定义规则。 ([camelot-py.readthedocs.io][16])

如果你给我一个样例论文 PDF（最好包含文字、表格与图），我可以按上面的流程直接跑一版，产出图片包、表格 CSV 与整文 TXT/JSON 清单，顺便给你对比 `Camelot` vs `pdfplumber` 在你这类版式上的效果。

[1]: https://pdfminersix.readthedocs.io/en/latest/tutorial/highlevel.html?utm_source=chatgpt.com "Extract text from a PDF using Python"
[2]: https://pymupdf.readthedocs.io/en/latest/app1.html?utm_source=chatgpt.com "Appendix 1: Details on Text Extraction - PyMuPDF"
[3]: https://pypdf.readthedocs.io/en/stable/user/extract-text.html?utm_source=chatgpt.com "Extract Text from a PDF — pypdf 6.1.1 documentation"
[4]: https://pymupdf.readthedocs.io/en/latest/page.html?utm_source=chatgpt.com "Page - PyMuPDF documentation"
[5]: https://pypdfium2.readthedocs.io/?utm_source=chatgpt.com "pypdfium2 — pypdfium2 documentation"
[6]: https://camelot-py.readthedocs.io/?utm_source=chatgpt.com "Camelot: PDF Table Extraction for Humans — Camelot 1.0.9 ..."
[7]: https://tabula-py.readthedocs.io/?utm_source=chatgpt.com "tabula-py: Read tables in a PDF into DataFrame — tabula-py ..."
[8]: https://github.com/jsvine/pdfplumber?utm_source=chatgpt.com "jsvine/pdfplumber - and easily extract text and tables."
[9]: https://tesseract-ocr.github.io/?utm_source=chatgpt.com "Tesseract OCR: Tesseract documentation"
[10]: https://mindee.github.io/doctr/?utm_source=chatgpt.com "docTR: Document Text Recognition - GitHub Pages"
[11]: https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/module_usage/table_structure_recognition.html?utm_source=chatgpt.com "Table Structure Recognition Module Tutorial"
[12]: https://github.com/Layout-Parser/layout-parser?utm_source=chatgpt.com "Layout-Parser/layout-parser: A Unified Toolkit for Deep ..."
[13]: https://pymupdf.readthedocs.io/en/latest/recipes-images.html?utm_source=chatgpt.com "Images - PyMuPDF documentation"
[14]: https://camelot-py.readthedocs.io/en/master/user/quickstart.html?utm_source=chatgpt.com "Quickstart — Camelot 1.0.9 documentation - Read the Docs"
[15]: https://layout-parser.readthedocs.io/en/latest/example/deep_layout_parsing/?utm_source=chatgpt.com "Deep Layout Parsing — Layout Parser 0.3.2 documentation"
[16]: https://camelot-py.readthedocs.io/en/master/user/advanced.html?utm_source=chatgpt.com "Advanced Usage — Camelot 1.0.9 documentation"
[17]: https://pdf2image.readthedocs.io/en/latest/index.html?utm_source=chatgpt.com "pdf2image's documentation — pdf2image latest documentation"
[18]: https://pypdf2.readthedocs.io/en/3.x/user/extract-text.html?utm_source=chatgpt.com "Extract Text from a PDF — PyPDF2 documentation"
[19]: https://pypi.org/project/camelot-py/?utm_source=chatgpt.com "camelot-py"
[20]: https://camelot-py.readthedocs.io/en/master/user/install.html?utm_source=chatgpt.com "Installation — Camelot 1.0.9 documentation - Read the Docs"
