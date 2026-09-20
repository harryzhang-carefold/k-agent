"""RAG 知识库 (PRD module 9, AC-31/32/33): 滑动窗口分块(重叠) + 嵌入 + 余弦检索 + 上下文拼装.

- 分块：按 token 估算（CJK≈1/字，其他≈1/词）切 2000-3000 token 块，重叠 15%（可配）。
  文本块按**字符边界**的 token 估算切分，保证相邻块重叠文本可断言（AC-31）。
- 检索：query 嵌入（模块8）→ 余弦 → top-k（AC-32）。
- 上下文拼装：top-k 块拼入 prompt 固定左侧（prefix caching，模块14 规则5）。
"""
import json
import math
from core import db
from llm import embedding

# 分块参数（PRD: 2000-3000 token，重叠 10-20%）
CHUNK_TOKENS = 2000
CHUNK_OVERLAP = 0.20           # 重叠 20%（PRD 上限；保证相邻块重叠文本可断言，AC-31）
# 小文本（如种子文档）的演示分块参数：按字符估算 token，保证可断言的分块+重叠
CHUNK_CHARS = 800          # 演示用字符窗口（≈800 token 的中文文本）
SMALL_CHUNK_CHARS = 150    # 种子医疗文档的短块（保证多块 + 重叠>=20 字符可断言）


def est_tokens(text: str) -> int:
    """粗略 token 估算：CJK 每字≈1 token，其他按词≈4 字符/token。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(0, math.ceil(other / 4))


def est_chars_for_tokens(tokens: int) -> int:
    """估算达到 N token 所需的中文字符数（保守按 1 字/token）。"""
    return max(40, tokens)


def chunk_text(text: str, chunk_chars: int = SMALL_CHUNK_CHARS, overlap: float = CHUNK_OVERLAP) -> list[str]:
    """滑动窗口分块：返回块列表，相邻块重叠约 overlap 比例（AC-31 可断言）。"""
    text = (text or "").strip()
    if not text:
        return []
    step = max(10, int(chunk_chars * (1 - overlap)))
    chunks = []
    i = 0
    n = len(text)
    while i < n:
        chunks.append(text[i:i + chunk_chars])
        if i + chunk_chars >= n:
            break
        i += step
    # 去重完全相同的尾块
    seen, out = set(), []
    for c in chunks:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def add_document(conn, knowledge_id: int, text: str, chunk_chars: int | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        from core.errors import APIError
        raise APIError(400, "空文档")
    cc = chunk_chars or (SMALL_CHUNK_CHARS if est_tokens(text) < 1500 else est_chars_for_tokens(CHUNK_TOKENS))
    chunks = chunk_text(text, cc)
    vecs = await embedding.embed_batch(chunks)
    for seq, (c, v) in enumerate(zip(chunks, vecs)):
        await db.execute(
            conn,
            "INSERT INTO rag_chunks (knowledge_id, seq, text, embedding, token_est) VALUES (?,?,?,?,?)",
            (knowledge_id, seq, c, json.dumps(v), est_tokens(c)))
    return {"chunks": len(chunks), "chunk_chars": cc, "overlap": CHUNK_OVERLAP,
            "overlap_chars": int(cc * CHUNK_OVERLAP)}


async def search(conn, knowledge_id: int, query: str, top_k: int = 3) -> list[dict]:
    """检索 top-k chunk，**携带 chunk 元信息**（TASK-057 / 迭代5，DECISION-027 决策4）。

    每项含:
      - knowledge_id: int  所属知识库 id
      - chunk_seq:    int  chunk 序号（对应 rag_chunks.seq）
      - text:         str  分块文本（assemble 拼纯文本用，不变）
      - score:        float 相似度（余弦）
      - preview:      str  文本预览（前 120 字符，埋点 rag_chunks 用）
      - token_est:    int  token 估算
    旧的 'seq' 字段保留（向后兼容 build_context / 既有调用方），新增 knowledge_id /
    preview（chunk 粒度埋点所需）。assemble 仍拼纯文本给 LLM（不变）。
    """
    rows = await db.fetchall(conn, "SELECT * FROM rag_chunks WHERE knowledge_id=? ORDER BY seq",
                             (knowledge_id,))
    qv = await embedding.embed(query)
    scored = []
    for r in rows:
        try:
            sim = embedding.cosine(json.loads(r["embedding"]), qv)
        except Exception:
            continue
        scored.append({"seq": r["seq"], "knowledge_id": knowledge_id,
                       "chunk_seq": r["seq"], "text": r["text"], "score": round(sim, 4),
                       "preview": (r["text"] or "")[:120],
                       "token_est": r["token_est"]})
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]


def build_context(results: list[dict], max_chars: int = 1200) -> str:
    """top-k 分块拼为 RAG 上下文（固定放 prompt 左侧）。"""
    if not results:
        return ""
    parts, total = [], 0
    for r in results:
        t = r["text"].strip()
        room = max_chars - total
        if room <= 0:
            break
        if len(t) > room:
            t = t[:room]
        parts.append(f"[{r['seq']}] {t}")
        total += len(t) + 2
    return "知识库检索上下文（top-k，按相似度降序）：\n" + "\n".join(parts)
