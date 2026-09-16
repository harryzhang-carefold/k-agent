"""hermes CLI 封装（TASK-037 / 需求1）：profile 增删改查 + 工具面控制。

统一 `asyncio.create_subprocess_exec`（**禁止 shell=True**），PATH 注入
~/.local/bin，超时受控，非 0 退出码抛 HermesCLIError（调用方转 4xx/5xx）。
密钥纪律（DECISION-004 + AC-H9）：profile 输出在返回前脱敏（api_key/token/
password/secret 的值 → 末 4 位掩码），绝不把明文密钥写进 API 响应或日志。
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_HERMES_BIN = "hermes"
_TIMEOUT = 60.0  # 任务书：profile 命令统一 60s
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# `hermes profile list` 中 active（运行中）profile 行带此前缀标记
# （U+25C6 BLACK DIAMOND "◆"），且该前缀使行前导只剩 1 个空格。
_ACTIVE_MARK = "◆"

# AC-H9：含这些子串的行，等号后的值一律脱敏
_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|passw(or)?d|credential)")


class HermesCLIError(Exception):
    """hermes CLI 非 0 退出 / 超时 / 不可用。message 已脱敏。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code  # 进程退出码（超时为 None）


def hermes_bin() -> str | None:
    """探测 hermes wrapper（任务书：PATH 注入 ~/.local/bin）。

    候选 PATH = $HOME/.local/bin（宿主 hermes 用户）+ /home/hermes/.local/bin
    （容器内固定路径，容器进程 root、$HOME=/root）+ 进程 PATH。找到后做
    venv 可用性检查：wrapper 指向的 hermes-agent venv python 必须存在，
    否则视为不可用（容器未挂 ~/.hermes 时 wrapper 在但 venv 缺失 →
    AC-H7 503 路径）。
    """
    import shutil as _sh
    cands = [
        os.path.join(os.environ.get("HOME", ""), ".local", "bin"),
        "/home/hermes/.local/bin",
    ]
    path = os.pathsep.join(cands) + os.pathsep + os.environ.get("PATH", "")
    found = _sh.which(_HERMES_BIN, path=path)
    if not found:
        return None
    # venv 可用性：wrapper 的 python 目标存在（符号链接→uv python 也要解析到）
    vpy = os.path.join(_hermes_home(), "hermes-agent", "venv", "bin", "python")
    if not os.path.exists(vpy) or not os.path.exists(os.path.realpath(vpy)):
        return None
    return found


def hermes_available() -> bool:
    return hermes_bin() is not None


def _env() -> dict:
    """子进程环境：PATH 注入 + 固定 HOME/HERMES_HOME + 剥离运行身份变量。

    - PATH：$HOME/.local/bin + /home/hermes/.local/bin + 原 PATH（任务书：
      PATH 注入 ~/.local/bin；容器内 root 的 $HOME=/root，需补固定路径）。
    - HOME/HERMES_HOME：显式设为 hermes root 的父目录/root 本身——子进程
      继承容器 root 的 $HOME=/root 时，hermes 的 profile 解析与 .env 加载
      会找错目录；显式固定后宿主/容器行为一致。
    - 剥离 Hermes 运行身份变量（HERMES_KANBAN_* / HERMES_PROFILE 等）：
      hermes 子进程继承这些会误认自己是某个 kanban worker 并真的操作
      共享 kanban 板（TASK-037 事故根因，实测复现）。剥离后 -z 子进程
      只做纯对话，无 kanban 身份。
    """
    env = os.environ.copy()
    extra = os.pathsep.join([
        os.path.join(os.environ.get("HOME", ""), ".local", "bin"),
        "/home/hermes/.local/bin",
    ])
    env["PATH"] = extra + os.pathsep + env.get("PATH", "")
    root = _hermes_home()
    env["HERMES_HOME"] = root
    env["HOME"] = os.path.dirname(root)
    for k in list(env):
        if k.startswith("HERMES_KANBAN_") or k in (
                "HERMES_PROFILE", "HERMES_PROFILE_NAME",
                "HERMES_YOLO_MODE", "HERMES_TASK_ID"):
            del env[k]
    return env


def _redact(text: str) -> str:
    """AC-H9 脱敏：key/secret 行的值 → ****末4位。逐行处理，不影响其他内容。"""
    out = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if "=" in stripped and _SECRET_KEY_RE.search(stripped):
            k, _, v = stripped.partition("=")
            v = v.strip().strip('"').strip("'")
            masked = "" if not v else ("****" + v[-4:])
            # 保持前导缩进
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f"{indent}{k.strip()}={masked}")
        else:
            out.append(line)
    return "\n".join(out)


