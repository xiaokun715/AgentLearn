"""诊断动作池 —— 声明式描述「排障系统能做哪些检查」（说明书 §6.1）。

说明书要求每个 Skill 都声明：输入输出 Schema、前置条件、权限要求、超时限制、
资源需求、是否可并行、是否有副作用、是否支持幂等执行 —— 理由是

> 这样，Planner 才能知道哪些动作可以执行，而不是仅根据 Skill 名称猜测。

本模块把这些声明集中成一个池子，并额外带上 §5.3 信息增益所需的**似然表**：

    likelihoods[hypothesis_key][observation] = P(observation | hypothesis)

似然表是「检查能区分哪些假设」这一判断的唯一依据。它的每一项都应来自
历史标注数据或领域专家，而不是拍脑袋 —— Demo 里给的是可解释的示意值，
每个值的含义都写在对应动作的注释里（见 README「似然表从哪来」）。

代码里给默认值，``configs/actions.yaml`` 可以覆盖（真机上覆盖值应当来自数据）。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ..domain.enums import Observation, RiskTier
from ..domain.models import ActionSpec

_A, _N = Observation.ANOMALY.value, Observation.NORMAL.value


def _row(anomaly: float) -> dict[str, float]:
    """一行似然 ``P(ANOMALY)=anomaly, P(NORMAL)=1-anomaly``。"""
    return {_A: anomaly, _N: round(1.0 - anomaly, 4)}


# ----------------------------------------------------------------------
# 只读诊断动作（R0）：全部无副作用、可并行、幂等
# ----------------------------------------------------------------------
_DIAGNOSTIC_SPECS: list[ActionSpec] = [
    ActionSpec(
        name="inspect_config",
        description="比对测试配置与需求约束，输出配置差异",
        tier=RiskTier.R0,
        side_effect="read",
        cost=1.0,
        risk=0.0,
        resource=None,
        produces=["config_diff"],
        permission="diagnose:read",
        # 配置项本身异常时，几乎必然在这里露出（0.92）；其余假设下也可能顺带看到
        # 一点点配置问题（0.10~0.20）—— 这正是「诊断动作与根因不是一对一映射」的体现。
        likelihoods={
            "H_CONFIG": _row(0.92),
            "H_ENV": _row(0.15),
            "H_SOFTWARE": _row(0.10),
            "H_CASE": _row(0.20),
            "H_TOOL": _row(0.10),
        },
    ),
    ActionSpec(
        name="check_environment_state",
        description="检查设备、资源与初始化状态",
        tier=RiskTier.R0,
        side_effect="read",
        cost=1.0,
        risk=0.0,
        produces=["environment_probe"],
        permission="diagnose:read",
        # 环境异常的检出率高（0.90）。注意 H_TOOL 也给了 0.25 —— 环境塌了之后
        # 采集工具往往跟着报错，所以这个动作**同时**对两个假设有信息量（§5.2）。
        likelihoods={
            "H_ENV": _row(0.90),
            "H_CONFIG": _row(0.10),
            "H_SOFTWARE": _row(0.20),
            "H_CASE": _row(0.05),
            "H_TOOL": _row(0.25),
        },
    ),
    ActionSpec(
        name="analyze_logs",
        description="解析日志引用，提取错误事件与时间线",
        tier=RiskTier.R0,
        side_effect="read",
        cost=2.0,
        risk=0.0,
        produces=["log_findings"],
        permission="diagnose:read",
        # 软件缺陷必留痕（0.85），但环境初始化失败同样会在日志里留痕（0.70）——
        # 所以这是一个**广谱**检查：区分力不如针对性检查，成本还高。规划器会给它
        # 一个中等分数，但不会优先选它（对比 README 的「第一轮选谁」实测输出）。
        likelihoods={
            "H_SOFTWARE": _row(0.85),
            "H_ENV": _row(0.70),
            "H_CONFIG": _row(0.45),
            "H_CASE": _row(0.10),
            "H_TOOL": _row(0.50),
        },
    ),
    ActionSpec(
        name="validate_case_definition",
        description="校验用例的预期结果是否与需求约束一致",
        tier=RiskTier.R0,
        side_effect="read",
        cost=2.0,
        risk=0.0,
        produces=["case_findings"],
        permission="diagnose:read",
        likelihoods={
            "H_CASE": _row(0.88),
            "H_CONFIG": _row(0.30),
            "H_SOFTWARE": _row(0.10),
            "H_ENV": _row(0.10),
            "H_TOOL": _row(0.05),
        },
    ),
    ActionSpec(
        name="inspect_tool_records",
        description="查询测试工具执行记录：返回码、超时、采集状态",
        tier=RiskTier.R0,
        side_effect="read",
        cost=2.0,
        risk=0.0,
        produces=["tool_records"],
        permission="diagnose:read",
        likelihoods={
            "H_TOOL": _row(0.90),
            "H_ENV": _row(0.35),
            "H_SOFTWARE": _row(0.30),
            "H_CONFIG": _row(0.20),
            "H_CASE": _row(0.20),
        },
    ),
    ActionSpec(
        name="retrieve_raw_log",
        description="拉取未过滤的原始日志（对象存储引用）",
        tier=RiskTier.R0,
        side_effect="read",
        cost=2.0,
        risk=0.0,
        produces=["raw_log"],
        permission="diagnose:read",
        likelihoods={
            "H_SOFTWARE": _row(0.80),
            "H_ENV": _row(0.65),
            "H_CONFIG": _row(0.35),
            "H_CASE": _row(0.10),
            "H_TOOL": _row(0.45),
        },
    ),
    ActionSpec(
        name="retrieve_failure_cases",
        description="按故障指纹检索历史案例",
        tier=RiskTier.R0,
        side_effect="read",
        cost=1.0,
        risk=0.0,
        produces=["history_cases"],
        permission="diagnose:read",
        # 所有假设下的似然完全相同 -> 信息增益恒等于 0。
        # 这是刻意的示范：**它在排障里有价值（提供先验与先例），但价值不在于区分根因**。
        # 规划器永远不会为了「缩小不确定性」选它 —— 说明书 §5.3 说的
        # 「比只能重复确认已知事实的检查更有价值」，指的就是这类动作。
        likelihoods={
            "H_SOFTWARE": _row(0.30),
            "H_ENV": _row(0.30),
            "H_CONFIG": _row(0.30),
            "H_CASE": _row(0.30),
            "H_TOOL": _row(0.30),
        },
    ),
]


# ----------------------------------------------------------------------
# 恢复动作（§7.1 分级）
# ----------------------------------------------------------------------
_RECOVERY_SPECS: list[ActionSpec] = [
    ActionSpec(
        name="refresh_environment_state",
        description="重新查询环境状态（无副作用的状态刷新）",
        kind="recovery",
        tier=RiskTier.R1,
        side_effect="read",
        cost=1.0,
        risk=0.05,
        resource="environment",
        permission="recovery:refresh",
    ),
    ActionSpec(
        name="reallocate_environment",
        description="重新申请一台符合条件的环境并重新初始化",
        kind="recovery",
        tier=RiskTier.R2,
        side_effect="mutate",
        idempotent=True,
        cost=3.0,
        risk=0.25,
        resource="environment",
        timeout_seconds=10.0,
        permission="recovery:reallocate",
    ),
    ActionSpec(
        name="rerun_test",
        description="在恢复后的环境上重跑测试用例",
        kind="recovery",
        tier=RiskTier.R2,
        side_effect="mutate",
        idempotent=True,
        cost=2.0,
        risk=0.15,
        resource="environment",
        timeout_seconds=10.0,
        permission="recovery:reallocate",
    ),
    ActionSpec(
        name="restart_device",
        description="重启被测设备（改变测试环境）",
        kind="recovery",
        tier=RiskTier.R3,
        side_effect="mutate",
        idempotent=True,
        cost=3.0,
        risk=0.60,
        resource="environment",
        timeout_seconds=15.0,
        # 这一条权限**不在**默认权限集里 —— 必须走人工审批（§7.2 验证门）
        permission="recovery:restart-device",
    ),
    ActionSpec(
        name="override_test_expectation",
        description="修改测试预期结果",
        kind="recovery",
        tier=RiskTier.R4,
        side_effect="mutate",
        idempotent=False,
        cost=1.0,
        risk=0.95,
        # R4 是项目建议的安全边界：改测试预期能让「通过率」变好看，
        # 但那不是排障，那是把测试质量做没了（说明书 §9 的原话）。
        permission="recovery:override-expectation",
    ),
    ActionSpec(
        name="create_defect_report",
        description="基于验证后的结论创建缺陷单",
        kind="recovery",
        tier=RiskTier.R1,
        side_effect="mutate",
        idempotent=True,
        cost=1.0,
        risk=0.0,
        permission="recovery:refresh",
    ),
]


class ActionPool:
    """诊断动作池：查询、筛选、YAML 覆盖。"""

    def __init__(self, specs: list[ActionSpec]) -> None:
        self._specs: dict[str, ActionSpec] = {spec.name: spec for spec in specs}

    # ------------------------------------------------------------------
    @classmethod
    def default(cls) -> ActionPool:
        return cls([*_DIAGNOSTIC_SPECS, *_RECOVERY_SPECS])

    @classmethod
    def from_yaml(cls, path: str | Path) -> ActionPool:
        """代码默认值 + YAML 覆盖。

        只覆盖声明的字段（``tier`` / ``cost`` / ``risk`` / ``timeout_seconds`` /
        ``likelihoods``），**不允许 YAML 新增未知动作** —— 否则运维改一次配置
        就能凭空造出一个没有实现、没有权限元数据的「动作」。
        """
        pool = cls.default()
        file = Path(path)
        if not file.exists():
            return pool
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        for name, override in (raw.get("actions") or {}).items():
            spec = pool._specs.get(name)
            if spec is None:
                raise KeyError(f"configs/actions.yaml 声明了未知动作：{name}")
            for field, value in (override or {}).items():
                if field == "likelihoods":
                    spec.likelihoods = {
                        key: {k: float(v) for k, v in row.items()}
                        for key, row in value.items()
                    }
                elif hasattr(spec, field):
                    setattr(spec, field, value)
                else:
                    raise KeyError(f"动作 {name} 不支持覆盖字段 {field}")
        return pool

    # ------------------------------------------------------------------
    def get(self, name: str) -> ActionSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> ActionSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"未知动作：{name}")
        return spec

    def all(self) -> list[ActionSpec]:
        return list(self._specs.values())

    def diagnostic(self) -> list[ActionSpec]:
        return [spec for spec in self._specs.values() if spec.kind == "diagnostic"]

    def recovery(self) -> list[ActionSpec]:
        return [spec for spec in self._specs.values() if spec.kind == "recovery"]

    def names(self) -> list[str]:
        return list(self._specs)

    # ------------------------------------------------------------------
    def relevant_to(self, hypothesis_keys: set[str]) -> list[ActionSpec]:
        """筛出**在当前候选假设上有信息量**的诊断动作。

        判据是「似然表里对这组假设不是全等概率」，而不是「名字里有关键词」。
        一个动作若对所有候选假设给出相同的似然（如 ``retrieve_failure_cases``），
        它就无法区分任何东西，不该进入候选集。
        """
        candidates: list[ActionSpec] = []
        for spec in self.diagnostic():
            rows = [
                spec.likelihoods.get(key)
                for key in hypothesis_keys
                if spec.likelihoods.get(key)
            ]
            if len(rows) < 2:
                continue
            first = rows[0]
            if any(row != first for row in rows[1:]):
                candidates.append(spec)
        return candidates
