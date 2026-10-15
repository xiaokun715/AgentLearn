# troubleshooting-agent

> **排障智能体 Demo —— 让排障不再是一次性的根因猜测，而是有状态、证据驱动、可验证、可恢复的诊断闭环**
>
> FailureContext ＋ 执行轨迹分析 ＋ 分层故障分类 ＋ 多根因假设 ＋ **信息增益选检查**
> ＋ 动态 Workflow ＋ Workflow Validator ＋ 分级受控恢复 ＋ 故障经验库

对应设计说明书：`../第十五章：排障 Agent 设计.md`

---

## 一句话定位

> 「分析日志并回答原因」**不是**排障。说明书第一句就把这件事讲清楚了：
> 排障要完成的是 *故障发现 → 分类 → 轨迹分析 → 根因假设 → 证据验证 →
> 动态诊断 Workflow → 受控恢复 → 结果验证 → 经验沉淀*。
>
> 本 Demo 里最核心的技术主张只有一个：
>
> **让排障智能体根据故障证据动态决定下一步检查什么，并通过可执行的验证动作
> 排除错误假设，而不是让大模型一次性猜测根因。**

这句话落地成三件可以检查的事：

1. 每个候选根因都有一个**可计算的可信度**，由**真实执行过的检查**推动 —— 不是模型说「我觉得是」；
2. 下一步检查什么，由**信息增益**决定，而不是「哪个工具看起来最相关」；
3. 「能不能下结论」是三条**确定性判据**，不是让模型自评「证据够不够」。

```
                    测试失败事件（FailureContext）
                              │
                              ▼
                 ┌────────────────────────┐
                 │  Troubleshooting Agent │  理解诊断状态、选择下一步动作
                 └───────────┬────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   执行轨迹分析          故障分类              证据库
  （§12 第一问）      （规则 → LLM → UNKNOWN） （RAW/OBSERVED/INFERRED/VERIFIED 分层）
        │                    │                    │
        └────────────────────┼────────────────────┘
                             ▼
                     根因假设管理器
              Σ 先验 → 似然表 → 后验 · 熵 · 信息增益
                             │
                             ▼
                      诊断规划器（§5.3）
              Score(a) = IG(a) / (Cost + λ·Risk + ε)
                             │
                ┌────────────┴────────────┐
                ▼                         ▼
         模式 A/B：模板 / 裁剪      模式 C：动态生成 DAG
                └────────────┬────────────┘
                             ▼
                   Workflow Validator（§6.3）
        skill 存在 · 契约一致 · 依赖完整 · 无环 · 权限 · 资源 · 预算 · 未授权恢复
                             │
                             ▼
                      诊断执行引擎
          节点级超时 · 重试 · 检查点 · 确定性拓扑序
                             │
                             ▼
                    观测 → 贝叶斯更新 → 假设
                             │
                             ▼
                    结论门（确定性判据）
                概率 · 独立来源/决定性检查 · 区分力
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   继续诊断             受控恢复 (R0~R4)       人工介入
                             │
                             ▼
                       结果验证器
                  环境是否恢复 ＋ 复测是否通过
                             │
                             ▼
                       经验提炼与质量门控
                             │
                             ▼
                        故障经验库
                  VERIFIED / CANDIDATE / FAILED
```

---

## 特性一览

