"""
doc2kb — 兼容性检测工具
========================
扫描源目录，找出无法自动转换的文档，列出清单供手工处理。

检测项：
  - 伪装为 .docx 的宏文档 (.docm)
  - 伪装为 .docx 的旧版 .doc（不是 ZIP 包）
  - 损坏的 PDF 文件
  - 编码无法识别的 .txt

用法:
  python check_compat.py                    # 扫描默认源目录
  python check_compat.py --dir ../my_docs   # 指定目录
"""

import argparse
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def is_docm(file_path: Path) -> tuple[bool, str]:
    """
    检查 .docx 文件是否为宏文档 (.docm) 或旧版 .doc。
    返回 (is_problem, reason)。
    """
    if file_path.suffix.lower() not in ('.docx',):
        return False, ''
    try:
        with zipfile.ZipFile(file_path) as z:
            ct = z.read('[Content_Types].xml')
            root = ET.fromstring(ct)
            ns = root.tag.split('}')[0].strip('{') if '}' in root.tag else ''
            tag = f'{{{ns}}}Override' if ns else 'Override'
            for override in root.iter(tag):
                ct_type = override.get('ContentType', '')
                if 'macroenabled' in ct_type.lower():
                    return True, '宏文档 (.docm)，需用 Word 另存为 .docx'
        return False, ''
    except (zipfile.BadZipFile, Exception):
        return True, '不是有效的 ZIP 包（可能是旧版 .doc 格式误标为 .docx）'


def is_broken_pdf(file_path: Path) -> tuple[bool, str]:
    """检查 PDF 文件是否可读。"""
    if file_path.suffix.lower() != '.pdf':
        return False, ''
    try:
        from pypdf import PdfReader, errors as pypdf_errors
        try:
            reader = PdfReader(str(file_path))
            _ = len(reader.pages)
            return False, ''
        except pypdf_errors.PdfStreamError:
            return True, 'PDF 流意外结束（文件损坏或不完整）'
        except Exception as e:
            return True, f'PDF 读取失败: {type(e).__name__}'
    except ImportError:
        return False, ''


def is_broken_txt(file_path: Path) -> tuple[bool, str]:
    """检查 .txt 文件编码。"""
    if file_path.suffix.lower() != '.txt':
        return False, ''
    for enc in ['utf-8', 'gbk', 'gb2312', 'gb18030', 'latin-1']:
        try:
            content = file_path.read_text(encoding=enc)
            if content.strip():
                return False, ''
        except (UnicodeDecodeError, UnicodeError):
            continue
    return True, '无法用常见编码 (utf-8/gbk/gb2312/latin-1) 解码'


def scan_directory(directory: Path) -> list[tuple[Path, str]]:
    """扫描目录，返回 (问题文件路径, 原因) 列表。"""
    problems = []
    for fp in sorted(directory.rglob('*')):
        if not fp.is_file():
            continue

        ext = fp.suffix.lower()
        if ext not in ('.docx', '.pdf', '.txt'):
            continue

        if ext == '.docx':
            is_prob, reason = is_docm(fp)
        elif ext == '.pdf':
            is_prob, reason = is_broken_pdf(fp)
        elif ext == '.txt':
            is_prob, reason = is_broken_txt(fp)
        else:
            continue

        if is_prob:
            problems.append((fp, reason))

    return problems


def main():
    parser = argparse.ArgumentParser(description='检测无法自动转换的文档')
    parser.add_argument('--dir', default='', help='扫描目录')
    args = parser.parse_args()

    if args.dir:
        src_dir = Path(args.dir)
    else:
        try:
            from config import SOURCE_DIR
            src_dir = SOURCE_DIR
        except ImportError:
            src_dir = Path('../source_docs')

    if not src_dir.exists():
        print(f'❌ 目录不存在: {src_dir}')
        return

    print(f'🔍 扫描目录: {src_dir.resolve()}')
    problems = scan_directory(src_dir)

    if not problems:
        print('✅ 所有文档兼容，无问题文件')
        return

    print(f'\n⚠  发现 {len(problems)} 个不兼容文件：\n')
    for fp, reason in problems:
        rel = fp.relative_to(src_dir)
        size = fp.stat().st_size / 1024
        print(f'  📄 {rel}  ({size:.0f} KB)')
        print(f'     原因: {reason}')
        print()

    print(f'共 {len(problems)} 个文件需手工处理，处理后重新运行流水线。')


if __name__ == '__main__':
    main()
