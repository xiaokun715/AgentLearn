"""模拟的业务世界 —— 在线链路与 Agent 链路共享的同一份数据。

这里同时扮演四个角色（真机上它们是四个独立系统）：

    account-service    账号画像
    device-graph       设备关联
    trade-service      交易与退款明细
    case-management    历史案件库

**为什么把它们放在一个文件里**：本 Demo 要考察的是「两条链路怎么分工」，
不是「四个微服务怎么拼」。但每个数据源都保留了**可用性开关**
（:attr:`RiskWorld.unavailable_sources`）—— 因为「数据源挂了」是调查场景里
最常见的真实状态之一，而一个不能表达「查不到」的 Demo 会掩盖掉
「证据不足」这一类结论（§4.5）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 演示用的时间基准（毫秒）—— 与真实系统的差异只在绝对值
T0 = 1_760_000_000_000
DAY_MS = 86_400_000
HOUR_MS = 3_600_000


@dataclass
class UserProfile:
    user_id: str
    registered_at_ms: int
    level: int = 1
    order_count: int = 0
    claim_count_1d: int = 0
    refund_count_7d: int = 0
    refund_amount_7d: float = 0.0
    order_amount_7d: float = 0.0
    tags: list[str] = field(default_factory=list)

    @property
    def refund_ratio_7d(self) -> float:
        return self.refund_amount_7d / self.order_amount_7d if self.order_amount_7d else 0.0

    @property
    def is_new(self) -> bool:
        return self.registered_at_ms >= T0 - DAY_MS


@dataclass
class DeviceProfile:
    device_id: str
    model: str = "generic"
    os_version: str = ""
    #: 该设备近期出现过的账号
    accounts: list[str] = field(default_factory=list)
    #: 设备指纹的稳定性（模拟器/群控设备的指纹往往异常一致）
    fingerprint_stable: bool = True
    first_seen_ms: int = T0


@dataclass
class CaseRecord:
    case_id: str
    pattern: str
    description: str
    confirmed: bool
    indicators: list[str] = field(default_factory=list)
    resolution: str = ""


@dataclass
class Campaign:
    campaign_id: str
    name: str
    campaign_type: str = "normal"
    started_at_ms: int = T0


class RiskWorld:
    """一次场景的全部数据。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.users: dict[str, UserProfile] = {}
        self.devices: dict[str, DeviceProfile] = {}
        self.ips: dict[str, list[str]] = {}
        self.campaigns: dict[str, Campaign] = {}
        self.cases: list[CaseRecord] = []
        #: 退款明细：``(user_id, order_id, refund_amount, order_amount, reason)``
        self.refunds: list[tuple[str, str, float, float, str]] = []
        #: 领券记录：``(user_id, campaign_id, coupon_id, claimed_at_ms)``
        self.coupons: list[tuple[str, str, str, int]] = []
        #: 不可用的数据源 —— 调查工具会因此返回 INCONCLUSIVE
        self.unavailable_sources: set[str] = set()
        #: 模拟器/群控设备（用于「设备农场」模式的识别）
        self.farm_devices: set[str] = set()
        #: **裁判视角**的真相。Agent 看不到它 —— 它只用于场景脚本里
        #: 判断「这次调查的结论对不对」。真机上这个位置是人工复核结果。
        self.truth: dict[str, str] = {}
        #: 已标注样本：``user_id -> 是否确认是黑产``。方式二（未知风险发现）
        #: 的离线回放要在它上面算召回与准确率 —— **没有标注集，就没有验证**。
        self.labelled_users: dict[str, bool] = {}
        #: 已复盘的历史告警：方式三（策略运营）的 badcase 分析输入
        self.reviewed_alerts: list[dict] = []

    # ==================================================================
    def add_user(
        self,
        user_id: str,
        *,
        registered_at_ms: int = T0,
        level: int = 1,
        order_count: int = 0,
        claim_count_1d: int = 0,
        device_id: str = "",
        ip: str = "",
        tags: list[str] | None = None,
    ) -> UserProfile:
        profile = UserProfile(
            user_id=user_id, registered_at_ms=registered_at_ms, level=level,
            order_count=order_count, claim_count_1d=claim_count_1d,
            tags=list(tags or []),
        )
        self.users[user_id] = profile
        if device_id:
            self.devices.setdefault(device_id, DeviceProfile(device_id=device_id))
            self.devices[device_id].accounts.append(user_id)
        if ip:
            self.ips.setdefault(ip, []).append(user_id)
        return profile

    def add_device(self, device_id: str, *, model: str = "generic",
                   os_version: str = "", farm: bool = False) -> DeviceProfile:
        """登记/更新一台设备。

        **必须保留已有的账号关联。** 早先版本直接 ``self.devices[id] = ...``，
        于是「先 add_user（建立了关联）、后 add_device（补型号）」的顺序
        会把关联清空 —— 设备图返回空列表，调查结论因此完全反过来。
        这类 bug 不会报错，只会安静地给出错误的结论。
        """
        profile = self.devices.get(device_id)
        if profile is None:
            profile = DeviceProfile(device_id=device_id)
            self.devices[device_id] = profile
        profile.model = model
        profile.os_version = os_version
        if farm:
            self.farm_devices.add(device_id)
        return profile

    def add_refund(self, user_id: str, *, amount: float, order_amount: float,
                   reason: str = "unspecified") -> None:
        profile = self.users[user_id]
        profile.refund_count_7d += 1
        profile.refund_amount_7d += amount
        profile.order_amount_7d += order_amount
        self.refunds.append((user_id, f"ord_{len(self.refunds):04d}", amount,
                             order_amount, reason))

    def add_coupon(self, user_id: str, *, campaign_id: str, claimed_at_ms: int) -> None:
        self.coupons.append(
            (user_id, campaign_id, f"cpn_{len(self.coupons):04d}", claimed_at_ms)
        )

    def add_case(self, case_id: str, *, pattern: str, description: str,
                 confirmed: bool, indicators: list[str] | None = None,
                 resolution: str = "") -> None:
        self.cases.append(CaseRecord(case_id=case_id, pattern=pattern,
                                     description=description, confirmed=confirmed,
                                     indicators=list(indicators or []),
                                     resolution=resolution))

    # ==================================================================
    # 数据源可用性
    # ==================================================================
    def available(self, source: str) -> bool:
        return source not in self.unavailable_sources

    def disable(self, *sources: str) -> None:
        self.unavailable_sources.update(sources)

    # ==================================================================
    # 特征计算要用到的聚合查询
    # ==================================================================
    def accounts_on_device(self, device_id: str) -> list[str]:
        device = self.devices.get(device_id)
        return list(device.accounts) if device else []

    def accounts_on_ip(self, ip: str) -> list[str]:
        return list(self.ips.get(ip, []))

    def minutes_since_register(self, user_id: str) -> float:
        profile = self.users.get(user_id)
        if profile is None:
            return 999.0
        return max(0.0, (T0 - profile.registered_at_ms) / 60_000.0)

    def register_spread_ms(self, user_ids: list[str]) -> int:
        """一组账号的注册时间跨度 —— 「批量注册」最直接的信号。"""
        times = [self.users[uid].registered_at_ms for uid in user_ids if uid in self.users]
        return (max(times) - min(times)) if len(times) >= 2 else DAY_MS