| 主题 | 落地 |
|---|---|
| **统一故障上下文** | `domain/models.py::FailureContext`（§3.1 的 13 个字段一个不少），带 `signature()` 指纹与 `validate_minimum()` 输入自检 |
| **轨迹 ≠ 现象** | `tracing/analyzer.py` 把轨迹当**依赖图**，区分 PHENOMENON / SUSPECT / NOISE。判据全是结构化字段（`verified` / `output_ref` / `result_complete` / 下游 `precondition_violation`），不靠模型「感觉不对」 |
| **规则优先分类** | `classification/classifier.py`：4 条规则只在**无歧义**时开火；命不中交给 LLM；LLM 输出必须过 Schema 校验，否则退化成 `UNKNOWN` |
| **UNKNOWN 是一等公民** | 「规则没命中」≠「规则判断失败」。证据不足时如实返回 UNKNOWN，而不是硬猜一个类别 |
| **不要只生成一个根因** | `hypothesis/manager.py` 从分类展开成一组候选假设，每个都有支持证据、反驳证据、尚需检查、状态、可信度、来源 |
| **似然表驱动** | `planning/actions.py` 每个诊断动作声明 `P(observation \| hypothesis)`。这是信息增益的燃料，也是「这个检查能区分哪些假设」的唯一依据 |
| **信息增益选检查** | `IG(a) = H(ℋ) − E_{o~a}[H(ℋ\|o)]`，评分 `IG/(Cost + λ·Risk + ε)`。实测：`check_environment_state` IG=0.434 vs `analyze_logs` IG=0.101 |
| **没有区分力的检查** | `retrieve_failure_cases` 对所有假设似然相同 → IG **恒等于 0** → 规划器永不为了「缩小不确定性」选它。这是刻意的示范 |
| **`None` 不是 0** | 似然表查不到某个假设时按**无信息**处理（等概率、似然比 1），绝不当作「不可能」。当成 0 会让一次无关检查直接判死一个假设 |
| **三种 Workflow 模式** | 模式 A 预定义模板 / 模式 B 模板裁剪（复用已有结果）/ 模式 C 动态生成。按说明书要求**优先模板** |
| **裁剪要验有效期** | 复用前校验时间戳、软件版本、上下文 —— 「历史上成功过」不构成复用理由 |
| **Workflow Validator** | 八项确定性校验：skill 存在 / 契约一致 / 依赖完整 / 无环 / 权限 / 资源冲突 / 预算 / 未授权恢复。6 类模型可能写出的坏计划全部拦下 |
| **模型只输出结构** | LLM 提议的是一份 `nodes[].{id, skill, depends_on}`，**没有代码**；即便如此仍然要过校验器 |
| **执行引擎无 LLM** | `execution/executor.py`：确定性拓扑序、节点级超时（可注入时钟）、节点级重试、每节点落检查点、模式 B 复用不调工具 |
| **结论门是判据** | `conclusion_ready()` 三道硬门：后验 ≥ 0.75、**两个独立来源或一次决定性检查**、领跑假设被主动推动过 |
| **「没查」≠「查过没问题」** | `REFUTED` 必须由**反证**触发；预算内没检验的假设标 `UNDECIDED`。混淆两者是排障系统最危险的自我欺骗 |
| **受控恢复分级** | R0 只读 / R1 无副作用刷新 / R2 重申请（查配额与幂等）/ R3 需审批 / R4 默认禁止 |
| **恢复阶梯** | 先试最小的动作：R1 刷新无效 → R2 重新申请 → R3 重启。实测 `refresh(无效) → reallocate(有效) → 复测通过 → RESOLVED` |
| **R4 结构性不可达** | 「R4 连审批通道都不开放」不是纪律，是结构：`APPROVAL_GRANTS` 里没有 R4 映射，`grant()` 直接抛 `PermissionError` |
| **复测 ≠ 根因确认** | `RecoveryOutcome.verified`（环境恢复）与 `rerun_passed`（复测通过）是两个字段。后者只证明「当前环境下不复现」 |
| **终止状态有语义** | `RESOLVED` / `ROOT_CAUSE_IDENTIFIED` / `UNRESOLVED` / `NEEDS_HUMAN` / `INVALID_INPUT` 五态，杜绝「生成一个看似合理的原因」冒充「排障成功」 |
| **经验质量门控** | 提取前过五道门：输入完整 → 证据充分 → 根因已验证 → 不与既有案例重复 → 适用条件明确。未定位的排障**不产生经验** |
| **失败经验必须留** | 「尤其需要保留失败经验，否则系统可能反复尝试已经证明无效的方法」。`FAILED` 案例可用于规避，但**不可复用其恢复动作** |
| **检索后验适用性** | 相似度只做预筛；软件版本 / 失败阶段不匹配的案例一律判为不适用，不允许「描述相似就套用」 |
| **启动自检** | `factory.check_config()` 八项检查：动作池 ↔ 工具注册表双向一致、似然表完整且归一、R0 不得有副作用、预算能否容下恢复阶梯、假设库的 required_checks 是否存在 |
| **零外部依赖** | 只有 `pydantic` + `pyyaml`；LLM 由确定性的 `ScriptedLLM` 扮演，克隆下来直接跑 |

