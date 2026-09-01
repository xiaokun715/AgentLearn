"""依赖装配（工厂模式）—— 按配置组装所有组件，供 main.py / 测试 / 脚本复用。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .audit.audit_service import AuditService
from .cache.base import ConfigCache
from .config import RegistryConfig
from .deployment.publisher import Publisher
from .deployment.rollback import RollbackService
from .registry.config_registry import ConfigRegistry
from .registry.prompt_registry import PromptRegistry
from .resolver.config_resolver import ConfigResolver
from .router.ab_router import AbRouter
from .storage.repository import RegistryRepository

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    """一次性持有全部组件，方便 API / 测试 / 脚本共用。"""

    config: RegistryConfig
    repo: RegistryRepository
    cache: ConfigCache
    prompt_registry: PromptRegistry
    config_registry: ConfigRegistry
    audit_service: AuditService
    publisher: Publisher
    rollback_service: RollbackService
    resolver: ConfigResolver
    ab_router: AbRouter
    _closers: list = field(default_factory=list)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        for closer in reversed(self._closers):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logger.exception("close failed")


async def build_runtime(config: RegistryConfig | None = None) -> Runtime:
    """构造完整运行环境。PostgreSQL / Redis 后端未装依赖时抛清晰的错误。"""
    config = config or RegistryConfig.from_env()

    # ---- 存储层 -------------------------------------------------------------
    closers: list = []
    if config.storage_backend == "memory":
        from .storage.memory import MemoryRepository

        repo: RegistryRepository = MemoryRepository()
    elif config.storage_backend == "postgres":
        try:
            from .storage.postgres import PostgresDatabase, PostgresRepository
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "STORAGE_BACKEND=postgres 需要 pip install prompt-config-registry[postgres]"
            ) from e
        db = PostgresDatabase(config.database_url)
        await db.connect()
        repo = PostgresRepository(db)
        closers.append(db.close)
    else:  # sqlite（默认）
        try:
            from .storage.sqlite import SqliteDatabase, SqliteRepository
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "默认 sqlite 后端需要 pip install prompt-config-registry[sqlite]"
            ) from e
        db = SqliteDatabase(config.sqlite_path)
        await db.connect()
        repo = SqliteRepository(db)
        closers.append(db.close)

    # ---- 缓存层 -------------------------------------------------------------
    if config.cache_backend == "redis":
        try:
            from .cache.redis_cache import RedisConfigCache

            cache: ConfigCache = RedisConfigCache(config.redis_url)
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "CACHE_BACKEND=redis 需要 pip install prompt-config-registry[redis]"
            ) from e
        closers.append(cache.close)
    else:
        from .cache.memory import MemoryConfigCache

        cache = MemoryConfigCache()

    # ---- 业务层 -------------------------------------------------------------
    audit_service = AuditService(repo)
    prompt_registry = PromptRegistry(repo, audit_service)
    config_registry = ConfigRegistry(repo, prompt_registry, audit_service)
    ab_router = AbRouter()
    publisher = Publisher(repo, cache, audit_service, config_registry)
    rollback_service = RollbackService(repo, cache, audit_service, config_registry)
    resolver = ConfigResolver(
        repo,
        cache,
        prompt_registry,
        config_registry,
        ab_router,
        cache_ttl=config.cache_ttl,
    )

    return Runtime(
        config=config,
        repo=repo,
        cache=cache,
        prompt_registry=prompt_registry,
        config_registry=config_registry,
        audit_service=audit_service,
        publisher=publisher,
        rollback_service=rollback_service,
        resolver=resolver,
        ab_router=ab_router,
        _closers=closers,
    )
