import os
import base64
import io
import requests
import re
import concurrent.futures
import time
import gc
import fitz  # 核心：用于检测文字走向和旋转页面 (PyMuPDF)
from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageEnhance
import warnings
import pandas as pd

# pytesseract is optional: it is only used to (a) skip image-LLM calls for
# pictures that contain no digits and (b) the legacy OSD helper. Missing it
# must not block the Docling path — degrade gracefully.
try:
    import pytesseract
except Exception:
    pytesseract = None

# Recognition-output filename suffix — kept in sync with the skill
# (engine.mineru.RECOGNIZED_MD_SUFFIX). Was the Chinese "_提取结果".
RECOGNIZED_MD_SUFFIX = "_extracted"

# ==========================================
# 🆕 新增功能：竖向表格自动检测与旋转预处理
# ==========================================
def _osd_rotation_angle(page, dpi=150):
    """
    视觉二次验证：把页面（按当前 /Rotate 显示效果）渲染成图，用 Tesseract OSD
    判断让文字变正所需的顺时针旋转角度。
    返回 (rotate_deg, confidence)；OSD 不可用或解析失败返回 None。
    rotate_deg=0 表示文字已经是正的（无需旋转）。
    """
    if pytesseract is None:
        return None
    try:
        pix = page.get_pixmap(dpi=dpi)          # 渲染时已应用 page.rotation
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        osd = pytesseract.image_to_osd(img)
        m_rot = re.search(r"Rotate:\s*(\d+)", osd)
        m_conf = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
        if not m_rot:
            return None
        rot = int(m_rot.group(1)) % 360
        conf = float(m_conf.group(1)) if m_conf else 0.0
        return (rot, conf)
    except Exception:
        # 未安装 tesseract / 无 osd 语言包 / 文字太少 → 交回上层回退
        return None


def auto_detect_and_rotate_text(pdf_path):
    """
    高级预处理（智能方向版 + 视觉二次验证）：
      1) 廉价预筛：扫描底层文字 dir 向量，找出"疑似横排被旋转"的候选页；
      2) 二次验证：对候选页渲染成图，用 Tesseract OSD 判断真实视觉方向——
         若 OSD 确认文字已可读（Rotate=0）则【跳过】，避免把本来可读的横版表格
         误转成竖排不可读；需要转时按 OSD 给的真实角度转。
      3) OSD 不可用/不确定时，回退到原 dir 启发式（不回归）。

    背景：很多"横版表格"其实是竖版页 + 已设 /Rotate 显示为横版（文字本就可读），
    底层文字 dir 仍是竖向；仅凭 dir 判断会误判并二次旋转，破坏可读性。
    """
    print("=" * 60)
    print("▶ [阶段一] 正在执行深度预处理：智能检测文字走向 + 视觉二次验证...")

    # OSD 确认"文字已正"所需的最低置信度（偏向"不旋转"，因为过度旋转才是痛点）
    OSD_UPRIGHT_CONF = 0.5
    # OSD 给出"需要旋转"时，采纳其角度所需的最低置信度
    OSD_ROTATE_CONF = 1.0

    doc = fitz.open(pdf_path)
    needs_rotation = False

    for page in doc:
        text_dict = page.get_text("dict")

        horizontal_char_count = 0
        up_char_count = 0    # 从下往上排的文字（通常表头在左侧）
        down_char_count = 0  # 从上往下排的文字（通常表头在右侧）

        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:  # 纯文本块
                for line in block.get("lines", []):
                    dir_vector = line.get("dir", (1.0, 0.0))
                    char_count = sum(len(span.get("text", "")) for span in line.get("spans", []))
                    if abs(dir_vector[1]) > 0.5:
                        if dir_vector[1] < 0:
                            up_char_count += char_count
                        else:
                            down_char_count += char_count
                    else:
                        horizontal_char_count += char_count

        vertical_char_count = up_char_count + down_char_count

        # 预筛：dir 上竖排占多数才视为候选
        is_candidate = vertical_char_count > horizontal_char_count and vertical_char_count > 0
        if not is_candidate:
            continue

        pno = page.number + 1
        # 启发式给出的兜底旋转角（OSD 不可用时用）
        heuristic_rot = 90 if up_char_count > down_char_count else 270

        osd = _osd_rotation_angle(page)
        if osd is not None:
            rot, conf = osd
            if rot % 360 == 0:
                # ✅ 关键修复：视觉确认文字已是正的，跳过旋转
                if conf >= OSD_UPRIGHT_CONF:
                    print(f"  - ✅ 第 {pno} 页：dir 疑似竖排，但 OSD 确认文字已可读"
                          f"（置信度 {conf:.1f}），跳过旋转")
                    continue
                # 置信度过低，OSD 也说不准 → 回退启发式
                chosen = heuristic_rot
                print(f"  - ⚠ 第 {pno} 页：OSD 置信度过低（{conf:.1f}），回退启发式旋转 {chosen} 度")
            elif conf >= OSD_ROTATE_CONF:
                chosen = rot
                print(f"  - 🎯 第 {pno} 页：OSD 判定需顺时针旋转 {chosen} 度（置信度 {conf:.1f}）")
            else:
                chosen = heuristic_rot
                print(f"  - ⚠ 第 {pno} 页：OSD 角度置信度不足（{conf:.1f}），回退启发式旋转 {chosen} 度")
        else:
            chosen = heuristic_rot
            print(f"  - 🎯 第 {pno} 页：OSD 不可用，按文字走向启发式旋转 {chosen} 度")

        page.set_rotation(int(page.rotation + chosen) % 360)
        needs_rotation = True

    if needs_rotation:
        temp_pdf_path = pdf_path.replace(".pdf", "_rotated_ready.pdf")
        doc.save(temp_pdf_path)
        doc.close()
        print("▶ 预处理完成，已生成旋转修正后的临时文件。")
        return temp_pdf_path, True
    else:
        doc.close()
        print("▶ 未检测到需旋转的页面，保持原样。")
        return pdf_path, False

