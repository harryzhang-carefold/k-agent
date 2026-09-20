"""HermesAgentAdapter（TASK-037 / 需求1 核心）：hermes 后端对话路由。

与 AgentEngine 对偶，但语义完全不同（任务书 §四.3，无 tool loop / 无 RAG /
无 AGP 缓存路由）：
  - 同步 run():      `hermes -p <profile> -z "<text>"`，stdout 整段作 answer
  - 流式 run_stream(): 同上，但 stdout 逐行读、逐行转 WS token（hermes -z 一次性
    吐出时即整段发一次，前端兼容）
  - L0 记忆照写（memory_l0_raw 复用，与 custom 一致）
  - 不传 AGP history（hermes 用自身 session 记忆；--resume 多轮连贯列 P1，本卡不做）
  - 进程失败/超时/CLI 不可用 → 受控降级（同 AC-14 风格：degraded 标志 + 提示文案，
    不裸 500），保证 hermes 缺失时 custom agent 链路完全不受影响（AC-H7）。

安全前提（已实测验证并写入 DEV_REPORT 风险章节）：AGP 创建的 hermes profile
在创建时即把 `platform_toolsets.cli` 清零（core.hermes_cli._neutralize_profile_tools），
使 `-z` 不加载任何工具 → 无工具循环、无改文件/调 kanban/开浏览器等副作用。
"""
import asyncio
import logging
import os

from core import hermes_cli
from core import trace as trace_mod
from memory import backend as memory

logger = logging.getLogger("engine.hermes_adapter")

# 单次对话超时（秒）：比 profile 命令的 60s 更宽（-z 要等真实 LLM 回合），
# 但受控——超时即降级，不无限挂起。可用 HERMES_CHAT_TIMEOUT 覆盖。
CHAT_TIMEOUT = float(os.environ.get("HERMES_CHAT_TIMEOUT", "120"))

_DEGRADED = ("[Hermes Agent 降级应答] Hermes CLI 不可用或调用失败（{detail}）。"
             "本 agent 为 Hermes 后端，请在服务器安装/挂载 hermes 后重试。")


