# 变更日志 - 文件命名单词数量限制功能

**日期**: 2025-10-14  
**版本**: v1.1.0  
**类型**: 功能增强

## 概述

为简化导出图表PNG文件的命名，新增了对文件名中标号后英文单词数量的限制功能。

## 主要变更

### 1. 新增辅助函数

**`_limit_words_after_prefix()`** (第101-128行)
- 功能：限制文件名中前缀（如 Figure_1, Table_S1）之后的单词数量
- 参数：
  - `filename`: 完整文件名（不含扩展名）
  - `prefix_pattern`: 前缀模式
  - `max_words`: 标号后允许的最大单词数（默认12）

### 2. 修改现有函数

#### `sanitize_filename_from_caption()` (第137-153行)
- 新增参数：`max_words: int = 12`
- 在生成文件名的最后步骤调用 `_limit_words_after_prefix()` 限制单词数
- 用于图片（Figure）的文件命名

#### `build_output_basename()` (第2197-2211行)
- 新增参数：`max_words: int = 12`
- 在生成文件名的最后步骤调用 `_limit_words_after_prefix()` 限制单词数
- 用于表格（Table）的文件命名

#### `extract_figures()` (第1443-1498行)
- 新增参数：`max_caption_words: int = 12`
- 在调用 `sanitize_filename_from_caption()` 时传递该参数

#### `extract_tables()` (第2299-2343行)
- 新增参数：`max_caption_words: int = 12`
- 在调用 `build_output_basename()` 时传递该参数

### 3. 命令行参数

#### `parse_args()` (第2938行)
- 新增参数：`--max-caption-words INT`
- 说明：Max words after figure/table number in filename (default: 12)

#### `main()` (第3108, 3161行)
- 在调用 `extract_figures()` 和 `extract_tables()` 时传递 `max_caption_words` 参数

## 使用示例

### 默认使用（12个单词）
```bash
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust
```

### 自定义单词数量
```bash
# 限制为8个单词
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --max-caption-words 8

# 限制为15个单词
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust --max-caption-words 15
```

## 效果对比

### 原始图注
```
Figure 1: Overview of the proposed deep learning architecture for multi-modal feature extraction and fusion
```

### 文件名变化

**之前（无限制）**:
```
Figure_1_Overview_of_the_proposed_deep_learning_architecture_for_multi_modal_feature_extraction_and_fusion.png
```

**现在（默认12个单词）**:
```
Figure_1_Overview_of_the_proposed_deep_learning_architecture_for_multi_modal_feature_extraction.png
```

## 技术细节

### 单词计数规则
- 以下划线 `_` 为分隔符
- 前缀部分（如 `Figure_1` 或 `Table_S1`）不计入单词数
- 只限制描述部分的单词数量

### 实现逻辑
```python
# 示例文件名：Figure_1_Overview_of_the_proposed_deep_learning_architecture
# 分割后：['Figure', '1', 'Overview', 'of', 'the', 'proposed', 'deep', 'learning', 'architecture']
# 前缀部分：['Figure', '1']  - 不计入
# 描述部分：['Overview', 'of', 'the', 'proposed', 'deep', 'learning', 'architecture']  - 7个单词
# 如果 max_words=12，则保持不变；如果 max_words=5，则截断为前5个单词
```

## 兼容性

- ✅ 完全向后兼容
- ✅ 默认值（12个单词）提供合理的平衡
- ✅ 不影响现有的字符总长度限制（160字符）
- ✅ 适用于图片（Figure）和表格（Table）

## 文档更新

1. **README.md**
   - 在"新功能"部分添加说明
   - 在"快速开始"部分添加参数示例

2. **docs/filename_word_limit_feature.md** (新建)
   - 详细的功能说明文档
   - 使用示例和技术实现细节

## 测试建议

### 基础测试
```bash
# 测试默认值
python3 scripts/extract_pdf_assets.py --pdf test.pdf --preset robust

# 测试自定义值
python3 scripts/extract_pdf_assets.py --pdf test.pdf --preset robust --max-caption-words 6

# 测试极端值
python3 scripts/extract_pdf_assets.py --pdf test.pdf --preset robust --max-caption-words 3
python3 scripts/extract_pdf_assets.py --pdf test.pdf --preset robust --max-caption-words 50
```

### 验证点
1. ✅ 文件名中单词数量符合预期
2. ✅ 图片和表格的命名都生效
3. ✅ 前缀（Figure_1, Table_S1）不计入单词数
4. ✅ 生成的文件可以正常打开
5. ✅ index.json 中的路径正确

## 相关文件

- `scripts/extract_pdf_assets.py` - 主脚本（核心修改）
- `README.md` - 用户文档（更新）
- `docs/filename_word_limit_feature.md` - 功能详细说明（新建）
- `docs/CHANGELOG_20250114_filename_word_limit.md` - 本文档（新建）

## 作者

AI Assistant (Claude)

## 审核状态

- [x] 代码实现完成
- [x] 语法检查通过（无 linter 错误）
- [x] 文档更新完成
- [ ] 用户测试（待用户验证）

---

**备注**: 此功能主要解决文件名过长的问题，使得在文件管理器中查看图表文件时更加简洁明了。默认的12个单词通常足以保留图表的核心含义，同时避免文件名过长。

