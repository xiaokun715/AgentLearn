"""排障系统枚举 —— 分类体系、假设状态、观测结论、风险等级、任务终态。

对齐《第十五章：排障 Agent 设计》：
- 四、§4.1  分层故障分类        -> :class:`FailureCategory`
- 四、§4.2  分类置信度来源       -> :class:`HypothesisStatus`
- 五、§5.3  诊断动作的观测结果    -> :class:`Outcome`
- 七、§7.1  恢复动作的风险分级    -> :class:`RiskTier`
- 六、§6.2  诊断 Workflow 三种模式 -> :class:`PlanMode`
- 六、§6.3  节点执行状态          -> :class:`NodeStatus`
- 八、§8.2  故障经验的三类         -> :class:`ExperienceStatus`
- 九、§9    排障任务的终止状态     -> :class:`TerminalState`
"""
from __future__ import annotations

from enum import Enum


class FailureCategory(str, Enum):
    """§4.1 分层故障分类的第一层。

    说明书明确说「不是说每次故障只能属于一个类别」—— 所以一次分类的结果是
    :class:`~app.domain.models.FailureClassification`：主类别 + 次级类别 +
    候选原因 + 置信度。这里的枚举只是**词汇表**，不是互斥的判定结果。

    ``UNKNOWN`` 是一等公民而不是失败分支：规则命不中、证据又不足以让 LLM
    给出候选类别时，返回 UNKNOWN 比强行猜一个类别诚实得多（§4.2）。
    """

    REQUIREMENT = "REQUIREMENT"            # 需求理解异常
    TEST_CASE = "TEST_CASE"                # 用例设计异常
    CONFIG = "CONFIG"                      # 配置异常
    ENVIRONMENT = "ENVIRONMENT"            # 环境异常
    TOOL_EXECUTION = "TOOL_EXECUTION"      # 工具执行异常
    SOFTWARE = "SOFTWARE"                  # 被测软件异常
    TEST_DATA = "TEST_DATA"                # 测试数据异常
    ORCHESTRATION = "ORCHESTRATION"        # 流程编排异常
    UNKNOWN = "UNKNOWN"                    # 未知或混合异常

    def __str__(self) -> str:  # 终端里打印友好
        return self.value


class Observation(str, Enum):
    """§5.3 一次诊断动作可能得到的观测结果。

    这是**假设更新与信息增益的共同货币**：诊断规划器用似然表
    ``P(observation | hypothesis)`` 估算预期信息增益，执行器拿到真实观测后
    再用同一张表做贝叶斯更新。两者共用一套符号，才不会出现
    「规划时按 A 算、更新时按 B 算」的漂移。

    - ``ANOMALY``     该检查项确实异常（有证据指向某个假设）
    - ``NORMAL``      该检查项正常（证据**排除**相关假设）
    - ``INCONCLUSIVE`` 检查跑了但没结论（日志被轮转、探针超时…）
    - ``ERROR``       检查本身失败（工具异常），不构成任何假设的证据
    """

    ANOMALY = "ANOMALY"
    NORMAL = "NORMAL"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


class HypothesisStatus(str, Enum):
    """§5.1 假设状态。

    刻意区分 ``UNDECIDED`` 和 ``REFUTED``：
    - ``REFUTED`` 是**被证据反驳**（某个检查正常，而这个假设下该检查极可能异常）；
    - ``UNDECIDED`` 是**还没被有效检验**（预算耗尽 / 检查不可用）。

    把两者混在一起，就会把「没查」当成「查过且没问题」，这是排障系统最危险的
    一类自我欺骗 —— 结论里的 ``未定位`` 正是靠这个区分才站得住。
    """

    PENDING = "PENDING"        # 待验证：刚生成，还没有任何证据
    SUPPORTED = "SUPPORTED"    # 被支持：有主动证据抬升了可信度
    REFUTED = "REFUTED"        # 被排除：有检查给出了反证
    UNDECIDED = "UNDECIDED"    # 未决：预算内没能有效检验