---

## 快速开始

```bash
cd troubleshooting-agent

# 看有哪些场景
python -m app.main --list

# 全跑（约 1 秒）
python -m app.main all

# 只跑关心的（可选：classify trace diagnose workflow recover unresolved experience tests）
python -m app.main diagnose recover
python -m app.main tests            # 48 项断言，退出码即结果

# 看平台内部日志
python -m app.main all --verbose
```

依赖只有两个：`pydantic` / `pyyaml`。

```bash
pip install -e .        # 或 pip install pydantic pyyaml
python -m pytest        # 148 个单测，约 1.2 秒
```

真机上把 `app/llm.py::ScriptedLLM` 换成真实模型调用即可：它对外只有三个方法
（`classify_failure` / `propose_plan` / `summarize`），返回的都是可被
`pydantic.model_validate` 的普通字典，上层代码一行不用改。

---

## 场景演示（§3 ~ §12）

| 命令 | 场景 | 看什么 |
|---|---|---|
| `classify` | §4 分层故障分类 | 环境错误码走**规则**（0.85）；模糊故障走 **LLM**（0.55，且被打折）；证据不足给 **UNKNOWN** |
| `trace` | §12 第一问 | `collect_srs_measurement` 报错是**现象**；真正的嫌疑人是上游 `prepare_environment` —— 它 `status=OK` 但 `verified=false` 且没有 `output_ref` |
| `diagnose` | §5.3 / §6.2 核心 | 六个候选检查的 IG×成本打分表；一次决定性检查把 H_ENV 从 0.45 推到 0.81；**动态 1 个动作 vs 模板 4 个动作**；以及 LLM 猜错的分类被证据纠正 |
| `workflow` | §6.2 / §6.3 | 三种模式各跑一遍；六类非法计划被逐条拦下并给出原因码 |
| `recover` | §7.1 / §7.2 | 完整分级表；R1 无效 → R2 生效 → 复测通过；R3 未批准停在 NEEDS_HUMAN；R4 连审批都申请不到 |
| `unresolved` | §9 / §10.3 第 7 条 | 预算耗尽时输出 UNRESOLVED、根因留空、假设标 UNDECIDED、且**不产生经验** |
| `experience` | §8 | 质量门控的五道门；失败经验入库；第二次排障检索 → 校验适用性 → 加权先验（0.45→0.80）；换版本后案例判为不适用 |
| `tests` | 回归 | 48 项断言，覆盖上面每一条 |

### 场景三：动态诊断的三段实测输出

**① 第一轮：按信息增益给所有候选检查打分**

```
check_environment_state    IG=0.434 cost=1.0 risk=0.00 score=0.347
inspect_config             IG=0.277 cost=1.0 risk=0.00 score=0.222
inspect_tool_records       IG=0.192 cost=2.0 risk=0.00 score=0.085
validate_case_definition   IG=0.135 cost=2.0 risk=0.00 score=0.060
analyze_logs               IG=0.101 cost=2.0 risk=0.00 score=0.045
retrieve_raw_log           IG=0.099 cost=3.0 risk=0.00 score=0.030
```

注意 `retrieve_failure_cases` 根本不在候选里 —— 它的 IG 是 0，被 `relevant_to()` 过滤掉了。

**② 观测 → 贝叶斯更新**

```
观测结果      ANOMALY
预期信息增益   0.269          ← 对所有可能观测的加权平均
实际信息增益   1.006          ← 命中了小概率但高区分力的分支
H_ENV       0.450 → 0.806
H_TOOL      0.200 → 0.100
H_CONFIG    0.150 → 0.030
H_SOFTWARE  0.150 → 0.060
```