# ======================================================================
# 场景构造
# ======================================================================
def world_group_fraud() -> RiskWorld:
    """真团伙：批量注册 + 共享设备 + 集中退款套利。

    这是传统规则能识别的典型形态（单账号规则 + 设备规则都会命中），
    Agent 的价值在于**把证据串成一份可复审的报告**，而不是「发现」它。
    """
    world = RiskWorld("group_fraud")
    world.campaigns["cmp_001"] = Campaign("cmp_001", "双十一大促", "promo_double11")
    world.add_device("dev_pool_a", model="Redmi Note 12", os_version="Android 13", farm=True)
    world.add_device("dev_pool_b", model="Redmi Note 12", os_version="Android 13", farm=True)

    for index in range(12):
        uid = f"u_fraud_{index:02d}"
        device = "dev_pool_a" if index < 7 else "dev_pool_b"
        world.add_user(
            uid,
            registered_at_ms=T0 - 2 * HOUR_MS + index * 90_000,   # 2 小时内注册
            level=0,
            claim_count_1d=6,                                     # 超过 R001 阈值 5
            device_id=device,
            ip=f"10.20.{index // 4}.{index % 4 + 10}",
            tags=["new"],
        )
        world.add_coupon(uid, campaign_id="cmp_001", claimed_at_ms=T0 - HOUR_MS)
        world.add_refund(uid, amount=88.0, order_amount=100.0, reason="不想要了")

    world.truth["u_fraud_00"] = "CONFIRMED_RISK"
    world.add_case(
        "CASE_2025_0912", pattern="DEVICE_POOL_COUPON_FARM",
        description="设备农场批量注册领券后退款套利",
        confirmed=True,
        indicators=["同型号设备集中出现", "注册间隔 < 2 分钟", "退款率 > 80%"],
        resolution="封禁设备池 + 追回券成本",
    )
    return world