class HermesAgentAdapter:
    def __init__(self, app):
        self.app = app
        self.conn = None

    def set_conn(self, conn):
        self.conn = conn

    # ---------------- 同步（POST /api/chat/{agent_id}） ----------------
    async def run(self, agent: dict, text: str, images: list[dict] | None = None,
                  history: list[dict] | None = None, on_token=None,
                  conv_id: str | None = None) -> dict:
        """返回与 AgentEngine.run 同形状的结果 dict（route_events 恒空，
        cache_hit 恒 None，llm_calls 恒 0——hermes 不走 AGP 缓存路由/AGP LLM）。
        TASK-057: conv_id 传入则记录 trace（hermes 后端 token 无 usage → NULL +
        status 注明 hermes_no_usage，不编造数字；CLI 工作目录文件变化记 file_op）。"""
        profile = agent.get("hermes_profile") or ""
        ctx = None
        if conv_id:
            try:
                ctx = trace_mod.TraceContext(self.conn, conv_id, agent["id"],
                                             agent.get("name"), "hermes")
                await ctx.init_seq()
            except Exception:
                ctx = None
        result = {"answer": None, "llm_calls": 0, "cache_hit": None,
                  "route_events": [], "sub_requests": [], "tool_calls": []}
        try:
            answer = await self._call(profile, text or "", ctx=ctx)
        except hermes_cli.HermesCLIError as e:
            answer = _DEGRADED.format(detail=str(e)[:200])
            result["answer"] = answer
            result["degraded"] = True
            await self._l0(agent["id"], text or "[image]", answer)
            if ctx:
                await ctx.record_error("hermes_cli", str(e))
                await ctx.finish(status="degraded", error=str(e))
            return result
        except Exception as e:
            answer = _DEGRADED.format(detail=f"{type(e).__name__}: {str(e)[:150]}")
            result["answer"] = answer
            result["degraded"] = True
            await self._l0(agent["id"], text or "[image]", answer)
            if ctx:
                await ctx.record_error("hermes_err", f"{type(e).__name__}: {str(e)[:150]}")
                await ctx.finish(status="degraded", error=str(e))
            return result
        result["answer"] = answer
        # L0 原始记录（与 custom 一致）
        await self._l0(agent["id"], text or "[image]", answer)
        if on_token and answer:
            await on_token(answer)
        if ctx:
            await ctx.finish(status="ok")
        return result

    # ---------------- 流式（WS /ws/chat/{agent_id}/{conv_id}） ----------------
    async def run_stream(self, agent: dict, text: str, images: list[dict] | None = None,
                         history: list[dict] | None = None,
                         conv_id: str | None = None) -> dict:
        """与 AgentEngine.run_stream 同形状：_tokens 列表供 WS 逐 token 发送。

        hermes -z 是「最后一次性打印 final response」，无真实 token 流；
        我们逐行读 stdout（-z 内部重定向后 stdout 只在结尾写出），因此通常是
        最后一次性收到整段——按任务书：「一次性吐出则整段发一次」。
        TASK-057: conv_id 传入则记录 trace（token NULL + hermes_no_usage）。
        """
        profile = agent.get("hermes_profile") or ""
        ctx = None
        if conv_id:
            try:
                ctx = trace_mod.TraceContext(self.conn, conv_id, agent["id"],
                                             agent.get("name"), "hermes")
                await ctx.init_seq()
            except Exception:
                ctx = None
        out = {"llm_calls": 0, "cache_hit": None,
               "route_events": [], "sub_requests": [], "tool_calls": []}
        try:
            lines = await self._call_streaming(profile, text or "", ctx=ctx)
        except hermes_cli.HermesCLIError as e:
            out["answer"] = _DEGRADED.format(detail=str(e)[:200])
            out["degraded"] = True
            # 注意：degraded 时**不**放 _tokens——WS 侧 degraded 分支单独
            # 整段发一次 answer（若同时进 _tokens 会重复发两遍）
            await self._l0(agent["id"], text or "[image]", out["answer"])
            if ctx:
                await ctx.record_error("hermes_cli", str(e))
                await ctx.finish(status="degraded", error=str(e))
            return out
        except Exception as e:
            out["answer"] = _DEGRADED.format(detail=f"{type(e).__name__}: {str(e)[:150]}")
            out["degraded"] = True
            await self._l0(agent["id"], text or "[image]", out["answer"])
            if ctx:
                await ctx.record_error("hermes_err", f"{type(e).__name__}: {str(e)[:150]}")
                await ctx.finish(status="degraded", error=str(e))
            return out
        out["answer"] = "".join(lines)
        out.setdefault("_tokens", []).extend(lines)
        await self._l0(agent["id"], text or "[image]", out["answer"])
        if ctx:
            await ctx.finish(status="ok")
        return out

    # ---------------- hermes 埋点辅助（TASK-057） ----------------
    async def _hermes_span(self, ctx, profile: str, text: str, answer: str,
                           status: str = "ok", error: str | None = None):
        """记录 hermes 后端的 llm_call span + 工作目录文件变化 file_op。
        token 无 usage → None（NULL）+ status 注明 hermes_no_usage（不编造数字）。
        非阻塞：ctx 为 None 或异常都静默。"""
        if ctx is None:
            return
        try:
            await ctx.record_llm_call(
                model="hermes:" + (profile or "unknown"),
                input_summary=text, output_summary=answer,
                tokens_in=None, tokens_out=None,
                status=status if status != "ok" else "ok",
                error=(error or "") + (" hermes_no_usage" if status == "ok" else ""))
            # 中间文件：CLI 工作目录 before/after diff（任务书 §3 中间文件 b 项）
            # 保守实现：列出 profile 工作目录下新增/变更文件（若可访问）。
            for p in self._workdir_files(profile):
                await ctx.record_file_op(p)
        except Exception as e:
            logger.warning("hermes trace 埋点失败（忽略，不阻塞）: %s: %s",
                           type(e).__name__, str(e)[:150])

    def _workdir_files(self, profile: str) -> list[str]:
        """hermes profile 工作目录（HERMES_HOME/profiles/<name>/workspace）下的
        文件列表（相对路径，最多 20 个，防大目录）。不可访问返回 []。
        这是中间文件机制预留（b 项）：零工具面 profile 通常无文件 → 返回空。"""
        try:
            root = hermes_cli._hermes_home()
            wd = os.path.join(root, "profiles", profile or "", "workspace")
            if not os.path.isdir(wd):
                return []
            out = []
            for dirpath, _dirnames, filenames in os.walk(wd):
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, wd)
                    out.append(os.path.join(wd, rel))
                    if len(out) >= 20:
                        return out
            return out
        except Exception:
            return []

    # ---------------- 子进程调用 ----------------
    def _env(self) -> dict:
        # 复用 hermes_cli._env()：PATH 注入 + 剥离 Hermes 运行身份变量
        # （HERMES_KANBAN_* / HERMES_PROFILE 等）。-z 子进程若继承这些会
        # 误认自己是 kanban worker 并操作共享 kanban 板（TASK-037 事故根因）。
        # -z 自身会设 HERMES_YOLO_MODE=1（approvals 自动绕过）；我们靠
        # profile 的 cli 工具面=[] 消除副作用，不额外注入。
        return hermes_cli._env()

    async def _call(self, profile: str, text: str, ctx=None) -> str:
        """同步式：等完整 stdout。成功时记录 hermes trace span（token NULL + hermes_no_usage）。"""
        if not profile:
            raise hermes_cli.HermesCLIError("agent 未绑定 hermes_profile")
        binpath = hermes_cli.hermes_bin()
        if not binpath:
            raise hermes_cli.HermesCLIError("hermes CLI 不可用（PATH 中找不到 hermes）")
        proc = await asyncio.create_subprocess_exec(
            binpath, "-p", profile, "-z", text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=CHAT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise hermes_cli.HermesCLIError(f"hermes -z 超时（>{int(CHAT_TIMEOUT)}s）")
        if proc.returncode != 0:
            err = (stderr_b or stdout_b).decode("utf-8", "replace").strip()
            # 脱敏 + 截断（AC-H9：stderr 可能含 .env 片段）
            raise hermes_cli.HermesCLIError(
                f"hermes -z 退出码 {proc.returncode}: "
                f"{hermes_cli._redact(err[:300]) or '(无输出)'}")
        answer = stdout_b.decode("utf-8", "replace").strip()
        await self._hermes_span(ctx, profile, text, answer)
        return answer

    async def _call_streaming(self, profile: str, text: str, ctx=None) -> list[str]:
        """流式式：逐行读 stdout（-z 通常一次性吐出 → 一次 append）。成功时记录 hermes span。"""
        if not profile:
            raise hermes_cli.HermesCLIError("agent 未绑定 hermes_profile")
        binpath = hermes_cli.hermes_bin()
        if not binpath:
            raise hermes_cli.HermesCLIError("hermes CLI 不可用（PATH 中找不到 hermes）")
        proc = await asyncio.create_subprocess_exec(
            binpath, "-p", profile, "-z", text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
        )
        lines: list[str] = []
        deadline = asyncio.get_event_loop().time() + CHAT_TIMEOUT
        try:
            assert proc.stdout is not None
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    proc.kill()
                    raise hermes_cli.HermesCLIError(f"hermes -z 超时（>{int(CHAT_TIMEOUT)}s）")
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                if not line:
                    break
                lines.append(line.decode("utf-8", "replace"))
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            raise hermes_cli.HermesCLIError(f"hermes -z 超时（>{int(CHAT_TIMEOUT)}s）")
        if proc.returncode != 0:
            err = ""
            if proc.stderr is not None:
                try:
                    err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
                except Exception:
                    err = ""
            raise hermes_cli.HermesCLIError(
                f"hermes -z 退出码 {proc.returncode}: "
                f"{hermes_cli._redact(err[:300]) or '(无输出)'}")
        await self._hermes_span(ctx, profile, text, "".join(lines))
        return lines

    async def _l0(self, agent_id, input_text, output_text):
        try:
            await memory.get_memory_backend().l0_append(agent_id, input_text, output_text)
        except Exception:
            pass
