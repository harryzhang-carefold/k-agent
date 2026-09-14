# AI Agent Platform — Python 3.12 运行时
FROM python:3.12-slim

WORKDIR /app
ENV TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 依赖先装，利用层缓存（Aliyun 镜像加速，规避 PyPI 直连超时）
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    -r requirements.txt

# 代码（core.app 从 /app import）
COPY . .

EXPOSE 8099

# 健康检查
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=15s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8099/healthz', timeout=4)" || exit 1

CMD ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8099", "--log-level", "info"]