**③ 动态 vs 模板（同一个故障）**

```
动态（模式 C）  RESOLVED  根因=H_ENV  诊断动作数=1  工具调用=4  模式=['DYNAMIC']
模板（模式 A）  RESOLVED  根因=H_ENV  诊断动作数=4  工具调用=7  模式=['PREDEFINED']
```

**④ 反例：LLM 的初始分类是错的**

```
初始分类   CONFIG + ['TEST_CASE'] (来源=llm, 估计置信度=0.55)
最终根因   H_CASE（后验 0.79）
诊断路径   inspect_config → check_environment_state → inspect_tool_records
           → validate_case_definition → analyze_logs

inspect_config              NORMAL   H_CONFIG 0.55→0.10   ← 第一轮就把 LLM 的猜测推翻
check_environment_state     NORMAL   H_ENV    0.29→0.05
inspect_tool_records        NORMAL   H_TOOL   0.38→0.08
validate_case_definition    ANOMALY  H_CASE   0.23→0.63   ← 真正的证据出现
analyze_logs                NORMAL   H_CASE   0.63→0.79   ← 排除软件缺陷
```

---

## 章节 ↔ 代码文件对照

| 说明书章节 | 本 Demo 落地 |
|---|---|
| 一、定位与十步闭环 | `agent/agent.py` 模块 docstring 逐条对应 §10.3 的伪代码与七条注意事项 |
| 二、§2.1 系统架构与职责表 | `factory.py` 的装配顺序就是那张表；每个模块 docstring 首段写明边界 |
| 三、§3.1 FailureContext | `domain/models.py::FailureContext`（含 `signature()` / `validate_minimum()` / `expected_actual_diff`） |
| 三、§3.1 证据四层 | `domain/enums.py::EvidenceKind` + `evidence/store.py`（RAW → OBSERVED → INFERRED → VERIFIED，逐层强制溯源） |
| 三、§3.2 统一事件模型 | `domain/models.py::TraceEvent`（**不记录隐藏推理**，只记结构化结论与 `artifact://` 引用） |
| 四、§4.1 分层故障分类 | `domain/enums.py::FailureCategory`（9 类，UNKNOWN 是一等公民） |
| 四、§4.2 规则优先 + LLM 补充 | `classification/classifier.py::FailureClassifier` + `RULES` |
| 五、§5.1 假设记录 | `domain/models.py::Hypothesis`（说明书要求的字段一个不少，另加 `discriminating_checks`） |
| 五、§5.2 判断下一步检查什么 | `planning/planner.py::DiagnosticPlanner.rank()`（按区分力排序，不按「相关性」） |
| 五、§5.3 信息增益与评分 | `hypothesis/manager.py::expected_information_gain()` + `domain/policy.py::SCORING` |
| 六、§6.1 诊断动作池 | `planning/actions.py`（每个动作声明输入输出/前置条件/权限/超时/资源/副作用/幂等/似然表）+ `configs/actions.yaml` 覆盖层 |
| 六、§6.2 三种 Workflow 模式 | `planner.py::build_from_template()` / `build_dynamic()` / `select_mode()` |
| 六、§6.3 防非法 Workflow | `planning/validator.py`（八项检查）+ `llm.py::propose_plan()` 的六种坏计划样例 |
| 七、§7.1 恢复风险分级 | `domain/enums.py::RiskTier` + `domain/policy.py::APPROVAL_MATRIX` |
| 七、§7.2 恢复前验证门 | `recovery/controller.py::evaluate()` 三道门 + `verify()`（复测与根因分离） |
| 八、§8.1 故障经验结构 | `domain/models.py::ExperienceCase` / `FailureSignature` |
| 八、§8.2 入库质量门控 | `experience/manager.py::extract()` 五道门 + `record_failure()` |
| 八、§8.3 经验复用 | `experience/manager.py::retrieve()` / `seed_priors()` / `avoid_actions()` |
| 九、终止状态与指标 | `domain/enums.py::TerminalState` + `DiagnosisConclusion.metrics` |
| 十、§10.2 项目目录 | 见下（模块名与说明书建议一一对应） |
| 十、§10.3 决策循环 | `agent/agent.py::TroubleshootingAgent.troubleshoot()` |
| 十二、第一问：从轨迹找根因 | `tracing/analyzer.py`（现象 / 疑似根因 / 噪声三类） |
| 十二、第二问：自适应 Workflow | 模式 A/B/C 的选择 + `parallel_groups()` 的资源冲突判断 |
| 十二、第三问：怎么证明经验有用 | `use_experience=False` 开关 + `metrics` 里记录的步数与工具调用数，可直接做 A/B 对照 |

