# 5G-AgentBench 评测系统设计说明书 (工程落地方案)

**系统名称：** 5G-AgentBench (面向 5G 基带多智能体测试平台的自动化评测基准系统)

**对应业务系统：** AI4Test 5G 基带测试多智能体平台 + 5G 集成验证部门知识库系统

**文档版本：** v2.4.0 (Production-Ready)

**核心特性：** 双核解耦 (Agent + RAG)、混合数仓 (PostgreSQL + ClickHouse)、VCR 录制回放、状态断言主导、配置包指纹化

---

## 一、 项目概述与业务背景

### 1.1 背景与痛点

在 5G NR 物理层（L1/PHY）软件测试中，系统需支持 3GPP 38.211/38.214 协议规范验证、软硬件耦合排障与长链路测试自动化。AI4Test 平台构建了包含 **10 个领域子 Agent**、**16 个可插拔底层 Skill**、**7 类 RAG 异构检索策略** 以及 **两条自愈回溯链路（$L \to F$ 环境自愈、$M \to D$ 用例优化）** 的复杂网络。

工程落地中面临三大核心痛点：

1. **“打地鼠”式负向回归（Regression）：** 修复一个 Sub-6G 速率不达标的诊断 Bad Case，可能导致原有 5 个已调优场景隐蔽崩溃；
2. **故障归因黑盒（Blackbox Attribution）：** 物理层测试失败交织着真实基带代码 Bug、板卡射频时钟失步（RF Clock Drift）、知识库检索召回缺失，传统端到端测试无法量化定位根因；
3. **高昂的长链路开销与非确定性：** 单条用例涉及多步交互与硬件仿真，人工测试无法支撑日常高频 CI/CD 提交。

### 1.2 系统建设目标

构建面向 AI4Test 与 5G 知识库的自动化 Benchmark 体系，实现：

* **质量防线：** 嵌入 GitLab CI/CD，提供 PR 级 5 分钟轻量门禁与每日 Nightly 深度回归；
* **客观度量：** 建立包含“任务规划、工具调用、知识检索、测试生成、结果分析、自愈回溯”的 6 维量化标尺；
* **数据闭环：** 实现线上 Failure 到线下 Golden Benchmark 的自动化流转，驱动 Prompt 与模型微调持续进化。

---

## 二、 系统总体架构设计

系统采用 **“四层解耦、双核驱动、闭环联动”** 的工业级分层架构：

```
┌────────────────────────────────────────────────────────────────────────┐
│                        业务与 CI/CD 接入层 (Ops & Pipelines)            │
│   PR 级 Smoke 门禁 (5min)  │  Nightly 回归流水线  │  模型/策略升级对比看板    │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼─────────────────────────────────────┐
│                   统一断言与度量中枢 (Metrics & Evaluation Engine)       │
│  ┌──────────────────────────────┐    ┌───────────────────────────────┐ │
│  │     RAG 度量模块 (Ragas/TRULENS)│    │     Agent 度量模块 (AgentBench) │ │
│  │ • 策略路由命中率 (Routing Hit)│    │ • 任务达成率 (Task Success)   │ │
│  │ • 检索召回 Context Recall@K  │    │ • 规划拓扑相似度 (DAG Edit)   │ │
│  │ • 上下文精准度 Context Prec. │    │ • 工具调用精度 (Tool Calling) │ │
│  │ • 5G协议忠实度 (Faithfulness)│    │ • 自愈成功率 (Self-Healing)   │ │
│  └──────────────┬───────────────┘    └───────────────┬───────────────┘ │
│                 │                                    │                 │
│                 └─────────────────┬──────────────────┘                 │
│                                   │ (根因归因分流: RAG vs. Agent)       │
├───────────────────────────────────┼────────────────────────────────────┤
│                       双核评测执行引擎 (Dual-Core Runner)                │
│  ┌──────────────────────────────┐ │  ┌───────────────────────────────┐ │
│  │         RAG 评测执行器        │ │  │        Agent 评测执行器        │ │
│  │ • 7类策略隔离跑 (Vector/SQL..)│ │  │ • 10个子Agent多轮调度模拟     │ │
│  │ • 8大协议知识库切分检索      │ │  │ • 16个可插拔Skill沙箱调用     │ │
│  │ • 多模态Chunk (表格/图)比对   │ │  │ • 异常/故障注入 (Chaos Tool)  │ │
│  └──────────────▲───────────────┘ │  └───────────────▲───────────────┘ │
│                 │                 │                  │                 │
├─────────────────┼─────────────────┴──────────────────┼─────────────────┤
│                 └─────────────────┬──────────────────┘                 │
│                                   │                                    │
│                     数据资产与环境沙箱层 (Assets & Mock Environments)     │
│  • 8个5G协议领域知识切片 (38.211/38.214/PHY架构/Markdown/Mermaid图)      │
│  • Golden Trajectory (标准规划DAG) & Golden State (沙箱SQLite/Jira状态)│
│  • 外部硬件环境 Mock (仪表/板卡/SSH/日志Dump，支持超时与错误状态重放)     │
└────────────────────────────────────────────────────────────────────────┘

```