# ==========================================
# 🐵 增强版猴子补丁：兼容 Docling V2 内部结构
# ==========================================
try:
    from docling_core.types.doc.document import TableItem
    _original_export_to_markdown = TableItem.export_to_markdown

    def _lossless_export_to_markdown(self, *args, **kwargs):
        try:
            cells = []
            if hasattr(self, "data") and self.data is not None:
                if hasattr(self.data, "table_cells") and self.data.table_cells:
                    cells = self.data.table_cells
                elif hasattr(self.data, "cells") and self.data.cells:
                    cells = self.data.cells
            
            if not cells:
                return _original_export_to_markdown(self, *args, **kwargs)

            table_data = []
            max_r, max_c = 0, 0
            for c in cells:
                r = getattr(c, "start_row_offset_idx", getattr(c, "row_index", 0))
                col = getattr(c, "start_col_offset_idx", getattr(c, "col_index", 0))
                text = str(getattr(c, "text", "")).strip().replace("\n", " ").replace("|", "\\|")
                table_data.append((r, col, text))
                max_r = max(max_r, r)
                max_c = max(max_c, col)

            grid = [["" for _ in range(max_c + 1)] for _ in range(max_r + 1)]
            for r, col, text in table_data:
                grid[r][col] = text

            lines = []
            for i, row in enumerate(grid):
                lines.append("| " + " | ".join(row) + " |")
                if i == 0:
                    lines.append("| " + " | ".join(["---"] * (max_c + 1)) + " |")
            return "\n" + "\n".join(lines) + "\n"
        except Exception as e:
            return _original_export_to_markdown(self, *args, **kwargs)

    TableItem.export_to_markdown = _lossless_export_to_markdown
except ImportError:
    pass
# ==========================================
# 核心优化：强制使用国内镜像源 & 环境变量配置
# ==========================================
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# ================= 大模型 API 配置 =================
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.5-plus") 
LLM_API_KEY = os.getenv("LLM_API_KEY", "")  # never hardcode keys
MAX_WORKERS = 5

