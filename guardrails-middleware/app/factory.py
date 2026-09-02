"""依赖装配（工厂）—— 从 configs/ 加载 YAML 策略，组装出完整 Guardrails。

供 main.py / 测试 / examples 共用；默认从 <repo>/configs 加载。
"""
from __future__ import annotations

from .actions.rewrite import Redactor
from .config import GuardrailsConfig
from .detectors.injection import InjectionDetector
from .detectors.pii import PIIDetector
from .detectors.schema import SchemaDetector
from .detectors.secret import SecretDetector
from .detectors.toxicity import ToxicityDetector
from .policies.loader import load_policy_engine, load_redactions
from .service import Guardrails
from .tools.loader import load_tools


def build_detectors() -> list:
    """内容阶段使用的一组 Detector；各 Detector 内部按 stage 声明适用范围。"""
    return [
        PIIDetector(),
        InjectionDetector(),
        SecretDetector(),
        ToxicityDetector(),
        SchemaDetector(),
    ]


def build_guardrails(config: GuardrailsConfig | None = None) -> Guardrails:
    config = config or GuardrailsConfig.from_env()
    cfg_dir = config.config_dir

    # policies.yaml -> PolicyEngine（stage + category -> action）
    policy_engine = load_policy_engine(cfg_dir / "policies.yaml")
    # tools.yaml -> ToolRegistry + RiskPolicy
    registry, risk_policy = load_tools(cfg_dir / "tools.yaml")
    # guardrails.yaml -> 脱敏模板
    redactions = load_redactions(cfg_dir / "guardrails.yaml")

    return Guardrails(
        config=config,
        detectors=build_detectors(),
        policy_engine=policy_engine,
        redactor=Redactor(redactions),
        tool_registry=registry,
        risk_policy=risk_policy,
    )


__all__ = ["build_guardrails", "build_detectors"]