### 核心分层说明

1. **沙箱与 Mock 层（Sandbox & Mock）：** 采用类 VCR 流量录制回放机制，将 16 个 Skill 的入参 Hash 与底层 BBU 板卡、Keysight 仪表的 Log/状态码快照绑定，剔除硬件通信抖动；
2. **双核评测引擎（Dual-Core Runner）：** 支持 10 个子 Agent 的状态机多轮调度，并支持 7 类 RAG 检索策略（Vector, Keyword, SQL, Graph, Routing 等）独立脱机评测；
3. **断言与归因中枢（Assertion & Attribution）：** 废弃脆弱的纯文本比对，采用“状态断言为主、轨迹比对为辅”；当用例失败时，根据关联矩阵自动分流判断是 RAG 召回缺失还是 Agent 逻辑缺陷；
4. **混合存储遥测层（Hybrid Telemetry Storage）：** PostgreSQL 承载用例资产与树状 JSON；ClickHouse 展平承载单步原子遥测，支撑秒级看板与长周期 OLAP 分析。

---

## 三、 评测指标体系设计

| 评测维度 | 核心量化指标 | 指标定义与判定标准 | 业务与工程价值 |
| --- | --- | --- | --- |
| **1. 任务规划 (Planning)** | DAG 编辑距离、步骤有效率 | $\text{有效步数} / \text{实际总步数}$；与基线拓扑比对 | 拦截死循环、冗余规划及工具滥用 |
| **2. 工具调用 (Tool Calling)** | Schema 合法率、必调/禁调遵从率 | 16 个 Skill 入参格式校验；$\text{Must-Call} \subseteq \text{Calls}$ 且 $\text{Never-Call} \cap \text{Calls} = \emptyset$ | 杜绝关键排障工具被绕过或误触发高危物理操作 |
| **3. 知识检索 (RAG)** | 路由命中率、多模态召回率、忠实度 | Routing Accuracy; Context Recall@K; Faithfulness | 确保协议参数查表精确，杜绝 5G 计算公式幻觉 |
| **4. 结果诊断 (Diagnosis)** | $K$ 节点三路混淆矩阵、分类准确率 | 环境问题 (ENV) / 设计问题 (DESIGN) / 缺陷 (DEFECT) 准确率 | 确保故障分流精准，防止误判导致无效提单 |
| **5. 动态自愈 (Self-Healing)** | 回溯成功率、平均自愈耗时 | $L \to F$ 及 $M \to D$ 回溯重跑后的通过率；自愈步数 | 评估复杂异常场景下的鲁棒性与容错恢复能力 |
| **6. 终态断言与成本** | 状态断言达成率、Token / 延迟开销 | Jira 缺陷单字段、物理层根因与报告落盘校验；P95 Latency | 生产交付可用性（Production-Ready）验收底线 |

---

## 四、 数据库存储架构设计

数据库系统采用 **12 张表** 组成的分层管理模型：

### 4.1 核心表格职责矩阵

* **资产与版本控制表（PostgreSQL）：**
* `prompt_asset_version`：10 个子 Agent 的 System Prompt 及内容 SHA256 哈希；
* `skill_asset_version`：16 个底层工具的 OpenAPI/JSON Schema 契约定义；
* `rag_snapshot_version`：8 大协议库切片只读索引快照版本（S3 URI）；
* `topology_asset_version`：多智能体 DAG 拓扑转移规则与回溯上限；
* `system_config_bundle`：★ **系统基线大包（核心表）**，级联聚合上述配置并生成全局唯一 `bundle_hash`。


