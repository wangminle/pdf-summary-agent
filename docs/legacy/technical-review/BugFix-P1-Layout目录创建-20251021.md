# Bug修复：Layout模型JSON写入前目录不存在

**日期**: 2025-10-21  
**优先级**: P1  
**状态**: ✅ 已修复

---

## 问题描述

### 发现者
用户Code Review

### 问题代码
**位置**: `scripts/extract_pdf_assets.py:4804-4817`

**问题**:
当启用`--layout-driven`参数时，脚本尝试在`extract_text_with_format()`中写入`layout_model.json`，但此时输出目录`out_dir`尚未被创建，导致`FileNotFoundError`。

### 触发条件
1. 用户首次运行，`<pdf_dir>/images/`目录不存在
2. 启用`--layout-driven`参数
3. 使用默认的`--out-dir`（即`<pdf_dir>/images/`）

### 错误堆栈
```python
FileNotFoundError: [Errno 2] No such file or directory: '/path/to/pdf_dir/images/layout_model.json'
```

### 根本原因
**执行顺序问题**:
1. **Line 4804**: 构建`layout_json_path = os.path.join(out_dir, "layout_model.json")`
2. **Line 4807-4812**: 调用`extract_text_with_format()`，尝试写入JSON
3. **Line 4602-4606**: `extract_text_with_format()`内部直接`open(out_json, 'w')`，**未创建目录**
4. **Line 4833**: `extract_figures()`才创建`out_dir`（在line 1946）

版式模型构建在图表提取**之前**，此时`out_dir`还不存在！

---

## 修复方案

### 修复位置
`scripts/extract_pdf_assets.py:4602-4608`

### 修复代码
```python
# 8. 保存为JSON（可选）
if out_json:
    # 确保目录存在（修复P1 review的bug）
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(layout_model.to_dict(), f, indent=2, ensure_ascii=False)
    if debug:
        print(f"\n[INFO] Saved layout model to: {out_json}")
```

### 关键改动
**新增一行**（Line 4604）:
```python
os.makedirs(os.path.dirname(out_json), exist_ok=True)
```

**作用**:
- 在写入JSON之前，确保父目录存在
- `exist_ok=True`避免目录已存在时报错
- 支持任意深度的嵌套目录（如`/a/b/c/layout_model.json`）

---

## 测试验证

### 测试场景
模拟首次运行场景，输出目录不存在：

```bash
# 清理测试环境
rm -rf /tmp/test_layout_fix
mkdir -p /tmp/test_layout_fix

# 复制测试PDF
cp tests/basic-benchmark/1706.03762v7-attention_is_all_you_need/1706.03762v7-attention_is_all_you_need.pdf \
   /tmp/test_layout_fix/

# 运行脚本（--out-dir指向不存在的目录）
python3 scripts/extract_pdf_assets.py \
  --pdf /tmp/test_layout_fix/1706.03762v7-attention_is_all_you_need.pdf \
  --preset robust \
  --layout-driven \
  --out-dir /tmp/test_layout_fix/images
```

### 测试结果

✅ **修复前**: `FileNotFoundError: [Errno 2] No such file or directory`

✅ **修复后**: 
```
[INFO] Layout model built successfully
  - Columns: 2 (double)
  - Text blocks: 102
  - Vacant regions: 0
```

✅ **文件验证**:
```bash
$ ls -lh /tmp/test_layout_fix/images/layout_model.json
-rw-r--r-- 1 user wheel 953B Oct 21 20:38 layout_model.json
```

✅ **内容验证**:
```json
{
  "page_size": [612.0, 792.0],
  "num_columns": 2,
  "margins": {"left": 0.0, "right": 612.0, "top": 0.0, "bottom": 792.0},
  "column_gap": -465.9161,
  "typical_metrics": {
    "font_size": 10.0,
    "line_height": 10.0,
    "line_gap": 0.9
  },
  "text_units_count": {...},
  "text_blocks_count": {...}
}
```

---

## 影响范围

### 受影响功能
- ✅ `--layout-driven`参数（V2版式驱动提取）
- ✅ 首次运行场景（输出目录不存在）

### 不受影响功能
- ✅ 默认模式（不使用`--layout-driven`）
- ✅ 已有输出目录的场景（二次运行）
- ✅ `extract_figures()`和`extract_tables()`（它们自己创建目录）

---

## 代码质量

### Linter检查
```bash
$ read_lints scripts/extract_pdf_assets.py
No linter errors found.
```

### 边缘情况考虑

1. **空路径**: 
   - 不会发生，因为`layout_json_path`总是包含目录路径
   - 要么是用户指定的完整路径，要么是`os.path.join(out_dir, ...)`

2. **权限问题**:
   - `os.makedirs()`会抛出`PermissionError`（符合预期）
   - 不需要额外处理，让错误向上传播

3. **并发创建**:
   - `exist_ok=True`处理了竞态条件
   - 多个进程同时创建目录不会冲突

---

## 经验教训

### 问题根源
**时序依赖**：版式模型构建依赖于输出目录，但构建时机早于目录创建。

### 最佳实践
1. ✅ **原则**: "谁写文件，谁创建目录"
   - 不应该依赖外部函数创建目录
   - 在写入操作前主动确保目录存在

2. ✅ **模式**: 
   ```python
   if need_write_file:
       os.makedirs(os.path.dirname(file_path), exist_ok=True)
       with open(file_path, 'w') as f:
           f.write(...)
   ```

3. ✅ **测试**: 
   - 测试首次运行场景（clean slate）
   - 测试嵌套目录创建（`/a/b/c/file.json`）
   - 测试目录已存在的场景

### 预防措施
在代码审查时，检查所有文件写入操作：
```python
# ❌ 危险模式
with open(path, 'w') as f:
    f.write(...)

# ✅ 安全模式
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w') as f:
    f.write(...)
```

---

## 相关文档

- **主脚本**: `scripts/extract_pdf_assets.py`
- **用户指南**: `AGENTS.md` - "版式驱动提取（V2 Architecture）"
- **技术文档**: `docs/technical-review/版式驱动提取功能实施总结-20251021.md`

---

## 修复确认

- [x] Bug已修复
- [x] 测试通过（首次运行场景）
- [x] 测试通过（已有目录场景）
- [x] Linter检查通过
- [x] 文档已更新（本文档）
- [x] 无新引入的问题

---

**修复状态**: ✅ 完成  
**修复者**: Claude (Sonnet 4.5)  
**测试者**: 自动化测试 + 用户反馈  
**审核者**: 待用户确认

