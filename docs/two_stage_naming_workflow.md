# 两阶段命名工作流（Two-Stage Naming Workflow）

**更新日期**: 2025-01-14  
**功能类型**: 工作流优化

## 概述

为了生成更具描述性和可读性的图表文件名，本项目采用**两阶段命名工作流**：
1. **阶段1（脚本）**：提取脚本基于原始图注生成**临时文件名**（默认12个单词）
2. **阶段2（AI Agent）**：AI Agent基于论文完整内容将图表重命名为**最终描述性名称**（5-15个单词）

## 设计理念

### 为什么需要两阶段命名？

**问题**：
- 原始图注通常较长且冗余（如："Figure 1: Overview of the proposed deep learning architecture for multi-modal feature extraction and fusion in autonomous driving systems with attention mechanisms"）
- 直接从图注生成的文件名不够简洁，难以快速识别图表核心内容
- 图注可能包含与图表内容不直接相关的描述性文字

**解决方案**：
- **脚本阶段**：快速提取，保留足够信息用于初步识别（12个单词足以区分不同图表）
- **AI阶段**：理解论文后生成精炼、专业、准确的描述性名称（5-15个单词）

## 工作流详解

### 阶段1：脚本自动提取（临时命名）

**执行者**：`extract_pdf_assets.py`  
**输入**：PDF 论文  
**输出**：临时命名的PNG文件  

**命名规则**：
- 基于原始图注文本
- 自动清理特殊字符
- 限制为12个单词（默认，可通过 `--max-caption-words` 调整）
- 格式：`Figure_N_<原始图注前12词>.png`

**示例**：
```bash
python3 scripts/extract_pdf_assets.py --pdf paper.pdf --preset robust

# 输出临时文件名：
images/Figure_1_Overview_of_the_proposed_deep_learning_architecture_for_multi_modal_feature.png
images/Figure_2_Experimental_results_showing_performance_comparison_on_multiple_benchmark_datasets_including.png
images/Figure_3_Ablation_study_results_demonstrating_the_impact_of_different_components_on.png
images/Table_1_Comparison_of_model_performance_across_different_architectures_and_training.png
```

### 阶段2：AI Agent 智能重命名（最终命名）

**执行者**：AI Agent（Claude、GPT、Gemini等）  
**输入**：
- 论文全文（`text/paper.txt`）
- 临时命名的图表PNG文件
- 图表内容（查看PNG图像）
- 图表索引（`images/index.json`）

**输出**：最终命名的PNG文件

**重命名规则**：
- 📏 **单词数量**：5-15个单词（不含前缀）
- 🎯 **命名原则**：
  - ✅ 准确反映图表的核心内容或贡献
  - ✅ 使用专业但简洁的描述性术语
  - ✅ 避免冗长的句式，突出关键概念
  - ✅ 保持与论文术语的一致性
  - ❌ 避免重复图注中的冗余信息
  - ❌ 避免过于宽泛或模糊的描述

**重命名流程**：
```bash
# 1. AI Agent 阅读论文和图表
# 2. 理解每个图表的核心含义
# 3. 执行批量重命名

cd images/

# 图1：架构图 - 从冗长的描述精炼为核心概念
mv "Figure_1_Overview_of_the_proposed_deep_learning_architecture_for_multi_modal_feature.png" \
   "Figure_1_Multimodal_Architecture_Overview.png"

# 图2：性能对比 - 突出关键信息"benchmark"和"comparison"
mv "Figure_2_Experimental_results_showing_performance_comparison_on_multiple_benchmark_datasets_including.png" \
   "Figure_2_Benchmark_Performance_Comparison.png"

# 图3：消融实验 - 保留专业术语"ablation"
mv "Figure_3_Ablation_study_results_demonstrating_the_impact_of_different_components_on.png" \
   "Figure_3_Ablation_Study_Results.png"

# 表1：模型性能 - 简洁明了
mv "Table_1_Comparison_of_model_performance_across_different_architectures_and_training.png" \
   "Table_1_Model_Performance_Metrics.png"

cd ..
```

## 命名对比示例

### 示例1：架构图

