# Agent Tool Sandbox

让 Agent 在受控沙箱中安全执行 **Python / Shell / SQL**，但拿不到宿主机权限。

对应设计说明书《第六章：工具执行沙箱设计说明书》，属于 Agent Runtime / AI Infra 系列 Demo 的**第 07 个项目**，与前面第 05 个 `async-agent-job-queue`（Job Queue）、第 03/04 个（LLM 流式 / 语义缓存）可串联成完整的 Agent Runtime。

---

## 一句话核心

> **Never trust model-generated code.**

LLM 输出一律视为 `UNTRUSTED INPUT`，执行必须走：

```
LLM → Tool Call → Policy → Sandbox → Result
```

而不是 `LLM → shell_exec()`。

并且，**Policy 与 Sandbox 是两层**：

| 层 | 回答的问题 | 解决什么 |
|---|---|---|
| **Policy（Authorization）** | 能不能做？ | What is Allowed |
| **Sandbox（Isolation）** | 做了也不能影响谁？ | Runtime Isolation |

> Docker 已经隔离了，为什么还需要 Policy Engine？
> Docker 解决 Runtime Isolation；Policy 解决 What is Allowed。两者不是一层。

---

## 架构

```
                    Agent
                      │ Tool Call
                      ▼
              ┌───────────────┐
              │ Policy Engine │   ← Agent 可以请求能力，但不能决定最终权限（§10）
              └───────┬───────┘
                ALLOW │        └ DENY
                      ▼               ▼
              Sandbox Manager    REJECTED
                      │
                      ▼
              Ephemeral Runtime（1 execution = 1 runtime，用完销毁）
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
        CPU         Memory      Timeout
          │           │           │
          └───────────┼───────────┘
                      ▼
            Filesystem Boundary   Network Egress
              (Default Deny)      (默认 disabled + allow-list)
                      │
                      ▼
                   Result → Agent
```

---

## 快速开始（零依赖）

默认 **SQLite 持久化 + ProcessSandbox 兜底**，不需要 Docker 就能跑通全部安全测试：

```bash
pip install -e ".[test]"

# 启动
uvicorn app.main:app --port 8000

# 换个终端跑一个正常代码
python examples/python.py

# 跑全部恶意样本，验证它们全被拦下
python examples/malicious.py
```

### 用 Docker 沙箱（推荐的生产路径）

```bash
# 1. 构建沙箱镜像
docker build -t agent-sandbox-python:latest sandbox_images/python
docker build -t agent-sandbox-node:latest  sandbox_images/node
docker build -t agent-sandbox-sql:latest   sandbox_images/sql

# 2. 用 Docker 后端启动（auto 会自动探测，无 Docker 退化为 process）
SANDBOX_BACKEND=docker uvicorn app.main:app --port 8000

# 或者一键起整套（API + PostgreSQL + Redis）
docker compose up --build sandbox-api
```

---

## API（§7 / §22）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/executions` | 创建执行 `{type, code, policy?}` → `{execution_id, status: queued}` |
| `GET` | `/v1/executions/{id}` | 查询状态与结果 `{stdout, stderr, exit_code, duration_ms, resource_usage}` |
| `POST` | `/v1/executions/{id}/kill` | Kill Switch（**幂等**，重复调用不报 500） |
| `GET` | `/v1/executions` | 列出最近执行 |
| `GET` | `/v1/policies` | 列出服务端策略 |

```bash
curl -X POST http://127.0.0.1:8000/v1/executions \
  -H "Content-Type: application/json" \
  -d '{"type":"python","code":"print(sum(range(100)))"}'
# {"execution_id":"exec_xxx","status":"queued"}
```

身份通过请求头传递（用于审计，§33）：`x-tenant-id` / `x-user-id` / `x-agent-id`。

---

## 状态机（§8）

```
QUEUED → POLICY_CHECK → STARTING → RUNNING → SUCCEEDED
                              ├─ REJECTED（策略拒绝）
                              ├─ FAILED
                              ├─ TIMEOUT（超时后必杀 runtime）
                              ├─ OOM（内存越限）
                              ├─ KILLED（kill switch）
                              └─ OUTPUT_LIMIT_EXCEEDED
```

---

## Policy：整个项目的核心（§10-13）

Agent 提交的 `policy` 只是**请求**，最终权限由服务端持久化的 Policy 决定：

