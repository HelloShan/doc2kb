"""
doc2kb — 知识库入库模块
===========================
从 Markdown 文件构建 RAG 向量知识库（LanceDB）：
  1. 读取 .md 文件
  2. 按 Markdown 结构分块 (MarkdownTextSplitter)
  3. 用 fastembed 生成本地 CPU 嵌入向量
  4. 写入 LanceDB 向量库

支持增量更新：文件变更时删除旧向量、插入新向量。
"""

import os
import traceback
from pathlib import Path
from typing import Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    DB_PATH, OUTPUT_MD_DIR, TABLE_NAME,
    EMBEDDING_MODEL, VECTOR_DIM,
    CHUNK_SIZE, CHUNK_OVERLAP,
    EMBED_BATCH_SIZE, DB_FLUSH_INTERVAL,
    CONVERT_WORKERS,
)
from validate import is_file_readable
from state import compute_sha256

# ============================================================
# 惰性加载（避免 import 时加载模型）
# ============================================================

_embed_model = None
_db = None
_table = None
_lancedb_lock = __import__('threading').Lock()

_imported_lancedb = False
_lancedb_module = None
_pa_module = None


def _get_embed_model():
    """获取（或初始化）fastembed 嵌入模型。"""
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        _embed_model = TextEmbedding(
            model_name=EMBEDDING_MODEL,
            max_length=CHUNK_SIZE,
        )
    return _embed_model


def _open_or_create_db():
    """
    打开（或创建）LanceDB 数据库和表。
    线程安全：使用全局锁防止并发创建。
    """
    global _db, _table, _lancedb_module, _pa_module

    with _lancedb_lock:
        if _db is None:
            import lancedb as _lancedb_module
            _db = _lancedb_module.connect(str(DB_PATH))

        if _table is None:
            import pyarrow as _pa_module
            schema = _pa_module.schema([
                _pa_module.field("vector", _pa_module.list_(_pa_module.float32(), VECTOR_DIM)),
                _pa_module.field("text", _pa_module.string()),
                _pa_module.field("source", _pa_module.string()),
                _pa_module.field("file_hash", _pa_module.string()),
                _pa_module.field("chunk_index", _pa_module.int32()),
                _pa_module.field("chunk_total", _pa_module.int32()),
            ])

            try:
                _table = _db.open_table(TABLE_NAME)
            except Exception:
                _table = _db.create_table(TABLE_NAME, schema=schema)

        return _table


def close_db():
    """关闭 LanceDB 连接（释放资源）。"""
    global _db, _table
    with _lancedb_lock:
        _table = None
        _db = None


# ============================================================
# 分块
# ============================================================

def chunk_markdown_file(md_path: Path) -> List[dict]:
    """
    将 .md 文件按结构分块。

    Returns
    -------
    List[dict]: [
        {
            "text": "块文本内容",
            "source": "相对路径",
            "file_hash": "sha256",
            "chunk_index": 0,
            "chunk_total": 10,
        },
        ...
    ]
    失败时返回空列表。
    """
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return []

    from langchain_text_splitters import MarkdownTextSplitter
    splitter = MarkdownTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    chunks = splitter.split_text(content)

    rel_path = md_path.relative_to(OUTPUT_MD_DIR).as_posix()
    sha256 = compute_sha256(md_path)
    total = len(chunks)

    return [
        {
            "text": chunk,
            "source": rel_path,
            "file_hash": sha256,
            "chunk_index": i,
            "chunk_total": total,
        }
        for i, chunk in enumerate(chunks)
    ]


# ============================================================
# 向量生成与入库
# ============================================================

def _delete_old_chunks(table, source_rel_path: str):
    """从 LanceDB 中删除指定源文件的所有旧分块。"""
    try:
        table.delete(f'source = "{source_rel_path}"')
    except Exception:
        pass  # 表可能是空的，忽略


