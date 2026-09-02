# guardrails-middleware

> AI Security Middleware Demo —— **Input / Context / Tool / Tool Result / Output 统一安全边界：Detect + Policy + Action + Audit + Metrics**

对应设计说明书：`第九章：Guardrails 安全中间件设计说明书.md`

## 一句话定位

> **Prompt 告诉模型应该怎么做，Guardrails 决定系统允许它做什么。**

Prompt 是 *Soft Constraint*，LLM 仍可能产出 `delete_database()` 之类的 Tool Call；
Guardrails 是 *Hard Boundary*，在代码层把危险动作挡在 Tool 执行之前。

```
LLM Tool Call ──▶ Guardrails ──▶ BLOCK ──▶ Tool 永远不会执行
```

---

## 01. 四道安全边界（设计说明书 §41）

```
             User Input
                 │
        [Boundary #1]  Input Guardrail        Prompt Injection / PII / 恶意输入
                 │
             Agent / LLM
                 │ Tool Call
        [Boundary #2]  Tool Guardrail         Allowlist / 权限 / 风险 / 参数 Schema
                 │
        [Boundary #3]  Tool Sandbox           （由 agent-tool-sandbox 提供执行隔离）
                 │ Tool Result
        [Boundary #4]  Tool Result Guard      Indirect Prompt Injection → SANITIZE
                 │
             Context Assembly ──▶ LLM ──▶ Output Guardrail（PII/Secret/Schema）──▶ 用户
```

组件职责（与兄弟项目 §40）：

| 组件                  | 核心问题               | 项目 |
| ---                  | ---                  | --- |
| **Guardrails**       | 这个请求 / Tool / 内容是否允许？ | 本 Demo |
| **Context Assembly** | 什么信息应该进入模型 Context？   | （前序 Demo） |
| **Tool Sandbox**     | 即使允许执行，实际最多能做什么？   | agent-tool-sandbox |

---

## 02. 核心概念

| 概念 | 说明 | 设计说明书 |
| --- | --- | --- |
| GuardrailContext | 统一请求上下文（request_id / stage / content / tool...） | §6 |
| SecurityFinding | Detector 只回答「发现了什么」 | §7 |
| Action | allow / block / redact / sanitize / retry / human_approval | §8 |
| Detector | PII / Injection / Secret / Toxicity / Schema，插件化 | §9~§12 |
| Redaction Engine | PHONE → `<PHONE_REDACTED>` | §13 |
| Policy Engine | 同一 Finding 在不同 Stage 有不同策略 | §14~§15 |
| Action Priority | BLOCK > APPROVAL > RETRY > SANITIZE > REDACT > ALLOW | §15 |
| Security Pipeline | Normalize → Detect → Policy → Action → Audit | §16~§17 |
| Tool Guardrail | Allowlist → 权限 → 风险 → 参数 Schema → 资源边界 | §18~§21 |
| Tool Result Guard | 外部内容默认不可信，注入指令 SANITIZE 后再进 Context | §22~§24 |
| Output Guardrail | PII/Secret 防泄露；JSON Schema 不符 → RETRY | §25~§27 |
| Human Approval | 高风险 Tool 生成票据，人工 Approve/Reject/过期 | §28 |
| Audit + Metrics | 每次 Allow/Block/Redact... 都可追溯、可观测 | §29、§38 |

---

## 03. 快速开始（零外部依赖）

```bash
# 纯 Python：FastAPI + PyYAML，无 DB / Redis / Docker
cd guardrails-middleware
uvicorn app.main:app --port 8000
# 或 python debug.py（可打断点）
```

三个核心 API（§30~§32）：