---

## 关键设计取舍（这些是面试会追问的地方）

### 1. 「轨迹是路标，检查是证据」

第一次实现时，我把轨迹分析找到的假成功节点直接挂成了 H_ENV 的 `supporting_evidence`。
结果是环境世界的第一步检查就出结论 —— 因为「轨迹里有个可疑节点」被当成了「根因已
被证据支持」。

这两件事必须分开：

* **路标**回答「往哪儿看」。轨迹里的 `verified=false` 是一个**线索**，它提高先验；
* **证据**回答「什么是真的」。只有**执行过的检查**产出的观测才算数。

所以 `boost_prior()` 只动 `prior`，不动 `supporting_evidence`。结论门数的是后者。
把路标计成证据，就是「模型一猜就中」的另一种写法。

### 2. 结论门里有一条门是**算出来不可达**的

最初的门是四道：概率过线、证据够、有区分力、领先第二名 ≥ 0.20。

第四道是错的。后验是归一化的，所以「领跑 ≥ 0.75」**必然**蕴含「次席 ≤ 0.25」，
也就是差距必然 ≥ 0.50 —— 任何小于 0.50 的边际阈值永远为真。

我把它删掉了，并在 `conclusion_ready()` 里留了一段注释说明为什么。
一串永远返回 True 的检查比没有检查更糟：它让人误以为系统多了一层保护。
「不急着一锤定音」这件事，由 `accept_probability = 0.75` 这一条阈值本身承担。

### 3. 似然表查不到时，既不是 0 也不是 1

这是信息增益实现里最容易踩的坑。动作池里没有某个假设的似然行时：

* 当成 **0** → 一次与该假设无关的检查会把它直接判死；
* 当成 **1** → 等于凭空加了一次伪证据；
* **正确做法** → 给它在所有可能结果上等概率，使似然比恒为 1、后验不变。

`HypothesisManager.expected_information_gain()` 与 `update()` 用**同一套**规则处理
这个 `None`，所以「规划时按 A 算、更新时按 B 算」的漂移在结构上就不可能发生。

### 4. 一个一锤定音的检查，不该因为「只有一次」而结不了案

`min_independent_sources = 2` 这道门的初衷是好的（防止单点证据下结论），
但它有个致命副作用：**当第一次检查就把某个假设推到 0.87 之后，后续任何检查都
挪不动它（Δ < 0.02），第二个来源永远攒不出来** —— 系统会卡在「结论明确但不敢下」。

修法是给这道门一个「或」的分支：**两个独立来源互相印证，或一次决定性检查**
（单次把领跑假设推高 ≥ 0.20）。再配一个兜底：当规划器找不到更有区分力的检查，
而领跑假设已被主动推动过时，直接定论而不是报 UNRESOLVED。

但兜底必须分辨两种处境，否则会把「查不动」写成「查明白了」：

| 处境 | 判据 | 结论 |
|---|---|---|
| 调查到头了 | 领跑假设有 `discriminating_checks` | `ROOT_CAUSE_IDENTIFIED` |
| 查不动 | 领跑假设从未被主动推动过 | `UNRESOLVED` |

### 5. `INCONCLUSIVE` 和 `ANOMALY` 是两回事

