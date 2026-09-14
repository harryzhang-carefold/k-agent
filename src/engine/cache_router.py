"""agent 缓存路由能力 (PRD module 14, AC-51/52) — 5 策略真实实现.

1. 图片: MD5 命中 L1 image 缓存 → 直返历史解析（cache_hit=md5, LLM=0）
2. 文本: 嵌入余弦 > 阈值(0.95) 命中 L1 → 直返缓存答案（cache_hit=semantic, LLM=0）
3. 混合(图+文): 先拆分为图片请求+文本请求，分别走 1/2（split:mixed 可观测）
4. 复杂任务: 先拆分为简单请求，各走 1/2（split:complex 可观测）
5. prefix caching: 由 engine 保证（固定内容最左、请求最右）

route_request 返回 RouteDecision：命中（cache_hit 类型+答案）或 需走 LLM（含拆分事件）。
"""
import base64
import hashlib
import json
import re
from core import stats
from core.config import S
from memory import backend as memory

MAX_SIMPLE_CHARS = 150          # 超过视为复杂任务
MIN_SENTENCES_COMPLEX = 2       # 多问句视为复杂


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def md5_base64(b64: str) -> str:
    try:
        return hashlib.md5(base64.b64decode(b64)).hexdigest()
    except Exception:
        return ""


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    return [p.strip() for p in parts if p.strip()]


async def _check_image(mb, img: dict) -> dict:
    m = md5_base64(img["b64"]) if "b64" in img else (img.get("md5") or "")
    rec = None
    if m:
        rec = await mb.l1_image_lookup(m)
    if rec:
        stats.bump("cache_md5_hits")
        return {"kind": "image", "md5": m, "hit": True, "cache_hit": "md5",
                "answer": json.dumps(rec["parsed"], ensure_ascii=False)}
    return {"kind": "image", "md5": m, "hit": False, "cache_hit": None,
            "answer": None, "need_llm": True}


async def _check_text(mb, agent_key: str, t: str) -> dict:
    if not t:
        return {"kind": "text", "text": t, "hit": False, "cache_hit": None,
                "similarity": None, "answer": None, "need_llm": True}
    th = await mb.l1_search(agent_key, t, S.SEMANTIC_CACHE_THRESHOLD)
    if th:
        stats.bump("cache_semantic_hits")
        return {"kind": "text", "text": t, "hit": True, "cache_hit": "semantic",
                "similarity": th["similarity"], "answer": th["answer"]}
    return {"kind": "text", "text": t, "hit": False, "cache_hit": None,
            "similarity": None, "answer": None, "need_llm": True}


async def route_request(agent_key: str, text: str | None,
                        images: list[dict] | None) -> dict:
    """
    images: [{"b64":..., "content_type":...}] 或 [{"md5":...}]
    返回 {hit, cache_hit, answer, sub_requests, route_events, need_llm, full_text}
    """
    mb = memory.get_memory_backend()
    events: list[str] = []
    subs: list[dict] = []
    text = (text or "").strip()
    images = images or []

    # ---- 规则 3: 混合（图片+文本）→ 拆分 ----
    if images and text:
        stats.bump("split_mixed")
        events.append("split:mixed")
        for img in images:
            subs.append(await _check_image(mb, img))
        subs.append(await _check_text(mb, agent_key, text))
        return _decide(subs, events, full_text=text)

    # ---- 规则 1: 纯图片 ----
    if images:
        for img in images:
            subs.append(await _check_image(mb, img))
        return _decide(subs, events)

    # ---- 规则 4: 复杂任务 → 拆分为简单请求 ----
    sents = _split_sentences(text)
    complex_task = (len(sents) >= MIN_SENTENCES_COMPLEX) or (len(text) > MAX_SIMPLE_CHARS)
    if complex_task:
        stats.bump("split_complex")
        events.append(f"split:complex({len(sents)}段)")
        for s in sents:
            subs.append(await _check_text(mb, agent_key, s))
        return _decide(subs, events, full_text=text)

    # ---- 规则 2: 简单文本语义缓存 ----
    subs.append(await _check_text(mb, agent_key, text))
    return _decide(subs, events, full_text=text)


