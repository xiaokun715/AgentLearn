# Python 沙箱镜像（设计说明书 §14）
#
# 构建：docker build -t agent-sandbox-python:latest sandbox_images/python
#
# 安全要点：
#   - 最小化：只保留 python 运行时，不装 requests 等网络库（默认 network disabled）
#   - 非 root：以 sandbox 用户运行（最小权限）
#   - 只读根文件系统由 DockerSandbox 用 read_only=True 强制
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 需要联网的 Policy 才需要 requests；默认不装，减小攻击面
# RUN pip install --no-cache-dir requests==2.32.0

RUN mkdir -p /workspace/input /workspace/output \
    && useradd -m -u 1000 sandbox \
    && chown -R sandbox:sandbox /workspace

WORKDIR /workspace

USER sandbox

CMD ["python", "-u"]