| 阶段 | 文件名 | 单词数 | 评价 |
|------|--------|--------|------|
| 原始图注 | "Figure 1: Overview of the proposed deep learning architecture for multi-modal feature extraction and fusion in autonomous driving systems" | - | 过长、冗余 |
| 临时命名 | `Figure_1_Overview_of_the_proposed_deep_learning_architecture_for_multi_modal_feature.png` | 12 | 可识别但冗长 |
| 最终命名 | `Figure_1_Multimodal_Architecture_Overview.png` | 3 | ✅ 简洁专业 |

### 示例2：实验结果

| 阶段 | 文件名 | 单词数 | 评价 |
|------|--------|--------|------|
| 原始图注 | "Figure 2: Experimental results showing performance comparison on multiple benchmark datasets including ImageNet, COCO, and ADE20K" | - | 包含细节 |
| 临时命名 | `Figure_2_Experimental_results_showing_performance_comparison_on_multiple_benchmark_datasets_including.png` | 12 | 截断了重要信息 |
| 最终命名 | `Figure_2_Benchmark_Performance_Comparison.png` | 3 | ✅ 核心概念清晰 |

### 示例3：消融实验

| 阶段 | 文件名 | 单词数 | 评价 |
|------|--------|--------|------|
| 原始图注 | "Figure 3: Ablation study results demonstrating the impact of different model components on final performance metrics" | - | 描述性强但冗长 |
| 临时命名 | `Figure_3_Ablation_study_results_demonstrating_the_impact_of_different_components_on.png` | 12 | 信息完整但长 |
| 最终命名 | `Figure_3_Component_Ablation_Study.png` | 3 | ✅ 保留关键术语 |

### 示例4：训练曲线

| 阶段 | 文件名 | 单词数 | 评价 |
|------|--------|--------|------|
| 原始图注 | "Figure 4: Training loss and validation accuracy curves during model training over 100 epochs" | - | 包含所有细节 |
| 临时命名 | `Figure_4_Training_loss_and_validation_accuracy_curves_during_model_training_over_100.png` | 12 | 信息完整 |
| 最终命名（选项A） | `Figure_4_Training_Validation_Curves.png` | 3 | ✅ 简洁 |
| 最终命名（选项B） | `Figure_4_Loss_and_Accuracy_Curves.png` | 4 | ✅ 更具体 |

## AI Agent 最佳实践

### 命名原则详解

#### 1. 识别图表类型
根据图表类型选择合适的命名模式：

**架构图/系统图**：
- ✅ `Architecture_Overview`
- ✅ `System_Design_Diagram`
- ✅ `Network_Architecture`

**实验结果/性能对比**：
- ✅ `Performance_Comparison`
- ✅ `Benchmark_Results`
- ✅ `Accuracy_Comparison`

**消融实验**：
- ✅ `Ablation_Study_Results`
- ✅ `Component_Analysis`
- ✅ `Feature_Importance_Analysis`

**训练过程**：
- ✅ `Training_Curves`
- ✅ `Loss_Convergence`
- ✅ `Learning_Progress`

**可视化/案例**：
- ✅ `Qualitative_Results`
- ✅ `Visual_Examples`
- ✅ `Sample_Outputs`

#### 2. 保留专业术语
保持与论文术语的一致性：
- 如果论文使用 "Multimodal"，命名中也使用 "Multimodal"
- 如果论文使用 "Transformer"，命名中也使用 "Transformer"
- 保留领域特定的缩写（如 CNN, BERT, GPT）

#### 3. 避免常见错误

❌ **过于宽泛**：
- `Figure_1_Results.png` - 什么结果？
- `Figure_2_Comparison.png` - 对比什么？

✅ **具体明确**：
- `Figure_1_Benchmark_Results.png`
- `Figure_2_Method_Comparison.png`

❌ **重复图号**：
- `Figure_1_Figure_1_Architecture.png` - 重复

✅ **简洁清晰**：
- `Figure_1_Architecture.png`

❌ **使用完整句子**：
- `Figure_1_This_Shows_Our_Proposed_Method.png`

✅ **使用名词短语**：
- `Figure_1_Proposed_Method.png`

## 集成到摘要生成

### 在 Markdown 中引用

使用重命名后的文件名嵌入图表：

