"""链路追踪（TASK-057 / 迭代5）：agent 全痕迹记录。

设计要点（任务书 §1/§3/§5 + DECISION-027）：
- **非阻塞铁律**：所有 trace 写入用 try/except 包裹，失败只 log 不 raise，
  绝不阻塞主聊天链路（测试项⑥：断表后主链路仍成功）。
- **单 span input/output 截断 2000 字符**（防大 JSON 撑爆 DB）。
- **span 增量落库**：每记录一个 span 立即 INSERT（单行，崩溃也保留部分痕迹）；
  会话聚合行（trace_conversations）在 run 结束 finish() 时 UPSERT（幂等，
  多轮会话累加 token/次数、并集 tools/models 等数组）。
- **hermes 后端 token**：无 usage 则 NULL + status 注明 hermes_no_usage（不编造数字）。

对外 API：
- TraceContext(conn, conv_id, agent_id, agent_name, backend)
  - record_llm_call / record_tool_call / record_mcp_call / record_rag_search /
    record_skill_inject / record_cache_hit / record_error / record_file_op
  - finish(status, error) -> None（写聚合行，幂等）
- record_file_op(ctx, path) -> None（公共函数，供未来插件记录中间文件）
- clean_expired(conn, days) -> int（启动时清理过期 trace，幂等，返回删除行数）
- retention_days(conn) -> int（从 settings 表读 trace_retention_days，默认 30）
"""
import json
import logging
import time
from datetime import datetime, timezone

from core import db

logger = logging.getLogger("core.trace")

# 单 span input/output 截断上限（任务书 §5：防大 JSON 撑爆 DB）。
SPAN_FIELD_MAX = 2000


