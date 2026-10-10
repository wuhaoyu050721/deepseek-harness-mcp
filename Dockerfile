# 候选基础版本：真实 Linux Docker 构建/隔离验收仍须在装好 daemon 后执行。
# 该 tag 尚未解析为经过验证的 immutable digest。
FROM python:3.12.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/harness/src \
    HOME=/home/harness \
    DSH_HOME=/home/harness

# 单独的容器平台依赖锁；不要把宿主 MCP 依赖安装进模型执行环境。
COPY worker-requirements.lock /opt/harness/worker-requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/harness/worker-requirements.lock \
    && groupadd --gid 10001 harness \
    && useradd --uid 10001 --gid 10001 --home-dir /home/harness --no-create-home harness \
    && mkdir -p /home/harness /source /control /output \
    && chown harness:harness /home/harness /output

COPY src/harness_mcp /opt/harness/src/harness_mcp

USER 10001:10001
WORKDIR /source
ENTRYPOINT ["python", "-m", "harness_mcp.worker"]