工具报错（接口 500、探针超时）说明**这次检查没做成**，不构成任何根因假设的证据。
把它当成 ANOMALY，系统就会把「查不到」读成「有问题」，然后开始改世界。

所以 `Observation` 有四个值：`ANOMALY` / `NORMAL` / `INCONCLUSIVE` / `ERROR`，
而 `update()` 对后两者**不做任何更新**（连证据都不挂）。稀疏世界能稳定输出
`UNRESOLVED` 而不是乱猜，靠的就是这条。

### 6. R4 的不可达性必须是结构性的

「改测试预期让测试通过」是说明书 §9 点名的反模式：指标上升，测试质量下降。

如果只靠「提示词里写一句不要这么做」，那它迟早会发生。本 Demo 把它挡住两次：

1. `WorkflowValidator` 的 `FORBIDDEN_RECOVERY`（任何阶段都拦，连 `allow_recovery=True` 也拦）；
2. `APPROVAL_GRANTS` 里**没有** R4 动作的映射 —— `recovery.grant("override_test_expectation")`
   直接抛 `PermissionError`。

第二条是关键：**「批了也不能跑」是结构性的，不是纪律性的。**

### 7. 恢复阶梯，而不是「挑一个最相关的动作」

`RECOVERY_LADDER` 按风险从低到高排：`refresh`（R1，只重查状态）→ `reallocate`（R2，重新申请）
→ `restart`（R3，需审批）。每次恢复后都真的去探测环境是否恢复，无效才升级。

实测环境世界：`refresh` 执行了但环境依旧 `NOT_READY` → 升级到 `reallocate` → 环境就绪
→ 复测通过 → `RESOLVED`。这一次「无效的 refresh」同时被记成一条 **FAILED 经验** —— 
下次同类故障可以直接跳过它。

> 顺带一个被删掉的东西：规划器里原本也有一份「挑恢复动作」的排序。同一件事两处实现，
> 规划器按 `risk` 排、控制器按 `RECOVERY_LADDER` 排，对软件缺陷场景给出的答案不一样。
> 删掉它是为了**只留一个真相来源**。

### 8. 时间读的是时钟，不是 `datetime.now()`

排障系统到处是时间：动作超时、总耗时预算、缓存结果有效期。
如果这些都读系统时间，一个 5 秒超时的测试用例就要真的等 5 秒 ——
于是没人愿意跑它，超时逻辑也就永远没被测过。

所以全部走 `Clock` 接口。测试里注入 `ManualClock`，`test_node_timeout_is_detected_via_injected_clock`
让工具「跑」999 秒，而整个测试套件 148 个用例总耗时 **1.2 秒**。

### 9. 按时间排证据在 Windows 上是个陷阱

证据溯源要「先原始后推断」，最初的实现是按 `collected_at` 排序。
测试挂了 —— 因为 Windows 上 `datetime.now()` 的粒度约 15ms，同一批证据会拿到
**完全相同**的时间戳，排序退化成「看运气」。

改成按**入库序号**排序：RAW 必然先于由它推出的 OBSERVED/INFERRED 入库，
所以这个顺序天然满足「先原始后推断」，而且完全可复现。

---

## 项目目录