def _now() -> str:
    """UTC 时间戳，与 DB 默认格式一致（YYYY-MM-DD HH:MM:SS），便于比对/排序。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ms_since(t0: float) -> int:
    try:
        return int((time.monotonic() - t0) * 1000)
    except Exception:
        return 0


def _trunc(s, limit: int = SPAN_FIELD_MAX) -> str | None:
    """截断到 limit 字符；None/空 → None（不落空串）。"""
    if s is None:
        return None
    s = s if isinstance(s, str) else str(s)
    if len(s) > limit:
        return s[:limit]
    return s


def _json_list(v) -> str | None:
    """把列表/集合序列化为 JSON 字符串（None → None）。"""
    if v is None:
        return None
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return None


def _load_array(raw) -> list:
    """把 DB 里的 JSON 数组字符串解析成 list（脏值 → []）。"""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


class TraceContext:
    """一次 run() 的追踪上下文。持有 conn + 会话聚合状态，记录 span 并在结束落聚合行。"""

    def __init__(self, conn, conv_id: str, agent_id, agent_name: str | None,
                 backend: str | None):
        self.conn = conn
        self.conv_id = conv_id
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.backend = backend
        self.started = _now()
        self._t0 = time.monotonic()
        # 聚合状态（本次 run 累加；finish 时与既有会话行合并）
        self.tokens_in = 0
        self.tokens_out = 0
        self.llm_calls = 0
        self.models: set = set()
        self.tools: set = set()
        self.skills: set = set()
        self.rag_kb: dict = {}      # {knowledge_id(str): name}
        self.files: set = set()
        self.active = True
        self._seq = 0

    async def init_seq(self):
        """加载本会话当前最大 seq 作为基线（多轮会话 seq 单调递增，不重叠）。
        在 run 开始时调用一次。非阻塞：失败退化为 0（seq 从 1 起，仍可展示）。"""
        try:
            row = await db.fetchone(
                self.conn,
                "SELECT COALESCE(MAX(seq),0) AS m FROM trace_spans WHERE conv_id=?",
                (self.conv_id,))
            if row:
                self._seq = int(row.get("m") or 0)
        except Exception as e:
            logger.warning("trace init_seq 失败（退化为 0，忽略）: %s: %s",
                           type(e).__name__, str(e)[:120])

    # ---------------- 核心 span 落库（非阻塞） ----------------
    async def _record_async(self, span_type, name=None, *, input=None, output=None,
                            tokens_in=None, tokens_out=None, model=None,
                            rag_chunks=None, duration_ms=None, status="ok",
                            error=None):
        try:
            if not self.active:
                return
            await db.execute(
                self.conn,
                """INSERT INTO trace_spans
                   (conv_id, seq, ts, span_type, name, input, output,
                    tokens_in, tokens_out, model, rag_chunks, duration_ms, status, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.conv_id, self._next_seq(), _now(), span_type, name,
                 _trunc(input), _trunc(output),
                 tokens_in, tokens_out, model,
                 _json_list(rag_chunks), duration_ms, status,
                 _trunc(error)),
            )
        except Exception as e:
            logger.warning("trace span 写入失败（忽略，不阻塞主链路）: %s: %s",
                           type(e).__name__, str(e)[:200])

    def _next_seq(self) -> int:
        self._seq = getattr(self, "_seq", 0) + 1
        return self._seq

    # ---------------- 语义化记录（span + 聚合） ----------------
    async def record_llm_call(self, model: str | None, input_summary: str | None,
                              output_summary: str | None,
                              tokens_in: int | None = None,
                              tokens_out: int | None = None,
                              duration_ms: int | None = None,
                              status: str = "ok", error: str | None = None):
        self.llm_calls += 1
        if model:
            self.models.add(model)
        if tokens_in:
            self.tokens_in += int(tokens_in)
        if tokens_out:
            self.tokens_out += int(tokens_out)
        await self._record_async("llm_call", name=model, input=input_summary,
                                 output=output_summary, tokens_in=tokens_in,
                                 tokens_out=tokens_out, model=model,
                                 duration_ms=duration_ms, status=status,
                                 error=error)

    async def record_tool_call(self, name: str | None, input_args, output,
                               duration_ms: int | None = None,
                               status: str = "ok", error: str | None = None):
        if name:
            self.tools.add(name)
        await self._record_async("tool_call", name=name, input=_j(input_args),
                                 output=output, duration_ms=duration_ms,
                                 status=status, error=error)

    async def record_mcp_call(self, server: str | None, tool: str | None, input_args,
                              output, duration_ms: int | None = None,
                              status: str = "ok", error: str | None = None):
        name = f"mcp:{server or 'unknown'}/{tool or 'unknown'}"
        self.tools.add(name)
        await self._record_async("mcp_call", name=name, input=_j(input_args),
                                 output=output, duration_ms=duration_ms,
                                 status=status, error=error)

    async def record_rag_search(self, kb_id, kb_name: str | None, query: str,
                                chunks: list | None = None,
                                duration_ms: int | None = None):
        key = str(kb_id)
        self.rag_kb[key] = kb_name or key
        await self._record_async("rag_search", name=kb_name or key, input=query,
                                 output=None, rag_chunks=chunks,
                                 duration_ms=duration_ms)

    async def record_skill_inject(self, skill_name: str, summary: str | None):
        self.skills.add(skill_name)
        await self._record_async("skill_inject", name=skill_name, output=summary)

    async def record_cache_hit(self, name: str | None):
        await self._record_async("cache_hit", name=name)

    async def record_error(self, name: str, error: str,
                           status: str = "error"):
        await self._record_async("error", name=name, output=error, status=status,
                                 error=error)

    async def record_file_op(self, path: str, status: str = "ok"):
        self.files.add(path)
        await self._record_async("file_op", name=path, status=status)

    # ---------------- 结束：写会话聚合行（幂等，多轮合并） ----------------
    async def finish(self, status: str = "ok", error: str | None = None):
        try:
            if not self.active:
                return
            self.active = False
            ended = _now()
            duration_ms = _ms_since(self._t0)
            existing = await db.fetchone(
                self.conn, "SELECT * FROM trace_conversations WHERE id=?",
                (self.conv_id,))
            # 多轮合并：既有行存在则累加 token/次数、并集数组
            if existing:
                base_in = int(existing.get("total_tokens_in") or 0)
                base_out = int(existing.get("total_tokens_out") or 0)
                base_calls = int(existing.get("total_llm_calls") or 0)
                models = _union(existing.get("models"), list(self.models))
                tools = _union(existing.get("tools_called"), list(self.tools))
                skills = _union(existing.get("skills_used"), list(self.skills))
                files = _union(existing.get("files_created"), list(self.files))
                # rag_kb：既有 list[{id,name}] 与本次 dict 合并
                rag_kb = _merge_rag_kb(existing.get("rag_kb_used"), self.rag_kb)
                started = existing.get("started_at") or self.started
            else:
                base_in = base_out = base_calls = 0
                models = list(self.models)
                tools = list(self.tools)
                skills = list(self.skills)
                files = list(self.files)
                rag_kb = [{"id": k, "name": v} for k, v in self.rag_kb.items()]
                started = self.started
            total_in = base_in + self.tokens_in
            total_out = base_out + self.tokens_out
            total_calls = base_calls + self.llm_calls
            await db.execute(
                self.conn,
                """INSERT INTO trace_conversations
                   (id, agent_id, agent_name, backend, started_at, ended_at,
                    total_tokens_in, total_tokens_out, total_llm_calls,
                    models, tools_called, skills_used, rag_kb_used, files_created,
                    duration_ms, status, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (id) DO UPDATE SET
                    agent_name=excluded.agent_name,
                    backend=excluded.backend,
                    ended_at=excluded.ended_at,
                    total_tokens_in=excluded.total_tokens_in,
                    total_tokens_out=excluded.total_tokens_out,
                    total_llm_calls=excluded.total_llm_calls,
                    models=excluded.models,
                    tools_called=excluded.tools_called,
                    skills_used=excluded.skills_used,
                    rag_kb_used=excluded.rag_kb_used,
                    files_created=excluded.files_created,
                    duration_ms=excluded.duration_ms,
                    status=excluded.status,
                    error=excluded.error""",
                (self.conv_id, self.agent_id, self.agent_name, self.backend,
                 started, ended, total_in, total_out, total_calls,
                 _json_list(models), _json_list(tools), _json_list(skills),
                 _json_list(rag_kb), _json_list(files), duration_ms, status,
                 _trunc(error)),
            )
        except Exception as e:
            logger.warning("trace 聚合行写入失败（忽略，不阻塞主链路）: %s: %s",
                           type(e).__name__, str(e)[:200])


