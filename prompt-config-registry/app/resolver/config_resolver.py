"""Config Resolver —— 运行时核心（设计说明书 §21~§22）。

Agent Runtime 不需要自己处理 dev/staging/prod、A/B、Canary、Rollback，
只需要：

    snapshot = await resolver.resolve(agent="test_case_agent",
                                      environment="prod",
                                      user_id="user_123")

Resolver 内部：
    Agent → Environment Binding → Deployment Rules → A/B Router
          → Version → Config Snapshot（展开 Prompt 模板）

缓存（§23）：
    · Deployment 路由表 —— 缓存到 ``deploy:{agent}:{env}``，发布/灰度/回滚时失效
    · Config Snapshot —— 缓存到版本化 key ``config:{agent}:v{version}``，
      因为版本不可变，同一 key 内容永远一致，天然避免「新 Prompt + 旧答案」。
"""
from __future__ import annotations

import json

from ..cache.base import ConfigCache
from ..cache.keys import deployment_key, snapshot_key
from ..domain.config import ResolvedSnapshot
from ..domain.deployment import Deployment
from ..domain.exceptions import NotFoundError
from ..registry.config_registry import ConfigRegistry
from ..registry.prompt_registry import PromptRegistry
from ..router.ab_router import AbRouter
from ..router.hash_router import bucket as hash_bucket
from ..storage.repository import RegistryRepository


class ConfigResolver:
    def __init__(
        self,
        repo: RegistryRepository,
        cache: ConfigCache,
        prompt_registry: PromptRegistry,
        config_registry: ConfigRegistry,
        ab_router: AbRouter,
        *,
        cache_ttl: int = 60,
    ) -> None:
        self._repo = repo
        self._cache = cache
        self._prompts = prompt_registry
        self._configs = config_registry
        self._router = ab_router
        self._cache_ttl = cache_ttl

    async def resolve(
        self, agent: str, environment: str, user_id: str
    ) -> ResolvedSnapshot:
        """解析出一次 Agent 执行要用的完整配置快照。"""
        deployment = await self._get_deployment(agent, environment)
        if deployment is None:
            raise NotFoundError(
                f"agent '{agent}' 在 environment '{environment}' 还没有部署，无法解析"
            )

        # A/B Router（§15）：hash(user_id) → bucket → version + variant
        version, variant = self._router.route(deployment, user_id)

        snapshot = await self._get_snapshot(agent, version)
        snapshot.routing = {
            "environment": environment,
            "experiment": deployment.experiment,
            "variant": variant,
            "bucket": hash_bucket(user_id, salt=deployment.experiment or ""),
            "rules": [r.to_dict() for r in deployment.rules],
        }
        return snapshot

    # ---- Deployment 路由（带缓存 + 失效）-----------------------------------
    async def _get_deployment(self, agent: str, environment: str) -> Deployment | None:
        key = deployment_key(agent, environment)
        raw = await self._cache.get(key)
        if raw is not None:
            return Deployment.from_dict(json.loads(raw))

        dep = await self._repo.get_deployment(agent, environment)
        if dep is not None:
            await self._cache.set(key, json.dumps(dep.to_dict()), ttl=self._cache_ttl)
        return dep

    # ---- Config Snapshot（版本化 key，无需失效）----------------------------
    async def _get_snapshot(self, agent: str, version: int) -> ResolvedSnapshot:
        key = snapshot_key(agent, version)
        raw = await self._cache.get(key)
        if raw is not None:
            return ResolvedSnapshot.from_dict(json.loads(raw))

        config = await self._configs.require_config(agent, version)
        pv = await self._prompts.require_version(config.prompt.name, config.prompt.version)

        snapshot = ResolvedSnapshot(
            agent=agent,
            config_version=version,
            prompt=pv.to_dict(),
            model=config.model.to_dict(),
            parameters=config.parameters.to_dict(),
            tools=config.tools,
            guardrails=config.guardrails,
        )
        await self._cache.set(key, json.dumps(snapshot.to_dict()), ttl=self._cache_ttl)
        return snapshot
