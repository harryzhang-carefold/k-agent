"""Embedding (PRD module 8, DECISION-003).

Default: local deterministic hash vector — char 3-gram hashed to fixed dim, L2-normalized.
Same text -> identical vector (deterministic, assertable).
OpenAI-compatible slot (EMBEDDING_BASE_URL / EMBEDDING_MODEL): used when configured and
reachable, else auto-fallback to local.
"""
import hashlib
import numpy as np
import httpx
from core.config import S

DIM = S.EMBEDDING_DIM
N = 3  # n-gram size


def _ngrams(text: str) -> list[str]:
    text = (text or "").strip().lower()
    if not text:
        return []
    if len(text) < N:
        return [text]
    grams = [text[i:i + N] for i in range(len(text) - N + 1)]
    return grams


def hash_embed(text: str) -> list[float]:
    v = np.zeros(DIM, dtype=np.float64)
    for g in _ngrams(text):
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest()[:12], 16)
        v[h % DIM] += 1.0
    norm = np.linalg.norm(v)
    if norm > 0:
        v = v / norm
    return v.tolist()


async def remote_embed(texts: list[str]) -> list[list[float]] | None:
    """OpenAI-compatible /v1/embeddings. Returns None when unconfigured/unreachable."""
    if not (S.EMBEDDING_BASE_URL and S.EMBEDDING_MODEL):
        return None
    url = S.EMBEDDING_BASE_URL.rstrip("/") + "/embeddings"
    headers = {"Content-Type": "application/json"}
    if S.EMBEDDING_API_KEY:
        headers["Authorization"] = f"Bearer {S.EMBEDDING_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json={"model": S.EMBEDDING_MODEL, "input": texts},
                                  headers=headers)
            if r.status_code >= 400:
                return None
            data = r.json()
            vecs = [d["embedding"] for d in sorted(data["data"], key=lambda x: x["index"])]
            # normalize to fixed local dim for consistent cosine math
            out = []
            for vec in vecs:
                v = np.array(vec, dtype=np.float64)
                if len(v) != DIM:
                    v = np.resize(v, DIM)
                n = np.linalg.norm(v)
                out.append((v / n if n > 0 else v).tolist())
            return out
    except Exception:
        return None


async def embed(text: str) -> list[float]:
    """Single text -> normalized vector (local fallback if remote unavailable)."""
    remote = await remote_embed([text])
    if remote:
        return remote[0]
    return hash_embed(text)


async def embed_batch(texts: list[str]) -> list[list[float]]:
    remote = await remote_embed(texts)
    if remote:
        return remote
    return [hash_embed(t) for t in texts]


def cosine(a: list[float], b: list[float]) -> float:
    a, b = np.array(a, dtype=np.float64), np.array(b, dtype=np.float64)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def backend_info() -> dict:
    return {
        "local": {"enabled": True, "dim": DIM, "ngram": N, "deterministic": True},
        "openai_compatible": {
            "configured": bool(S.EMBEDDING_BASE_URL and S.EMBEDDING_MODEL),
            "base_url": S.EMBEDDING_BASE_URL or None,
            "model": S.EMBEDDING_MODEL or None,
        },
    }
