"""Publisher —— 发布 / 灰度（设计说明书 §14 / §17 / §20）。

核心：**版本不可变，部署可变**。这里只改 (agent, environment) 的路由表
（rules），从不修改任何 Version 内容。

状态机（§14）：STAGED → CANARY → RELEASED
  · publish 到 <100%  → CANARY（新版本小流量）
  · publish 到 100%   → RELEASED（全量）
  · rollout 逐步加流量 → CANARY 直到 100% → RELEASED
"""
from __future__ import annotations

import uuid
from typing import Any

from ..audit.audit_service import AuditService
from ..cache.base import ConfigCache
from ..cache.keys import deployment_key
from ..domain.audit import AuditAction
from ..domain.deployment import (
    Deployment,
    DeploymentRule,
    DeploymentStatus,
    utcnow,
)
from ..domain.exceptions import DeploymentError, NotFoundError
from ..registry.config_registry import ConfigRegistry
from ..router.canary import next_status
from ..storage.repository import RegistryRepository


class Publisher:
    """负责把某个 Config 版本发布到某个环境，以及调整灰度流量。"""

    def __init__(
        self,
        repo: RegistryRepository,
        cache: ConfigCache,
        audit: AuditService,
        config_registry: ConfigRegistry,
    ) -> None:
        self._repo = repo
        self._cache = cache
        self._audit = audit
        self._configs = config_registry

    async def publish(
        self,
        agent: str,
        environment: str,
        version: int,
        *,
        traffic_percent: int = 100,
        experiment: str | None = None,
        created_by: str = "",
        reason: str = "",
    ) -> Deployment:
        """把 ``version`` 发布到 ``environment``，初始流量 ``traffic_percent``。"""
        if not 0 <= traffic_percent <= 100:
            raise DeploymentError(f"traffic_percent 必须在 0~100，收到 {traffic_percent}")

        # 校验目标 Config 版本存在（不能发布指向空气的版本）
        await self._configs.require_config(agent, version)

        existing = await self._repo.get_deployment(agent, environment)
        now = utcnow()
        experiment = experiment if experiment is not None else (
            existing.experiment if existing else None
        )

        if existing is None:
            # 环境首次部署：没有对照组，直接 100%
            rules = [DeploymentRule(version=version, weight=100)]
            status = DeploymentStatus.RELEASED
        elif existing.primary_version == version:
            # 重新发布同一个版本：调整该版本自身流量
            rules = [DeploymentRule(version=version, weight=traffic_percent)]
            status = next_status(traffic_percent)
        elif traffic_percent >= 100:
            # 新版本直接全量：路由表里不再保留老版本
            rules = [DeploymentRule(version=version, weight=100)]
            status = DeploymentStatus.RELEASED
        else:
            # 典型 canary：老版本(对照组) + 新版本(实验组)
            incumbent = existing.primary_version
            assert incumbent is not None
            rules = [
                DeploymentRule(version=incumbent, weight=100 - traffic_percent),
                DeploymentRule(version=version, weight=traffic_percent),
            ]
            status = DeploymentStatus.CANARY

        dep = Deployment(
            id=existing.id if existing else str(uuid.uuid4()),
            agent_name=agent,
            environment=environment,
            status=status,
            rules=rules,
            experiment=experiment,
            previous_rules=existing.rules if existing else None,
            created_by=created_by,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        # 全量发布后实验结束
        if dep.is_single_version:
            dep.experiment = None

        await self._commit(
            dep,
            action=AuditAction.DEPLOY,
            before=existing.to_dict() if existing else None,
            created_by=created_by,
            reason=reason,
        )
        return dep

    async def rollout(
        self,
        deployment_id: str,
        version: int,
        traffic_percent: int,
        *,
        created_by: str = "",
        reason: str = "",
    ) -> Deployment:
        """灰度加（或减）流量。``version`` 为目标版本，``traffic_percent`` 为目标流量。"""
        if not 0 <= traffic_percent <= 100:
            raise DeploymentError(f"traffic_percent 必须在 0~100，收到 {traffic_percent}")

        dep = await self._require_deployment(deployment_id)
        await self._configs.require_config(dep.agent_name, version)

        # 状态机 + 流量合法性校验
        if dep.status == DeploymentStatus.RELEASED and traffic_percent < 100:
            raise DeploymentError(
                f"{dep} 已全量发布，不允许缩回流量；如需回退请走 rollback"
            )
        if dep.is_single_version and traffic_percent < 100:
            raise DeploymentError(
                f"{dep} 是单版本部署，无法原地切流量；请用 publish 引入候选版本做 canary"
            )

        before = dep.to_dict()
        # 注意：previous_rules 在 publish 时已定格为「引入当前版本之前的路由」，
        # rollout 不改它 —— 这样无论灰度到几，回滚都回到"发布前的老版本"（§18 / §37）。
        incumbent = dep.primary_version
        assert incumbent is not None

        if traffic_percent >= 100:
            new_rules = [DeploymentRule(version=version, weight=100)]
            status = DeploymentStatus.RELEASED
        elif version == incumbent:
            new_rules = [DeploymentRule(version=version, weight=traffic_percent)]
            status = DeploymentStatus.CANARY
        else:
            new_rules = [
                DeploymentRule(version=incumbent, weight=100 - traffic_percent),
                DeploymentRule(version=version, weight=traffic_percent),
            ]
            status = DeploymentStatus.CANARY

        dep.rules = new_rules
        dep.status = status
        dep.experiment = None if dep.is_single_version else dep.experiment
        dep.updated_at = utcnow()

        await self._commit(
            dep,
            action=AuditAction.ROLLOUT,
            before=before,
            created_by=created_by,
            reason=reason,
        )
        return dep

    async def _commit(
        self,
        dep: Deployment,
        *,
        action: str,
        before: dict[str, Any] | None,
        created_by: str,
        reason: str,
    ) -> Deployment:
        """保存 + 失效缓存 + 审计。"""
        await self._repo.upsert_deployment(dep)
        # Cache Invalidation（§23）：路由表变了，旧的 deploy:key 必须作废
        await self._cache.delete(deployment_key(dep.agent_name, dep.environment))
        await self._audit.record(
            created_by,
            action,
            resource_type="deployment",
            resource_id=f"{dep.agent_name}:{dep.environment}",
            before=before,
            after=dep.to_dict(),
            reason=reason,
        )
        return dep

    async def _require_deployment(self, deployment_id: str) -> Deployment:
        dep = await self._repo.get_deployment_by_id(deployment_id)
        if dep is None:
            raise NotFoundError(f"Deployment {deployment_id} 不存在")
        return dep