# ================= 文档切片配置 =================
CHUNK_SIZE = 100

# 初始化 Docling
try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    
    pipeline_options = PdfPipelineOptions()
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = 6
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )
    print("💡 已启用 Docling V2 高级模式 (已开启图片提取)")
except ImportError:
    from docling.document_converter import DocumentConverter
    converter = DocumentConverter()
    print("💡 使用基础 Docling 模式")

CHART_ANALYSIS_PROMPT = """
请作为专业的金融图表分析师，提取这张图片中的核心数据。请务必遵循以下极其严格的提取规则：

1. **识别与过滤**：如果图片是普通照片、无数据价值的装饰插图，直接且仅输出：2. **图例与坐标系映射（极其重要，必须遵守）**：
   - **强制图例映射**：识别图表中的图例（Legend），必须准确捕捉图例的文本（如"营业收入"、"同比增速"）。在提取的数据表中，**必须强制使用识别到的图例真实文本作为列名或类别名**。
   - **严禁使用方位/视觉词汇**：绝对不允许在表格中使用"上部数据"、"中部数据"、"左侧"、"深色柱子"、"浅色折线"等描述物理空间或颜色的词汇作为数据类别名称。必须将其还原为图例表示的真实业务指标名称！
   - **单位提取**：仔细留意坐标轴顶部或图例旁的单位标识（如"亿元"、"万吨"、"%"），并将其合并到对应的表头名称中，例如写成"净利润(亿元)"。
   - **⚠️ 极易混淆数字校验（白色/浅色字体）**：图表中的白色或浅色小字体极易产生视觉发光效应，导致数字「6」和「8」互相混淆！请务必仔细辨认，并强制结合**上下文业务逻辑**（如：总计等于分项之和）或**前后年份趋势连贯性**进行数学逻辑上的二次交叉验证，坚决杜绝 6 和 8 看错的情况！

3. **数据提取规范**：
   - 对于柱状图/折线图：通常将横坐标（X轴）的类别（如年份/月份）作为表格的第一列，各个图例对应的指标作为后续的列名。交叉提取对应数值。若无数据标签，请根据Y轴刻度精确估算。
   - 对于饼图：提取每个扇区的标签名称及对应百分比/数值，形成如 `类别 | 占比(%)` 的两列表格。
   - 对于图片表格：忠实还原原始表头和所有单元格数据，处理好合并单元格的逻辑。
   - ⚠️ 【极度重要】：无论数字有多长（哪怕达到十亿、百亿级别且带有两位小数），【绝对禁止】使用科学计数法（例如 1.2e9）或任何形式的省略！你必须一字不差地输出图片中的每一位数字。如果图片中是 1234567890.12，你必须完整抄写 1234567890.12

4. **强制输出格式**：
   - 只输出 Markdown 格式的表格（如果是包含多个子图的复杂图片，可分别输出多个表格，以空行分隔）。
   - 绝对不要输出任何前言、后语、分析过程或解释性文字。
"""

def compress_image(image_bytes, max_size=3072, quality=95):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        enhancer_contrast = ImageEnhance.Contrast(img)
        img = enhancer_contrast.enhance(1.0)  
        
        enhancer_sharpness = ImageEnhance.Sharpness(img)
        img = enhancer_sharpness.enhance(1.1) 

        width, height = img.size
        if width > max_size or height > max_size:
            if width > height:
                new_width, new_height = max_size, int(height * (max_size / width))
            else:
                new_width, new_height = int(width * (max_size / height)), max_size
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
        output_io = io.BytesIO()
        img.save(output_io, format="JPEG", quality=quality)
        return output_io.getvalue()
    except Exception as e:
        print(f"   ⚠️ 画质增强/压缩失败: {e}，将尝试使用原图")
        return image_bytes

def encode_image_to_base64(image_bytes):
    return base64.b64encode(image_bytes).decode('utf-8')