async def _run(args: list[str], timeout: float = _TIMEOUT) -> str:
    """跑 hermes 子进程，返回 stdout（已脱敏）。非 0/超时 → HermesCLIError。"""
    binpath = hermes_bin()
    if not binpath:
        raise HermesCLIError(
            "hermes CLI 不可用（PATH 中找不到 hermes，含 ~/.local/bin）；"
            "请确认容器已挂载宿主 ~/.hermes 且安装完成")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binpath, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(),
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        raise HermesCLIError(f"hermes {' '.join(args[:3])}… 超时（>{int(timeout)}s）")
    out = stdout_b.decode("utf-8", "replace")
    err = stderr_b.decode("utf-8", "replace")
    if proc.returncode != 0:
        # message 只用 stderr 摘要（脱敏），不拼入 stdout（防大输出/密钥）
        detail = (err or out).strip()
        raise HermesCLIError(
            f"hermes CLI 退出码 {proc.returncode}: {_redact(detail[:500]) or '(无输出)'}",
            code=proc.returncode)
    return _redact(out)


def _validate_profile_name(name: str) -> str:
    if not name or not _PROFILE_RE.match(name):
        raise ValueError(f"非法 profile 名: {name!r}（仅小写字母/数字/_/-，字母数字开头）")
    return name


def _hermes_home() -> str:
    """hermes root 目录（含 hermes-agent/venv 与 profiles/ 的那一层）。

    解析优先级（实测验证，容器与宿主通用）——与 hermes CLI 自身的
    get_default_hermes_root 语义对齐：
      1. HERMES_HOME 环境变量（compose 设为 root；若它指向
         <root>/profiles/<name> 则取其 grandparent）；
      2. $HOME/.hermes（hermes 用户宿主默认）；
      3. /home/hermes/.hermes（容器内固定路径，与 Dockerfile wrapper 一致；
         容器进程以 root 运行、$HOME=/root，但挂载点固定在 /home/hermes）；
      4. os.path.expanduser("~")/.hermes 兜底。
    每个候选都验证 <cand>/hermes-agent/venv/bin/python 存在（root 的
    可靠标志），第一个命中的返回；全不命中时返回首个候选（无 venv 的
    纯 sqlite 部署，hermes_available() 会因 wrapper 不存在而为 False）。
    """
    cands = []
    eh = os.environ.get("HERMES_HOME", "")
    if eh:
        c = Path(eh)
        # profile 模式（HERMES_HOME=<root>/profiles/<name>）→ 取 root
        if c.parent.name == "profiles":
            c = c.parent.parent
        cands.append(str(c))
    cands.append(os.path.join(os.environ.get("HOME", ""), ".hermes"))
    cands.append("/home/hermes/.hermes")
    cands.append(os.path.join(os.path.expanduser("~"), ".hermes"))
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.exists(os.path.join(c, "hermes-agent",
                                       "venv", "bin", "python")):
            return c
    return cands[0] if cands else os.path.join(
        os.path.expanduser("~"), ".hermes")


def _profile_cfg_path(name: str) -> str:
    """per-profile 配置文件 = <HERMES_HOME>/profiles/<name>/config.yaml。

    注意区分：同目录的 profile.yaml 只存 description（hermes profile
    describe 写它）；工具面/模型等运行时配置在 config.yaml（`-p` 时
    HERMES_HOME 指向 profile 目录，config 加载走 <profile>/config.yaml）。
    `profile create --no-skills` 可能不生成 config.yaml（实测），此时
    profile 回落到默认工具面（全部工具）——必须由本模块显式创建。
    """
    return os.path.join(_hermes_home(), "profiles", name, "config.yaml")


