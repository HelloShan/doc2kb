"""
doc2kb — 文档转换引擎模块
=============================
支持将 6 种核心格式转换为 Markdown：
  - .docx → md (使用 python-docx)
  - .md   → 复制+乱码校验
  - .pdf  → md (使用 pypdf 或 docling)
  - .txt  → md (直接复制，支持编码回退)
  - .pptx → md (使用 python-pptx)
  - .xlsx → md (使用 openpyxl，含空行/零值行过滤)

每个文件返回 (status, md_rel_path_or_None, error_msg_or_None) 三元组。
"""

import os
import re
import traceback
from pathlib import Path
from typing import Optional, Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    SOURCE_DIR, OUTPUT_MD_DIR, SUPPORTED_EXTENSIONS,
    PDF_ENGINE, DOCX_KEEP_IMAGES,
    CONVERT_WORKERS,
)
from validate import is_file_readable
from state import compute_sha256
from docx.opc.exceptions import PackageNotFoundError


# ============================================================
# 工具函数
# ============================================================

def _get_rel_path(source_path: Path) -> str:
    """获取源文件的相对路径（统一用 POSIX 风格）。"""
    return source_path.relative_to(SOURCE_DIR).as_posix()


def _get_output_md_path(source_path: Path) -> Path:
    """计算源文件对应的输出 MD 路径。"""
    rel = source_path.relative_to(SOURCE_DIR)
    md_rel = rel.with_suffix(".md")
    return OUTPUT_MD_DIR / md_rel