```markdown
## 方法

本文提出了一种新的多模态架构...

![Figure 1: 架构概览](images/Figure_1_Multimodal_Architecture_Overview.png)
**图1** 展示了提出的多模态Transformer架构的整体设计...

## 实验结果

![Figure 2: 基准测试性能对比](images/Figure_2_Benchmark_Performance_Comparison.png)
**图2** 对比了本文方法与现有方法在ImageNet、COCO等基准数据集上的性能...

![Table 1: 模型性能指标](images/Table_1_Model_Performance_Metrics.png)
**表1** 列出了不同模型配置的详细性能指标，包括准确率、推理速度等...
```

## 工作流自动化

### 批量重命名脚本示例

AI Agent 可以生成批量重命名脚本：

```bash
#!/bin/bash
# rename_figures.sh - Auto-generated by AI Agent

cd images/

# 移动前备份（可选）
# cp -r . ../images_backup/

# 批量重命名
mv "Figure_1_Overview_of_the_proposed_deep_learning_architecture_for_multi_modal_feature.png" "Figure_1_Multimodal_Architecture_Overview.png"
mv "Figure_2_Experimental_results_showing_performance_comparison_on_multiple_benchmark_datasets_including.png" "Figure_2_Benchmark_Performance_Comparison.png"
mv "Figure_3_Ablation_study_results_demonstrating_the_impact_of_different_components_on.png" "Figure_3_Ablation_Study_Results.png"
mv "Figure_4_Training_loss_and_validation_accuracy_curves_during_model_training_over_100.png" "Figure_4_Training_Validation_Curves.png"
mv "Figure_5_Qualitative_results_showing_sample_outputs_of_our_model_on_various_test.png" "Figure_5_Qualitative_Results.png"
mv "Table_1_Comparison_of_model_performance_across_different_architectures_and_training.png" "Table_1_Model_Performance_Metrics.png"
mv "Table_2_Hyperparameter_settings_and_configurations_used_in_our_experiments_including.png" "Table_2_Training_Hyperparameters.png"

cd ..

echo "✅ All figures and tables renamed successfully!"
echo "📁 Total renamed: 7 files"
```

## 验证检查清单

重命名完成后，AI Agent 应验证：

- [x] ✅ 所有图表都已重命名
- [x] ✅ 新文件名符合5-15个单词的要求
- [x] ✅ 保留了 `Figure_N_` 或 `Table_N_` 前缀
- [x] ✅ 文件名准确反映图表内容
- [x] ✅ 摘要文档中的所有图表路径已更新为新文件名
- [x] ✅ 图表在 Markdown 预览中正确显示
- [x] ✅ 没有遗漏任何图表文件

## 常见问题

### Q1: 如果论文中有多个相似的图表怎么办？

**A**: 在命名中添加区分性的描述词：
```bash
# 不好
Figure_2_Performance.png
Figure_3_Performance.png

# 好
Figure_2_ImageNet_Performance.png
Figure_3_COCO_Performance.png
```

### Q2: 如果图表包含多个子图怎么办？

**A**: 在命名中体现整体概念，不需要列举每个子图：
```bash
# 不好
Figure_5_Subfigure_a_shows_attention_maps_subfigure_b_shows_features.png

# 好
Figure_5_Attention_Visualization.png
```

### Q3: 表格命名有什么特殊注意事项吗？

**A**: 表格命名应突出数据类型和对比维度：
```bash
# 好的表格命名
Table_1_Model_Performance_Metrics.png
Table_2_Training_Hyperparameters.png
Table_3_Ablation_Study_Results.png
Table_4_Comparison_with_SOTA_Methods.png
```

### Q4: 如果重命名后发现错误怎么办？

**A**: 可以再次重命名，只需确保更新摘要文档中的所有引用：
```bash
# 修正命名错误
mv "images/Figure_1_Wrong_Name.png" "images/Figure_1_Correct_Name.png"

# 在摘要文档中全局替换
sed -i 's/Figure_1_Wrong_Name/Figure_1_Correct_Name/g' paper_阅读摘要-20250114.md
```

## 总结

两阶段命名工作流的优势：
- ✅ **自动化**：脚本快速提取，无需人工干预
- ✅ **智能化**：AI理解内容后生成准确描述
- ✅ **可读性**：最终文件名简洁专业
- ✅ **一致性**：与论文术语保持一致
- ✅ **灵活性**：可根据具体论文调整命名策略

这种方法确保了文件名既能快速生成（脚本阶段），又能准确描述（AI阶段），是自动化与智能化的完美结合。

