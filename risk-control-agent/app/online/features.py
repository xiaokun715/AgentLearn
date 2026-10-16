"""特征计算（说明书 §1.2）。

特征层的职责只有一件事：**把事件流变成一组确定的数字**。
它不做判断，也不该做判断 —— 一旦特征里出现「if 这个值 > X 就算是风险」，
阈值就被复制到了两个地方，策略迭代时会漏改其中一个。

真机上这一层是 Flink/Spark 上的窗口聚合 + 特征平台。本 Demo 用一个
内存版的 :class:`FeatureComputer` 等价替代，但保留两个重要的性质：

* **每个特征都有来源**（``sources``）——「这个数从哪来的」必须可回答；
* **确定性与可缓存** —— 同样的输入得到同样的输出，否则规则无法回归。
"""
from __future__ import annotations

from ..domain.models import Event, FeatureSet
from ..sim import HOUR_MS, RiskWorld

#: 各阶段的模拟耗时（毫秒）。在线链路的总延迟必须是个位数毫秒 ——
#: 这是「为什么交易决策不经过 Agent」的量化依据（§1.4）。
FEATURE_LATENCY_MS = 1.2


class FeatureComputer:
    def __init__(self, world: RiskWorld) -> None:
        self.world = world

    # ------------------------------------------------------------------
    def compute(self, event: Event) -> FeatureSet:
        world = self.world
        values: dict[str, float] = {}
        sources: dict[str, str] = {}

        def put(name: str, value: float, source: str) -> None:
            values[name] = float(value)
            sources[name] = source

        profile = world.users.get(event.user_id)
        put("coupon_claims_1d",
            profile.claim_count_1d if profile else 0.0, "account-service")
        put("account_level", profile.level if profile else 0.0, "account-service")
        put("order_count", profile.order_count if profile else 0.0, "account-service")
        put("minutes_since_register",
            world.minutes_since_register(event.user_id), "account-service")

        device_accounts = world.accounts_on_device(event.device_id)
        put("accounts_per_device_1d", len(device_accounts), "device-graph")
        put("device_fingerprint_stable",
            1.0 if (world.devices.get(event.device_id, None)
                    and world.devices[event.device_id].fingerprint_stable) else 0.0,
            "device-graph")
        put("is_farm_device",
            1.0 if event.device_id in world.farm_devices else 0.0, "device-graph")

        put("accounts_per_ip_1h", len(world.accounts_on_ip(event.ip)), "network-service")

        put("refund_ratio_7d", profile.refund_ratio_7d if profile else 0.0,
            "trade-service")
        put("refund_count_7d", profile.refund_count_7d if profile else 0.0,
            "trade-service")

        # 场景特征：促销活动期间，退款/领券的基线本来就与平时不同（§6.2）
        campaign = world.campaigns.get(event.campaign_id)
        is_promo = bool(campaign and campaign.campaign_type.startswith("promo"))
        put("is_promo_scene", 1.0 if is_promo else 0.0, "campaign-service")

        # 时间窗特征：这组账号的注册聚集度（批量注册最直接的信号）
        spread_hours = world.register_spread_ms(device_accounts) / HOUR_MS
        put("device_register_spread_h", min(spread_hours, 24.0), "account-service")

        return FeatureSet(values=values, sources=sources,
                          computed_ms=int(FEATURE_LATENCY_MS))