```
troubleshooting-agent/
├── app/
│   ├── main.py                  # CLI 入口：八个场景 + 48 项回归断言
│   ├── factory.py               # 装配 + 启动自检 check_config()
│   ├── sim.py                   # 模拟的被测平台：五个故障世界（只有它知道真根因）
│   ├── llm.py                   # 确定性 LLM 替身（含六种「坏计划」样例）
│   ├── clock.py                 # 可注入时钟
│   │
│   ├── domain/                  # 共享契约
│   │   ├── enums.py             #   分类 / 观测 / 假设状态 / 风险分级 / 终态 / 经验三类
│   │   ├── models.py            #   §3.1 §3.2 §5.1 §6.3 §8.1 §9 的全部数据模型
│   │   └── policy.py            #   评分权重 / 结论门阈值 / 审批矩阵 / 假设库 / Workflow 模板
│   │
│   ├── evidence/store.py        # §3.1 证据分层与溯源（推断必须指回原始证据）
│   ├── tracing/analyzer.py      # §12 第一问：现象 / 疑似根因 / 噪声
│   ├── classification/classifier.py  # §4 规则优先 + LLM 补充 + Schema 校验
│   ├── hypothesis/manager.py    # §5.1 §5.3 假设维护 / 熵 / 信息增益 / 贝叶斯更新 / 结论门
│   │
│   ├── planning/
│   │   ├── actions.py           #   §6.1 诊断动作池 + 似然表
│   │   ├── planner.py           #   §5.2 §6.2 评分选动作 + 三种 Workflow 模式
│   │   └── validator.py         #   §6.3 八项确定性校验（含找环 / 资源冲突）
│   │
│   ├── execution/executor.py    # §6.3 §10.3 确定性拓扑序 / 超时 / 重试 / 检查点 / 复用
│   ├── recovery/controller.py   # §7 验证门 / 恢复阶梯 / 审批 / 结果验证
│   ├── experience/manager.py    # §8 质量门控 / 检索 / 适用性校验 / 失败经验
│   ├── agent/agent.py           # §10.3 决策循环 + 收尾（结论、指标、经验沉淀）
│   │
│   └── tools/                   # §6.1 动作池背后的真实操作
│       ├── base.py              #   工具契约（ToolError ≠ Observation.ANOMALY）
│       ├── log_tools.py         #   analyze_logs / retrieve_raw_log
│       ├── config_tools.py      #   inspect_config / validate_case_definition
│       ├── environment_tools.py #   环境探针 + R1/R2/R3 三个恢复动作
│       └── test_tools.py        #   工具记录 / 复测 / 历史案例 / 缺陷单 / R4
│
├── configs/actions.yaml         # 动作池的声明式覆盖层（成本/风险/超时/似然表）
└── tests/                       # 148 个单测
```

---

## 已知边界（说明书本身也承认的部分）

* **`confidence` 不是校准过的概率**。说明书 §4.2 明确说「不应直接视为经过校准的真实
  概率」。本 Demo 只用它排序与展示 —— 恢复决策走的是**风险分级 + 证据门**，不看置信度。
  真机上要做自动恢复，得先用历史标注样本验证「置信度 ↔ 实际正确率」的对应关系。
* **似然表是示意值**。README 与代码里每个数字都写了它表达什么，但它们不是从数据里
  回归出来的。这是本 Demo 与真系统之间最大的一处差距 —— 也是最容易补上的一处：
  接口已经就位（`configs/actions.yaml` 覆盖 `likelihoods`），缺的只是历史标注数据。
* **并行只是算出来，没有真的并行**。`parallel_groups()` 计算可并行分组，执行器按
  确定性顺序串行跑。理由是演示与回归测试需要可复现的证据序列。换 `asyncio.gather`
  即可并行，但要把「同批节点共享同一份预算」这件事一起处理掉。
* **没有故障回放集**。说明书 §12 第三问要求用历史故障回放集对照「不用经验 /
  只用知识库 / 用故障经验 / 用故障经验 + 动态规划」。本 Demo 提供了开关
  （`use_experience`、`use_templates`）与指标（步数、工具调用、预期/实际信息增益），
  但没有真实的回放数据集。
* **五个故障世界是手工构造的**。它保证演示可复现，但也意味着它**不会**暴露真实
  日志的那种脏乱（时间戳乱序、字段缺失、同一错误码多种成因）。接真机时这一层
  要整个换掉 —— `sim.py` 的接口（`probe` / `apply_recovery` / `rerun`）就是为替换而划的。
* **没有 HTTP API 层**。与本书其它 Demo 一致：考察重点是排障语义而不是接口形态。
* **恢复动作只模拟了环境类**。配置修正、资源重申请、数据重建这些都有各自的幂等性
  与配额语义，本 Demo 只实现了「能演示分级与审批」的最小集合。
