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

## 配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DOC2KB_QA_SERVER_HOST` | `127.0.0.1` | 检索 server 地址 |
| `DOC2KB_QA_SERVER_PORT` | `8788` | 检索 server 端口 |
| `DOC2KB_QA_SERVER_IDLE_TIMEOUT` | `3600` | 空闲超时秒数 |

## 🔴 核心铁律

**所有对知识库的访问，必须只经过 `server.py`，禁止直接搜 MD 文件。**

| ✅ 正确 | ❌ 错误 |
|---------|--------|
| `server.py --question "..."` | `search_files` / `grep` / `read_file` |

## 启动 server

```bash
cd /opt/data/doc2kb
python3 query/server.py --server
```

## 查询

```bash
cd /opt/data/doc2kb

# 单题查询
python3 query/server.py --question "你的问题" --top-k 5 --format json

# 上下文格式（对话友好）
python3 query/server.py --question "你的问题" --format context
```

## 返回格式

```json
{
  "found": true,
  "results": [
    {"text": "匹配原文...", "file_name": "文档.md", "similarity": 0.89}
  ],
  "confidence": "high"
}
```

## 回答规范

- ✅ 引用原文，标注 `📄 [文件名]`
- `similarity ≥ 0.85` → 高置信度，可直接引用
- `0.70 ≤ similarity < 0.85` → 中置信度，建议交叉验证
- `similarity < 0.70` → 低置信度，需声明
- ❌ 知识库无覆盖 → 如实说"知识库未覆盖"，禁止脑补

## Pitfalls

1. **禁止直接搜 MD 文件** — 任何场景下不准用 `search_files` / `grep` / `read_file`
2. **端口 8788** — 独立于 VMAX 的 8787，可同时运行
3. **冷启动 ~5s** — 首次查询加载模型，后续毫秒级
4. **必须先 build** — 无知识库时所有查询报错
