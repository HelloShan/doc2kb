#!/usr/bin/env python3
"""
doc2kb 通用 LanceDB 检索服务器
================================

常驻 HTTP 服务，提供 LanceDB 向量检索能力。
支持 CLI 单题/批量查询 + HTTP API。
无 server 时自动拉起，默认空闲 1 小时后退出。

用法:
  # 启动常驻服务器
  python retrieval/server.py --server

  # CLI 单题查询（自动拉起 server）
  python retrieval/server.py --question "你的问题"

  # CLI 批量查询
  python retrieval/server.py --batch '[{"id":"1","question":"问题A"}]'

  # HTTP API
  curl http://127.0.0.1:8788/health
  curl -X POST http://127.0.0.1:8788/ -d '{"action":"search","question":"..."}'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# ── 路径推导：server.py 的父目录的父目录 = doc2kb 项目根 ──
_SERVER_DIR = Path(__file__).parent.absolute()
_KB_DIR = _SERVER_DIR.parent
sys.path.insert(0, str(_KB_DIR))

from config import DB_PATH, TABLE_NAME, EMBEDDING_MODEL, VECTOR_DIM, TOP_K, SIMILARITY_THRESHOLD  # noqa: E402

# ── 服务端配置（可通过环境变量覆盖）──
HOST = os.environ.get("DOC2KB_QA_SERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("DOC2KB_QA_SERVER_PORT", "8788"))
IDLE_TIMEOUT = int(os.environ.get("DOC2KB_QA_SERVER_IDLE_TIMEOUT", "3600"))
PID_FILE = os.environ.get("DOC2KB_QA_SERVER_PID_FILE", str(_KB_DIR / ".retrieval_server.pid"))

_server_start = time.time()
_last_activity = time.time()


def _get_db_and_table():
    """惰性加载 LanceDB 表和嵌入模型（单例缓存）。"""
    import lancedb

    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"知识库路径不存在: {DB_PATH}。请先运行: python doc_pipeline.py build"
        )

    db = lancedb.connect(str(DB_PATH))
    table = db.open_table(TABLE_NAME)
    return db, table


@lru_cache(maxsize=1)
def _get_embedding_model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=EMBEDDING_MODEL, max_length=512)


def _touch_activity():
    global _last_activity
    _last_activity = time.time()


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split())


def search(question: str, top_k: int = TOP_K, threshold: float = SIMILARITY_THRESHOLD) -> dict:
    """
    对知识库执行向量检索。

    Returns
    -------
    {
        "found": bool,
        "query": str,
        "results": [{"text": ..., "source": ..., "file_name": ..., "similarity": ...}],
        "hits": int,
        "best_similarity": float,
        "confidence": "high" | "medium" | "low",
        "error": str | None,
    }
    """
    _touch_activity()

    try:
        _, table = _get_db_and_table()
        model = _get_embedding_model()
    except Exception as e:
        return {"found": False, "query": question, "results": [], "hits": 0, "error": str(e)}

    try:
        query_vec = list(model.embed([question], batch_size=1))[0]
    except Exception as e:
        return {"found": False, "query": question, "results": [], "hits": 0, "error": f"向量化失败: {e}"}

    try:
        raw = table.search(query_vec.tolist()).metric("cosine").limit(max(top_k * 2, top_k)).to_list()
    except Exception as e:
        return {"found": False, "query": question, "results": [], "hits": 0, "error": f"检索失败: {e}"}

    filtered = []
    for r in raw:
        sim = 1 - r.get("_distance", 0) / 2
        if sim >= threshold:
            filtered.append({
                "text": r.get("text", "").strip(),
                "source": r.get("source", ""),
                "file_name": os.path.basename(r.get("source", "未知.md")),
                "similarity": round(sim, 4),
            })

    filtered.sort(key=lambda x: x["similarity"], reverse=True)
    best = filtered[0]["similarity"] if filtered else 0.0
    confidence = "high" if best >= 0.85 else "medium" if best >= 0.70 else "low"

    return {
        "found": len(filtered) > 0,
        "query": question,
        "results": filtered[:top_k],
        "hits": len(filtered),
        "best_similarity": round(best, 4),
        "confidence": confidence,
    }


def search_batch(items: list[dict[str, Any]], top_k: int = TOP_K, threshold: float = SIMILARITY_THRESHOLD) -> list[dict]:
    """批量检索。"""
    out = []
    for item in items:
        qid = str(item.get("id", ""))
        question = str(item.get("question", "")).strip()
        if not question:
            out.append({"id": qid, "error": "empty question", "found": False, "results": [], "hits": 0})
            continue
        r = search(question, top_k=top_k, threshold=threshold)
        r["id"] = qid
        out.append(r)
    return out


def format_as_context(results: list[dict]) -> str:
    """将检索结果格式化为对话友好文本。"""
    if not results:
        return "[知识库检索无结果]"
    parts = []
    for i, r in enumerate(results, 1):
        text = r["text"]
        if len(text) > 2000:
            text = text[:2000] + "...(截断)"
        parts.append(f"【知识片段 {i}】📄 {r['file_name']} (相关度: {r['similarity']:.2%})\n{text}")
    return "\n\n---\n\n".join(parts)


# ════════════════════════════════════════════════════════════
# HTTP Server
# ════════════════════════════════════════════════════════════

def _server_should_stop() -> bool:
    return (time.time() - _last_activity) >= IDLE_TIMEOUT


def _idle_watcher(server: ThreadingHTTPServer):
    while True:
        time.sleep(5)
        if _server_should_stop():
            try:
                server.shutdown()
            except Exception:
                pass
            return


class Handler(BaseHTTPRequestHandler):
    server_version = "doc2kb-search/1.0"

    def _send_json(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _touch_activity()
        if self.path == "/health":
            self._send_json(200, {
                "ok": True,
                "uptime": round(time.time() - _server_start, 3),
                "idle_timeout": IDLE_TIMEOUT,
                "db_path": str(DB_PATH),
            })
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        _touch_activity()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"invalid json: {e}"})
            return

        action = payload.get("action", "search")
        top_k = int(payload.get("top_k", TOP_K))
        threshold = float(payload.get("threshold", SIMILARITY_THRESHOLD))

        if action == "search":
            result = search(str(payload.get("question", "")), top_k=top_k, threshold=threshold)
            self._send_json(200, {"ok": True, "result": result})
        elif action == "batch":
            result = search_batch(payload.get("items", []), top_k=top_k, threshold=threshold)
            self._send_json(200, {"ok": True, "result": result})
        elif action == "context":
            r = search(str(payload.get("question", "")), top_k=top_k, threshold=threshold)
            self._send_json(200, {"ok": True, "result": format_as_context(r.get("results", []))})
        else:
            self._send_json(400, {"ok": False, "error": f"unknown action: {action}"})

    def log_message(self, format, *args):
        return  # 静默 HTTP 日志


def _write_pid():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _remove_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def run_server():
    _write_pid()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        watcher = threading.Thread(target=_idle_watcher, args=(server,), daemon=True)
        watcher.start()
        print(json.dumps({
            "ok": True, "host": HOST, "port": PORT,
            "idle_timeout": IDLE_TIMEOUT, "pid": os.getpid(),
            "db_path": str(DB_PATH),
        }, ensure_ascii=False))
        server.serve_forever(poll_interval=1)
    finally:
        _remove_pid()


def ensure_server_running() -> bool:
    import socket
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except Exception:
        return False


# ════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════

def main():
    global HOST, PORT

    parser = argparse.ArgumentParser(description="doc2kb LanceDB 向量检索")
    parser.add_argument("--question", "-q", type=str, help="问题文本")
    parser.add_argument("--batch", type=str, help="批量问题 JSON 数组")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--threshold", type=float, default=SIMILARITY_THRESHOLD)
    parser.add_argument("--format", choices=["json", "context"], default="json")
    parser.add_argument("--server", action="store_true", help="启动常驻检索服务")
    parser.add_argument("--host", type=str, default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    HOST = args.host
    PORT = args.port

    if args.server:
        run_server()
        return

    # ── 非 server 模式 → 确保 server 在运行，然后通过 HTTP 调用 ──
    if not ensure_server_running():
        import subprocess
        cmd = [sys.executable, os.path.abspath(__file__), "--server", "--host", HOST, "--port", str(PORT)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            time.sleep(0.5)
            if ensure_server_running():
                break

    import urllib.request

    if args.batch:
        payload = {"action": "batch", "items": json.loads(args.batch), "top_k": args.top_k, "threshold": args.threshold}
    else:
        if not args.question:
            raise SystemExit("--question 和 --batch 至少需要一个")
        payload = {
            "action": "context" if args.format == "context" else "search",
            "question": args.question,
            "top_k": args.top_k,
            "threshold": args.threshold,
        }

    req = urllib.request.Request(
        f"http://{HOST}:{PORT}/",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise SystemExit(data)

    result = data["result"]
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