def ingest_single_md(md_path: Path, source_rel_path: str,
                     file_sha256: str) -> dict:
    """
    将单个 .md 文件入库到 LanceDB。

    Parameters
    ----------
    md_path : Path
        .md 文件的绝对路径。
    source_rel_path : str
        源文件的相对路径（用于 LanceDB 索引删除/查询）。
    file_sha256 : str
        源文件的 SHA256（用于变更检测）。

    Returns
    -------
    dict: {
        "rel_path": source_rel_path,
        "status": "ok" | "empty" | "garbled" | "error",
        "chunks": int,
        "error": str | None,
    }
    """
    result = {
        "rel_path": source_rel_path,
        "status": "error",
        "chunks": 0,
        "error": None,
    }

    try:
        # 1. 检查文件是否存在且可读
        if not md_path.exists():
            result["status"] = "error"
            result["error"] = f"MD 文件不存在: {md_path}"
            return result

        if not is_file_readable(md_path):
            result["status"] = "garbled"
            result["error"] = "MD 文件内容疑似乱码"
            return result

        # 2. 分块
        chunks = chunk_markdown_file(md_path)
        if not chunks:
            result["status"] = "empty"
            result["error"] = "分块后内容为空"
            return result

        # 3. 打开 LanceDB
        table = _open_or_create_db()

        # 4. 删除该文件的旧向量（增量更新）
        _delete_old_chunks(table, source_rel_path)

        # 5. 生成 embedding 向量
        model = _get_embed_model()
        texts = [c["text"] for c in chunks]

        # fastembed 返回的是生成器，分批消费
        all_vectors = []
        for batch_vecs in model.embed(texts, batch_size=EMBED_BATCH_SIZE):
            all_vectors.append(batch_vecs)

        # 扁平化向量列表
        vectors = []
        for batch in all_vectors:
            vectors.extend(batch)

        # 6. 组装 LanceDB 记录
        import numpy as np
        import pyarrow as pa

        # 将所有向量转为 numpy float32 数组 (n_chunks, vector_dim)
        vec_array = np.array([np.asarray(v, dtype=np.float32) for v in vectors],
                            dtype=np.float32)

        # 构建 pyarrow FixedSizeListArray（LanceDB 要求）
        flat_vecs = pa.array(vec_array.ravel().tolist(), type=pa.float32())
        vectors_pa = pa.FixedSizeListArray.from_arrays(flat_vecs, VECTOR_DIM)

        # 构建各个字段的 pyarrow 数组
        texts_pa = pa.array([c["text"] for c in chunks], type=pa.string())
        sources_pa = pa.array([c["source"] for c in chunks], type=pa.string())
        hashes_pa = pa.array([file_sha256] * len(chunks), type=pa.string())
        indices_pa = pa.array([c["chunk_index"] for c in chunks], type=pa.int32())
        totals_pa = pa.array([c["chunk_total"] for c in chunks], type=pa.int32())

        # 组装成 pyarrow Table 并写入
        schema = pa.schema([
            pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
            pa.field("text", pa.string()),
            pa.field("source", pa.string()),
            pa.field("file_hash", pa.string()),
            pa.field("chunk_index", pa.int32()),
            pa.field("chunk_total", pa.int32()),
        ])
        pa_table = pa.table({
            "vector": vectors_pa,
            "text": texts_pa,
            "source": sources_pa,
            "file_hash": hashes_pa,
            "chunk_index": indices_pa,
            "chunk_total": totals_pa,
        }, schema=schema)
        table.add(pa_table)

        result["status"] = "ok"
        result["chunks"] = len(chunks)

    except ImportError as e:
        missing_pkg = str(e).split(" ")[-1].replace("'", "")
        result["status"] = "error"
        result["error"] = f"缺少依赖库 {missing_pkg}"
        result["error"] += "\n请执行: uv pip install fastembed lancedb langchain-text-splitters pyarrow"
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

    return result


def ingest_batch(md_file_map: List[Tuple[Path, str, str]],
                 max_workers: int = CONVERT_WORKERS,
                 flush_interval: int = DB_FLUSH_INTERVAL,
                 progress_callback=None) -> List[dict]:
    """
    批量入库多个 .md 文件。

    Parameters
    ----------
    md_file_map : List[(md_abs_path, source_rel_path, file_sha256)]
        待入库的文件信息。
    max_workers : int
        并行线程数。
    flush_interval : int
        每处理 N 个文件后强制 flush 并释放内存。
    progress_callback : callable, optional
        每完成一个文件的回调。

    Returns
    -------
    List[dict]
    """
    results = []
    count_since_flush = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for md_path, src_rel, sha in md_file_map:
            future = pool.submit(ingest_single_md, md_path, src_rel, sha)
            futures[future] = (md_path, src_rel)

        try:
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                count_since_flush += 1

                if progress_callback:
                    progress_callback(result)

                # 定期 flush LanceDB 写缓冲区并提示 GC
                if count_since_flush >= flush_interval:
                    import gc
                    gc.collect()
                    count_since_flush = 0

        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            pool.shutdown(wait=False)
            raise

    # 排序保证结果稳定
    results.sort(key=lambda r: r["rel_path"])
    return results


# ============================================================
# 数据库管理
# ============================================================

def get_db_stats() -> dict:
    """
    获取知识库统计信息。
    """
    try:
        table = _open_or_create_db()
        count = table.count_rows()
        return {
            "path": str(DB_PATH),
            "table": TABLE_NAME,
            "total_chunks": count,
            "vector_dim": VECTOR_DIM,
            "model": EMBEDDING_MODEL,
        }
    except Exception as e:
        return {"error": f"无法读取知识库: {e}"}


def rebuild_table():
    """
    重建 LanceDB 表（清空所有数据）。
    用于 --full 全量重建场景。
    """
    close_db()
    import shutil
    db_path = Path(DB_PATH)
    if db_path.exists():
        shutil.rmtree(db_path)
    # 重新创建
    _open_or_create_db()
    close_db()