def _neutralize_profile_tools(name: str) -> bool:
    """把 profile 的 cli 工具面清零（platform_toolsets.cli=[]）+ 禁 skills。

    原因（已实测验证）：`hermes -z` 默认按 profile 的 `platform_toolsets.cli`
    加载全部工具并跑 agent 工具循环（approvals 自动绕过、继承父进程环境变量），
    既慢又可能产生改文件/调 kanban/开浏览器等副作用。AGP 的 hermes agent
    「默认仅对话」（任务书需求2），故创建 profile 后强制零工具。
    用宿主 venv 的 python 读改 profile config.yaml（pyyaml 已在其 venv）。
    config.yaml 不存在（裸 `profile create --no-skills` 的常见情况）时创建
    最小文件：只含 platform_toolsets.cli=[] + skills={}，其余配置回落默认值。
    成功（文件已写入且 cli=[]）返回 True；失败返回 False（不阻断创建，
    但此时该 profile 的 -z 会带全量工具——AC-H3 自测前必须确认 True）。
    """
    cfg = _profile_cfg_path(name)
    skills_dir = os.path.join(_hermes_home(), "profiles", name, "skills")
    py = os.path.join(_hermes_home(), "hermes-agent",
                      "venv", "bin", "python")
    if not os.path.exists(py):
        return False
    script = (
        "import sys, yaml, os, shutil\n"
        "p = sys.argv[1]\n"
        "sd = sys.argv[2]\n"
        "if os.path.isfile(p):\n"
        "    cfg = yaml.safe_load(open(p, encoding='utf-8')) or {}\n"
        "    if not isinstance(cfg, dict):\n"
        "        cfg = {}\n"
        "else:\n"
        "    cfg = {}\n"
        "pt = cfg.get('platform_toolsets')\n"
        "if not isinstance(pt, dict):\n"
        "    pt = {}\n"
        "pt['cli'] = []\n"
        "cfg['platform_toolsets'] = pt\n"
        "cfg['skills'] = {}\n"
        "yaml.safe_dump(cfg, open(p, 'w', encoding='utf-8'), "
        "allow_unicode=True, sort_keys=False)\n"
        "shutil.rmtree(sd, ignore_errors=True)\n"
    )
    try:
        r = subprocess.run([py, "-c", script, cfg, skills_dir],
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            return False
        # 回读确认 cli 工具面确实为 []（不信任"写了就算"）
        back = subprocess.run([py, "-c",
                               "import sys,yaml;print((yaml.safe_load(open(sys.argv[1],encoding='utf-8'))"
                               "or{}).get('platform_toolsets',{}).get('cli'))", cfg],
                              capture_output=True, timeout=10)
        return back.returncode == 0 and back.stdout.decode().strip() == "[]"
    except Exception:
        return False


def _profile_has_model(name: str) -> bool:
    """profile 的 config.yaml 是否配了模型 provider（-z 可用的前提）。

    判据：config.yaml 存在且 model.provider 非空（provider 是 -z 真正
    需要的"推理后端"；model.default 只是模型名，provider 为空时 hermes
    会报 No inference provider configured 并退出码 1）。
    """
    cfg = _profile_cfg_path(name)
    if not os.path.isfile(cfg):
        return False
    try:
        import yaml
        data = yaml.safe_load(open(cfg, encoding="utf-8")) or {}
        m = data.get("model") or {}
        return bool(m.get("provider"))
    except Exception:
        return False


def _profile_description(name: str) -> str:
    """profile 描述（profile describe 写入 profile.yaml；CLI show 不打印）。

    hermes `profile describe` 把描述存到 <profile>/profile.yaml 的
    description 字段，`profile show` 输出并不含它——所以 API 侧 show 需要
    本地补读（输出经 _redact，description 非敏感）。读失败返回 ""。
    """
    p = os.path.join(_hermes_home(), "profiles", name, "profile.yaml")
    if not os.path.isfile(p):
        return ""
    try:
        import yaml
        data = yaml.safe_load(open(p, encoding="utf-8")) or {}
        return str(data.get("description") or "")
    except Exception:
        return ""


def _default_clone_source() -> str | None:
    """挑一个"可用"的克隆源：自身 config.yaml 配了模型 provider 的 profile。

    首选 default（宿主主 profile），其次任意已配置 profile；找不到返回
    None（调用方走裸创建 + 提示需手动 `hermes model`）。
    """
    pdir = os.path.join(_hermes_home(), "profiles")
    candidates = ["default"]
    try:
        candidates += sorted(os.listdir(pdir))
    except Exception:
        pass
    for c in candidates:
        cfg = os.path.join(pdir, c, "config.yaml")
        if os.path.isfile(cfg):
            try:
                import yaml
                data = yaml.safe_load(open(cfg, encoding="utf-8")) or {}
                m = data.get("model") or {}
                if m.get("provider"):
                    return c
            except Exception:
                continue
    return None


async def create_profile(name: str, description: str | None = None,
                         clone_from: str | None = None) -> dict:
    """创建 hermes profile，产出"可对话 + 零工具"的 hermes agent 基座。

    创建策略（实测验证）：
      - 指定 clone_from → `hermes profile create <name> --clone-from <src>`
        （继承源的 model + .env 密钥，AGP 可立即对话）。
      - 未指定 → 默认从"可用"源（_default_clone_source）克隆；找不到可用
        源则裸 `profile create --no-skills`（无 model，-z 会降级提示，
        响应带 model_configured=False 明示）。
    注意：`--no-skills` 与 `--clone/--clone-from` 互斥（CLI 限制），所以克隆
    路径创建后由 _neutralize_profile_tools 删 skills 目录 + 清零工具面。
    """
    _validate_profile_name(name)
    if not clone_from:
        clone_from = _default_clone_source()
    if clone_from:
        _validate_profile_name(clone_from)
        args = ["profile", "create", name, "--clone-from", clone_from]
    else:
        args = ["profile", "create", name, "--no-skills"]
    if description:
        args += ["--description", description]
    await _run(args)
    tools_disabled = _neutralize_profile_tools(name)
    return {"name": name, "created": True,
            "tools_disabled": tools_disabled,
            "clone_from": clone_from,
            "model_configured": _profile_has_model(name)}


def _parse_profile_list(out: str) -> list[dict]:
    """解析 `hermes profile list` 表格输出 → [{name, model, gateway, alias, distribution}]。

    BUG-008 修复（TASK-041）：原实现用 `^\\s{2,}(\\S+)\\s{2,}` 匹配数据行，
    依赖"name 之后至少 2 个分隔空格"。但 `profile list` 是固定/自适应列宽
    表格——name ≥15 字符时 name 与 model 之间**只剩 1 个分隔空格** → 匹配
    失败 → 该 profile 被 `continue` **静默丢弃**（API 不可见但数据真实落地）。
    且 active（运行中）profile 行带 `◆` 前缀使行前导只剩 1 个空格，原正则
    `\\s{2,}` 同样丢它。

    改为**按空白切 token、不依赖固定列宽 / 分隔空格数量**：
      - 首 token 必须匹配 `_PROFILE_RE`（合法 profile 名）且行至少有
        name+model 两个 token，否则判为表头/分隔线/说明行，跳过；
      - active 行的 `◆` 前缀在切词前剥离；
      - 看起来像数据行（首 token 是合法名）却缺必需列 → **logging.warning**
        告警（杜绝再次静默丢弃，本 BUG 最恶劣的部分）。
    """
    rows: list[dict] = []
    for line in (out or "").splitlines():
        if not line.strip():
            continue
        # active 标记（◆）只可能出现在数据行首，剥离后不影响 token
        parts = line.strip().lstrip(_ACTIVE_MARK).split()
        if not parts:
            continue
        first = parts[0]
        if not _PROFILE_RE.match(first):
            # 表头（Profile…）/ 分隔线（─…）/ 其它非数据行：首 token 不是
            # 合法 profile 名，静默跳过（无信息可告警）。
            continue
        if len(parts) < 2:
            # 首 token 是合法 profile 名却没有 model 列 → 疑似被截断/变形的
            # 数据行，绝不能静默丢（BUG-008 根因）。
            logger.warning("hermes profile list 行解析失败（疑似数据行缺列）: %r", line)
            continue
        rec: dict = {"name": first}
        rec["model"] = parts[1]
        if len(parts) > 2:
            rec["gateway"] = parts[2]
        if len(parts) > 3:
            rec["alias"] = None if parts[3] in ("—", "-", "") else parts[3]
        if len(parts) > 4:
            rec["distribution"] = None if parts[4] in ("—", "-", "") else parts[4]
        rows.append(rec)
    return rows


async def list_profiles() -> list[dict]:
    """`hermes profile list` 表格输出 → [{name, model, gateway, alias, distribution}]。"""
    out = await _run(["profile", "list"])
    return _parse_profile_list(out)


async def show_profile(name: str) -> dict:
    """`hermes profile show <name>` → {name, ..., raw, description}（已脱敏）。

    description 从 profile.yaml 本地补读（`profile describe` 的写入位置；
    CLI show 输出不含它，见 _profile_description）。
    """
    _validate_profile_name(name)
    out = await _run(["profile", "show", name])
    rec: dict = {"name": name}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if k in ("Profile", "Path", "Model", "Gateway", "Skills",
                 "Alias", "Description"):
            rec[k.lower()] = v
    rec["description"] = _profile_description(name)
    rec["raw"] = out
    return rec


async def update_profile(name: str, text: str) -> dict:
    """`hermes profile describe <name> --text '...'`（改描述）。"""
    _validate_profile_name(name)
    await _run(["profile", "describe", name, "--text", text])
    return {"name": name, "updated": True}


async def delete_profile(name: str) -> dict:
    """`hermes profile delete <name> -y`。"""
    _validate_profile_name(name)
    await _run(["profile", "delete", name, "-y"])
    return {"name": name, "deleted": True}