* **评测用例与批次管理表（PostgreSQL）：**
* `benchmark_case_asset`：与 Git 同步的黄金用例元数据及沙箱初态；
* `benchmark_batch`：CI/CD 批次总控（记录通过率、总消耗 Token、Commit ID）；
* `case_trajectory_record`：★ **用例执行明细表（核心表）**，内嵌时序树状 JSON；
* `failure_triage_record`：跑挂用例的归因、复盘与数据飞轮流转。


* **遥测与高并发分析表（ClickHouse）：**
* `benchmark_multiagent_step_trajectory`：★ **单步展平遥测表**，记录原子 Step 耗时、Token、自愈标记；
* `benchmark_rag_metrics`：7 类 RAG 策略细粒度分值；
* `agg_model_benchmark_daily`：模型版本与每日胜率对比宽表。



### 4.2 树状 JSON 结构规范 (`case_trajectory_record.trajectory_tree`)

```json
{
  "trace_id": "e7c2f821-4f1b-4d7a-8f2c-5b23d9a10001",
  "case_id": "CASE-5G-NR-PUSCH-PERF-01",
  "execution_flow": [
    {
      "step_order": 1,
      "agent_name": "RequirementUnderstandingAgent",
      "node_id": "B",
      "loop_cycle": 0,
      "actions": [
        { "action_type": "RAG_RETRIEVAL", "action_name": "query_5g_knowledge", "latency_ms": 420 },
        { "action_type": "LLM_REASONING", "thought": "提取 38.214 调制阶数与 MCS 约束...", "latency_ms": 1150 }
      ]
    },
    {
      "step_order": 6,
      "agent_name": "ResultDiagnosisAgent",
      "node_id": "J",
      "branch_evaluation": {
        "predicted_branch": "ENV_ISSUE",
        "ground_truth": "ENV_ISSUE",
        "is_accurate": true
      }
    },
    {
      "step_order": 7,
      "agent_name": "EnvHealingAgent",
      "node_id": "L",
      "backtrack_dispatch": {
        "is_triggering_backtrack": true,
        "target_node": "F",
        "reason": "BBU-SLOT-03 时钟源失步"
      }
    },
    {
      "step_order": 8,
      "agent_name": "EnvPlanningAgent",
      "node_id": "F",
      "loop_cycle": 1,
      "actions": [
        { "action_type": "SKILL_CALL", "action_name": "skill_plan_topology", "input": {"exclude_board_ids": ["BBU-SLOT-03"]} }
      ]
    }
  ]
}

```

---

## 五、 核心代码工程模块实现

### 5.1 工程目录结构

```text
5g_agent_bench/
├── configs/                  # Bundle 配置清单与 Git 映射定义
├── core/
│   ├── runner.py             # 确定性调度引擎 (Seed, Temperature, Mock Clock)
│   ├── asserter.py           # 状态机与沙箱终态断言器 (State Asserter)
│   ├── attribution.py        # 失败分流归因分析器 (Root-cause Analyzer)
│   └── telemetry.py          # 异步遥测收集器 (PG + ClickHouse 双写)
├── datasets/
│   ├── smoke_set/            # PR 门禁用例集 (30~50 条，5分钟快速执行)
│   └── regression_set/       # Nightly 全量回归用例集 (含混沌注入)
└── storage/
    ├── pg_client.py          # PostgreSQL 事务管理客户端
    └── ch_client.py          # ClickHouse 高性能批量写入客户端

```

### 5.2 核心归因分析器实现 (`core/attribution.py`)

