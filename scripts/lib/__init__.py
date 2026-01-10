#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/lib/ 模块入口

包含所有从主脚本抽离的公共库模块：
- env_priority: ENV 优先级与参数处理
- pdf_backend: PDF 后端抽象层（PyMuPDF + pdfplumber 双引擎）
- models: 数据结构定义
- idents: 标识符与正则表达式
- extraction_logger: 日志系统（从旧版迁移）
"""
