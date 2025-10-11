#!/usr/bin/env python3
"""
提取效果分析工具
用于复盘PDF图表提取过程的关键决策点
"""

import json
import sys
from pathlib import Path

def analyze_extraction_results(pdf_dir: Path):
    """分析提取结果"""
    
    images_dir = pdf_dir / "images"
    text_dir = pdf_dir / "text"
    index_file = images_dir / "index.json"
    
    if not index_file.exists():
        print(f"❌ 未找到索引文件: {index_file}")
        return
    
    # 读取索引
    with open(index_file, 'r', encoding='utf-8') as f:
        items = json.load(f)
    
    print("=" * 70)
    print(f"📊 提取结果分析：{pdf_dir.name}")
    print("=" * 70)
    print()
    
    # 统计
    figures = [x for x in items if x['type'] == 'figure']
    tables = [x for x in items if x['type'] == 'table']
    
    print(f"📈 总体统计")
    print(f"  • 总计：{len(items)} 个元素")
    print(f"  • 图片：{len(figures)} 个")
    print(f"  • 表格：{len(tables)} 个")
    print()
    
    # 页面分布
    page_dist = {}
    for item in items:
        page = item['page']
        if page not in page_dist:
            page_dist[page] = []
        page_dist[page].append(f"{item['type'].capitalize()} {item['id']}")
    
    print(f"📄 页面分布")
    for page in sorted(page_dist.keys()):
        elements = ", ".join(page_dist[page])
        print(f"  • Page {page}: {elements}")
    print()
    
    # 详细信息
    print(f"📋 详细信息")
    for i, item in enumerate(items, 1):
        print(f"\n  [{i}] {item['type'].upper()} {item['id']}")
        print(f"      页码：Page {item['page']}")
        
        # 文件信息
        img_file = images_dir / item['file']
        if img_file.exists():
            size_kb = img_file.stat().st_size / 1024
            print(f"      文件：{size_kb:.1f} KB")
            
            # 尝试获取图片尺寸
            try:
                from PIL import Image
                img = Image.open(img_file)
                print(f"      尺寸：{img.width} × {img.height} px")
            except:
                pass
        
        # Caption预览
        caption = item['caption']
        if len(caption) > 80:
            caption = caption[:77] + "..."
        print(f"      图注：{caption}")
        
        if item.get('continued'):
            print(f"      续页：✓")
    
    print()
    print("=" * 70)
    
    # 质量评估
    print()
    print(f"✅ 质量评估")
    
    # 检查是否有漏图
    text_files = list(text_dir.glob("*.txt"))
    if text_files:
        text_file = text_files[0]
        with open(text_file, 'r', encoding='utf-8') as f:
            text = f.read()
        
        import re
        fig_mentions = len(re.findall(r'\bFigure\s+\d+', text, re.IGNORECASE))
        tab_mentions = len(re.findall(r'\bTable\s+\d+', text, re.IGNORECASE))
        
        print(f"  • 文本中提到：Figure {fig_mentions}次, Table {tab_mentions}次")
        print(f"  • 实际提取：Figure {len(figures)}个, Table {len(tables)}个")
        
        if len(figures) + len(tables) >= fig_mentions + tab_mentions - 2:
            print(f"  • 完整性：✅ 优秀（提取数量 ≥ 提及次数）")
        elif len(figures) + len(tables) >= (fig_mentions + tab_mentions) * 0.8:
            print(f"  • 完整性：⚠️  良好（提取数量 ≥ 80%）")
        else:
            print(f"  • 完整性：❌ 需改进（可能有漏图）")
    
    # 检查文件大小
    total_size = sum((images_dir / item['file']).stat().st_size 
                    for item in items if (images_dir / item['file']).exists())
    avg_size = total_size / len(items) if items else 0
    
    print(f"  • 总大小：{total_size / 1024:.1f} KB")
    print(f"  • 平均大小：{avg_size / 1024:.1f} KB/图")
    
    if 100 < avg_size / 1024 < 300:
        print(f"  • 文件大小：✅ 合理（100-300KB/图）")
    elif avg_size / 1024 < 100:
        print(f"  • 文件大小：⚠️  偏小（可能需要提高DPI）")
    else:
        print(f"  • 文件大小：⚠️  偏大（可考虑压缩）")
    
    print()


def compare_extractions(dirs: list[Path]):
    """对比多个提取结果"""
    
    print("=" * 70)
    print(f"📊 多文档提取效果对比")
    print("=" * 70)
    print()
    
    results = []
    for pdf_dir in dirs:
        index_file = pdf_dir / "images" / "index.json"
        if not index_file.exists():
            continue
        
        with open(index_file, 'r', encoding='utf-8') as f:
            items = json.load(f)
        
        figures = [x for x in items if x['type'] == 'figure']
        tables = [x for x in items if x['type'] == 'table']
        
        # 计算平均文件大小
        total_size = 0
        count = 0
        for item in items:
            img_file = pdf_dir / "images" / item['file']
            if img_file.exists():
                total_size += img_file.stat().st_size
                count += 1
        
        avg_size = total_size / count if count > 0 else 0
        
        results.append({
            'name': pdf_dir.name,
            'figures': len(figures),
            'tables': len(tables),
            'total': len(items),
            'avg_size_kb': avg_size / 1024
        })
    
    # 打印表格
    print(f"{'文档':<30} {'图片':<6} {'表格':<6} {'总计':<6} {'平均大小':<12}")
    print("-" * 70)
    for r in results:
        print(f"{r['name']:<30} {r['figures']:<6} {r['tables']:<6} {r['total']:<6} {r['avg_size_kb']:>8.1f} KB")
    print()


def main():
    if len(sys.argv) < 2:
        print("用法：")
        print("  # 分析单个提取结果")
        print("  python analyze_extraction.py <pdf_dir>")
        print()
        print("  # 对比多个提取结果")
        print("  python analyze_extraction.py <pdf_dir1> <pdf_dir2> ...")
        print()
        print("示例：")
        print("  python analyze_extraction.py tests/DeepSeek_V3_2/")
        print("  python analyze_extraction.py tests/*/")
        sys.exit(1)
    
    paths = [Path(p) for p in sys.argv[1:]]
    
    if len(paths) == 1:
        # 单个分析
        analyze_extraction_results(paths[0])
    else:
        # 多个对比
        compare_extractions(paths)
        print()
        print("详细分析每个文档：")
        print("-" * 70)
        for path in paths:
            print()
            analyze_extraction_results(path)


if __name__ == "__main__":
    main()

