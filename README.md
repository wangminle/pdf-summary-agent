# pdf-summary-agent

> 从研究论文 PDF 中提取文本与图表 PNG，并生成带图摘要的 AI Agent 工具。

## 项目概述

**pdf-summary-agent** 是一款专为学术论文设计的图表提取与摘要生成工具。它能够：

- 🖼️ **智能提取图表**：基于图注定位（Caption-Anchored），支持 Figure 和 Table
- 📝 **提取结构化文本**：保留段落结构、页眉页脚检测、双栏重排
- 🤖 **AI 摘要生成**：与 LLM Agent（如 Claude/GPT/Gemini）配合，生成带图的中英文摘要
- 🔧 **模块化架构**：V0.2.0 重构，提供 PDF 后端抽象层，便于扩展

## 版本信息

- **当前版本**：V0.2.0（重构版）
- **Python 要求**：3.12+（推荐），3.10+ 兼容
- **核心依赖**：PyMuPDF (pymupdf)
- **平台支持**：macOS / Linux / Windows

## 目录结构

```
pdf-summary-agent/
├── scripts/                    # V0.2.0 重构后的脚本目录
│   ├── __init__.py
│   ├── requirements.txt        # 依赖清单
│   ├── core/                   # 兼容入口（预留）
│   │   └── __init__.py
│   └── lib/                    # 核心模块库
│       ├── __init__.py
│       ├── pdf_backend.py      # PDF 后端抽象层（PyMuPDF + pdfplumber 可选）
│       ├── env_priority.py     # ENV 优先级与参数处理
│       ├── models.py           # 数据结构定义（15+ dataclass）
│       ├── idents.py           # 标识符与正则表达式
│       └── extraction_logger.py # 统一日志系统
├── scripts-old/                # V0.1.x 旧版脚本（完整功能，不再修改）
│   ├── extract_pdf_assets.py   # 主提取脚本
│   ├── generate_rename_plan.py # 重命名计划生成
│   ├── sync_index_after_rename.py
│   └── ...
├── docs/                       # 文档目录
│   ├── extract_pdf_assets_CLI参数说明.md
│   └── ...
├── AGENTS.md                   # Agent 工作流指南
└── README.md                   # 本文件
```

## 快速安装

```bash
# 安装核心依赖
python3 -m pip install --user pymupdf

# 或使用 requirements.txt
python3 -m pip install --user -r scripts/requirements.txt
```

## 快速开始

### 基本用法（推荐）

```bash
# macOS/Linux
python3 scripts-old/extract_pdf_assets.py --pdf <PDF_DIR>/<paper>.pdf --preset robust

# Windows/PowerShell
python .\scripts-old\extract_pdf_assets.py --pdf ".\<paper>.pdf" --preset robust
```

### 输出文件

执行后会在 PDF 所在目录生成：

| 文件 | 说明 |
|------|------|
| `text/<paper>.txt` | 纯文本 |
| `text/gathered_text.json` | 结构化段落（含页眉页脚移除、双栏重排） |
| `images/*.png` | Figure_* 与 Table_* 图表 PNG |
| `images/index.json` | 统一索引（含可追溯元数据） |
| `images/figure_contexts.json` | 图表首次提及上下文 |
| `images/layout_model.json` | 文档版式模型 |

### 常用参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--preset robust` | 启用稳健预设（推荐） | - |
| `--layout-driven [auto\|on\|off]` | 版式驱动模式 | `on` |
| `--debug-visual` | 可视化调试模式 | `False` |
| `--allow-continued` | 允许同图号多页导出 | `False` |
| `--below 2,3` | 强制从图注下方取图 | - |
| `--t-above 1,S1` | 强制从表注上方取表 | - |

完整参数说明请参阅 [`docs/extract_pdf_assets_CLI参数说明.md`](docs/extract_pdf_assets_CLI参数说明.md)。

## 核心特性

### 1. 智能图注识别（Smart Caption Detection）

- **四维评分机制**：位置特征（40分）+ 格式特征（30分）+ 结构特征（20分）+ 上下文特征（10分）
- **区分真实图注与引用**：自动识别正文中的 "如图1所示" 与实际图注 "Figure 1: Overview"
- **支持复杂格式**：罗马数字（Figure I）、S前缀（Figure S1）、中文（图1）、附录（Table A1）

