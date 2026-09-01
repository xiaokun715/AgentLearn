"""A/B Router —— 依据 Deployment 路由表 + user bucket 选出版本（设计说明书 §15 / §21）。

路由表（§20）示例：
    [{version:12, weight:90}, {version:13, weight:10}]

路由过程：
    user_id ──hash──► bucket(0~99) ──累计权重──► version + variant

Variant 命名：按权重降序排成 A / B / C ...，A = 对照组（老版本/最高权重），
B = 实验组（新版本）。这样 Resolver 的 routing 里就能带出实验元数据（§31）。
"""
from __future__ import annotations

from typing import Any

from ..domain.deployment import Deployment, DeploymentRule
from .hash_router import bucket as hash_bucket


def _ordered_rules(rules: list[DeploymentRule]) -> list[DeploymentRule]:
    """稳定排序：权重降序（对照组在前）。权重相同时保持原顺序。"""
    return sorted(rules, key=lambda r: r.weight, reverse=True)


class AbRouter:
    """基于 Hash 的粘性 A/B 路由器。"""

    def route(self, deployment: Deployment, user_id: str) -> tuple[int, str]:
        """返回 (version, variant)。variant 为 'A' / 'B' / 'single'。"""
        if not deployment.rules:
            raise ValueError(f"deployment {deployment.id} 没有可路由的规则")

        if deployment.is_single_version:
            return deployment.rules[0].version, "single"

        b = hash_bucket(user_id, salt=deployment.experiment or "")
        ordered = _ordered_rules(deployment.rules)
        cumulative = 0
        for index, rule in enumerate(ordered):
            cumulative += rule.weight
            if b < cumulative:
                return rule.version, chr(ord("A") + index)
        # 兜底：极端情况下权重和 < 100，落到最后一个
        return ordered[-1].version, chr(ord("A") + len(ordered) - 1)

    def variant_for(self, deployment: Deployment, version: int) -> str:
        """给定版本返回它的 Variant 名（供观测 / 归因使用）。"""
        ordered = _ordered_rules(deployment.rules)
        for index, rule in enumerate(ordered):
            if rule.version == version:
                return "single" if len(ordered) == 1 else chr(ord("A") + index)
        return "unknown"

    @staticmethod
    def expected_distribution(rules: list[DeploymentRule]) -> list[dict[str, Any]]:
        """导出期望流量分布（用于统计 / 实验报告）。"""
        ordered = _ordered_rules(rules)
        total = sum(r.weight for r in ordered) or 1
        return [
            {
                "version": r.version,
                "weight": r.weight,
                "percent": round(r.weight * 100 / total, 2),
            }
            for r in ordered
        ]