```bash
# 1) Input Guardrail：手机号 → REDACT
curl -X POST http://localhost:8000/v1/guardrails/input \
  -H "Content-Type: application/json" \
  -d '{"agent":"fault_diagnosis","content":"我的手机号是13812345678"}'
# → {"action":"redact","content":"我的手机号是<PHONE_REDACTED>", ...}

# 2) Tool Guardrail：delete_file 越界 → BLOCK
curl -X POST http://localhost:8000/v1/guardrails/tool \
  -H "Content-Type: application/json" \
  -d '{"agent":"environment_recovery","tool":"delete_file",
       "arguments":{"path":"/etc/passwd"}}'
# → {"action":"block","risk":"CRITICAL",
#    "reason":"invalid arguments: arguments.path: Path is outside allowed resource boundary"}

# 3) Output Guardrail：LLM 泄露 API Key → BLOCK
curl -X POST http://localhost:8000/v1/guardrails/output \
  -H "Content-Type: application/json" \
  -d '{"agent":"fault_diagnosis","content":"api_key = sk-xxxxxxxxxxxxxxxxxxxx"}'
# → {"action":"block", "findings":[{"category":"SECRET","severity":"CRITICAL"}]}
```

更多端点（§22 / §28 / §29）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/guardrails/context` | 外部内容进 Context 前过滤 |
| POST | `/v1/guardrails/tool_result` | Tool 返回（间接注入防线） |
| GET | `/v1/guardrails/approvals?status=PENDING` | 人工审批票据 |
| POST | `/v1/guardrails/approvals/{id}/approve` | 放行高风险 Tool |
| POST | `/v1/guardrails/approvals/{id}/reject` | 拒绝 |
| GET | `/v1/guardrails/events` | 安全审计事件 |
| GET | `/v1/guardrails/tools` | Tool Allowlist 视图 |
| GET | `/v1/guardrails/policy` | 当前策略视图 |
| GET | `/metrics` | Prometheus 指标（§38） |
| GET | `/healthz` | 健康检查 |

---

## 04. 一键跑通 8 个实验（§37）

```bash
python experiments/run_experiments.py
# [PASS] demo1_pii_input_redact
# [PASS] demo2_prompt_injection_block
# [PASS] demo3_secret_leak_block
# [PASS] demo4_tool_permission_block
# [PASS] demo5_argument_attack_block
# [PASS] demo6_indirect_injection_sanitize
# [PASS] demo7_human_approval
# [PASS] demo8_output_schema_retry
# 8/8 experiments passed
```

单独示例：

```bash
python examples/input_guard.py          # PII 脱敏 + Prompt Injection 拦截
python examples/tool_guard.py           # 权限 / 参数攻击 / 人工审批
python examples/indirect_injection.py   # ⭐ 间接注入 → Tool Result Guard
python examples/output_guard.py         # Secret 泄露拦截 + Schema RETRY
```

跑测试：

```bash
pip install -e .[test]
pytest -q     # 54 tests
```

---

## 05. 本项目最值得看的一条链路：Indirect Prompt Injection（Demo 6）

```text
User ─▶ Agent ─▶ web_search ─▶ 恶意网页        （网页里写着
   "Ignore previous instructions."）              ┌──────────────────┐
        │ Tool Result                            │ External Data =   │
        ▼                                        │ UNTRUSTED（默认）  │
   Tool Result Guard                             └──────────────────┘
        │ Injection Detector ─▶ PROMPT_INJECTION
        ▼
   Policy（TOOL_RESULT）──▶ SANITIZE
        │  注入指令被替换为 [injected-instruction-removed]
        ▼
   Context Assembly ─▶ LLM                       （安全内容才可进 Context）
```

```bash
python examples/indirect_injection.py
# [tool]   action=allow allowed=True
# [guard]  action=sanitize blocked=False  injection phrases neutralized = True
# [context] content passed to Context Assembly (188 chars)
```

它演示了 Agent Security 与 Web/API Security 的本质差别：
外部数据永远不应直接成为 Agent Instruction —— 先经过 **Tool Result Guard**。

---

## 06. 策略配置化（原则五）

策略全部来自 YAML，改策略不改代码（`configs/policies.yaml`）：

```yaml
stages:
  INPUT:
    PHONE:            { action: redact }
    PROMPT_INJECTION: { action: block }
    TOXICITY:         { action: block }
  TOOL_RESULT:
    PROMPT_INJECTION: { action: sanitize }   # 间接注入：中和后放行
    SECRET:           { action: redact }
  OUTPUT:
    SECRET:           { action: block }
    SCHEMA_MISMATCH:  { action: retry, max_retries: 2 }