def has_numbers_in_image(image_bytes):
    # without pytesseract we cannot pre-filter — send everything to the LLM
    if pytesseract is None:
        return True
    try:
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image, lang='eng')
        numbers = re.findall(r'\d+', text)
        return len(numbers) > 0
    except Exception:
        return True

def analyze_image_with_llm(image_bytes, page_no, img_idx, context_text="", max_retries=3):
    print(f"   🚀 开始大模型分析 -> 相对页码 {page_no} | 图表 {img_idx}")
    compressed_bytes = compress_image(image_bytes)
    base64_image = encode_image_to_base64(compressed_bytes)
    
    dynamic_prompt = CHART_ANALYSIS_PROMPT
    if context_text:
        dynamic_prompt += f"\n\n【❗极其重要：图表上下文补充信息】\n{context_text}"

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": dynamic_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }
        ],
        "max_tokens": 2000
    }
    
    for attempt in range(max_retries):
        try:
            response = requests.post(f"{LLM_API_BASE}/chat/completions", headers=headers, json=payload, timeout=200)
            response.raise_for_status() 
            
            response_json = response.json()
            if 'choices' in response_json and len(response_json['choices']) > 0:
                content = response_json['choices'][0]['message']['content'].strip()
            else:
                raise ValueError(f"大模型返回了意外的结构: {response_json}")
            
            content = re.sub(r"^\x60\x60\x60(?:markdown|md)?\s*", "", content, flags=re.IGNORECASE)
            content = re.sub(r"\s*\x60\x60\x60$", "", content).strip()
                
            print(f"   ✅ 图表 {img_idx} 解析完成！")
            return content
            
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))
            else:
                return ""
        except Exception as e:
            return f""

def _docling_bbox_to_fitz_rect(bbox, page):
    """Map a Docling provenance bbox to a PyMuPDF Rect (top-left origin)."""
    H = page.rect.height
    try:
        tl = bbox.to_top_left_origin(page_height=H)
        rect = fitz.Rect(tl.l, tl.t, tl.r, tl.b)
    except Exception:
        rect = fitz.Rect(bbox.l, H - bbox.t, bbox.r, H - bbox.b)
    rect.normalize()
    return fitz.Rect(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2)


def _reconstruct_table_md(fitz_doc, item):
    """Header-anchored coordinate reconstruction for one Docling TableItem.
    Returns clean markdown, or None to fall back to TableFormer's output."""
    if os.getenv("HEADER_ANCHORED_TABLES", "1") == "0":   # opt-out switch
        return None
    try:
        from engine.table_reconstruct import region_markdown
    except Exception:
        return None
    prov = item.prov[0] if getattr(item, "prov", None) else None
    if prov is None:
        return None
    pno = getattr(prov, "page_no", None)
    bbox = getattr(prov, "bbox", None)
    if pno is None or bbox is None or not (1 <= pno <= fitz_doc.page_count):
        return None
    page = fitz_doc[pno - 1]
    return region_markdown(page, clip=_docling_bbox_to_fitz_rect(bbox, page))


