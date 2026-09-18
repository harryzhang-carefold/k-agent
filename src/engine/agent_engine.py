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
from core import db
from core.config import S
from core.errors import LLMError
from core import stats
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


class AgentEngine:
    def __init__(self, app):
        self.app = app
        self.conn = None

    def set_conn(self, conn):
        self.conn = conn

    # ---------------- prompt assembly ----------------
    async def assemble(self, agent: dict, text: str | None = "",
                       history: list[dict] | None = None,
                       include_tools: bool = True) -> list[dict]:
        """返回 messages（单 system + 历史 + 请求）。固定内容在最左，请求在最右。"""
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
            ctx = []
            for rid in rag_ids:
                res = await rag.search(conn, int(rid), text, top_k=3)
                if res:
                    ctx.append(rag.build_context(res))
            if ctx:
                system += "\n\n[RAG 知识库上下文]\n" + "\n\n".join(ctx)

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
                  on_token=None) -> dict:
        """同步执行一轮 agent。返回 {answer, llm_calls, cache_hit, route_events, tool_calls}"""
        conn = self.conn
        agent_key = f"agent:{agent['id']}"
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
            return result

        # ---- LLM 路径（prefix caching 布局由 assemble 保证）----
        try:
            messages = await self.assemble(agent, text, history)
        except Exception as e:
            return {"answer": f"[系统错误] prompt 组装失败: {e}", "llm_calls": 0,
                    "cache_hit": None, "route_events": decision.get("route_events", []),
                    "sub_requests": decision.get("sub_requests", []), "tool_calls": [],
                    "degraded": True}

        before = stats.snapshot()["llm_calls"]
        try:
            answer = await self._tool_loop(messages, agent, mcp_row, on_token)
        except LLMError as e:
            # 受控降级（AC-14）：不崩溃、不裸 500
            result["llm_calls"] = stats.snapshot()["llm_calls"] - before
            result["answer"] = f"[LLM 降级应答] 模型服务暂不可用（{e}）。请检查 LLM_BASE_URL / AI_MODEL_API_KEY。"
            result["degraded"] = True
            await self._l0(agent["id"], text or "[image]", result["answer"])
            return result

        result["llm_calls"] = stats.snapshot()["llm_calls"] - before
        result["answer"] = answer
        # 写 L1 语义缓存（含拆分后的每个子句）+ L0 原始记录
        await cache_router.remember_answer(agent_key, text, answer, decision.get("sub_requests"))
        # 写 L1 图片 MD5 缓存（AC-51）：LLM 真实解析后，把未命中的图片按 md5 落库，
        # 下次同图请求直返（cache_hit=md5, llm_calls=0）
        await cache_router.remember_images(images, answer, decision.get("sub_requests"))
        await self._l0(agent["id"], text or "[image]", answer)
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
                         on_token=None) -> str:
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
        content = ""
        for _round in range(S.MAX_TOOL_ROUNDS):
            content = await llm.chat(messages, **args)
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
            ctx = {}
            if mcp_row:
                # TASK-053 迭代4: 传全行（含 transport/url/headers）供 mcp_call 按传输分流
                ctx = _mcp_ctx(mcp_row)
            res = await plugin_registry.call_plugin(name, tc.get("arguments") or {}, ctx)
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

    async def run_stream(self, agent: dict, text: str, images: list[dict] | None = None,
                         history: list[dict] | None = None) -> dict:
        """WS 流式：先走缓存路由（命中直发），否则 LLM 流式逐 token。"""
        conn = self.conn
        agent_key = f"agent:{agent['id']}"
        decision = await cache_router.route_request(agent_key, text, images)
        out = {"llm_calls": 0, "cache_hit": decision.get("cache_hit"),
               "route_events": decision.get("route_events", []),
               "sub_requests": decision.get("sub_requests", [])}
        if decision["hit"]:
            out["answer"] = decision["answer"]
            await self._l0(agent["id"], text or "[image]", decision["answer"])
            return out
        mcp_row = None
        for b in await db.fetchall(conn, "SELECT * FROM agent_bindings WHERE agent_id=? AND type='mcp'",
                                   (agent["id"],)):
            mcp_row = await db.fetchone(conn, "SELECT * FROM mcp_servers WHERE id=?", (int(b["ref_id"]),))
            if mcp_row:
                break
        try:
            messages = await self.assemble(agent, text, history)
        except Exception as e:
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
        before = stats.snapshot()["llm_calls"]
        parts: list[str] = []
        try:
            async for tok in llm.chat_stream(messages, **args):
                parts.append(tok)
                out.setdefault("_tokens", []).append(tok)
        except LLMError as e:
            out["llm_calls"] = stats.snapshot()["llm_calls"] - before
            out["answer"] = f"[LLM 降级应答] 模型服务暂不可用（{e}）。"
            out["degraded"] = True
            await self._l0(agent["id"], text or "[image]", out["answer"])
            return out
        out["llm_calls"] = stats.snapshot()["llm_calls"] - before
        out["answer"] = "".join(parts)
        # 流式路径若 LLM 以 tool_call 应答：同步补一轮工具执行（最多1轮）
        tc = self.extract_tool_call(out["answer"])
        if tc is not None:
            res = await plugin_registry.call_plugin(
                tc.get("name"), tc.get("arguments") or {},
                _mcp_ctx(mcp_row))
            messages.append({"role": "assistant", "content": out["answer"]})
            messages.append({"role": "user",
                             "content": f"[tool] {tc.get('name')} 执行结果: {json.dumps(res, ensure_ascii=False)}\n请基于以上工具结果回答用户原始问题。"})
            parts2 = []
            before2 = stats.snapshot()["llm_calls"]
            async for tok in llm.chat_stream(messages, **args):
                parts2.append(tok)
                out.setdefault("_tokens", []).append(tok)
            out["llm_calls"] += stats.snapshot()["llm_calls"] - before2
            out["answer"] = "".join(parts2) or out["answer"]
            out["tool_calls"] = [tc.get("name")]
        await cache_router.remember_answer(agent_key, text, out["answer"], decision.get("sub_requests"))
        # 写 L1 图片 MD5 缓存（AC-51，与 run() 对齐）
        await cache_router.remember_images(images, out["answer"], decision.get("sub_requests"))
        await self._l0(agent["id"], text or "[image]", out["answer"])
        return out

    async def _l0(self, agent_id, input_text, output_text):
        try:
            await memory.get_memory_backend().l0_append(agent_id, input_text, output_text)
        except Exception:
            pass
