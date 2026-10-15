"""共享 fixtures。

约定：
1. 每个测试独立构造配置与仿真，避免互相污染；
2. 「量容量」的测试一律用 ``upstream_bound`` / ``queue_only`` —— 见
   :func:`tests.conftest.upstream_bound` 的注释，那是本 Demo 最重要的一条实验纪律。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:      # 允许直接 pytest，不必先 pip install -e .
    sys.path.insert(0, str(ROOT))

from app.domain.models import GatewayConfig, WorkloadSpec  # noqa: E402
from app.domain.policy import DEFAULT_UPSTREAM, governed, steady_workload  # noqa: E402
from app.sim.engine import SimResult, Simulation  # noqa: E402


def upstream_bound(**overrides) -> GatewayConfig:
    """关掉缓存与合并 —— 让**上游**成为真正的瓶颈。

    测「容量 / 准入 / 清扫 / 扩容 / 重试 / 公平」时必须这样：
    如果缓存把九成请求都吸收了，你测的就不是「上游过载时系统怎么反应」，
    而是「缓存有多能扛」。**衡量的对象被另一个机制挡在了后面**，
    是这类实验里最容易犯的错误。
    """
    base = dict(l1_capacity=0, l2_capacity=0, semantic_cache_enabled=False,
                coalescing_enabled=False,
                # 降级也要关：降级会把服务时间缩短到 0.35 倍，等价于
                # 凭空把容量放大近 3 倍 —— 那量出来的就不是上游容量了。
                degrade_enabled=False)
    base.update(overrides)
    return governed(**base)


def queue_only(**overrides) -> GatewayConfig:
    """只留队列：关掉丢弃、准入、清扫，队列开到很大。

    分级丢弃在过载时非常霸道 —— 它会在准入之前把大部分请求拒掉，
    于是队列永远建不起来，「调度公不公平」「清扫释放了多少算力」
    这些问题就都观察不到了。**一个实验只回答一个问题。**
    """
    base = dict(shedding_enabled=False, admission_enabled=False,
                sweep_expired=False, queue_capacity=100_000)
    base.update(overrides)
    return upstream_bound(**base)


def simulate(config: GatewayConfig, workload: WorkloadSpec,
             upstream=None, **kwargs) -> SimResult:
    return Simulation(config, upstream or DEFAULT_UPSTREAM, workload, **kwargs).run()


@pytest.fixture
def run():
    return simulate


@pytest.fixture
def bound():
    return upstream_bound


@pytest.fixture
def queued():
    return queue_only


@pytest.fixture
def upstream():
    return DEFAULT_UPSTREAM


@pytest.fixture
def steady():
    return steady_workload


@pytest.fixture
def capacity() -> float:
    return DEFAULT_UPSTREAM.capacity_rps
