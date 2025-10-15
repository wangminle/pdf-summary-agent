# 以下脚本是一个增强版的PDF图片提取工具，支持递归处理子文件夹中的PDF文件，来自网页分享链接：https://mp.weixin.qq.com/s/5zItiGMkFTNi3_LDHBMibg
# 我们准备参考这段代码，改进我们自有的工具，非常感谢分享脚本的大咖。

import os
import fitz  # PyMuPDF
import time
from PIL import Image
import argparse
import logging
import re
import io
import sys
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Enhanced_PDF_Image_Extractor')
class EnhancedPDFImageExtractor:
    def __init__(self, input_path, output_path, min_size=500, max_size=10000, 
                 output_format='png', dpi=300, quality=95, skip_text_images=True,
                 render_pages=False, extract_vectors=False, password=None,
                 skip_empty_folders=True):
        """
        初始化增强版PDF图片提取器
        参数:
            input_path: PDF文件或包含PDF的文件夹路径
            output_path: 图片输出文件夹
            min_size: 最小图片尺寸(像素)，小于此值的图片将被忽略
            max_size: 最大图片尺寸(像素)，大于此值的图片将被调整
            output_format: 输出图片格式 (png, jpg, webp)
            dpi: 图片DPI值
            quality: 图片质量 (1-100)
            skip_text_images: 是否跳过包含文本的图片
            render_pages: 是否渲染整个页面来提取图片（解决tiled images问题）
            extract_vectors: 是否尝试提取矢量图形（转换为位图）
            password: PDF密码（如果有）
            skip_empty_folders: 如果没有提取到图片，是否跳过创建文件夹
        """
        self.input_path = input_path
        self.output_path = output_path
        self.min_size = min_size
        self.max_size = max_size
        self.output_format = output_format.lower()
        self.dpi = dpi
        self.quality = quality
        self.skip_text_images = skip_text_images
        self.render_pages = render_pages
        self.extract_vectors = extract_vectors
        self.password = password
        self.skip_empty_folders = skip_empty_folders
        # 创建输出目录
        os.makedirs(output_path, exist_ok=True)
        # 验证输出格式
        if self.output_format not in ['png', 'jpg', 'jpeg', 'webp']:
            raise ValueError(f"不支持的输出格式: {output_format}")
    def process_pdf(self, pdf_path, relative_path):
        """
        处理单个PDF文件
        参数:
            pdf_path: PDF文件的完整路径
            relative_path: 相对于输入文件夹的相对路径
        """
        try:
            # 打开PDF文件
            try:
                doc = fitz.open(pdf_path)
                if doc.is_encrypted:
                    if self.password:
                        auth = doc.authenticate(self.password)
                        if not auth:
                            logger.error(f"PDF已加密且提供的密码无效: {pdf_path}")
                            return 0
                    else:
                        # 尝试空密码解密（许多PDF使用空密码"加密"）
                        if not doc.authenticate(""):
                            logger.warning(f"PDF已加密且未提供密码: {pdf_path}")
                            return 0
            except Exception as e:
                logger.error(f"打开PDF失败: {pdf_path} - {str(e)}")
                return 0
            pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
            logger.info(f"开始处理: {pdf_name} (共 {len(doc)} 页)")
            total_images = 0
            extracted_images = 0
            # 创建输出文件夹路径，保持原始文件夹结构
            pdf_output_dir = os.path.join(self.output_path, relative_path, pdf_name)
            # 存储提取的图片路径，稍后保存
            images_to_save = []
            # 遍历每一页
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                # 方法1: 直接提取图片对象
                image_list = page.get_images(full=True)
                # 方法2: 渲染整个页面来提取图片（解决tiled images问题）
                if self.render_pages or not image_list:
                    rendered_image = self._extract_images_from_rendered_page(page, pdf_output_dir, pdf_name, page_num)
                    if rendered_image:
                        images_to_save.append(rendered_image)
                        extracted_images += 1
                    continue
                logger.debug(f"第 {page_num+1} 页找到 {len(image_list)} 张图片")
                # 处理每张图片
                for img_index, img in enumerate(image_list):
                    total_images += 1
                    # 提取图片
                    try:
                        xref = img[0]
                        base_image = doc.extract_image(xref)
                        if not base_image:
                            logger.warning(f"无法提取图片 (xref={xref})")
                            continue
                        image_bytes = base_image.get("image")
                        if not image_bytes:
                            logger.warning(f"图片数据为空 (xref={xref})")
                            continue
                        image_ext = base_image.get("ext", "png")
                        # 检查图片尺寸
                        width = base_image.get("width", 0)
                        height = base_image.get("height", 0)
                        # 跳过过小的图片
                        if width < self.min_size or height < self.min_size:
                            logger.debug(f"跳过小图片: {width}x{height} (小于 {self.min_size}px)")
                            continue
                        # 检查是否为文本图片（如公式、表格等）
                        if self.skip_text_images and self._is_text_image(page, img):
                            logger.debug("跳过文本图片")
                            continue
                        # 生成文件名
                        img_filename = f"{pdf_name}_p{page_num+1}_i{img_index+1}.{self.output_format}"
                        img_path = os.path.join(pdf_output_dir, img_filename)
                        # 存储图片信息，稍后保存
                        images_to_save.append({
                            'path': img_path,
                            'bytes': image_bytes,
                            'width': width,
                            'height': height
                        })
                        extracted_images += 1
                        logger.debug(f"准备保存图片: {img_filename} ({width}x{height})")
                    except Exception as e:
                        logger.error(f"提取图片失败 (第 {page_num+1} 页, 图片 {img_index+1}): {str(e)}")
            # 方法3: 提取矢量图形（转换为位图）
            if self.extract_vectors:
                vector_images = self._extract_vector_graphics(doc, pdf_output_dir, pdf_name)
                if vector_images:
                    images_to_save.extend(vector_images)
                    extracted_images += len(vector_images)
            # 如果有图片需要保存，创建文件夹并保存图片
            folder_created = False
            if images_to_save:
                # 创建输出文件夹（包括所有父目录）
                os.makedirs(pdf_output_dir, exist_ok=True)
                folder_created = True
                logger.info(f"创建输出文件夹: {pdf_output_dir}")
                # 保存所有图片
                saved_count = 0
                for img_info in images_to_save:
                    try:
                        # 保存原始图片
                        with open(img_info['path'], "wb") as img_file:
                            img_file.write(img_info['bytes'])
                        # 处理图片（调整大小、格式转换等）
                        self._process_image(img_info['path'], img_info['width'], img_info['height'])
                        saved_count += 1
                    except Exception as e:
                        logger.error(f"保存图片失败: {img_info['path']} - {str(e)}")
                # 检查是否所有图片保存都失败
                if saved_count == 0:
                    logger.warning(f"所有图片保存失败，删除文件夹: {pdf_output_dir}")
                    try:
                        shutil.rmtree(pdf_output_dir)
                        folder_created = False
                    except Exception as e:
                        logger.error(f"删除文件夹失败: {pdf_output_dir} - {str(e)}")
                else:
                    logger.info(f"成功保存 {saved_count}/{len(images_to_save)} 张图片")
            else:
                # 没有提取到任何图片
                if self.skip_empty_folders:
                    logger.info(f"未提取到图片，跳过创建文件夹: {pdf_output_dir}")
                else:
                    # 即使没有图片也创建文件夹（用于记录）
                    os.makedirs(pdf_output_dir, exist_ok=True)
                    folder_created = True
                    logger.info(f"创建空文件夹: {pdf_output_dir} (无图片提取)")
            doc.close()
            logger.info(f"完成处理: {pdf_name} - 提取 {extracted_images}/{total_images} 张图片")
            return extracted_images if folder_created else 0
        except Exception as e:
            logger.error(f"处理PDF失败: {pdf_path} - {str(e)}")
            return 0
    def _extract_images_from_rendered_page(self, page, output_dir, pdf_name, page_num):
        """通过渲染整个页面来提取图片（解决tiled images问题）"""
        try:
            # 设置渲染参数
            zoom = self.dpi / 72  # 72是PDF的标准DPI
            mat = fitz.Matrix(zoom, zoom)
            # 渲染页面为Pixmap
            pix = page.get_pixmap(matrix=mat, alpha=False)
            # 转换为PIL图像
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            # 生成文件名
            img_filename = f"{pdf_name}_p{page_num+1}_rendered.{self.output_format}"
            img_path = os.path.join(output_dir, img_filename)
            # 返回图片信息（稍后保存）
            img_byte_arr = io.BytesIO()
            if self.output_format == 'png':
                img.save(img_byte_arr, format='PNG')
            elif self.output_format in ['jpg', 'jpeg']:
                img.save(img_byte_arr, format='JPEG', quality=self.quality)
            elif self.output_format == 'webp':
                img.save(img_byte_arr, format='WEBP', quality=self.quality)
            return {
                'path': img_path,
                'bytes': img_byte_arr.getvalue(),
                'width': img.width,
                'height': img.height
            }
        except Exception as e:
            logger.error(f"渲染页面失败 (第 {page_num+1} 页): {str(e)}")
            return None
    def _extract_vector_graphics(self, doc, output_dir, pdf_name):
        """提取矢量图形（转换为位图）"""
        extracted = []
        try:
            # 遍历所有页面
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                # 获取所有矢量图形
                drawings = page.get_drawings()
                if not drawings:
                    continue
                # 创建空白图像
                zoom = self.dpi / 72
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                # 保存矢量图形
                for i, drawing in enumerate(drawings):
                    try:
                        # 生成文件名
                        img_filename = f"{pdf_name}_p{page_num+1}_vector_{i+1}.{self.output_format}"
                        img_path = os.path.join(output_dir, img_filename)
                        # 保存矢量图形
                        # 注意：这里简化了处理，实际需要解析drawing对象
                        # 更完整的实现需要解析路径、形状等
                        img_byte_arr = io.BytesIO()
                        if self.output_format == 'png':
                            img.save(img_byte_arr, format='PNG')
                        elif self.output_format in ['jpg', 'jpeg']:
                            img.save(img_byte_arr, format='JPEG', quality=self.quality)
                        elif self.output_format == 'webp':
                            img.save(img_byte_arr, format='WEBP', quality=self.quality)
                        extracted.append({
                            'path': img_path,
                            'bytes': img_byte_arr.getvalue(),
                            'width': img.width,
                            'height': img.height
                        })
                        logger.debug(f"准备保存矢量图形: {img_filename}")
                    except Exception as e:
                        logger.error(f"提取矢量图形失败: {str(e)}")
        except Exception as e:
            logger.error(f"提取矢量图形失败: {str(e)}")
        return extracted
    def _is_text_image(self, page, img_info):
        """检查图片是否包含文本（如公式、表格等）"""
        try:
            # 获取图片位置
            bbox = img_info[1:5]
            # 获取图片区域的文本
            text = page.get_text("text", clip=bbox)
            # 如果有文本，则可能是公式或表格
            return len(text.strip()) > 0
        except:
            return False
    def _process_image(self, img_path, orig_width, orig_height):
        """处理提取的图片（调整大小、转换格式等）"""
        try:
            with Image.open(io.BytesIO(open(img_path, "rb").read())) as img:
                # 检查是否需要调整大小
                if orig_width > self.max_size or orig_height > self.max_size:
                    # 计算新尺寸，保持宽高比
                    ratio = min(self.max_size / orig_width, self.max_size / orig_height)
                    new_size = (int(orig_width * ratio), int(orig_height * ratio))
                    # 高质量缩放
                    img = img.resize(new_size, Image.LANCZOS)
                    logger.debug(f"调整图片大小: {orig_width}x{orig_height} -> {new_size[0]}x{new_size[1]}")
                # 设置DPI信息
                img.info['dpi'] = (self.dpi, self.dpi)
                # 根据输出格式保存
                if self.output_format == 'png':
                    img.save(img_path, 'PNG', dpi=(self.dpi, self.dpi))
                elif self.output_format in ['jpg', 'jpeg']:
                    img.save(img_path, 'JPEG', quality=self.quality, dpi=(self.dpi, self.dpi))
                elif self.output_format == 'webp':
                    img.save(img_path, 'WEBP', quality=self.quality, dpi=(self.dpi, self.dpi))
        except Exception as e:
            logger.error(f"处理图片失败: {img_path} - {str(e)}")
    def process_all(self, max_workers=4):
        """处理所有PDF文件（包括子文件夹）"""
        start_time = time.time()
        # 获取所有PDF文件（递归查找）
        pdf_files = []
        relative_paths = []
        if os.path.isfile(self.input_path) and self.input_path.lower().endswith('.pdf'):
            pdf_files = [self.input_path]
            relative_paths = [os.path.basename(os.path.dirname(self.input_path))]
        elif os.path.isdir(self.input_path):
            # 递归查找所有PDF文件
            for root, _, files in os.walk(self.input_path):
                for file in files:
                    if file.lower().endswith('.pdf'):
                        full_path = os.path.join(root, file)
                        pdf_files.append(full_path)
                        # 计算相对于输入文件夹的相对路径
                        rel_path = os.path.relpath(root, self.input_path)
                        relative_paths.append(rel_path)
        else:
            raise ValueError("输入路径必须是PDF文件或包含PDF文件的文件夹")
        if not pdf_files:
            logger.warning("未找到PDF文件")
            return 0
        logger.info(f"找到 {len(pdf_files)} 个PDF文件（包括所有子文件夹）")
        total_extracted = 0
        # 使用线程池并行处理
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for i, pdf_path in enumerate(pdf_files):
                rel_path = relative_paths[i]
                futures.append(executor.submit(self.process_pdf, pdf_path, rel_path))
            for future in as_completed(futures):
                try:
                    extracted = future.result()
                    total_extracted += extracted
                except Exception as e:
                    logger.error(f"处理失败: {str(e)}")
        elapsed_time = time.time() - start_time
        logger.info(f"处理完成! 共提取 {total_extracted} 张图片, 耗时 {elapsed_time:.2f} 秒")
        return total_extracted
