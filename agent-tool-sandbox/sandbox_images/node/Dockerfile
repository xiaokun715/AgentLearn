# Node.js 沙箱镜像（设计说明书 §14 的 node 变体）
#
# 构建：docker build -t agent-sandbox-node:latest sandbox_images/node
FROM node:20-alpine

ENV NODE_ENV=production

RUN mkdir -p /workspace/input /workspace/output \
    && addgroup -S sandbox && adduser -S -G sandbox sandbox \
    && chown -R sandbox:sandbox /workspace

WORKDIR /workspace

USER sandbox

CMD ["node"]
