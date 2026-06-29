---
name: doc2kb-query
description: |
  doc2kb 通用 LanceDB 知识库查询 Skill。
  与 doc2kb 项目一起分发：构建知识库后，复制或链接此目录到 Hermes skills 下即可使用。
  所有查询走 LanceDB 向量检索 server（server.py），禁止 search_files/grep/read_file 直接搜 MD。
skills: []
last_updated: 2026-06-29
---

# doc2kb 知识库查询 Skill

> 此 skill 随 [doc2kb](https://github.com/HelloShan/doc2kb) 项目一起分发。
> 使用前请确保已运行 `python doc_pipeline.py build` 构建好知识库。

## 依赖安装

```bash
pip install fastembed lancedb pyarrow
```

## 配置

### LanceDB 知识库路径

skill 自动读取 doc2kb 项目 `config.py` 中的路径配置，无需额外设置。
如需覆盖，可通过环境变量指定：

| 环境变量 | 默认值（来自 config.py） | 说明 |
|----------|--------------------------|------|
| `DOC2KB_DB_PATH` | `../doc2kb.lancedb` | LanceDB 向量库路径 |
| `DOC2KB_QA_SERVER_HOST` | `127.0.0.1` | 检索 server 地址 |
| `DOC2KB_QA_SERVER_PORT` | `8788` | 检索 server 端口 |
| `DOC2KB_QA_SERVER_IDLE_TIMEOUT` | `3600` | 空闲超时秒数（1h 无请求自动退出） |

### 嵌入模型

默认使用 `BAAI/bge-small-zh-v1.5`（512 维，CPU 运行），首次运行自动下载。

## 🔴 核心铁律

**所有对知识库的访问，必须只经过 `server.py`，禁止直接搜 MD 文件。**

| ✅ 正确 | ❌ 错误 |
|---------|--------|
| `server.py --question "..."` | `search_files` / `grep` / `read_file` |

## 启动 server

```bash
cd /path/to/doc2kb
python3 skill_query/server.py --server
```

## 查询

```bash
cd /path/to/doc2kb

# 单题查询
python3 skill_query/server.py --question "你的问题" --top-k 5 --format json

# 上下文格式（对话友好）
python3 skill_query/server.py --question "你的问题" --format context

# 批量查询
python3 skill_query/server.py --batch '[{"id":"1","question":"问题A"}]'

# HTTP API
curl http://127.0.0.1:8788/health
curl -X POST http://127.0.0.1:8788/ \
  -d '{"action":"search","question":"你的问题","top_k":5}'
```

## 返回格式

### 混合检索策略

doc2kb 使用 **BM25 全文检索 + 向量语义检索** 的 hybrid 模式，经 RRF (Reciprocal Rank Fusion) 融合排序：

| 成分 | 作用 |
|------|------|
| BM25 FTS | 精确关键词匹配（专有名词、缩写、型号等） |
| 向量检索 | 语义相似度匹配（近义词、同义表述） |
| RRF 融合 | 两种分数按 rank 融合，取 top-K |

> LanceDB 的 `_distance` 在 hybrid 模式下是 RRF 融合分（非 cosine 距离），
> server.py 自动归一化为 0~1 相似度：`sim = 1 / (1 + _distance)`。

### 置信度分级

| 相似度 | 置信度 | 建议 |
|--------|--------|------|
| ≥ 0.85 | high | 可直接引用 |
| 0.70 ~ 0.85 | medium | 建议交叉验证 |
| < 0.70 | low | 需声明置信度低 |

### JSON 格式

```json
{
  "found": true,
  "results": [
    {"text": "匹配原文...", "file_name": "文档.md", "similarity": 0.89}
  ],
  "hits": 5,
  "best_similarity": 0.8901,
  "confidence": "high"
}
```

## 回答规范

- ✅ 引用原文，标注 `📄 [文件名]`
- `similarity ≥ 0.85` → 高置信度，可直接引用
- `0.70 ≤ similarity < 0.85` → 中置信度，建议交叉验证
- `similarity < 0.70` → 低置信度，需声明
- ❌ 知识库无覆盖 → 如实说"知识库未覆盖"，禁止脑补
- ❌ 不准降级到 `search_files` / `grep` 直接搜 MD 文件

## Pitfalls

1. **禁止直接搜 MD 文件** — 任何场景下不准用 `search_files` / `grep` / `read_file`
2. **冷启动 ~5s** — 首次查询加载模型，后续查询毫秒级
3. **必须先 build** — 无知识库时所有查询报错
4. **端口 8788** — 如端口冲突，设 `DOC2KB_QA_SERVER_PORT` 换一个
5. **全量重建后 FTS 索引才生效** — 混合检索依赖 LanceDB FTS 索引，存量表需 `python doc_pipeline.py build --full` 重建后才会有 FTS