class RiskTier(str, Enum):
    """§7.1 恢复动作风险分级 —— 这是排障系统的**安全边界**。

    ========== ================================ ==============================
    等级        典型操作                          处理方式
    ========== ================================ ==============================
    R0         查询日志、读取配置                 可自动执行
    R1         无副作用的状态刷新、只读校验        通常可自动执行
    R2         重新申请环境、重跑可重复测试        检查配额、成本和幂等性
    R3         重启设备、修改配置                  需要授权策略或人工审批
    R4         改测试预期、覆盖正式配置、不可逆操作  默认禁止自动执行
    ========== ================================ ==============================
    """

    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"

    @property
    def auto_executable(self) -> bool:
        """无需审批即可执行（诊断阶段与低风险恢复）。"""
        return self in (RiskTier.R0, RiskTier.R1)

    @property
    def needs_approval(self) -> bool:
        """必须先拿到人工审批（§7.2 验证门）。"""
        return self is RiskTier.R3

    @property
    def forbidden(self) -> bool:
        """默认禁止自动执行 —— 连「申请审批」都不给走自动通道。"""
        return self is RiskTier.R4


class PlanMode(str, Enum):
    """§6.2 诊断 Workflow 的三种生成方式。

    ``PREDEFINED`` / ``TRIMMED`` 优先于 ``DYNAMIC``：说明书要求
    「优先执行已知模板；只有模板不适用时才动态生成，以减少规划成本和不确定性」。
    """

    PREDEFINED = "PREDEFINED"  # 模式 A：预定义 Workflow
    TRIMMED = "TRIMMED"        # 模式 B：模板裁剪（复用已有结果）
    DYNAMIC = "DYNAMIC"        # 模式 C：动态生成


class NodeStatus(str, Enum):
    """§6.3 诊断节点的执行状态（节点级状态是断点续跑的前提）。"""

    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"      # 依赖未满足 / 被裁剪
    TIMED_OUT = "TIMED_OUT"
    BLOCKED = "BLOCKED"      # 权限不足 / 风险不允许，未执行

    @property
    def terminal(self) -> bool:
        return self is not NodeStatus.PENDING


class ExperienceStatus(str, Enum):
    """§8.2 故障经验的三种类型。

    保留 ``FAILED`` 是刻意的：说明书原文「尤其需要保留失败经验，否则系统可能
    反复尝试已经证明无效的方法」。经验库里只有成功案例，等于没有记忆。
    """

    VERIFIED = "VERIFIED"      # 已验证经验：证据充分、根因明确、适用范围清晰
    CANDIDATE = "CANDIDATE"    # 候选经验：观察到关联性，尚未确认根因
    FAILED = "FAILED"          # 失败经验：诊断/恢复方案无效或产生过副作用


class TerminalState(str, Enum):
    """§9 排障任务的终止状态。

    这张表存在的唯一目的是：**避免把「生成了一个看似合理的原因」误判为
    「排障成功」**。因此 ``ROOT_CAUSE_IDENTIFIED`` 与 ``RESOLVED`` 必须分开 ——
    前者是「知道为什么」，后者是「知道为什么**且**验证过已经好了」。
    """

    RESOLVED = "RESOLVED"
    ROOT_CAUSE_IDENTIFIED = "ROOT_CAUSE_IDENTIFIED"
    UNRESOLVED = "UNRESOLVED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    INVALID_INPUT = "INVALID_INPUT"

    @property
    def is_success(self) -> bool:
        return self in (TerminalState.RESOLVED, TerminalState.ROOT_CAUSE_IDENTIFIED)


class EvidenceKind(str, Enum):
    """§3.1「原始证据与模型推断必须分开保存」的落点。

    四层是递进的，每一层只能由下一层推导，且必须留下 ``derived_from`` 指针：

        RAW（原始日志原文） → OBSERVED（观测事实） → INFERRED（模型推断） → VERIFIED（验证结论）

    没有这个分层，「设备可能异常」这种模型措辞会被当成事实沉淀进经验库，
    下一次排障就会拿着一个幻觉当前提。
    """

    RAW = "raw"
    OBSERVED = "observed"
    INFERRED = "inferred"
    VERIFIED = "verified"


class StepKind(str, Enum):
    """§12 Q1：失败节点不一定是根因节点，因此轨迹里要能区分「现象」与「原因」。"""

    PHENOMENON = "PHENOMENON"  # 现象节点：报错在这里，但它只是受害者
    SUSPECT = "SUSPECT"        # 疑似根因节点：声称成功、实际没做到
    NOISE = "NOISE"            # 噪声节点：与本次故障无关的异常
