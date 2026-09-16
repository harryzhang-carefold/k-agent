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
                  history: list[dict] | None = None, on_token=None) -> dict:
        """返回与 AgentEngine.run 同形状的结果 dict（route_events 恒空，
        cache_hit 恒 None，llm_calls 恒 0——hermes 不走 AGP 缓存路由/AGP LLM）。"""
        profile = agent.get("hermes_profile") or ""
        result = {"answer": None, "llm_calls": 0, "cache_hit": None,
                  "route_events": [], "sub_requests": [], "tool_calls": []}
        try:
            answer = await self._call(profile, text or "")
        except hermes_cli.HermesCLIError as e:
            answer = _DEGRADED.format(detail=str(e)[:200])
            result["answer"] = answer
            result["degraded"] = True
            await self._l0(agent["id"], text or "[image]", answer)
            return result
        except Exception as e:
            answer = _DEGRADED.format(detail=f"{type(e).__name__}: {str(e)[:150]}")
            result["answer"] = answer
            result["degraded"] = True
            await self._l0(agent["id"], text or "[image]", answer)
            return result
        result["answer"] = answer
        # L0 原始记录（与 custom 一致）
        await self._l0(agent["id"], text or "[image]", answer)
        if on_token and answer:
            await on_token(answer)
        return result

    # ---------------- 流式（WS /ws/chat/{agent_id}/{conv_id}） ----------------
    async def run_stream(self, agent: dict, text: str, images: list[dict] | None = None,
                         history: list[dict] | None = None) -> dict:
        """与 AgentEngine.run_stream 同形状：_tokens 列表供 WS 逐 token 发送。

        hermes -z 是「最后一次性打印 final response」，无真实 token 流；
        我们逐行读 stdout（-z 内部重定向后 stdout 只在结尾写出），因此通常是
        最后一次性收到整段——按任务书：「一次性吐出则整段发一次」。
        """
        profile = agent.get("hermes_profile") or ""
        out = {"llm_calls": 0, "cache_hit": None,
               "route_events": [], "sub_requests": [], "tool_calls": []}
        try:
            lines = await self._call_streaming(profile, text or "")
        except hermes_cli.HermesCLIError as e:
            out["answer"] = _DEGRADED.format(detail=str(e)[:200])
            out["degraded"] = True
            # 注意：degraded 时**不**放 _tokens——WS 侧 degraded 分支单独
            # 整段发一次 answer（若同时进 _tokens 会重复发两遍）
            await self._l0(agent["id"], text or "[image]", out["answer"])
            return out
        except Exception as e:
            out["answer"] = _DEGRADED.format(detail=f"{type(e).__name__}: {str(e)[:150]}")
            out["degraded"] = True
            await self._l0(agent["id"], text or "[image]", out["answer"])
            return out
        out["answer"] = "".join(lines)
        out.setdefault("_tokens", []).extend(lines)
        await self._l0(agent["id"], text or "[image]", out["answer"])
        return out

    # ---------------- 子进程调用 ----------------
    def _env(self) -> dict:
        # 复用 hermes_cli._env()：PATH 注入 + 剥离 Hermes 运行身份变量
        # （HERMES_KANBAN_* / HERMES_PROFILE 等）。-z 子进程若继承这些会
        # 误认自己是 kanban worker 并操作共享 kanban 板（TASK-037 事故根因）。
        # -z 自身会设 HERMES_YOLO_MODE=1（approvals 自动绕过）；我们靠
        # profile 的 cli 工具面=[] 消除副作用，不额外注入。
        return hermes_cli._env()

    async def _call(self, profile: str, text: str) -> str:
        """同步式：等完整 stdout。"""
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
        return stdout_b.decode("utf-8", "replace").strip()

    async def _call_streaming(self, profile: str, text: str) -> list[str]:
        """流式式：逐行读 stdout（-z 通常一次性吐出 → 一次 append）。"""
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
        return lines

    async def _l0(self, agent_id, input_text, output_text):
        try:
            await memory.get_memory_backend().l0_append(agent_id, input_text, output_text)
        except Exception:
            pass
