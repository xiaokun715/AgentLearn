# SQL 沙箱镜像（设计说明书 §14 的 sql 变体）
#
# 构建：docker build -t agent-sandbox-sql:latest sandbox_images/sql
#
# 说明：V1 是 psql 客户端镜像。真正执行 SQL 需要一个目标数据库，
# 而默认 network=disabled —— 这正是安全姿态：沙箱里没有网络就谈不上连库 / 数据外泄。
# 若要执行 SQL，需要：network.enabled=true + allow_domains 指向数据库域名 + Egress Proxy（V2）。
FROM postgres:16-alpine

RUN mkdir -p /workspace/input /workspace/output \
    && chown -R postgres:postgres /workspace

WORKDIR /workspace

USER postgres

CMD ["psql", "--version"]