def world_false_positive() -> RiskWorld:
    """误报：退款率高的原因是商品质量问题，账号/设备都正常。

    Agent 的价值在这里最明显：**它要否掉一条已经命中的规则**。
    这比确认风险难得多 —— 确认只需要找到支持证据，否掉需要找到
    「看起来像但其实是另一回事」的解释。
    """
    world = RiskWorld("false_positive")
    world.campaigns["cmp_001"] = Campaign("cmp_001", "双十一大促", "promo_double11")

    for index in range(3):
        uid = f"u_normal_{index:02d}"
        world.add_user(
            uid,
            registered_at_ms=T0 - (400 + index * 90) * DAY_MS,    # 老账号
            level=4,
            order_count=120 + index * 30,
            device_id=f"dev_own_{index}",                          # 各自的设备
            ip=f"172.16.1.{index + 5}",
            tags=["vip"],
        )
        world.add_device(f"dev_own_{index}", model=f"iPhone 1{index}", os_version="iOS 17")
        # 同一批商品的退款 —— 这是「商品质量问题」的典型形态
        for _ in range(6):
            world.add_refund(uid, amount=95.0, order_amount=100.0,
                             reason="商品质量-色差")
        world.add_coupon(uid, campaign_id="cmp_001", claimed_at_ms=T0 - 3 * HOUR_MS)

    world.truth["u_normal_00"] = "LIKELY_FALSE_POSITIVE"
    world.add_case(
        "CASE_2025_0777", pattern="PRODUCT_QUALITY_REFUND_CLUSTER",
        description="同一批次商品质量导致集中退款，非恶意",
        confirmed=False,
        indicators=["退款原因集中在商品质量", "账号历史良好", "无设备关联"],
        resolution="判定误报，同步商品侧处理质量问题",
    )
    return world


def world_sparse_data() -> RiskWorld:
    """数据缺失：设备图不可用，交易明细只回了一部分。

    用来演示 ``INSUFFICIENT_EVIDENCE`` 这个结论 ——
    它是真实调查里**最常见**的结果，也是最容易被系统藏起来的结果。
    """
    world = RiskWorld("sparse_data")
    # 领券 2 次 —— 刻意不命中任何规则，让这个案子的**唯一特征**就是数据缺失
    world.add_user("u_sparse_00", registered_at_ms=T0 - 30 * DAY_MS, level=2,
                   order_count=8, claim_count_1d=2, device_id="dev_x", ip="10.9.9.9")
    world.add_device("dev_x", model="unknown")
    world.add_refund("u_sparse_00", amount=50.0, order_amount=60.0, reason="商品质量-破损")
    # **只停设备图**：这样 Agent 能查完其它维度，最后诚实地报告
    # 「设备维度无法验证」—— 而固定流程会把查不到当成没问题。
    world.disable("device_graph")
    world.truth["u_sparse_00"] = "INSUFFICIENT_EVIDENCE"
    return world


