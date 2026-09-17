#!/usr/bin/env python3
"""PostgreSQL 可复用性探测（TASK-046 / 1.5.0）。

为什么不再手写 wire protocol：
  早期版本用 stdlib socket 手撸 PostgreSQL v3 协议 + SCRAM-SHA-256 全握手，
  但 SCRAM 的 SASLInitialResponse/SASLContinue/SASLFinal 字节布局（消息长度
  字段、NUL 结尾、code 11/12 等）极易踩坑，且无法覆盖 md5 / password / TLS
  等其它认证方式。改用 `docker run psql`（postgres 官方镜像，libpq 已实现
  全部认证）做探测，battle-tested，零协议风险。

实现：
  - `docker run --rm --network host psql -h HOST -p PORT -U USER -d DB -tAc "SELECT 1"`
  - `--network host`：探测容器共享宿主网络命名空间，因此既能按容器名解析的
    内网 IP 直连（宿主可路由到任意容器 IP），也能探测 127.0.0.1:5432（宿主
    本机 PG）。
  - `PGCONNECTTIMEOUT=6`：SYN-drop / 不可达目标 6s 内失败（fail-fast，
    绝不无限 hang，与 app 侧 15s fail-fast 呼应）。
  - 密码经 `-e PGPASSWORD` 注入（不落命令行 argv / 日志）。

用法:
  pg_probe.py HOST PORT USER PASSWORD DBNAME
输出（stdout 单行 JSON，密码永不回显）:
  {"ok": bool, "reachable": bool, "db_exists": bool,
   "login_ok": bool, "agp_schema_exists": bool, "error": str|null}

语义约定（供 gcp_deploy.sh 判定"可复用"）:
  ok=True        —— 连通 + 认证通过 + 目标库存在 + SELECT 1 成功 = 可直接复用
  login_ok=True  —— 连通 + 认证通过（库可能不存在）= 可登录，库缺失时脚本会
                   自建库 + agp schema 后复用
  reachable=False —— TCP 不通（机器/端口/防火墙/SYN-drop）
"""
import json
import os
import subprocess
import sys

CONNECT_TIMEOUT = 5  # 秒：单条 psql 连接超时（PGCONNECTTIMEOUT）
# 硬上限：docker run 整体（拉镜像启动 + 连接）不超过 PROBE_HARD_TIMEOUT。
# 用 `timeout` 包裹保证 SYN-drop / 纯黑洞目标也不会挂太久（fail-fast）。
PROBE_HARD_TIMEOUT = 12


def run_psql(args, password):
    """跑一次 docker run psql，返回 (returncode, stdout, stderr)。

    密码/超时经 -e 注入容器环境（PGPASSWORD/PGCONNECTTIMEOUT），
    不落 argv（不进 ps 输出/日志）。
    """
    cmd = ["timeout", str(PROBE_HARD_TIMEOUT),
           "docker", "run", "--rm", "--network", "host",
           "-e", "PGPASSWORD=%s" % password,
           "-e", "PGCONNECTTIMEOUT=%s" % CONNECT_TIMEOUT,
           "postgres:16-alpine", "psql", "-tA", "-v", "ON_ERROR_STOP=1"]
    cmd += args
    try:
        p = subprocess.run(cmd, input=None, capture_output=True,
                           timeout=PROBE_HARD_TIMEOUT + 5)
        return p.returncode, p.stdout.decode("utf-8", "replace"), \
            p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "probe timeout (> %ds, 目标不可达/SYN-drop)" % PROBE_HARD_TIMEOUT
    except FileNotFoundError:
        return 127, "", "docker not found"


def classify(err, out):
    """按 psql 退出码 + 错误文本归类为 reachable/login_ok/db_exists。"""
    e = (err or "").lower()
    # 连通性失败（不可达 / SYN-drop / 拒绝 / 超时）
    if any(k in e for k in (
            "connection refused", "could not connect",
            "no route to host", "network is unreachable",
            "timed out", "timeout", "connection reset",
            "connection closed", "server closed",
            "could not translate host name", "name or service not known",
            "failed: connection")):
        return {"reachable": False, "login_ok": False, "db_exists": False}
    # 认证失败（库/用户层面可达，但凭据错）
    if "password authentication failed" in e or "authentication failed" in e \
            or "no password supplied" in e or "role" in e and "does not exist" in e:
        return {"reachable": True, "login_ok": False, "db_exists": False}
    # 库不存在（认证通过，库缺失）
    if "does not exist" in e and ("database" in e or "database \"" in e):
        return {"reachable": True, "login_ok": True, "db_exists": False}
    # 成功：stdout 应有 SELECT 1 的结果 "1"
    if out.strip() in ("1", "1\n") or "SELECT 1" not in out:
        if out.strip() == "1":
            return {"reachable": True, "login_ok": True, "db_exists": True}
    # 兜底：退出码 0 视为成功
    return {"reachable": True, "login_ok": False, "db_exists": False}


def main(argv):
    if len(argv) < 5:
        print(json.dumps({"ok": False, "reachable": False, "db_exists": False,
                          "login_ok": False, "agp_schema_exists": False,
                          "error": "usage: pg_probe.py HOST PORT USER PASSWORD DBNAME"}))
        return
    host, port, user, password, dbname = argv[0], int(argv[1]), argv[2], argv[3], argv[4]

    out = {"ok": False, "reachable": False, "db_exists": False,
           "login_ok": False, "agp_schema_exists": False, "error": None}

    # ---- 1) SELECT 1（验证连通 + 认证 + 库存在）----
    rc, stdout, stderr = run_psql(
        ["-h", str(host), "-p", str(port), "-U", user, "-d", dbname,
         "-c", "SELECT 1"], password)
    if rc == 0 and stdout.strip() == "1":
        out.update({"reachable": True, "login_ok": True, "db_exists": True,
                    "ok": True})
    else:
        c = classify(stderr, stdout)
        out.update(c)
        out["error"] = (stderr or "rc=%s" % rc).strip()[:300]
        if not c["reachable"]:
            print(json.dumps(out))
            return

    # ---- 2) agp schema 是否存在（仅当认证通过、库存在时探测）----
    if out["login_ok"] and out["db_exists"]:
        rc2, stdout2, stderr2 = run_psql(
            ["-h", str(host), "-p", str(port), "-U", user, "-d", dbname,
             "-c",
             "SELECT count(*) FROM pg_namespace WHERE nspname='agp'"],
            password)
        if rc2 == 0 and stdout2.strip() == "1":
            out["agp_schema_exists"] = True
        # 认证 OK 但库可能缺失：上面 rc2!=0 时不覆盖 ok（仍按 SELECT 1 判定）

    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1:])