```yaml
# python_basic（内置默认策略）
name: python_basic
resources:
  cpu: 0.5
  memory_mb: 256
  timeout_seconds: 10
  pids: 64          # 防 fork bomb（§20）
  disk_mb: 100
  output_kb: 512    # 防输出爆炸（§24）
filesystem:
  read: [/workspace/input]
  write: [/workspace/output]
network:
  enabled: false    # 默认全隔离（§17）
syscalls:
  profile: restricted
```

管道：

```
Policy（声明式安全策略）→ PolicyCompiler → SandboxConfig（底层 Runtime 配置）
```

- Agent 请求 `memory_mb > 上限` → **REJECTED**（而不是悄悄 clamp，保证可审计）。
- Agent 请求 `network: true` 但 Policy 是 disabled → **REJECTED**。
- 就算 Policy 允许网络，也必须过 **Egress allow-list**：私网 `10.x` / `192.168.x`、回环 `127.0.0.1`、云 metadata `169.254.169.254` **永远禁止**（§18）。不提供 `network=true` 的等价物。

---

## 沙箱安全参数（§14）

Docker 后端把 `SandboxConfig` 翻译成这些安全参数，每个都有安全意义：

```python
containers.create(
    image="agent-sandbox-python:latest",
    command=["python", "/workspace/main.py"],
    mem_limit="256m",              # 内存上限 → OOM
    nano_cpus=500_000_000,         # CPU 上限
    pids_limit=64,                 # PID 上限 → 防 fork bomb
    network_disabled=True,         # 网络全关
    read_only=True,                # 根文件系统只读
    tmpfs={"/tmp": "size=100m"},   # 临时盘上限
    cap_drop=["ALL"],              # 丢光全部 capability
    security_opt=["no-new-privileges:true"],
    user="sandbox",                # 非 root
    binds=[f"{workspace}:/workspace"],  # 只挂工作区，绝不挂宿主机
)
```

**Filesystem Boundary（§15-16，Default Deny）**：Agent 只能碰 `/workspace`，容器根文件系统只读。绝不 `-v /:/host`。

**Ephemeral Runtime（§25）**：`1 execution = 1 ephemeral runtime`，执行完 `destroy()` 强制回收，杜绝跨执行数据泄漏：

```
CREATE → START → RUN → COLLECT → CLEANUP → DESTROY
```

---

## 安全测试（§29-32，`tests/`）

| 测试 | 攻击 | 防线 | 预期 |
|---|---|---|---|
| `test_timeout` | `while True: pass` | Timeout → **真 kill runtime**（不是只停止等待） | `TIMEOUT` |
| `test_memory` | 无限 append | ResourceMonitor 越限 kill | `OOM` |
| `test_filesystem` | `open("/host-secret")` | 静态规则 + 只读根 + 只挂 /workspace | `REJECTED` |
| `test_network` | `requests.get(...)` | network disabled + egress allow-list | `REJECTED` |
| `test_escape` | `os.system` / `ctypes` / `os.fork` | 静态规则 + syscall 白名单 | `REJECTED` |
| `test_escape` | fork bomb（PID limit） | pids_limit + ResourceMonitor | `FAILED` |
| `test_escape` | 输出爆炸 | 有界管道采集 + 超限 kill | `OUTPUT_LIMIT_EXCEEDED` |
| `test_kill` | 长任务 | Kill Switch（幂等） | `KILLED` |

```bash
pytest -q            # 58 passed, 1 skipped（真实 fork bomb 测试在 Linux/Docker 上跑）
```

---

## 目录结构（§6）

```
agent-tool-sandbox/
├── app/
│   ├── api/            # FastAPI 路由（execution.py / schemas.py）
│   ├── domain/         # Execution / Policy / Result / 状态机
│   ├── policy/         # rules（静态扫描）→ engine（决策）→ compiler（翻译成 SandboxConfig）
│   ├── sandbox/        # base 抽象 / docker 实现 / process 兜底 / manager（kill + 并发）
│   ├── runtime/        # executor（生命周期）/ resource（监控）/ timeout
│   ├── filesystem/     # boundary（Default Deny 工作区）
│   ├── network/        # egress（allow-list）
│   ├── security/       # identity / audit
│   ├── storage/        # execution / policy / audit（memory · sqlite · postgres）
│   ├── service.py      # 编排（§38 Agent Loop）
│   ├── factory.py      # 装配
│   └── main.py         # create_app
├── sandbox_images/     # python / node / sql 沙箱镜像（非 root、最小攻击面）
├── tests/              # 8 个安全测试文件
└── examples/           # python.py / shell.py / malicious.py
```

