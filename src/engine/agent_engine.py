"""Agent 执行引擎 (PRD module 2/5/14): prefix-caching prompt 组装 + 工具调用循环 + L0/L1 记忆.

Prompt 布局（prefix caching 规则5，AC-18 可断言）：
  [固定左侧] system_prompt + skills 指令 + 工具 schema + RAG 上下文
  [左]       记忆上下文
  [最右]     历史消息 + 用户请求

工具调用协议（跨端点可靠、可断言）：LLM 输出
  {"tool_call": {"name": <plugin>, "arguments": {...}}}
引擎执行后把结果作为 tool 消息喂回，循环上限 MAX_TOOL_ROUNDS。
记忆解耦（AC-50）：本模块只 import memory.backend（接口抽象），不 import 具体后端。
"""
import json
import re
import time
from core import db
from core.config import S
from core.errors import LLMError
from core import stats
from core import trace as trace_mod
from llm import provider as llm
from rag import rag
from memory import backend as memory
from mcp import plugins as plugin_registry
from mcp.mcp_client import mcp_ctx_from_row as _mcp_ctx
from engine import cache_router

TOOL_PROTOCOL = (
    "\n\n[工具调用协议]\n"
    "你可以调用以下工具。需要调用时，回复一个 JSON（不要 markdown 代码块）：\n"
    '{"tool_call": {"name": "<工具名>", "arguments": {<参数>}}}\n'
    "工具执行后结果会作为 [tool] 消息返回，你再基于结果回答用户。不要编造工具结果。\n"
)

TOOL_NAMES_BLOCK = (
    "\n[可用工具 schema]\n"
    "{schemas}\n"
)


