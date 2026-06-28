"""
doc2kb — 中心化配置模块
==========================
所有可调参数集中在此文件，方便管理和覆盖。
支持通过环境变量覆盖（如 DOC2KB_SOURCE_DIR, DOC2KB_OUTPUT_MD_DIR 等）。
"""

import os
import warnings
import logging
from datetime import datetime
from pathlib import Path

# 注入 HuggingFace 镜像源，解决国内下载模型超时问题
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# ============================================================
# 抑制第三方库的无效告警
# ============================================================

# Pillow：图片无法处理时不告警（直接丢弃）
warnings.filterwarnings("ignore", category=UserWarning, module="PIL")
warnings.filterwarnings("ignore", message=".*cannot write mode CMYK.*")
warnings.filterwarnings("ignore", message=".*cannot identify image file.*")

# Docling & python-docx：图片/VML/无LibreOffice 等噪音
logging.getLogger("docling").setLevel(logging.ERROR)
logging.getLogger("docling.datamodel").setLevel(logging.ERROR)
logging.getLogger("docling.backend").setLevel(logging.ERROR)
logging.getLogger("docling.pipeline").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=".*VML image cannot be found.*")
warnings.filterwarnings("ignore", message=".*Found DrawingML elements.*")
warnings.filterwarnings("ignore", message=".*LibreOffice.*")

# ============================================================
# 1. 目录路径
# ============================================================

# 原始文档目录 A：存放 docx/pdf/md 等源文件
SOURCE_DIR = Path(os.getenv("DOC2KB_SOURCE_DIR", "../source_docs"))

# MD 目标文件目录 B：转换后的 .md 文件存放位置
OUTPUT_MD_DIR = Path(os.getenv("DOC2KB_OUTPUT_MD_DIR", "../output_md"))

# LanceDB 知识库路径 C：向量数据库存储位置
DB_PATH = Path(os.getenv("DOC2KB_DB_PATH", "../doc2kb.lancedb"))

# 流水线状态文件
STATE_FILE = Path(os.getenv("DOC2KB_STATE_FILE", "./pipeline_state.json"))

# 日志文件（默认按日期自动生成，如 pipeline_20260628.log）
_DEFAULT_LOG = f"pipeline_{datetime.now().strftime('%Y%m%d')}.log"
LOG_FILE = os.getenv("DOC2KB_LOG_FILE", _DEFAULT_LOG)

# ============================================================
# 2. 文档转换配置
# ============================================================

# 支持转换的源文件扩展名（小写）—— 仅支持以下 6 种核心格式
SUPPORTED_EXTENSIONS = {".docx", ".md", ".pdf", ".txt", ".pptx", ".xlsx"}

# 转换引擎：Docling 为主力（支持 docx/pdf/xlsx/pptx），原生解析为降级
# Docling 提供表格还原、多栏识别、页眉页脚剥离等高级能力
CONVERT_ENGINE = os.getenv("DOC2KB_CONVERT_ENGINE", "docling")

# 生成 MD 文件超过此大小（MB）时打印警告
MAX_MD_FILE_SIZE_MB = 20

# ============================================================
# 3. RAG / 分块配置
# ============================================================

# Embedding 模型（fastembed，CPU运行）
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"   # dim=512, 最快
# 备选: "BAAI/bge-base-zh-v1.5" (dim=768), "BAAI/bge-m3" (dim=1024)
VECTOR_DIM = 512

# Chunk 大小与重叠
CHUNK_SIZE = 1200         # 每块最大字符数
CHUNK_OVERLAP = 300       # 块之间重叠字符数

# LanceDB 表名
TABLE_NAME = "docs"

# 检索参数
TOP_K = 5
SIMILARITY_THRESHOLD = 0.5

# ============================================================
# 4. 并发与内存控制
# ============================================================

# 转换阶段的并行线程数
CONVERT_WORKERS = 4

# 入库阶段的 batch 大小（每批处理的文本数）
EMBED_BATCH_SIZE = 32

# 每处理 N 个文件后强制 flush LanceDB（避免内存累积）
DB_FLUSH_INTERVAL = 20

# 跳过超大文件（超过此 MB 的文件跳过转换）
LARGE_FILE_THRESHOLD_MB = 50

# ============================================================
# 5. 乱码检测配置
# ============================================================

# 前 N 字节中需要包含的 CJK 标点数量（≥此值视为正常文档）
GARBLED_PUNCT_THRESHOLD = 3

# 检查文件的头部字节数
GARBLED_CHECK_BYTES = 2000

# ============================================================
# 6. 输出格式
# ============================================================

# 是否在终端输出带颜色的日志
COLOR_OUTPUT = True


def validate_config():
    """检查配置合理性，打印警告"""
    if CONVERT_WORKERS < 1 or CONVERT_WORKERS > 16:
        print(f"[WARN] CONVERT_WORKERS={CONVERT_WORKERS}，推荐 1-16")
    if DB_FLUSH_INTERVAL < 5:
        print(f"[WARN] DB_FLUSH_INTERVAL={DB_FLUSH_INTERVAL} 过小，可能影响性能")
    if CHUNK_SIZE < CHUNK_OVERLAP * 2:
        print(f"[WARN] CHUNK_SIZE ({CHUNK_SIZE}) 应至少为 CHUNK_OVERLAP ({CHUNK_OVERLAP}) 的两倍")


# 首次导入时自动验证
validate_config()