### 2. 版式驱动提取（Layout-Driven V2）

- **三态控制**：`on`（始终启用）/ `auto`（复杂版式自动启用）/ `off`
- **文档版式建模**：检测双栏、典型字号、行高、留白区域
- **智能裁剪**：主动避开正文段落（如 Abstract、章节标题）

### 3. 自适应行高

- 自动统计文档的典型行高、字号、行距
- 动态调整裁切参数：`adjacent_th`、`far_text_th`、`text_trim_gap` 等
- 适配单栏/双栏、不同字号的论文

### 4. 远距文字清除（Phase C）

- 基于全局锚点方向，自动识别多余文字位置
- 三阶段 Trim 策略：Phase A（紧邻）→ Phase B（近侧）→ Phase C（远侧大段正文）
- 安全保护：最多 trim 50% 原始窗口高度

### 5. 可视化调试模式

启用 `--debug-visual` 后，在 `images/debug/` 生成：
- 多阶段边界框叠加图
- 图例文件（各阶段尺寸和描述）

**颜色方案**：
- 🔵 蓝色 = Baseline（锚点选择）
- 🟢 绿色 = Phase A（文本裁切）
- 🟠 橙色 = Phase B（对象对齐）
- 🔴 红色 = Phase D（Autocrop 最终）
- 🟡 黄色 = Fallback（回退）
- 🟣 紫色 = Caption（图注位置）

## V0.2.0 模块化架构

### `scripts/lib/` 核心模块

| 模块 | 功能 |
|------|------|
| `pdf_backend.py` | PDF 后端抽象层：`PDFDocument`、`PDFPage` 封装，支持 PyMuPDF 主引擎 + pdfplumber 可选 |
| `env_priority.py` | 参数优先级：CLI > ENV > 默认值；`apply_preset_robust()` 预设 |
| `models.py` | 数据结构：`AttachmentRecord`、`CaptionCandidate`、`DocumentLayoutModel` 等 15+ dataclass |
| `idents.py` | 标识符与正则：`FIGURE_LINE_RE`、`TABLE_LINE_RE`、罗马数字转换、QC 引用统计 |
| `extraction_logger.py` | 统一日志：结构化事件、上下文感知、JSONL 输出 |

### 使用示例

```python
from scripts.lib.pdf_backend import open_pdf
from scripts.lib.models import AttachmentRecord
from scripts.lib.idents import FIGURE_LINE_RE, extract_figure_ident

# 打开 PDF
with open_pdf("paper.pdf") as doc:
    print(f"Pages: {doc.page_count}")
    for page in doc:
        text_dict = page.get_text_dict()
        # ...
```

## Agent 工作流

详细的 Agent 工作流说明请参阅 [AGENTS.md](AGENTS.md)，包括：

- 目标与产出
- 环境与命令差异（macOS/Linux vs Windows）
- 一键稳健预设参数
- 方向与续页控制
- 智能 Caption 识别
- 版式驱动提取
- 可视化调试模式
- 图表重命名工作流
- 生成带图摘要的提示词模板

## 许可证

MIT License - 详见 [LICENSE](LICENSE)

## 更新日志

### V0.2.0（2026-01-09）

- **架构重构**：模块化设计，抽离核心组件到 `scripts/lib/`
- **PDF 后端抽象层**：`pdf_backend.py`（PyMuPDF 主 + pdfplumber 可选）
- **ENV 优先级统一**：`env_priority.py`（CLI > ENV > 默认值）
- **数据结构集中**：`models.py`（15+ dataclass）
- **标识符正则抽离**：`idents.py`（支持罗马数字、S前缀、中文）

### V0.1.7（2025-12-30）

- **P3-01**：修复 `--layout-driven off` 日志记录问题

### V0.1.6（2025-12-29）

- **P1-01~11**：版式分析强化、Gathering 阶段、PDF 预验证、QC 独立化、图表上下文锚点、重命名工作流半自动化
- **QA-06**：QC 引用检测增强（罗马数字、S前缀、Extended Data）