def _file_paths_from_result(res: dict) -> list[str]:
    """从 call_plugin 结果提取中间文件路径（任务书 §3 中间文件 a 项）。

    识别结果中的 files / file_path 字段（字符串或字符串列表）。无则返回 []。
    非阻塞的纯函数，供埋点调用。
    """
    if not isinstance(res, dict):
        return []
    paths: list[str] = []
    for key in ("files", "file_path", "file_paths"):
        v = res.get(key)
        if v is None:
            continue
        if isinstance(v, str):
            if v:
                paths.append(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item:
                    paths.append(item)
                elif isinstance(item, dict):
                    p = item.get("path") or item.get("file_path")
                    if isinstance(p, str) and p:
                        paths.append(p)
    # 去重保序
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


class AgentEngine:
    def __init__(self, app):
        self.app = app
        self.conn = None

    def set_conn(self, conn):
        self.conn = conn

    # ---------------- prompt assembly ----------------
    async def assemble(self, agent: dict, text: str | None = "",
                       history: list[dict] | None = None,
                       include_tools: bool = True,
                       ctx=None) -> list[dict]:
        """返回 messages（单 system + 历史 + 请求）。固定内容在最左，请求在最右。
        ctx: 可选 TraceContext（TASK-057 埋点）；有则记录 skill_inject / rag_search span。"""
        conn = self.conn
        system = agent["system_prompt"]

        # skills 指令（固定左侧）
        skill_rows = await db.fetchall(
            conn,
            """SELECT s.* FROM skills s JOIN agent_bindings b
               ON b.ref_id = CAST(s.id AS TEXT) AND b.type='skill' AND b.agent_id=?""",
            (agent["id"],))
        if skill_rows:
            system += "\n\n[Skills 指令]\n" + "\n".join(
                f"## {s['name']}\n{s['content']}" for s in skill_rows)
            # TASK-057 埋点：每个注入 skill → span skill_inject
            for s in skill_rows:
                if ctx:
                    await ctx.record_skill_inject(
                        s["name"], (s["content"] or "")[:400])

        # 工具 schema（固定左侧）
        tool_names = [r["ref_id"] for r in await db.fetchall(
            conn, "SELECT ref_id FROM agent_bindings WHERE agent_id=? AND type='plugin'",
            (agent["id"],))]
        if include_tools and tool_names:
            schemas = plugin_registry.schemas_for(tool_names)
            if schemas:
                system += TOOL_NAMES_BLOCK.format(
                    schemas=json.dumps(schemas, ensure_ascii=False))
                system += TOOL_PROTOCOL

        # RAG 上下文（固定左侧）：用请求文本检索 top-k
        rag_ids = [r["ref_id"] for r in await db.fetchall(
            conn, "SELECT ref_id FROM agent_bindings WHERE agent_id=? AND type='rag'",
            (agent["id"],))]
        if rag_ids and text:
            ctxs = []
            for rid in rag_ids:
                res = await rag.search(conn, int(rid), text, top_k=3)
                if res:
                    ctxs.append(rag.build_context(res))
                    # TASK-057 埋点：每个 kb → span rag_search（rag_chunks 携带 chunk 粒度）
                    if ctx:
                        kb_row = await db.fetchone(
                            conn, "SELECT name FROM rag_knowledge WHERE id=?", (int(rid),))
                        kb_name = kb_row["name"] if kb_row else str(rid)
                        chunks = [{"knowledge_id": int(rid),
                                   "chunk_seq": c.get("chunk_seq", c.get("seq")),
                                   "score": c.get("score"),
                                   "preview": c.get("preview", "")} for c in res]
                        await ctx.record_rag_search(int(rid), kb_name, text, chunks)
            if ctxs:
                system += "\n\n[RAG 知识库上下文]\n" + "\n\n".join(ctxs)

        # 记忆上下文（左）
        mem_ctx = await self._memory_context(agent)
        if mem_ctx:
            system += mem_ctx

        messages = [{"role": "system", "content": system}]
        for h in (history or [])[-10:]:
            messages.append({"role": h["role"], "content": h["content"]})
        if text:
            messages.append({"role": "user", "content": text})
        return messages

    async def _memory_context(self, agent: dict) -> str:
        """从 L2 图取与 agent 相关的子图摘要作为记忆上下文（解耦接口调用）。"""
        try:
            mb = memory.get_memory_backend()
            q = await mb.l2_query({"subgraph": agent.get("memory_center") or "", "hops": 2}) if agent.get("memory_center") else None
            if q and q.get("subgraph"):
                return "\n\n[长期记忆 子图摘要]\n" + q["subgraph"][:800]
        except Exception:
            return ""
        return ""

    # ---------------- tool loop ----------------
    _TOOL_RE = re.compile(r'\{[^{}]*"tool_call"[^{}]*\{[^{}]*\}[^{}]*\}')

    def extract_tool_call(self, content: str) -> dict | None:
        c = (content or "").strip()
        if c.startswith("```"):
            c = re.sub(r"^```[a-zA-Z]*\n?|```$", "", c).strip()
        # 直接 JSON
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and "tool_call" in obj:
                return obj["tool_call"]
        except Exception:
            pass
        # 文本中嵌 JSON
        m = self._TOOL_RE.search(c)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict) and "tool_call" in obj:
                    return obj["tool_call"]
            except Exception:
                pass
        return None

    async def run(self, agent: dict, text: str, images: list[dict] | None = None,
                  history: list[dict] | None = None,
                  on_token=None, conv_id: str | None = None) -> dict:
        """同步执行一轮 agent。返回 {answer, llm_calls, cache_hit, route_events, tool_calls}
        TASK-057: conv_id 传入则开启 trace 上下文并记录全痕迹（非阻塞，绝不因埋点失败影响主链路）。"""
        conn = self.conn
        agent_key = f"agent:{agent['id']}"
        # TASK-057: 开 trace 上下文（非阻塞：建/初始化失败则 ctx=None，主链路照常）
        ctx = None
        if conv_id:
            try:
                ctx = trace_mod.TraceContext(conn, conv_id, agent["id"],
                                             agent.get("name"),
                                             (agent.get("backend") or "custom").lower())
                await ctx.init_seq()
            except Exception:
                ctx = None
        # 绑定 MCP server 行（供 mcp_call 插件路由）
        mcp_row = None
        for b in await db.fetchall(conn, "SELECT * FROM agent_bindings WHERE agent_id=? AND type='mcp'",
                                   (agent["id"],)):
            mcp_row = await db.fetchone(
                conn, "SELECT * FROM mcp_servers WHERE id=?", (int(b["ref_id"]),))
            if mcp_row:
                break

        # ---- 缓存路由（规则 1/2/3/4）----
        decision = await cache_router.route_request(agent_key, text, images)
        result = {
            "answer": None, "llm_calls": 0, "cache_hit": decision.get("cache_hit"),
            "route_events": decision.get("route_events", []),
            "sub_requests": decision.get("sub_requests", []),
            "tool_calls": [],
        }
        if decision["hit"]:
            result["answer"] = decision["answer"]
            # 图片命中仍需记录 L0
            await self._l0(agent["id"], text or "[image]", decision["answer"])
            if on_token:
                await on_token(decision["answer"])
            # TASK-057 埋点：缓存命中 → span cache_hit + 聚合行
            if ctx:
                await ctx.record_cache_hit(decision.get("cache_hit"))
                await ctx.finish(status="ok")
            return result

        # ---- LLM 路径（prefix caching 布局由 assemble 保证）----
        try:
            messages = await self.assemble(agent, text, history, ctx=ctx)
        except Exception as e:
            if ctx:
                await ctx.record_error("assemble", str(e))
                await ctx.finish(status="error", error=str(e))
            return {"answer": f"[系统错误] prompt 组装失败: {e}", "llm_calls": 0,
                    "cache_hit": None, "route_events": decision.get("route_events", []),
                    "sub_requests": decision.get("sub_requests", []), "tool_calls": [],
                    "degraded": True}

        before = stats.snapshot()["llm_calls"]
        try:
            answer = await self._tool_loop(messages, agent, mcp_row, on_token, ctx=ctx)
        except LLMError as e:
            # 受控降级（AC-14）：不崩溃、不裸 500
            result["llm_calls"] = stats.snapshot()["llm_calls"] - before
            result["answer"] = f"[LLM 降级应答] 模型服务暂不可用（{e}）。请检查 LLM_BASE_URL / AI_MODEL_API_KEY。"
            result["degraded"] = True
            await self._l0(agent["id"], text or "[image]", result["answer"])
            if ctx:
                await ctx.record_error("llm_degraded", str(e))
                await ctx.finish(status="degraded", error=str(e))
            return result

        result["llm_calls"] = stats.snapshot()["llm_calls"] - before
        result["answer"] = answer
        # 写 L1 语义缓存（含拆分后的每个子句）+ L0 原始记录
        await cache_router.remember_answer(agent_key, text, answer, decision.get("sub_requests"))
        # 写 L1 图片 MD5 缓存（AC-51）：LLM 真实解析后，把未命中的图片按 md5 落库，
        # 下次同图请求直返（cache_hit=md5, llm_calls=0）
        await cache_router.remember_images(images, answer, decision.get("sub_requests"))
        await self._l0(agent["id"], text or "[image]", answer)
        if ctx:
            await ctx.finish(status="ok")
        return result

    # ---------------- endpoint 解析（TASK-029 / 需求1） ----------------
    async def _endpoint_kwargs(self, agent: dict) -> dict:
        """按 agent.model 解析 llm_endpoints（存 endpoint **name**）。

        返回传给 llm.chat/chat_stream 的覆盖参数 {base_url, api_key, timeout}。
        向后兼容（RISK-018）：model 为空 / 查不到 / 未启用（is_active=0）/
        端点已删 → 返回 {}（回退 S.* 单端点，不崩）。
        旧 agent 的 model 是自由文本（如 vllm-qwen3.8-27b），匹配不到
        endpoint 名时同样回退 S.*，不报错。
        """
        model = agent.get("model")
        if not model:
            return {}
        try:
            row = await db.fetchone(
                self.conn,
                "SELECT * FROM llm_endpoints WHERE name=? AND is_active=1",
                (model,))
        except Exception:
            return {}  # 表不可用等异常 → 回退 S.*
        if not row:
            return {}
        kw = {"base_url": row["base_url"],
              "timeout": float(row["timeout"] or S.LLM_TIMEOUT)}
        # model 覆盖：agent.model 存的是 endpoint **name**（绑定标识），
        # LLM payload 的 model 字段必须是 endpoint 行的 model 标识（如
        # vllm-qwen3.8-27b）——否则 vLLM 404 "model does not exist"
        if row.get("model"):
            kw["model"] = row["model"]
        # api_key 为空时不传（provider 回退 S.LLM_API_KEY 共享 key——
        # 指向同一 vLLM 集群的端点常见场景）；显式配置了 key 才覆盖
        if row.get("api_key"):
            kw["api_key"] = row["api_key"]
        return kw

    async def _tool_loop(self, messages: list[dict], agent: dict, mcp_row: dict | None,
                         on_token=None, ctx=None) -> str:
        args = {"temperature": agent["temperature"], "max_tokens": agent["max_tokens"],
                "top_p": agent["top_p"]}
        # TASK-029: endpoint 覆盖 + BUG-007 回退（同步路径）。
        # 仅当 agent.model 能解析为"可用 endpoint 行"（含 base_url）时，才用
        # 端点行覆盖 base_url/api_key/timeout/model；解析失败（查不到/停用/
        # 已删/表异常）时，payload model 不得停在 endpoint **name**（对 LLM
        # 无意义 → 默认端点 404 model does not exist → 降级），而是回退
        # S.LLM_MODEL 走系统默认端点（RISK-018 兜底不变式，TASK-034）。
        _epk = await self._endpoint_kwargs(agent)
        if _epk:
            args.update(_epk)
        elif agent.get("model"):
            args["model"] = S.LLM_MODEL
        # 埋点用：模型名（args.model 经 endpoint 解析后是真实 LLM 模型标识）
        model_name = args.get("model") or S.LLM_MODEL
        content = ""
        for _round in range(S.MAX_TOOL_ROUNDS):
            _t0 = time.monotonic()
            content, usage = await llm.chat(messages, **args)
            _dur = int((time.monotonic() - _t0) * 1000)
            # TASK-057 埋点：每次 LLM 调用 → span llm_call（真实 token/模型/耗时）
            if ctx:
                last_user = self._last_user_text(messages)
                await ctx.record_llm_call(
                    model_name, last_user, content,
                    tokens_in=usage.get("prompt_tokens") if usage else None,
                    tokens_out=usage.get("completion_tokens") if usage else None,
                    duration_ms=_dur)
            tc = self.extract_tool_call(content)
            if tc is None:
                if on_token:
                    await on_token(content)
                return content
            name = tc.get("name")
            plugin_registry_names = list(plugin_registry.PLUGIN_REGISTRY.keys())
            if name not in plugin_registry_names:
                messages.append({"role": "user", "content":
                                 f"(上一轮你要求调用未知工具 {name}。可用工具: {plugin_registry_names}。请改调可用工具或直接回答用户问题。)"})
                continue
            _t1 = time.monotonic()
            plugin_ctx = {}
            if mcp_row:
                # TASK-053 迭代4: 传全行（含 transport/url/headers）供 mcp_call 按传输分流
                plugin_ctx = _mcp_ctx(mcp_row)
            res = await plugin_registry.call_plugin(name, tc.get("arguments") or {}, plugin_ctx)
            _dur2 = int((time.monotonic() - _t1) * 1000)
            # TASK-057 埋点：工具/MCP 调用 → span tool_call / mcp_call
            if ctx:
                is_mcp = (name == "mcp_call")
                if is_mcp:
                    server = (mcp_row.get("name") if mcp_row else None)
                    tool_name = (tc.get("arguments") or {}).get("name", "")
                    await ctx.record_mcp_call(server, tool_name, tc.get("arguments"),
                                              res, duration_ms=_dur2,
                                              status="ok" if res.get("ok") else "error",
                                              error=None if res.get("ok") else str(res.get("error")))
                else:
                    await ctx.record_tool_call(name, tc.get("arguments"), res,
                                               duration_ms=_dur2,
                                               status="ok" if res.get("ok") else "error",
                                               error=None if res.get("ok") else str(res.get("error")))
                # 中间文件：call_plugin 结果含 files/file_path 字段 → file_op span
                for f in _file_paths_from_result(res):
                    await ctx.record_file_op(f)
            if on_token:
                await on_token(f"[tool_call:{name}] ")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"[tool] {name} 执行结果: {json.dumps(res, ensure_ascii=False)}\n请基于以上工具结果回答用户原始问题。"})
            content = ""
        # 超轮次：返回最后一轮内容
        if on_token:
            await on_token(content)
        return content

    @staticmethod
    def _last_user_text(messages: list[dict]) -> str | None:
        """取最后一条 user 消息文本（llm_call span 的 input 摘要）。"""
        for m in reversed(messages):
            if m.get("role") == "user":
                return (m.get("content") or "")[:2000]
        return None

    async def run_stream(self, agent: dict, text: str, images: list[dict] | None = None,
                         history: list[dict] | None = None,
                         conv_id: str | None = None) -> dict:
        """WS 流式：先走缓存路由（命中直发），否则 LLM 流式逐 token。
        TASK-057: conv_id 传入则记录 trace（流式 usage 有则记、无则 0 不报错）。"""
        conn = self.conn
        agent_key = f"agent:{agent['id']}"
        ctx = None
        if conv_id:
            try:
                ctx = trace_mod.TraceContext(conn, conv_id, agent["id"],
                                             agent.get("name"),
                                             (agent.get("backend") or "custom").lower())
                await ctx.init_seq()
            except Exception:
                ctx = None
        decision = await cache_router.route_request(agent_key, text, images)
        out = {"llm_calls": 0, "cache_hit": decision.get("cache_hit"),
               "route_events": decision.get("route_events", []),
               "sub_requests": decision.get("sub_requests", [])}
        if decision["hit"]:
            out["answer"] = decision["answer"]
            await self._l0(agent["id"], text or "[image]", decision["answer"])
            if ctx:
                await ctx.record_cache_hit(decision.get("cache_hit"))
                await ctx.finish(status="ok")
            return out
        mcp_row = None
        for b in await db.fetchall(conn, "SELECT * FROM agent_bindings WHERE agent_id=? AND type='mcp'",
                                   (agent["id"],)):
            mcp_row = await db.fetchone(conn, "SELECT * FROM mcp_servers WHERE id=?", (int(b["ref_id"]),))
            if mcp_row:
                break
        try:
            messages = await self.assemble(agent, text, history, ctx=ctx)
        except Exception as e:
            if ctx:
                await ctx.record_error("assemble", str(e))
                await ctx.finish(status="error", error=str(e))
            out["answer"] = f"[系统错误] prompt 组装失败: {e}"
            out["degraded"] = True
            return out
        args = {"temperature": agent["temperature"], "max_tokens": agent["max_tokens"],
                "top_p": agent["top_p"]}
        # TASK-029: endpoint 覆盖 + BUG-007 回退（流式路径）。
        # 与 _tool_loop 同一逻辑：endpoint 解析成功（含 base_url）才覆盖；
        # 解析失败时 model 回退 S.LLM_MODEL，不得停在 endpoint name
        # （否则默认端点 404 → degraded，违反 RISK-018 兜底，TASK-034）。
        _epk = await self._endpoint_kwargs(agent)
        if _epk:
            args.update(_epk)
        elif agent.get("model"):
            args["model"] = S.LLM_MODEL
        model_name = args.get("model") or S.LLM_MODEL
        before = stats.snapshot()["llm_calls"]
        parts: list[str] = []
        try:
            _t0 = time.monotonic()
            content, usage = await llm.chat_stream(messages, **args)
            _dur = int((time.monotonic() - _t0) * 1000)
            parts = list(content or "")
            out.setdefault("_tokens", []).extend(list(content or ""))
            if ctx:
                await ctx.record_llm_call(
                    model_name, self._last_user_text(messages), content,
                    tokens_in=usage.get("prompt_tokens") if usage else None,
                    tokens_out=usage.get("completion_tokens") if usage else None,
                    duration_ms=_dur)
        except LLMError as e:
            out["llm_calls"] = stats.snapshot()["llm_calls"] - before
            out["answer"] = f"[LLM 降级应答] 模型服务暂不可用（{e}）。"
            out["degraded"] = True
            await self._l0(agent["id"], text or "[image]", out["answer"])
            if ctx:
                await ctx.record_error("llm_degraded", str(e))
                await ctx.finish(status="degraded", error=str(e))
            return out
        out["llm_calls"] = stats.snapshot()["llm_calls"] - before
        out["answer"] = "".join(parts)
        # 流式路径若 LLM 以 tool_call 应答：同步补一轮工具执行（最多1轮）
        tc = self.extract_tool_call(out["answer"])
        if tc is not None:
            _t1 = time.monotonic()
            res = await plugin_registry.call_plugin(
                tc.get("name"), tc.get("arguments") or {},
                _mcp_ctx(mcp_row))
            _dur2 = int((time.monotonic() - _t1) * 1000)
            if ctx:
                if tc.get("name") == "mcp_call":
                    server = (mcp_row.get("name") if mcp_row else None)
                    tool_name = (tc.get("arguments") or {}).get("name", "")
                    await ctx.record_mcp_call(server, tool_name, tc.get("arguments"), res,
                                              duration_ms=_dur2,
                                              status="ok" if res.get("ok") else "error",
                                              error=None if res.get("ok") else str(res.get("error")))
                else:
                    await ctx.record_tool_call(tc.get("name"), tc.get("arguments"), res,
                                               duration_ms=_dur2,
                                               status="ok" if res.get("ok") else "error",
                                               error=None if res.get("ok") else str(res.get("error")))
                for f in _file_paths_from_result(res):
                    await ctx.record_file_op(f)
            messages.append({"role": "assistant", "content": out["answer"]})
            messages.append({"role": "user",
                             "content": f"[tool] {tc.get('name')} 执行结果: {json.dumps(res, ensure_ascii=False)}\n请基于以上工具结果回答用户原始问题。"})
            parts2 = []
            before2 = stats.snapshot()["llm_calls"]
            _t3 = time.monotonic()
            content2, usage2 = await llm.chat_stream(messages, **args)
            _dur3 = int((time.monotonic() - _t3) * 1000)
            parts2 = list(content2 or "")
            out.setdefault("_tokens", []).extend(list(content2 or ""))
            if ctx:
                await ctx.record_llm_call(
                    model_name, self._last_user_text(messages), content2,
                    tokens_in=usage2.get("prompt_tokens") if usage2 else None,
                    tokens_out=usage2.get("completion_tokens") if usage2 else None,
                    duration_ms=_dur3)
            out["llm_calls"] += stats.snapshot()["llm_calls"] - before2
            out["answer"] = "".join(parts2) or out["answer"]
            out["tool_calls"] = [tc.get("name")]
        await cache_router.remember_answer(agent_key, text, out["answer"], decision.get("sub_requests"))
        # 写 L1 图片 MD5 缓存（AC-51，与 run() 对齐）
        await cache_router.remember_images(images, out["answer"], decision.get("sub_requests"))
        await self._l0(agent["id"], text or "[image]", out["answer"])
        if ctx:
            await ctx.finish(status="ok")
        return out

    async def _l0(self, agent_id, input_text, output_text):
        try:
            await memory.get_memory_backend().l0_append(agent_id, input_text, output_text)
        except Exception:
            pass