def _ensure_output_dir(output_path: Path):
    """确保输出文件的目录存在。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)


# ============================================================
# MD 内容清洁：移除封面/目录/版权/版本记录/作者信息
# ============================================================

# 需要移除的单行模式（匹配即移除该行）
_BOILERPLATE_LINE_PATTERNS = [
    re.compile(r'^#+\s*(前\s*言|引言|概述|背景|前\s*言\s*$)'),
    re.compile(r'^#+\s*(目\s*录|目录|Contents)'),
    re.compile(r'(版权|著作权|著作权声明|版权声明|©\s*\d{4})'),
    re.compile(r'(All\s+rights?\s+reserved)', re.IGNORECASE),
    re.compile(r'(版本\s*[：:]|版\s*本\s*[：:])'),
    re.compile(r'(修订记录|修\s*订\s*记\s*录|变更记录|变更历史|修订历史)'),
    re.compile(r'^(作者|编写[：:]?|编制[：:]?|审核[：:]?|批准[：:]?|校对[：:]?|会审[：:]?|评审[：:]?|起草[：:]?)'),
    re.compile(r'^(第\s*\d+\s*页|Page\s+\d+|—\s*\d+\s*—)$'),
]

# 检测"疑似目录"段落
# 目录行特征：行首是编号（1.1 / 1.1.1 等），后有中文内容再跟点线+页码
_TOC_ENTRY = re.compile(
    r'^\d+(\.\d+)*\s*[\u4e00-\u9fff][\u4e00-\u9fff\w]*[\s.…·]+\d+\s*$'  # 1.1 概述........5
)
# 简单的编号+短文本行（如 "1 概述"、"一、概述"）
_TOC_NUM_LINE = re.compile(r'^[\d一二三四五六七八九十]+[.、．]\s*\S{1,40}$')
# 纯点线行：..... 或 …………
_TOC_PURE_DOTS = re.compile(r'^[\s.…·]+$')
# 目录标题行 —— 必须包含#号或"目录"字样
_TOC_HEADING = re.compile(r'^#+\s*[目\t ]*[录録]\s*$|^[#\s]*目录|^[#\s]*Contents')


def _is_toc_section(lines: list[str], start: int, max_lookahead: int = 50) -> int:
    """
    从 start 开始检测是否是目录段落。
    目录特征：连续多行都符合目录行特征。
    返回 ex_end（下一段落的起始行号），不是目录则返回 start。
    """
    # 快速检查：当前行是不是目录标题
    if not _TOC_HEADING.match(lines[start].strip()):
        return start

    end = min(start + max_lookahead, len(lines))
    toc_count = 1  # 算上标题行
    for i in range(start + 1, end):
        raw = lines[i].strip()
        if not raw:
            toc_count += 1  # 空行也算在目录段落内
            continue
        if raw.startswith('#'):
            continue  # 子标题也算目录
        if _TOC_PURE_DOTS.match(raw):
            toc_count += 1
            continue
        if _TOC_ENTRY.match(raw) or _TOC_NUM_LINE.match(raw):
            toc_count += 1
            continue
        # 不是目录特征行，终止扫描
        break

    # 至少 4 行才认为是目录
    return start + toc_count if toc_count >= 4 else start


def _clean_md_content(content: str) -> str:
    """
    移除 Markdown 中的封面/目录/版权/版本记录/作者信息等模板化段落。
    策略：
      1. 先检测 TOC 段落（基于段落特征），标注范围
      2. 再移除匹配的单行模板模式
      3. 最后检测并移除文件开头的"封面"段
    """
    if not content.strip():
        return content

    lines = content.split('\n')
    n = len(lines)
    keep = [True] * n

    # ── Pass 1: 先检测并移除目录段落 ──
    # 必须在单行模式之前，因为目录标题可能被单行模式先行标记删除
    i = 0
    while i < n:
        toc_end = _is_toc_section(lines, i)
        if toc_end > i:
            for j in range(i, toc_end):
                keep[j] = False
            i = toc_end
            continue
        i += 1

    # ── Pass 2: 移除单行模板模式 ──
    for i, line in enumerate(lines):
        if not keep[i]:
            continue
        stripped = line.strip()
        if any(p.search(stripped) for p in _BOILERPLATE_LINE_PATTERNS):
            keep[i] = False

    # ── Pass 3: 移除文件开头的封面段 ──
    first_heading_idx = -1
    for i, line in enumerate(lines):
        if not keep[i]:
            continue
        stripped = line.strip()
        if stripped.startswith('# ') or stripped.startswith('## '):
            first_heading_idx = i
            break

    if first_heading_idx > 0:
        pre_lines = [lines[j] for j in range(first_heading_idx) if keep[j] and lines[j].strip()]
        if pre_lines and all(len(l.strip()) < 40 for l in pre_lines):
            for j in range(first_heading_idx):
                keep[j] = False

    # ── 组装 ──
    cleaned = '\n'.join(line for i, line in enumerate(lines) if keep[i])

    # 清理多余的空行（连续 3+ 空行 → 2 空行）
    cleaned = re.sub(r'\n{4,}', '\n\n\n', cleaned)
    return cleaned.strip()


# ============================================================
# Word (.docx)
# ============================================================

def _convert_docx(source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """
    将 .docx 转换为 Markdown（纯 python-docx，无需外部工具）。

    支持：标题级别识别、段落、表格。
    兼容 .docm（宏文档）：python-docx 拒绝时，直接从 ZIP 中提取 XML 文本。
    兼容损坏的 .docx：捕获 'NULL' 引用等异常并回退 ZIP 方案。
    """
    try:
        from docx import Document
        doc = Document(str(source_path))
        return _docx_to_md(doc, source_path, output_path)

    except ValueError as e:
        err_msg = str(e)
        if "not a Word file" in err_msg:
            # .docm 或非标准 .docx → ZIP 回退
            return _docx_fallback_zip(source_path, output_path)
        return ("error", f"{type(e).__name__}: {e}")
    except PackageNotFoundError:
        return ("error", "不是有效的 .docx 文件（ZIP 包无法解析），"
                         "可能是旧版 .doc 格式误标为 .docx 扩展名")
    except KeyError as e:
        e_str = str(e)
        if "'NULL'" in e_str or "NULL" in e_str:
            # 损坏的 docx 引用了 "NULL" 项 → 尝试 ZIP 回退
            return _docx_fallback_zip(source_path, output_path)
        return ("error", f"{type(e).__name__}: {e}")
    except ImportError:
        return ("error", "缺少 python-docx 库，请执行: uv pip install python-docx")
    except Exception as e:
        e_str = f"{type(e).__name__}: {e}"
        e_str_lower = e_str.lower()
        # 常见 docx 损坏模式：关系丢失、NULL引用、XML解析错误
        if ("no relationship" in e_str_lower and "officedocument" in e_str_lower) \
           or "'null'" in e_str_lower \
           or "xmlsyntaxerror" in e_str_lower \
           or ("xml" in e_str_lower and "expected" in e_str_lower):
            return _docx_fallback_zip(source_path, output_path)
        return ("error", e_str)


def _docx_to_md(doc, source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """将 python-docx Document 对象写出为 Markdown 文件。"""
    md_lines = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        style_name = para.style.name.lower() if para.style else ""
        if "heading 1" in style_name or "title" in style_name:
            md_lines.append(f"# {text}")
        elif "heading 2" in style_name:
            md_lines.append(f"## {text}")
        elif "heading 3" in style_name:
            md_lines.append(f"### {text}")
        elif "heading" in style_name:
            level = 4
            for s in ("heading 4", "heading 5", "heading 6"):
                if s in style_name:
                    level = int(s[-1])
                    break
            md_lines.append(f"{'#' * level} {text}")
        else:
            md_lines.append(text)

    for table in doc.tables:
        md_lines.append("")
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            md_lines.append("| " + " | ".join(cells) + " |")
        md_lines.append("")

    md_content = "\n\n".join(md_lines).strip()
    if not md_content:
        return ("empty", "文档内容为空")

    # 清洁模板段落
    md_content = _clean_md_content(md_content)
    if not md_content:
        return ("empty", "文档内容为空（移除了所有模板段落）")

    _ensure_output_dir(output_path)
    output_path.write_text(md_content, encoding="utf-8")

    if not is_file_readable(output_path):
        return ("garbled", "转换后内容疑似乱码")

    return ("ok", None)


def _docx_fallback_zip(source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """
    python-docx 拒绝损坏/宏文档时，直接从 ZIP 中提取 word/document.xml 文本。
    这是对损坏 .docx 和 .docm 伪装 .docx 的降级处理。
    """
    try:
        import zipfile
        from xml.etree import ElementTree as ET

        with zipfile.ZipFile(str(source_path)) as z:
            # 尝试标准路径
            xml_paths = ["word/document.xml", "Word/document.xml"]
            xml_content = None
            for p in xml_paths:
                try:
                    xml_content = z.read(p)
                    break
                except KeyError:
                    continue
            if xml_content is None:
                return ("error", "ZIP 中未找到 word/document.xml")

        root = ET.fromstring(xml_content)
        ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        tag_p = f"{{{ns_w}}}p"
        tag_t = f"{{{ns_w}}}t"

        md_lines = []
        for para in root.iter(tag_p):
            texts = []
            for t in para.iter(tag_t):
                if t.text:
                    texts.append(t.text)
            line = "".join(texts).strip()
            if line:
                md_lines.append(line)

        md_content = "\n\n".join(md_lines).strip()
        if not md_content:
            return ("empty", "ZIP 回退提取内容为空")

        md_content = _clean_md_content(md_content)
        if not md_content:
            return ("empty", "ZIP 回退提取内容为空（移除模板后）")

        _ensure_output_dir(output_path)
        output_path.write_text(md_content, encoding="utf-8")
        return ("ok", None)

    except Exception as e:
        return ("error", f"ZIP 回退提取失败: {type(e).__name__}: {e}")


# ============================================================
# Markdown (.md)
# ============================================================

def _convert_md(source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """处理 .md 文件：复制+乱码校验+清洁模板段落。"""
    try:
        content = source_path.read_text(encoding="utf-8")
        if not content.strip():
            return ("empty", "文件内容为空")

        if not is_file_readable(source_path):
            return ("garbled", "源文件内容疑似乱码")

        # 清洁模板段落
        content = _clean_md_content(content)
        if not content.strip():
            return ("empty", "文件内容为空（移除了所有模板段落）")

        _ensure_output_dir(output_path)
        output_path.write_text(content, encoding="utf-8")
        return ("ok", None)

    except Exception as e:
        return ("error", f"{type(e).__name__}: {e}")


# ============================================================
# PDF
# ============================================================

def _convert_pdf_docling(source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """使用 Docling 将 PDF 转换为 Markdown（高质量，支持复杂布局）。"""
    try:
        from docling.document_converter import DocumentConverter
        converter = DocumentConverter()
        result = converter.convert(str(source_path))
        md_content = result.document.export_to_markdown()

        if not md_content.strip():
            return ("empty", "PDF 内容为空")

        md_content = _clean_md_content(md_content)
        if not md_content.strip():
            return ("empty", "PDF 内容为空（移除了所有模板段落）")

        _ensure_output_dir(output_path)
        output_path.write_text(md_content, encoding="utf-8")

        if not is_file_readable(output_path):
            return ("garbled", "转换后内容疑似乱码")

        return ("ok", None)

    except ImportError:
        return ("error", "缺少 docling 库，请执行: uv pip install docling")
    except Exception as e:
        return ("error", f"Docling {type(e).__name__}: {e}")


def _convert_pdf_pypdf(source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """
    使用 pypdf 将 PDF 转换为 Markdown（轻量、纯Python）。

    适合文本型 PDF，对于扫描件或复杂布局的 PDF 效果较差。
    捕获 PdfStreamError 等损坏 PDF 异常。
    """
    try:
        from pypdf import PdfReader, errors as pypdf_errors

        try:
            reader = PdfReader(str(source_path))
        except pypdf_errors.PdfStreamError:
            return ("error", "PDF 流意外结束（文件可能损坏或不完整）")
        except Exception as e:
            return ("error", f"PDF 读取失败: {type(e).__name__}: {e}")

        md_lines = []
        for i, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text()
            except Exception:
                text = ""
            if text and text.strip():
                md_lines.append(f"<!-- Page {i} -->\n\n{text.strip()}")

        md_content = "\n\n".join(md_lines).strip()
        if not md_content:
            return ("empty", "PDF 内容为空（可能是扫描件，建议使用 Docling 或 OCR）")

        md_content = _clean_md_content(md_content)
        if not md_content.strip():
            return ("empty", "PDF 内容为空（移除了所有模板段落）")

        _ensure_output_dir(output_path)
        output_path.write_text(md_content, encoding="utf-8")

        if not is_file_readable(output_path):
            return ("garbled", "转换后内容疑似乱码")

        return ("ok", None)

    except ImportError:
        return ("error", "缺少 pypdf 库，请执行: uv pip install pypdf")
    except Exception as e:
        return ("error", f"PyPDF {type(e).__name__}: {e}")


def _convert_pdf(source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """PDF 转换调度器，根据配置选择引擎。"""
    if PDF_ENGINE == "docling":
        return _convert_pdf_docling(source_path, output_path)
    else:
        return _convert_pdf_pypdf(source_path, output_path)


# ============================================================
# 纯文本 (.txt) —— 支持编码回退
# ============================================================

_TXT_ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]


def _convert_txt(source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """
    处理 .txt 文件：尝试多种编码读取，复制为 Markdown。
    编码回退顺序：utf-8 → gbk → gb2312 → gb18030 → latin-1
    """
    for enc in _TXT_ENCODINGS:
        try:
            content = source_path.read_text(encoding=enc)
            if content.strip():
                break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        return ("error", f"无法用 {', '.join(_TXT_ENCODINGS)} 解码文件内容")

    if not content.strip():
        return ("empty", "文件内容为空")

    content = _clean_md_content(content)
    if not content.strip():
        return ("empty", "文件内容为空（移除了所有模板段落）")

    _ensure_output_dir(output_path)
    output_path.write_text(content, encoding="utf-8")

    return ("ok", None)


# ============================================================
# PowerPoint (.pptx)
# ============================================================

def _convert_pptx(source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """
    将 .pptx 转换为 Markdown（纯 python-pptx，无需外部工具）。
    提取每页幻灯片的文本内容，保留标题层级。
    """
    try:
        from pptx import Presentation
        prs = Presentation(str(source_path))
        md_lines = []

        for i, slide in enumerate(prs.slides, 1):
            md_lines.append(f"## Slide {i}")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = para.text.strip()
                        if text:
                            md_lines.append(text)
            md_lines.append("")

        md_content = "\n".join(md_lines).strip()
        if not md_content:
            return ("empty", "幻灯片内容为空")

        md_content = _clean_md_content(md_content)
        if not md_content.strip():
            return ("empty", "幻灯片内容为空（移除了所有模板段落）")

        _ensure_output_dir(output_path)
        output_path.write_text(md_content, encoding="utf-8")

        return ("ok", None)

    except ImportError:
        return ("error", "缺少 python-pptx 库，请执行: uv pip install python-pptx")
    except Exception as e:
        return ("error", f"{type(e).__name__}: {e}")


# ============================================================
# Excel (.xlsx) —— 含空行/零值行过滤
# ============================================================

def _is_junk_cell(value) -> bool:
    """判断单元格是否为'垃圾值'（空、None、0、'0'等）。"""
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return value == 0 or value == 0.0
    s = str(value).strip()
    return s == "" or s == "0" or s == "0.0"


def _is_junk_row(row: tuple) -> bool:
    """判断整行是否都是垃圾值（全空/全零），应过滤掉。"""
    if not row:
        return True
    return all(_is_junk_cell(cell) for cell in row)


def _convert_xlsx(source_path: Path, output_path: Path) -> Tuple[str, Optional[str]]:
    """
    将 .xlsx 转换为 Markdown 表格（纯 openpyxl，无需外部工具）。

    保留工作表名称为二级标题，每个工作表转为 Markdown 表格。
    自动过滤全空/全零的数据行，但不破坏 |---| 表头分割行。
    """
    try:
        from openpyxl import load_workbook

        wb = load_workbook(str(source_path), read_only=True, data_only=True)
        md_lines = []

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            md_lines.append(f"## {sheet_name}")
            md_lines.append("")

            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue

            # 表头行
            headers = [str(c) if c is not None else "" for c in rows[0]]
            md_lines.append("| " + " | ".join(headers) + " |")
            md_lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

            # 数据行：过滤掉全空/全零的垃圾行
            data_rows = [row for row in rows[1:] if not _is_junk_row(row)]
            for row in data_rows:
                cells = [str(c) if c is not None else "" for c in row]
                md_lines.append("| " + " | ".join(cells) + " |")

            md_lines.append("")

        wb.close()
        md_content = "\n".join(md_lines).strip()
        if not md_content:
            return ("empty", "Excel 内容为空")

        md_content = _clean_md_content(md_content)
        if not md_content.strip():
            return ("empty", "Excel 内容为空（移除了所有模板段落）")

        _ensure_output_dir(output_path)
        output_path.write_text(md_content, encoding="utf-8")

        return ("ok", None)

    except ImportError:
        return ("error", "缺少 openpyxl 库，请执行: uv pip install openpyxl")
    except Exception as e:
        return ("error", f"{type(e).__name__}: {e}")


# ============================================================
# 转换调度器注册表
# ============================================================

_CONVERTER_REGISTRY = {
    ".docx": _convert_docx,
    ".md":   _convert_md,
    ".pdf":  _convert_pdf,
    ".txt":  _convert_txt,
    ".pptx": _convert_pptx,
    ".xlsx": _convert_xlsx,
}


def convert_single_file(source_path: Path) -> dict:
    """
    转换单个源文件为 Markdown。

    Parameters
    ----------
    source_path : Path
        源文件绝对路径。

    Returns
    -------
    dict: {
        "rel_path": "relative/path/file.docx",
        "status": "ok" | "garbled" | "empty" | "error" | "skip",
        "md_path": "output_md/relative/path.md" | None,
        "error": str | None,
        "sha256": str,
        "size": int,
        "mtime": str,
    }
    """
    rel_path = _get_rel_path(source_path)
    ext = source_path.suffix.lower()
    result = {
        "rel_path": rel_path,
        "status": "error",
        "md_path": None,
        "error": None,
        "sha256": "",
        "size": 0,
        "mtime": "",
    }

    try:
        # 基本信息
        stat = source_path.stat()
        result["size"] = stat.st_size
        result["sha256"] = compute_sha256(source_path)

        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        result["mtime"] = dt.isoformat(timespec="seconds")

        # 查找转换器
        converter = _CONVERTER_REGISTRY.get(ext)
        if converter is None:
            # 不支持格式 → 仅打印文件名到日志，明确提示
            result["status"] = "skip"
            result["error"] = f"不支持的格式 {ext}，忽略文件: {rel_path}"
            return result

        # 执行转换
        output_path = _get_output_md_path(source_path)
        status, error = converter(source_path, output_path)

        result["status"] = status
        result["error"] = error
        if status == "ok":
            result["md_path"] = output_path.relative_to(OUTPUT_MD_DIR).with_suffix(".md").as_posix()

    except Exception as e:
        result["status"] = "error"
        result["error"] = f"意外异常: {type(e).__name__}: {e}\n{traceback.format_exc()}"

    return result


def convert_batch(file_paths: List[Path],
                  max_workers: int = CONVERT_WORKERS,
                  progress_callback=None) -> List[dict]:
    """
    批量转换文件，支持 Ctrl+C 中断。

    Parameters
    ----------
    file_paths : List[Path]
        要转换的源文件路径列表。
    max_workers : int
        并行线程数。
    progress_callback : callable, optional
        每完成一个文件的回调函数 fn(result_dict)。

    Returns
    -------
    List[dict]
        每个文件的结果字典列表。
    """
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(convert_single_file, fp): fp for fp in file_paths}
        try:
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                if progress_callback:
                    progress_callback(result)
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            pool.shutdown(wait=False)
            raise

    # 按 rel_path 排序，保证结果顺序稳定
    results.sort(key=lambda r: r["rel_path"])
    return results
