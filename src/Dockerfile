# AI Agent Platform — Python 3.12 运行时 + Hermes CLI 支持（TASK-037）
#
# hermes 安装策略（TASK-037 / 需求5）：
#   - Dockerfile 创建 hermes wrapper（~/.local/bin/hermes），指向
#     /home/hermes/.hermes/hermes-agent/venv（由 compose 挂载宿主目录提供）。
#   - compose 挂载宿主 ~/.hermes → 容器 /home/hermes/.hermes（HERMES_HOME 卷），
#     以及 ~/.local/share/uv → 容器 /home/hermes/.local/share/uv（venv python 符号链接目标）。
#   - .env 不进镜像（DECISION-004 密钥纪律）：.env 在宿主 ~/.hermes/ 下，
#     经 bind-mount 运行时注入，容器文件系统无密钥。
#   - 双模式兼容：未挂载 ~/.hermes 的纯 SQLite 部署 → hermes_available()=False
#     → /api/hermes/* 返回 503，custom agent 完全不受影响（AC-H7）。
FROM python:3.12-slim

WORKDIR /app
ENV TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERMES_HOME=/home/hermes/.hermes \
    PATH=/home/hermes/.local/bin:${PATH}

# 依赖先装，利用层缓存（Aliyun 镜像加速，规避 PyPI 直连超时）
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    -r requirements.txt

# hermes CLI wrapper（TASK-037）：与宿主 ~/.local/bin/hermes 一致。
# venv 实际在 /home/hermes/.hermes/hermes-agent/venv/（compose 挂载宿主目录），
# venv/bin/python 符号链接指向 /home/hermes/.local/share/uv/python/（也需挂载）。
# 本 wrapper 只在运行时 hermes-agent 目录存在时才可用（shutil.which 探测）。
RUN mkdir -p /home/hermes/.local/bin && \
    printf '#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\nexec "/home/hermes/.hermes/hermes-agent/venv/bin/python" "/home/hermes/.hermes/hermes-agent/hermes" "$@"\n' \
    > /home/hermes/.local/bin/hermes && \
    chmod +x /home/hermes/.local/bin/hermes

# TASK-037: 以 hermes 用户（uid/gid 1000，与宿主一致）运行——挂载的宿主
# ~/.hermes 属主为宿主 hermes 用户；若容器以 root 运行，hermes CLI 在宿主
# 目录写入的文件会是 root:root，反噬宿主 hermes 用户的读写。uid/gid 固定
# 1000 与本服务器宿主一致（单机部署场景；换主机时按宿主 hermes 用户调整）。
# 建用户 + 切 USER 必须在 COPY 代码之前，保证 /app 属主为 hermes。
# T-AGP-NAMEDVOL（1.5.x）：预先创建 /app/data 并一并 chown 给 hermes（uid 1000）。
# 原因：sqlite 数据文件写 /app/data/agp.db（compose 环境变量 SQLITE_PATH 固定），
# 而 /app/data 默认由 named volume agpdata 挂载。named volume 首次挂载时，Docker
# 会把镜像里同路径目录的内容（含属主）复制进卷——因此必须让镜像内 /app/data
# 属主 = hermes，否则卷以 root:root 挂载 → 容器以 uid 1000 运行无法写入 →
# sqlite fail-fast 退出。bind 卷（./src/data 覆盖）不受此影响（宿主目录已存在）。
RUN groupadd -g 1000 hermes 2>/dev/null; useradd -u 1000 -g 1000 \
    -d /home/hermes -s /bin/bash hermes 2>/dev/null; \
    mkdir -p /app/data && \
    chown -R 1000:1000 /home/hermes/.local /app
USER hermes

# 代码（core.app 从 /app import；USER hermes 之后 COPY，属主 = hermes）
COPY . .

EXPOSE 8099

# 健康检查
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=15s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8099/healthz', timeout=4)" || exit 1

CMD ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8099", "--log-level", "info"]