def process_single_chunk(chunk_pdf_path):
    result = converter.convert(chunk_pdf_path)
    doc = result.document
    fitz_doc = fitz.open(chunk_pdf_path)   # for coordinate-based table rebuild
    tbl_rebuilt = tbl_fallback = 0

    final_md_lines = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
    future_to_md_index = {} 
    recent_texts_buffer = [] 
    
    if hasattr(doc, "iterate_items"):
        pic_count = 0
        for yielded_val in doc.iterate_items():
            # 【优化1】：不仅提取内容，还同时捕获该元素在文档树中的 level(层级)
            item = yielded_val[0] if isinstance(yielded_val, tuple) else yielded_val
            docling_level = yielded_val[1] if isinstance(yielded_val, tuple) and len(yielded_val) > 1 else 1
            
            item_type = type(item).__name__
            page_no = getattr(item.prov[0], "page_no", "?") if hasattr(item, "prov") and item.prov else "?"

            if item_type == "PictureItem":
                pic_count += 1
                pil_img = None
                
                if hasattr(item, "get_image"):
                    try: pil_img = item.get_image(doc)
                    except: pass
                if pil_img is None and hasattr(item, "_pil_image"):
                    pil_img = item._pil_image
                if pil_img is None and hasattr(item, "image") and item.image is not None:
                    pil_img = getattr(item.image, "pil_image", item.image)
                    
                if pil_img is not None:
                    img_byte_arr = io.BytesIO()
                    pil_img.save(img_byte_arr, format='PNG')
                    img_bytes = img_byte_arr.getvalue()
                    
                    if has_numbers_in_image(img_bytes):
                        placeholder_idx = len(final_md_lines)
                        final_md_lines.append(f"\n\n")
                        context_str = "\n".join(recent_texts_buffer)
                        
                        future = executor.submit(analyze_image_with_llm, img_bytes, page_no, pic_count, context_str)
                        future_to_md_index[future] = placeholder_idx
                    else:
                        final_md_lines.append("\n\n")

            # 表格：优先用表头锚定的坐标重建（数字原生 PDF 上更准），
            # 失败/无可靠表头时回退 Docling TableFormer 的导出
            elif item_type == "TableItem":
                md_tbl = _reconstruct_table_md(fitz_doc, item)
                if md_tbl:
                    tbl_rebuilt += 1
                else:
                    tbl_fallback += 1
                    try:
                        md_tbl = item.export_to_markdown()
                    except Exception:
                        md_tbl = ""
                final_md_lines.append("\n\n" + md_tbl + "\n\n")

            # 【优化2】：非图片内容的标题强化处理核心逻辑
            else:
                try:
                    raw_text = getattr(item, "text", "").strip()
                    item_label = str(getattr(item, "label", "")).lower()
                    exported_text = ""

                    # === 强化版层级判定 ===
                    detected_level = 0

                    # 预处理：去除首尾空白，用于正则匹配
                    clean_logic_text = raw_text.strip()

                    # Level 1: "第X节" 或 中文数字+顿号（如"七、合并报表附注（续）"）
                    if re.match(r"^第[一二三四五六七八九十百]+节", clean_logic_text) or \
                       re.match(r"^[一二三四五六七八九十百]+[、\s]", raw_text):
                        detected_level = 1

                    # Level 2: "（一）" 或 "(一)" —— 括号内只有数字才算标题
                    elif re.match(r"^[（\(][一二三四五六七八九十]+[）\)]", clean_logic_text):
                        detected_level = 2

                    # Level 3: "4 、衍生金融工具" (支持数字+空格+顿号)
                    elif re.match(r"^\d+[\s、\.]", raw_text):
                        detected_level = 3

                    # Level 4: "1.1" (1）" 或 "(1)" 
                    elif re.match(r"^\d+\.\d+", clean_logic_text) or \
                       re.match(r"^[（\(]\d+[）\)]", clean_logic_text):
                        detected_level = 4

                    # === 渲染输出 ===
                    exported_text = item.export_to_markdown() if hasattr(item, "export_to_markdown") else raw_text

                    if exported_text:
                        clean_text = exported_text.strip()

                        # === 强化注入标题格式 ===
                        if detected_level > 0:
                            # 根据 detected_level 决定 '#' 数量
                            heading_prefix = "#" * detected_level

                            if not clean_text.startswith("#"):
                                # 原文没有 #，强行加上并加粗
                                exported_text = f"\n\n{heading_prefix} **{raw_text}**\n\n"
                            else:
                                # 原文已有 #，替换为新层级并包裹加粗
                                text_without_hash = re.sub(r"^#+\s*", "", clean_text)
                                exported_text = f"\n\n{heading_prefix} **{text_without_hash}**\n\n"
                        else:
                            # 普通文本，正常换行
                            exported_text = exported_text + "\n"
                        
                        final_md_lines.append(exported_text)
                        
                        # 记录上下文用于喂给大模型识别图表
                        if clean_text and item_type == "TextItem":
                            recent_texts_buffer.append(clean_text)
                            if len(recent_texts_buffer) > 5:
                                recent_texts_buffer.pop(0)
                except Exception:
                    pass
                    
    for future in concurrent.futures.as_completed(future_to_md_index):
        idx = future_to_md_index[future]
        try:
            final_md_lines[idx] = f"\n{future.result()}\n\n"
        except Exception:
            final_md_lines[idx] = f"\n\n\n"

    executor.shutdown(wait=True)
    try:
        fitz_doc.close()
    except Exception:
        pass
    if tbl_rebuilt or tbl_fallback:
        print(f"   📐 表格重建：坐标法 {tbl_rebuilt} 个，回退 TableFormer {tbl_fallback} 个")
    return "".join(final_md_lines)

