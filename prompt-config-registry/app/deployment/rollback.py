"""Rollback Service —— 回滚（设计说明书 §18 / §37）。

最重要的语义：**Rollback 不是删除新版本**。
  v13 的 Prompt / Config 仍然存在、仍然可以随时重新部署；
  Rollback 只是把 Deployment 的路由表还原到变更之前。

     Production   v13 (Tool Error 5%→20%)
        │
        ▼ Rollback
     Production   v12 (恢复)
"""
from __future__ import annotations

from ..audit.audit_service import AuditService
from ..cache.base import ConfigCache
from ..cache.keys import deployment_key
from ..domain.audit import AuditAction
from ..domain.deployment import Deployment, DeploymentRule, DeploymentStatus, utcnow
from ..domain.exceptions import DeploymentError, NotFoundError
from ..registry.config_registry import ConfigRegistry
from ..storage.repository import RegistryRepository


class RollbackService:
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

    async def rollback(
        self,
        deployment_id: str,
        *,
        target_version: int | None = None,
        created_by: str = "",
        reason: str = "",
    ) -> Deployment:
        """回滚到 ``target_version``；不指定则还原到 ``previous_rules`` 快照。"""
        dep = await self._require_deployment(deployment_id)
        before = dep.to_dict()

        if target_version is not None:
            # 显式指定目标版本（如实验对比后回退 v13 → v12）
            await self._configs.require_config(dep.agent_name, target_version)
            new_rules = [DeploymentRule(version=target_version, weight=100)]
        elif dep.previous_rules:
            # 还原到上一次发布 / 灰度之前的路由（一键回滚）
            new_rules = dep.previous_rules
        else:
            raise DeploymentError(
                f"{dep} 没有可回滚的历史（previous_rules 为空）"
            )

        dep.rules = new_rules
        dep.previous_rules = None  # 回滚后历史清空，避免连环回滚
        dep.status = DeploymentStatus.RELEASED
        dep.experiment = None
        dep.updated_at = utcnow()

        await self._repo.upsert_deployment(dep)
        # Cache Invalidation：路由变了
        await self._cache.delete(deployment_key(dep.agent_name, dep.environment))
        await self._audit.record(
            created_by,
            AuditAction.ROLLBACK,
            resource_type="deployment",
            resource_id=f"{dep.agent_name}:{dep.environment}",
            before=before,
            after=dep.to_dict(),
            reason=reason or "rollback",
        )
        return dep

    async def _require_deployment(self, deployment_id: str) -> Deployment:
        dep = await self._repo.get_deployment_by_id(deployment_id)
        if dep is None:
            raise NotFoundError(f"Deployment {deployment_id} 不存在")
        return dep