```

核心设计：**同一个 Finding 在不同 Stage 拥有不同策略**（§14）。

Tool 安全（`configs/tools.yaml`）：

```yaml
tools:
  delete_file:
    allowed_agents: [environment_recovery]
    risk_level: CRITICAL
    require_approval: true
    schema:
      type: object
      required: [path]
      properties:
        path:
          type: string
          pattern: "^/tmp/.*"
          message: "Path is outside allowed resource boundary"
```

`Tool Call` 检查顺序（§19）：Allowlist → Agent Permission → Risk Policy
→ Argument Schema → Resource Policy → Action（BLOCK / HUMAN_APPROVAL / ALLOW）。

换一套策略目录启动：

```bash
GUARDRAILS_CONFIG_DIR=/path/to/configs uvicorn app.main:app --port 8000
```

---

## 07. 架构分层

```
GuardrailsService (Agent SDK, §33)
      │ check_input / check_tool / check_tool_result / check_output
      ▼
┌─────────────────────────── Security Pipeline (§16) ──────────────────────────┐
│  GuardrailContext ─▶ Detectors ─▶ Findings ─▶ PolicyEngine ─▶ ActionExecutor │
│                          (PII/Injection/Secret/           (ALLOW/BLOCK/…)     │
│                           Toxicity/Schema)                Redactor/Sanitizer  │
└──────────────┬──────────────────────────────┬────────────────────────────────┘
               ▼                              ▼
        AuditRepository（§29）          ApprovalService（§28）
        SecurityEvent → /events         PENDING/APPROVED/REJECTED/EXPIRED
               ▼
        Metrics（§38）→ /metrics
```

Tool 侧独立一条链（不经文本 Detector）：`ToolCallValidator`（§19）。

---

## 08. 为什么必须独立成 Middleware（§02、§42）

| 方案 | 性质 | 可靠性 |
| --- | --- | --- |
| System Prompt「不要泄露隐私 / 不要删库」 | Soft Constraint | ❌ 模型仍可能产出危险 Tool Call |
| 独立 Middleware 在边界校验 | Hard Boundary | ✅ 代码层拦截，Tool 永不执行 |

工程原则（§39）：手机号/Email/API Key/JSON Schema/Tool Allowlist 等**确定性判断**
一律用 Regex / Parser / Schema / Rule，绝不全交给 LLM——否则 Guardrails 自己就变成
一个高延迟、高成本、不稳定的 LLM 系统。

---

## 09. 目录结构

```
guardrails-middleware/
├── app/
│   ├── api/            # /v1/guardrails/*（input/context/tool/tool_result/output/approvals/events）
│   ├── core/           # GuardrailContext / SecurityFinding / Action / Pipeline / exceptions
│   ├── detectors/      # base + pii / injection / secret / toxicity / schema
│   ├── policies/       # engine（stage+category→action）/ loader / models
│   ├── tools/          # ToolRegistry / permission / risk / loader
│   ├── validators/     # tool / argument / output / json-schema 子集
│   ├── actions/        # executor + rewrite（Redactor/Sanitizer）
│   ├── approval/       # ApprovalService（§28）
│   ├── audit/          # SecurityEvent + AuditRepository（§29）
│   ├── metrics.py      # 计数器 + P50/P95/P99（§38）
│   ├── service.py      # Guardrails 门面 / Agent SDK（§33）
│   ├── factory.py      # 从 configs/*.yaml 装配
│   └── main.py         # FastAPI 入口
├── configs/            # guardrails.yaml / policies.yaml / tools.yaml
├── tests/              # 54 tests
├── examples/           # input_guard / tool_guard / indirect_injection / output_guard
├── experiments/        # run_experiments.py —— 8 个实验一键验收
├── docker-compose.yml
└── README.md
```