def _decide(subs: list[dict], events: list[str], full_text: str | None = None) -> dict:
    # 图片 MD5 命中具有最高优先级（AC-51 / BUG-001）：只要任一图片子请求命中
    # memory_l1_image，即直返该图历史解析并跳过文本子请求与 LLM（llm_calls=0）。
    # 否则在"混合"请求中，第二次同图但文本不同（语义相似度 <0.95）时，文本子请求
    # 会 miss，导致 all(hit) 不成立、无法命中 MD5 缓存 —— 这正是 BUG-001 的深层根因。
    img_hits = [s for s in subs if s.get("kind") == "image" and s["hit"]]
    if img_hits:
        return {"hit": True, "cache_hit": "md5",
                "answer": "\n".join(s["answer"] for s in img_hits),
                "sub_requests": subs, "route_events": events, "need_llm": False}
    if subs and all(s["hit"] for s in subs):
        kinds = {s["cache_hit"] for s in subs}
        cache_hit = "md5" if kinds == {"md5"} else ("semantic" if kinds == {"semantic"} else "mixed")
        return {"hit": True, "cache_hit": cache_hit,
                "answer": "\n".join(s["answer"] for s in subs),
                "sub_requests": subs, "route_events": events, "need_llm": False}
    hit_parts = [s for s in subs if s["hit"]]
    miss_parts = [s for s in subs if not s["hit"]]
    if hit_parts and not miss_parts:
        return {"hit": True, "cache_hit": hit_parts[0]["cache_hit"],
                "answer": "\n".join(s["answer"] for s in hit_parts),
                "sub_requests": subs, "route_events": events, "need_llm": False}
    return {"hit": False, "cache_hit": None, "answer": None,
            "sub_requests": subs, "route_events": events, "need_llm": True,
            "full_text": full_text}


async def remember_answer(agent_key: str, text: str, answer: str,
                          sub_requests: list[dict] | None = None):
    """LLM 真实作答后写入 L1 语义缓存（下次相似请求 >0.95 直返）。

    复杂任务被拆分为多句时，路由按"句子"查缓存，故必须把答案也写进每个
    子句（text）下，否则第二次相同问题拆出的句子全部 miss。
    """
    mb = memory.get_memory_backend()
    texts = {text} if text else set()
    for s in (sub_requests or []):
        t = (s.get("text") or "").strip()
        if t:
            texts.add(t)
    for t in texts:
        await mb.l1_write(agent_key, t, answer)


async def remember_image(b64: str, content_type: str, parsed: dict):
    await memory.get_memory_backend().l1_image_store(md5_base64(b64), content_type, parsed)


async def remember_images(images: list[dict] | None, answer: str,
                          sub_requests: list[dict] | None = None):
    """LLM 真实作答后，把本次请求中**未命中缓存**的图片按 md5 写入 L1 图片缓存（AC-51）。

    - 已命中的图片（sub.hit=True）不重写（保留既有解析）。
    - 仅写"本次走 LLM 路径"的请求；缓存命中 / LLM 降级（degraded）路径不调用本函数，
      避免把"无真实解析"的空/降级文案写入缓存。
    - parsed 存 LLM 解析文本（dict 包裹），保证命中分支 json.dumps(rec["parsed"]) 可直返。
    """
    if not images or not (answer or "").strip():
        return
    mb = memory.get_memory_backend()
    hit_md5s = {s.get("md5") for s in (sub_requests or []) if s.get("hit")}
    for img in images:
        b64 = img.get("b64")
        if not b64:
            continue
        m = md5_base64(b64)
        if not m or m in hit_md5s:
            continue
        parsed = {"answer": answer}
        try:
            obj = json.loads(answer)
            if isinstance(obj, dict):
                parsed = obj
        except Exception:
            pass
        await mb.l1_image_store(m, img.get("content_type"), parsed)