def world_device_farm_evasion() -> RiskWorld:
    """规避型团伙：**每一条现有规则都不命中**。

    30 个账号，各自一台设备，各自领 3 张券（阈值 5），退款率 40%（阈值 55%）。
    单看每一个账号都正常。

    但整体上有三个信号：设备都是同一型号的模拟器、注册时间集中在 90 分钟内、
    并且这批设备的指纹异常一致。

    这正是方式二（未知风险发现）要处理的问题 —— 也是「为什么不能只靠规则」
    的最直接证据。
    """
    world = RiskWorld("device_farm_evasion")
    world.campaigns["cmp_001"] = Campaign("cmp_001", "双十一大促", "promo_double11")
    for index in range(30):
        uid = f"u_farm_{index:02d}"
        device = f"dev_farm_{index:02d}"
        # 刻意与正常用户里的「型号字符串不可用」那一群长得一样 ——
        # 单看型号是分不开的（§5.2 的起点）
        world.add_device(device, model="generic_android", os_version="Android 11",
                         farm=True)
        world.add_user(
            uid,
            registered_at_ms=T0 - 90 * 60_000 + index * 180_000,   # 90 分钟内
            level=1,
            order_count=2,
            claim_count_1d=3,                                       # 未超过阈值 5
            device_id=device,
            ip=f"10.30.{index // 10}.{index % 10 + 1}",
            tags=["new"],
        )
        world.add_coupon(uid, campaign_id="cmp_001", claimed_at_ms=T0 - HOUR_MS)
        # 退款率 40% —— 未超过阈值 55%
        world.add_refund(uid, amount=40.0, order_amount=100.0, reason="不想要了")

    # ---- 标注样本 ----------------------------------------------------
    # 30 个真实黑产 + 500 个正常用户。**正常用户里有相当一部分也用同样的
    # 设备型号字符串、也落在同一个注册时间窗内** —— 这正是「单看某个信号
    # 精度很低」的原因，也是离线回放要回答的问题。
    world.labelled_users.update({f"u_farm_{i:02d}": True for i in range(30)})
    for index in range(500):
        uid = f"u_legit_{index:03d}"
        generic_model = index % 4 == 0          # 125 人设备型号字符串也是 generic
        in_window = index % 25 == 0             # 20 人注册时间也落在窗口内
        registered = (T0 - 45 * 60_000) if in_window else (T0 - (30 + index) * DAY_MS)
        device = f"dev_legit_{index:03d}"
        world.add_device(device,
                         model="generic_android" if generic_model else f"RealPhone {index % 7}",
                         os_version="Android 11")
        world.add_user(uid, registered_at_ms=registered, level=2,
                       order_count=20 + index % 50, claim_count_1d=1 + index % 2,
                       device_id=device, ip=f"100.64.{index // 100}.{index % 100 + 1}")
        world.add_coupon(uid, campaign_id="cmp_001", claimed_at_ms=T0 - HOUR_MS)
        world.labelled_users[uid] = False

    # ---- 方式三：已复盘的 R003 告警 -----------------------------------
    # 200 条人工复盘过的 R003 告警，其中相当一部分是误报，而且**误报
    # 集中在促销场景** —— 这正是策略分析要发现的模式。
    for index in range(200):
        promo = index % 2 == 0
        is_fp = (index % 10 < (7 if promo else 2))
        world.reviewed_alerts.append({
            "alert_id": f"alert_rev_{index:04d}",
            "rule_id": "R003_REFUND_RATIO",
            "user_id": f"u_rev_{index:03d}",
            "scene": "promo" if promo else "normal",
            "campaign_type": "promo_double11" if promo else "normal",
            "human_label": "FALSE_POSITIVE" if is_fp else "TRUE_RISK",
            "refund_ratio": 0.60 + (index % 30) / 100.0,
            "refund_count": 5 + index % 12,
            "account_level": 1 + index % 5,
            "order_count": 3 + index % 40,
        })

    world.truth["u_farm_00"] = "UNKNOWN_DISPERSED_PATTERN"
    world.add_case(
        "CASE_2025_0301", pattern="EMULATOR_FARM_DISPERSED",
        description="模拟器农场分散操作规避单账号规则",
        confirmed=False,     # **历史上没有被确认过** —— 这就是「未知」
        indicators=["设备型号高度一致", "注册时间集中", "单账号行为均正常"],
        resolution="样本不足，当年未定论",
    )
    return world