```python
from typing import Any, Dict, Tuple


class DiagnosticAttributor:
  """自动化故障归因矩阵：精准判定用例失败根因属于 RAG 还是 Agent"""

  @staticmethod
  def analyze_failure(
      case_spec: Dict[str, Any],
      trajectory: Dict[str, Any],
      rag_scores: Dict[str, float],
  ) -> Tuple[str, str]:
    # 1. 检查 RAG 知识检索环节
    if rag_scores.get("context_recall", 1.0) < 0.70:
      return (
          "RAG_RETRIEVAL_MISS",
          "5G 协议规范检索未召回关键切片，导致下游 Agent 上下文缺失",
      )
    if not rag_scores.get("is_routing_hit", True):
      return (
          "RAG_ROUTING_ERROR",
          "Agentic Routing 策略选择错误，未命中预期的表格/SQL引擎",
      )

    # 2. 检查多智能体决策与工具调用
    flow = trajectory.get("execution_flow", [])
    for step in flow:
      # 检查结果分析节点的分支路由
      if step.get("node_id") == "J":
        branch_eval = step.get("branch_evaluation", {})
        if not branch_eval.get("is_accurate", True):
          return "AGENT_DIAGNOSIS_MISCLASSIFIED", (
              f"K 节点分类错误: 预测为 {branch_eval.get('predicted_branch')}，"
              f"真值为 {branch_eval.get('ground_truth')}"
          )

      # 检查底层 Skill 契约执行
      for action in step.get("actions", []):
        if action.get("status") == "TIMEOUT":
          return (
              "TOOL_EXECUTION_TIMEOUT",
              f"Skill {action.get('action_name')} 耗时超过硬件安全阈值",
          )
        if action.get("status") == "SCHEMA_INVALID":
          return (
              "TOOL_PARAMETER_ERROR",
              f"Skill {action.get('action_name')} 入参抽取不符合 OpenAPI Schema",
          )

    # 3. 检查自愈回溯上限
    if trajectory.get("total_backtracks", 0) > case_spec.get(
        "max_backtracks", 2
    ):
      return (
          "AGENT_HEALING_LOOP_EXCEEDED",
          "环境修复或用例优化触发死循环，超过最大允许回溯圈数",
      )

    return "UNKNOWN_LOGIC_ERROR", "业务终态断言未满足，需人工接入复核"

```

---

## 六、 科学实验、版本化与防泄漏机制

### 6.1 全局版本包指纹化 (Bundle Fingerprint)

评测流水线启动前，计算当前 10 个 Agent Prompt、16 个 Skill Schema、DAG 拓扑及 8 大协议库快照的 SHA-256 级联指纹，生成不可变的 `bundle_hash`。**配置改动一个标点符号，哈希立即跳变，严格阻断非受控对比。**

### 6.2 防数据泄漏机制 (Anti-Contamination)

1. **静态特征扫描：** 评测集与 System Prompt 中的 Few-Shot 示例做文本相似度扫描（相似度 $>0.85$ 强行告警拦截）；
2. **微调黑名单：** 提取 Golden Cases 及其轨迹 Hash 注入模型微调流水线，过滤重复训练样本；
3. **动态参数混淆：** 在评测执行前对用例中的基站扇区 ID、UE IMSI、时间戳等做动态置换，迫使模型执行真实推理而非记忆答案。

### 6.3 消除非确定性 (Flakiness Control)

1. **推理环境确定性：** 设置 `seed=42, temperature=0`，固定 Serving 框架 Batch Size；
2. **沙箱隔离：** 每次用例运行前后彻底销毁并重建轻量沙箱；
3. **测试系统自体验收：** 基准正式发布前运行 3 轮空跑自回归，确保系统抖动率（Flakiness Rate）$< 1\%$。

### 6.4 Golden Case 动态晋升机制

新版本运行走出了与老 Golden 不同的更优轨迹（步数更少、Token 消耗更低）：

* **状态断言绝对冻结：** 业务及格线不动；
* **候选标记（Golden Candidate）：** 满足“核心排障工具未绕过、终态断言 100% 达成”的新轨迹自动打标；
* **专家 Review 与多参考路径（Multi-Reference）：** 5G 专家审批确认后提 Git PR，将新路径作为并列合法解合入测试集，实现基准与系统能力的协同演进。

---

## 七、 CI/CD 流水线接入与落地规范

```
[代码 / Prompt 提交 (PR)]
           │
           ▼
[PR 级 Smoke 门禁 (5分钟)]
 • 30~50 条全 Mock 核心用例
 • 校验 10 个 Agent 路由、16 个 Skill 入参 Schema
 • 拦截阻断性致命 Bug (通过率要求 100%)
           │
           ▼ (合并主分支)
[Nightly 深度回归流水线 (30分钟)]
 • 250 条高保真业务用例
 • 全量覆盖 7 类 RAG 异构召回与注入硬件异常的自愈回溯
 • 自动生成故障归因报表与性能趋势图
           │
           ▼ (大版本发布 / 模型换型)
[Full Benchmark 全量基线验收 (按需)]
 • 1,000+ 条用例全量评测
 • 严格对齐上下文与步数预算，为技术选型与上线提供量化背书

```

---

### 说明书总结

本设计说明书将前序所有讨论（Multi-Agent 拓扑、7 类 RAG 策略、12 张数据库表、双核断言、遥测流转与防泄漏）完整收敛为一个具备工业可用性、可直接指导研发编码落地的端到端工程标准。生成的 PDF 格式规范文档已在上方输出供随时下载查阅。