---

## 三个 Phase 的演进（§40）

| Phase | 内容 | 本仓库状态 |
|---|---|---|
| **V1 Docker** | FastAPI + Docker + Resource + Security | ✅ 已实现（含 process 兜底） |
| **V2 Kubernetes** | Namespace / Pod / ResourceQuota / NetworkPolicy / SecurityContext | 设计好抽象（`Sandbox` ABC），可插 `KubernetesSandbox` |
| **V3 MicroVM / WASM** | Firecracker / gVisor / WASM | 同样是换一个 `Sandbox` 实现 |

> 核心收益：**Agent / 上层完全不用改** —— 抽象把 Docker 换成 Kubernetes / Firecracker 时，接口不变。

---

## 与前面 Demo 串起来（§36-38）

```
User → Job API → Job Queue（05）→ Worker → Agent → Tool Call
                                                    │
                              ┌─────────────────────┤
                              ▼                     ▼
                         LLM Gateway（03/04）   Policy Engine
                                                   ▼
                                              Sandbox Manager
                                                   ▼
                                              Ephemeral Runtime
                                                   ▼
                                               Result → Agent → Final Answer
```

这就是经典的 **Code → Execute → Observe → Re-plan** Agent Loop。

---

## 已知限制（诚实说明）

1. **ProcessSandbox 只是学习兜底**：没有 namespace，Windows 上 `/workspace` 路径映射不生效，文件系统边界靠静态规则 + 只读根（Docker 才真正隔离）。生产必须用 Docker / gVisor / Firecracker。
2. **Egress per-domain 放行是 V2**：V1 的 egress 在决策层校验白名单并把 `network_disabled` 传给 Docker；真正的域名级代理放行（Egress Proxy）留到 V2。
3. **SQL 工具是 psql 客户端 stub**：默认 network=disabled，沙箱里连不上库 —— 这正是安全姿态；要执行 SQL 需 allow-list + Egress Proxy。
4. **Docker 输出上限是后置截断**（收集后 truncate + kill），超大输出会先缓冲在日志驱动；ProcessSandbox 则是有界管道即时截断。生产可用 attach + 流式截断。

---

## 面试题速查（§42）

1. 为什么 Agent 执行代码不能直接 `exec`？ → Untrusted input，需要 Isolation + Authorization 两层。
2. Docker 容器和真正的安全沙箱有什么区别？ → 共享内核，需 cap_drop / seccomp / no-new-privileges；更强要 gVisor / Firecracker。
3. 为什么需要 Ephemeral Runtime？ → 防跨执行数据泄漏、防资源残留。
4. 怎么限制 CPU / Memory / Disk / PID？ → cgroup / Docker mem_limit / nano_cpus / pids_limit / tmpfs size / output_kb。
5. 怎么限制访问宿主机文件？ → 只读根 + 只挂 /workspace + Default Deny。
6. 怎么限制网络？ → 默认 network_disabled + allow-list + 拒绝私网/metadata。
7. 为什么 allow-list 比 network=true 安全？ → Network Least Privilege。
8. Policy Engine 为什么不能让 Agent 自己决定权限？ → Authorization 在服务端，Agent 只能请求能力。
9. Policy Compiler 解决什么问题？ → 把声明式策略翻译成底层 Runtime 配置。
10. 容器被打穿怎么办？ → 最小权限 + 无持久化 + Ephemeral + 审计 + 可重建。
11. Docker / K8s / gVisor / Firecracker / WASM 怎么选？ → 隔离强度 vs 性能 vs 启动速度的权衡。
12. 超时后怎么保证进程真被杀？ → `wait_for` 超时后必须 `sandbox.kill()`，而不是只停止等待（见 `runtime/timeout.py`）。
13. 怎么实现 Kill Switch？ → `POST /kill` → manager → sandbox.kill（幂等，§23）。
14. 怎么防恶意代码读其他 Agent 数据？ → 1 execution = 1 ephemeral runtime。
15. 怎么审计一次 Tool Execution？ → tenant/user/agent + policy 版本 + image + command + 资源用量 + 结果（`security/audit.py`）。

---

## License

仅供学习使用（学习项目）。
