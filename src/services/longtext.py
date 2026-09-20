"""长文本 4 策略 (PRD module 15, AC-53/54/55/56) — 全部真实实现.

1. Map-Reduce：2000-3000 token 滑窗分块(重叠10-20%) → Map 逐块 LLM 提取检验指标
   → Reduce 聚合 prompt 让 LLM 去重/时间线排序/冲突标记"需人工复核"；
   平台侧确定性兜底：同名指标单位不一致 → 强制追加"需人工复核"（可断言）。
2. 增量图构建：流式逐段 → LLM 仅输出标准三元组 JSON → MERGE 写入 L2 图（幂等）
   → 最终问题只查子图摘要（不回顾原文）。
3. critique-refine：pydantic QAItem 校验（question/answer/source_field、数值非负）
   → 失败带具体错误重喂 LLM ≤5 次 → 达上限转 HITL 队列（不硬失败）。
4. pandas 确定性预处理：散乱检验记录 CSV → 清洗/按 patient_id+date 聚合 → 结构化 JSON。
"""
import asyncio
import csv
import io
import json
import re
from pydantic import BaseModel, ValidationError
from core import db
from core import stats
from core.errors import LLMError, APIError
from llm import provider as llm
from llm import embedding
from memory import backend as memory
from memory.graph import validate_edge_type
from rag import rag

# ---------------- 策略 1: Map-Reduce ----------------

MAP_PROMPT = (
    "你是医疗数据提取器。从下面文本块中提取所有检验指标，输出 JSON 数组，"
    "每项 {\"name\": 指标名, \"value\": 数值或字符串, \"unit\": 单位, \"date\": 日期或null}。"
    "只输出 JSON，不要解释。\n文本块：\n"
)

REDUCE_PROMPT = (
    "你是医疗数据聚合器。下面是多段文本块分别提取的检验指标（JSON 数组的合集）。\n"
    "请：1) 按指标名去重；2) 按日期时间线升序排序（无日期的按出现顺序）；"
    "3) 若同一指标在不同块中单位不一致或数值冲突，在该项加 \"note\": \"需人工复核\"。\n"
    "输出 JSON 数组，每项 {name, value, unit, date, note?}。只输出 JSON。\n"
    "各块提取结果：\n"
)


def _chunk_long_text(text: str, chunk_tokens: int = 2400, overlap: float = 0.15) -> list[str]:
    """按 token 估算滑窗切分（2000-3000 token，重叠 10-20%）。"""
    from rag.rag import est_tokens, est_chars_for_tokens
    cc = est_chars_for_tokens(chunk_tokens)
    return rag.chunk_text(text, chunk_chars=cc, overlap=overlap)


def _deterministic_conflict_check(items: list[dict]) -> list[dict]:
    """平台侧兜底：同名指标单位不一致 → 强制"需人工复核"（保证可断言，不依赖 LLM）。"""
    units: dict[str, set] = {}
    for it in items:
        n = str(it.get("name", "")).strip()
        u = str(it.get("unit", "")).strip()
        units.setdefault(n, set()).add(u)
    multi = {n for n, us in units.items() if len(us) > 1}
    for it in items:
        n = str(it.get("name", "")).strip()
        if n in multi:
            it["note"] = "需人工复核"
    return items