if __name__ == "__main__":
    # 设置命令行参数
    parser = argparse.ArgumentParser(description='增强版PDF图片提取工具（递归处理子文件夹）')
    parser.add_argument('input', help='PDF文件或包含PDF的文件夹路径（将递归处理所有子文件夹）')
    parser.add_argument('output', help='图片输出文件夹路径')
    parser.add_argument('--min-size', type=int, default=500, 
                        help='最小图片尺寸(像素), 默认500')
    parser.add_argument('--max-size', type=int, default=10000, 
                        help='最大图片尺寸(像素), 默认10000')
    parser.add_argument('--format', choices=['png', 'jpg', 'webp'], default='png', 
                        help='输出图片格式, 默认png')
    parser.add_argument('--dpi', type=int, default=300, 
                        help='图片DPI值, 默认300')
    parser.add_argument('--quality', type=int, default=95, 
                        help='图片质量(1-100), 默认95')
    parser.add_argument('--keep-text-images', action='store_true', 
                        help='保留文本图片(如公式、表格等)')
    parser.add_argument('--render-pages', action='store_true',
                        help='渲染整个页面提取图片(解决tiled images问题)')
    parser.add_argument('--extract-vectors', action='store_true',
                        help='尝试提取矢量图形(转换为位图)')
    parser.add_argument('--password', type=str, default=None,
                        help='PDF密码(如果有)')
    parser.add_argument('--keep-empty-folders', action='store_true',
                        help='即使没有提取到图片也创建文件夹')
    parser.add_argument('--threads', type=int, default=4, 
                        help='并行处理线程数, 默认4')
    parser.add_argument('--verbose', action='store_true', 
                        help='显示详细日志信息')
    args = parser.parse_args()
    # 设置日志级别
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    # 创建提取器并处理
    extractor = EnhancedPDFImageExtractor(
        input_path=args.input,
        output_path=args.output,
        min_size=args.min_size,
        max_size=args.max_size,
        output_format=args.format,
        dpi=args.dpi,
        quality=args.quality,
        skip_text_images=not args.keep_text_images,
        render_pages=args.render_pages,
        extract_vectors=args.extract_vectors,
        password=args.password,
        skip_empty_folders=not args.keep_empty_folders
    )
    extractor.process_all(max_workers=args.threads)