def parse_large_pdf_safely(pdf_path, chunk_size=CHUNK_SIZE, output_md_path=None):
    base_name = os.path.splitext(pdf_path)[0]
    if output_md_path is None:
        output_md_path = f"{base_name}{RECOGNIZED_MD_SUFFIX}.md"
    temp_dir = f"{base_name}_temp_chunks"
    
    os.makedirs(temp_dir, exist_ok=True)
    open(output_md_path, 'w', encoding='utf-8').close()

    print("=" * 60)
    print(f"▶ 启动防 OOM 模式：按 {chunk_size} 页/块 切分处理...")
    print(f"▶ 目标文件：{pdf_path}")
    print("=" * 60)

    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    
    for start_page in range(0, total_pages, chunk_size):
        end_page = min(start_page + chunk_size, total_pages)
        chunk_pdf_name = os.path.join(temp_dir, f"chunk_{start_page+1}_{end_page}.pdf")
        
        print(f"\n[任务生成] 正在切割 PDF：第 {start_page+1} 到 {end_page} 页...")
        writer = PdfWriter()
        for page_num in range(start_page, end_page):
            writer.add_page(reader.pages[page_num])
            
        with open(chunk_pdf_name, "wb") as f_out:
            writer.write(f_out)
            
        print(f"[开始解析] 块文档生成完毕，进入 Docling + LLM 管线...")
        chunk_md = process_single_chunk(chunk_pdf_name)
        
        with open(output_md_path, "a", encoding="utf-8") as f_md:
            # 加入一个大标题级别的隔离区，方便大模型认知页面被切分的地方
            f_md.write(f"\n\n# --- PDF 物理切片：第 {start_page+1} - {end_page} 页 ---\n\n")
            f_md.write(chunk_md)
            
        print(f"💾 [阶段保存] 第 {start_page+1}-{end_page} 页的结果已追加保存硬盘。")
        
        os.remove(chunk_pdf_name)
        gc.collect() 

    try: os.rmdir(temp_dir)
    except: pass

    print("\n✅ 所有块处理完毕，防 OOM 提取完美收官！")
    print(f"✅ 最终完整结果已保存至：{output_md_path}")
    return output_md_path

# ==========================================
if __name__ == "__main__":
    target_pdf_file = r"c:\Users\Administrator\Desktop\广发证券：2024年年度报告-200-292.pdf"
    
    if not os.path.exists(target_pdf_file):
        print(f"❌ 找不到文件: {target_pdf_file}")
    else:
        # 1. 先进行旋转检测
        processed_path, is_temp = auto_detect_and_rotate_text(target_pdf_file)
        
        try:
            # 2. 将检测后的路径传入原有的解析函数
            parse_large_pdf_safely(processed_path)
            
            # 3. 清理工作：如果是生成的临时文件，解析完后删除
            if is_temp and os.path.exists(processed_path):
                os.remove(processed_path)
                print(f"🧹 已清理临时旋转文件: {processed_path}")
                
        except Exception as e:
            print(f"\n❌ 程序运行出错：{e}")