def _parse_json_array(s: str) -> list:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?|```$", "", s).strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        m = re.search(r"\[[\s\S]*\]", s)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, list) else []
            except Exception:
                return []
    return []


async def map_reduce(conn, text: str) -> dict:
    chunks = _chunk_long_text(text)
    if not chunks:
        raise APIError(400, "空文本")
    # Map（逐块 LLM 提取；串行以保证确定性顺序）
    map_results = []
    for i, c in enumerate(chunks):
        try:
            out, _usage = await llm.chat([{"role": "user", "content": MAP_PROMPT + c}],
                                 temperature=0, max_tokens=600)
            map_results.append({"chunk": i, "items": _parse_json_array(out)})
        except LLMError as e:
            map_results.append({"chunk": i, "items": [], "error": str(e)[:120]})
    all_items = [it for r in map_results for it in r["items"]]
    # Reduce（LLM 聚合去重/时间线/冲突）
    reduce_items = []
    if all_items:
        try:
            out, _usage = await llm.chat([{"role": "user",
                                   "content": REDUCE_PROMPT + json.dumps(all_items, ensure_ascii=False)}],
                                 temperature=0, max_tokens=800)
            reduce_items = _parse_json_array(out)
        except LLMError:
            reduce_items = all_items
    # 确定性兜底 + 时间线排序
    reduce_items = _deterministic_conflict_check(reduce_items)
    reduce_items.sort(key=lambda x: (x.get("date") or "9999-99-99", str(x.get("name", ""))))
    return {
        "strategy": "map-reduce",
        "chunks": len(chunks),
        "overlap": 0.15,
        "map_items_total": len(all_items),
        "reduced_items": reduce_items,
        "conflicts_marked": sum(1 for x in reduce_items if x.get("note") == "需人工复核"),
    }


# ---------------- 策略 2: 增量图构建 ----------------

TRIPLET_PROMPT = (
    "你是医疗知识抽取器。从下面文本段抽取标准化三元组，输出 JSON 数组，"
    "每项 {\"s\": 主体, \"p\": 关系(UPPER_SNAKE_CASE，如 DIAGNOSED_WITH/HAS_TEST_RESULT/"
    "UNDERWENT_TEST/ALLERGIC_TO/ATTENDED), \"o\": 客体, \"props\": {可选属性如 test_date/value/unit}}。"
    "关系类型禁止使用 RELATED_TO/LINKED_TO。只输出 JSON。\n文本段：\n"
)


async def incremental_graph(conn, text: str, center_hint: str = "") -> dict:
    mb = memory.get_memory_backend()
    chunks = _chunk_long_text(text, chunk_tokens=2000)
    # 流式逐段：每段独立抽取（无上下文保留），MERGE 即时写入
    triples_total, triples_valid = 0, 0
    for c in chunks:
        try:
            out, _usage = await llm.chat([{"role": "user", "content": TRIPLET_PROMPT + c}],
                                 temperature=0, max_tokens=600)
            trs = _parse_json_array(out)
        except LLMError:
            continue
        triples_total += len(trs)
        for t in trs:
            s, p, o = str(t.get("s", "")).strip(), str(t.get("p", "")).strip(), str(t.get("o", "")).strip()
            if not (s and p and o):
                continue
            try:
                validate_edge_type(p)
            except Exception:
                continue  # 拒绝非 UPPER_SNAKE / 万能边
            sid = "ENT:" + s
            oid = "ENT:" + o
            await mb.l2_node_or_create(sid, "Entity", {"name": s})
            await mb.l2_node_or_create(oid, "Entity", {"name": o})
            try:
                await mb.l2_merge_edge(sid, oid, p, t.get("props") or {})
                triples_valid += 1
            except Exception:
                continue
    # 最终问题：只查子图摘要
    center = center_hint or ""
    sub = ""
    if center:
        await mb.l2_node_or_create("ENT:" + center, "Entity", {"name": center})
        sub = (await mb.l2_query({"subgraph": "ENT:" + center, "hops": 2}))["subgraph"]
    return {
        "strategy": "incremental-graph",
        "chunks": len(chunks),
        "triplets_extracted": triples_total,
        "triplets_merged": triples_valid,
        "graph_nodes": (await mb.l2_query({"counts": 1}))["counts"]["nodes"],
        "subgraph_summary": sub,
    }


# ---------------- 策略 3: critique-refine 循环 ----------------

class QAItem(BaseModel):
    question: str
    answer: str
    source_field: str
    value: float | None = None  # 数值不能为负

    @property
    def ok(self):
        return self.value is None or self.value >= 0


CRITIQUE_PROMPT = (
    "从下面临床摘要生成 3 个 QA 对。输出 JSON 数组，每项必须含 "
    "{\"question\": str, \"answer\": str, \"source_field\": str（来源字段名）, "
    "\"value\": 数值或 null（若涉及检验值，必须 >= 0）}。只输出 JSON。\n"
    "临床摘要：\n"
)


async def critique_refine(conn, text: str, max_rounds: int = 5) -> dict:
    last_errors = "（首次生成）"
    round_log = []
    for r in range(1, max_rounds + 1):
        prompt = CRITIQUE_PROMPT + text + (
            f"\n\n[第{r-1}轮校验错误] {last_errors}\n请根据以上错误信息重新生成，确保满足全部格式与业务约束。"
            if r > 1 else "")
        try:
            out, _usage = await llm.chat([{"role": "user", "content": prompt}],
                                 temperature=0, max_tokens=700)
        except LLMError as e:
            round_log.append({"round": r, "ok": False, "error": f"LLM: {str(e)[:100]}"})
            last_errors = f"LLM 调用失败: {str(e)[:100]}"
            continue
        items_raw = _parse_json_array(out)
        errors = []
        parsed = []
        for i, it in enumerate(items_raw):
            if not isinstance(it, dict):
                errors.append(f"第{i}项不是对象")
                continue
            for f in ("question", "answer", "source_field"):
                if not it.get(f):
                    errors.append(f"第{i}项缺少 {f}")
            if not errors:
                try:
                    v = QAItem(**it)
                    if not v.ok:
                        errors.append(f"第{i}项 value 为负数")
                    else:
                        parsed.append(v)
                except ValidationError as ve:
                    errors.append(f"第{i}项 schema 校验失败: {ve.errors()[0]['msg']}")
        round_log.append({"round": r, "ok": len(parsed) >= 1 and not errors,
                          "errors": errors[:5]})
        if parsed and not errors:
            return {"strategy": "critique-refine", "rounds": r, "passed": True,
                    "items": [p.model_dump() for p in parsed], "round_log": round_log}
        last_errors = "; ".join(errors[:5]) or "输出为空"
    # 达上限 → HITL 队列（不硬失败）
    await db.execute(conn, "INSERT INTO hitl_queue (task, reason, status) VALUES (?,?,?)",
                     ("critique-refine: " + text[:80], last_errors, "pending"))
    return {"strategy": "critique-refine", "rounds": max_rounds, "passed": False,
            "items": [], "round_log": round_log, "hitl": True,
            "reason": last_errors}


# ---------------- 策略 4: pandas 确定性预处理 ----------------

async def preprocess_csv(conn: str | dict, llm_convert: bool = True) -> dict:
    """散乱检验记录 CSV → 清洗 → 按 patient_id+date 聚合为结构化 JSON。
    conn: CSV 文本 或 {columns, rows}。返回 {structured_json, llm_qa?}。"""
    import pandas as pd
    if isinstance(conn, dict):
        df = pd.DataFrame(conn.get("rows", []), columns=conn.get("columns"))
    else:
        df = pd.read_csv(io.StringIO(conn))
    n_raw = len(df)
    # 清洗：去空 patient_id/indicator，数值列转 numeric
    df = df.dropna(subset=[c for c in df.columns if c.lower() in ("patient_id", "indicator", "value")])
    for c in ("value",):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[c for c in df.columns if c.lower() in ("patient_id", "indicator")])
    df["date"] = df.get("date", pd.Series([None] * len(df)))
    # 按 patient_id + date 聚合
    grouped = df.groupby(["patient_id", "date"], dropna=False)
    structured = {}
    for (pid, d), g in grouped:
        key = f"{pid}|{d}"
        tests = {}
        for rec in g.to_dict("records"):
            val = rec.get("value")
            tests[str(rec.get("indicator"))] = {
                "value": None if val is None or pd.isna(val) else float(val),
                "unit": str(rec.get("unit", "")),
            }
        structured[key] = {
            "patient_id": str(pid),
            "date": str(d),
            "tests": tests,
        }
    result = {"strategy": "pandas-preprocess", "raw_rows": n_raw,
              "clean_rows": len(df), "patients": len(structured),
              "structured_json": structured}
    if llm_convert:
        # LLM 仅做语义转换（QA 生成），输入是清洗后的高浓度 JSON
        try:
            qa = await _llm_semantic_convert(structured)
            result["llm_qa"] = qa
        except Exception:
            result["llm_qa"] = None
    return result


async def _llm_semantic_convert(structured: dict) -> list:
    prompt = (
        "下面是按患者+日期聚合的结构化检验数据（JSON）。请生成 3 个高质量医学 QA 对，"
        "只理解医学语义并生成，不要重述全部数据。输出 JSON 数组，每项 {question, answer, source_field}。"
        "只输出 JSON。\n数据：\n" + json.dumps(structured, ensure_ascii=False)[:3000])
    out, _usage = await llm.chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=500)
    return _parse_json_array(out)