# ---------------- 公共函数 ----------------

async def record_file_op(ctx: TraceContext | None, path: str):
    """公共函数（任务书 §3 中间文件 c 项）：供未来插件记录中间文件 file_op span。
    非阻塞：ctx 为 None 或写入失败都静默。"""
    try:
        if ctx is not None:
            await ctx.record_file_op(path)
    except Exception as e:
        logger.warning("record_file_op 失败（忽略）: %s: %s", type(e).__name__,
                       str(e)[:200])


async def retention_days(conn) -> int:
    """读 trace 保留天数。优先级：settings 表 trace_retention_days > S.TRACE_RETENTION_DAYS。
    脏值 → 回退 S 默认（30）。"""
    from core.config import S
    default = getattr(S, "TRACE_RETENTION_DAYS", 30) or 30
    try:
        row = await db.fetchone(
            conn, "SELECT value FROM settings WHERE key='trace_retention_days'")
        if row and row.get("value") is not None:
            v = int(json.loads(row["value"]))
            if v > 0:
                return v
    except Exception:
        pass
    return default


async def clean_expired(conn, days: int) -> int:
    """清理超过 days 天的 trace（启动时调用，幂等）。返回删除的会话数。

    非阻塞：失败只 log。删除顺序先 spans 后 conversations（逻辑外键）。
    双后端同一 SQL（? 占位符由 core.db 分派翻译）。
    """
    try:
        # cutoff：started_at 为 'YYYY-MM-DD HH:MM:SS'（UTC），字符串比较即可。
        # N 天前的时间戳（started_at < 该值 视为过期）。
        from datetime import timedelta
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S")
        # 删除过期会话（先查 id 集，再删 spans，最后删 conversations）。
        # 保守：started_at IS NULL 的行不清（可能未写完/异常），避免误删。
        old_ids = await db.fetchall(
            conn,
            "SELECT id FROM trace_conversations WHERE started_at IS NOT NULL AND started_at < ?",
            (cutoff_ts,))
        ids = [r["id"] for r in old_ids]
        if not ids:
            return 0
        # 批量删除（分批防超长 IN 列表）
        removed = 0
        batch = 500
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            qmarks = ",".join("?" * len(chunk))
            await db.execute(conn, f"DELETE FROM trace_spans WHERE conv_id IN ({qmarks})",
                             tuple(chunk))
            await db.execute(
                conn, f"DELETE FROM trace_conversations WHERE id IN ({qmarks})",
                tuple(chunk))
            removed += len(chunk)
        if removed:
            logger.info("trace 保留策略清理: 删除 %d 个过期会话（> %d 天）", removed, days)
        return removed
    except Exception as e:
        logger.warning("trace 保留策略清理失败（忽略，不阻塞启动）: %s: %s",
                       type(e).__name__, str(e)[:200])
        return 0


# ---------------- 小工具 ----------------

def _j(v) -> str | None:
    """入参/出参 JSON 序列化（供 record_* 的 input 字段）。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


def _union(existing_raw, new_list) -> list:
    """并集（保序去重）：既有数组 + 本次新增。"""
    out = list(_load_array(existing_raw))
    seen = set(out)
    for x in new_list:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _merge_rag_kb(existing_raw, new_dict: dict) -> list:
    """合并 rag 知识库引用为 [{id, name}]（按 id 去重，name 取最新）。"""
    merged: dict = {}
    for item in _load_array(existing_raw):
        if isinstance(item, dict) and item.get("id") is not None:
            merged[str(item["id"])] = item.get("name")
    for k, v in new_dict.items():
        merged[str(k)] = v
    return [{"id": k, "name": v} for k, v in merged.items()]
