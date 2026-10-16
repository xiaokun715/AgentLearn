"""调查工具集 —— Agent 能调用的全部「手脚」。

每个工具都只做一件事，并且返回**结构化的观测**（:class:`Observation`），
而不是一段自然语言。理由是第十五章就确立过的那条纪律：

    如果工具返回一段话，那么「这条证据到底支持哪个假设」
    就变成了模型的理解问题；返回结构化观测之后，它是查表问题。

## 三个工具的设计刻意做出了「区分力」

调查工具的价值不在于「能查到数据」，而在于**能区分假设**：

* ``query_transaction_history`` 会把**退款原因分布**带回来 ——
  「不想要了」和「商品质量-色差」都表现为高退款率，但一个指向套利、
  一个指向误报。只看比率是分不开的；
* ``query_device_graph`` 返回的是**账号列表**而不是一个计数，
  因为「3 个账号共享设备」和「3 个账号共享设备且注册间隔 90 秒」
  是两种完全不同的东西；
* ``query_account_profile`` 返回**注册时间**而不是「是否新账号」——
  布尔值会把「批量注册」这个信息压没。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from ..domain.enums import EvidenceKind, Observation
from ..domain.models import Evidence, ToolResult
from ..sim import DAY_MS, HOUR_MS, CaseRecord, RiskWorld


# ======================================================================
def _draft(kind: EvidenceKind, claim: str, payload: dict, source: str) -> Evidence:
    return Evidence(kind=kind, source=source, claim=claim, payload=payload)


def _inconclusive(tool: str, why: str, payload: dict) -> ToolResult:
    """统一的「查不到」返回。

    **它必须带证据。** 一次「查过了但没查到」本身就是一条证据 ——
    它是「数据不足」这个假设的依据。不带证据的话，下游无法区分
    「没查」和「查了没结果」，而这两者的处置完全不同（§4.4）。
    """
    return ToolResult(
        tool=tool, observation=Observation.INCONCLUSIVE, summary=why, payload=payload,
        evidence=[_draft(EvidenceKind.RAW, f"{tool}：{why}", payload, tool)],
    )


def query_account_profile(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    user_id = str(args.get("user_id", ""))
    if not world.available("account_service"):
        return _inconclusive("query_account_profile", "账号服务不可用", {"user_id": user_id})
    profile = world.users.get(user_id)
    if profile is None:
        return ToolResult(
            tool="query_account_profile", observation=Observation.INCONCLUSIVE,
            summary=f"账号 {user_id} 不存在或不可查",
            evidence=[_draft(EvidenceKind.OBSERVED, f"账号 {user_id} 无画像数据",
                             {"user_id": user_id}, "query_account_profile")],
        )

    is_new = profile.is_new
    # ``findings`` 只装**风险信号**。年龄、等级、订单数这些是「背景」，
    # 它们要放进 payload 供人判断，但不能被算成异常 ——
    # 「活跃老账号」被判成风险信号，会让一次本应中止的调查反向跑偏。
    findings: list[str] = []
    if is_new:
        findings.append(
            f"账号注册于 {abs(profile.registered_at_ms - 1_760_000_000_000) // HOUR_MS} 小时前（一天内）"
        )
    if profile.level == 0:
        findings.append("账号等级为 0（无历史行为沉淀）")
    if profile.claim_count_1d > 5:
        findings.append(f"单日领券 {profile.claim_count_1d} 次，超过阈值 5")

    context_note = []
    if profile.order_count > 100:
        context_note.append(f"历史订单 {profile.order_count} 笔（活跃老账号）")

    payload = {
        "user_id": user_id,
        "registered_at_ms": profile.registered_at_ms,
        "is_new": is_new,
        "level": profile.level,
        "order_count": profile.order_count,
        "claim_count_1d": profile.claim_count_1d,
        "refund_ratio_7d": round(profile.refund_ratio_7d, 4),
        "tags": profile.tags,
        "findings": findings,
        "context": context_note,
    }
    return ToolResult(
        tool="query_account_profile",
        observation=Observation.ANOMALY if findings else Observation.NORMAL,
        summary=("；".join(findings) if findings
                 else f"{user_id} 画像无明显异常"
                      + (f"（{'；'.join(context_note)}）" if context_note else "")),
        payload=payload,
        evidence=[_draft(
            EvidenceKind.OBSERVED,
            f"账号 {user_id} 画像：" + ("；".join(findings) if findings else "无明显异常"),
            payload, "query_account_profile",
        )],
    )


def query_device_graph(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    device_id = str(args.get("device_id", ""))
    if not world.available("device_graph"):
        return _inconclusive("query_device_graph", "设备图服务不可用（数据源故障）",
                             {"device_id": device_id})
    accounts = world.accounts_on_device(device_id)
    device = world.devices.get(device_id)
    payload = {
        "device_id": device_id,
        "accounts": accounts,
        "account_count": len(accounts),
        "model": device.model if device else "unknown",
        "os_version": device.os_version if device else "",
        "register_spread_ms": world.register_spread_ms(accounts) if accounts else 0,
        "is_farm_device": device_id in world.farm_devices,
    }
    anomalous = len(accounts) > 3
    summary = (
        f"设备 {device_id} 近期关联 {len(accounts)} 个账号"
        + (f"，注册时间跨度仅 {world.register_spread_ms(accounts) // 60_000} 分钟"
           if len(accounts) > 1 else "")
        if anomalous else f"设备 {device_id} 未发现多账号共用"
    )
    return ToolResult(
        tool="query_device_graph",
        observation=Observation.ANOMALY if anomalous else Observation.NORMAL,
        summary=summary, payload=payload,
        evidence=[_draft(EvidenceKind.OBSERVED, summary, payload, "query_device_graph")],
    )


def query_transaction_history(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    user_id = str(args.get("user_id", ""))
    if not world.available("trade_service"):
        return _inconclusive("query_transaction_history", "交易服务不可用",
                             {"user_id": user_id})
    records = [item for item in world.refunds if item[0] == user_id]
    if not records:
        return ToolResult(
            tool="query_transaction_history", observation=Observation.NORMAL,
            summary=f"{user_id} 近 7 日无退款记录",
            payload={"user_id": user_id, "refund_count": 0, "reason_distribution": {}},
            evidence=[_draft(EvidenceKind.OBSERVED, f"{user_id} 无退款记录",
                             {"user_id": user_id}, "query_transaction_history")],
        )

    reasons = Counter(item[4] for item in records)
    total_refund = sum(item[2] for item in records)
    total_order = sum(item[3] for item in records)
    ratio = total_refund / total_order if total_order else 0.0
    # **退款原因是区分「套利」与「商品质量」的关键**：
    # 两者的比率可能一样高，但一个是主动套利，一个是被动受害。
    quality_reasons = sum(count for reason, count in reasons.items() if "质量" in reason)
    abusive_reasons = sum(count for reason, count in reasons.items()
                          if reason in ("不想要了", "不喜欢", "拍错了"))

    payload = {
        "user_id": user_id,
        "refund_count": len(records),
        "refund_ratio": round(ratio, 4),
        "reason_distribution": dict(reasons),
        "quality_related": quality_reasons,
        "abusive_related": abusive_reasons,
        "amount": round(total_refund, 2),
    }
    if quality_reasons >= len(records) * 0.8:
        summary = (f"{user_id} 退款 {len(records)} 笔，均为商品质量原因 —— "
                   "指向商品问题而非套利")
        observation = Observation.NORMAL
    elif abusive_reasons >= len(records) * 0.8:
        summary = (f"{user_id} 退款 {len(records)} 笔，原因高度集中在"
                   f"{list(reasons)[0]!r}，退款率 {ratio:.0%} —— 指向套利")
        observation = Observation.ANOMALY
    else:
        summary = f"{user_id} 退款 {len(records)} 笔，原因分散：{dict(reasons)}"
        observation = Observation.INCONCLUSIVE

    return ToolResult(
        tool="query_transaction_history", observation=observation,
        summary=summary, payload=payload,
        evidence=[_draft(EvidenceKind.OBSERVED, summary, payload,
                         "query_transaction_history")],
    )


def query_coupon_usage(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    user_id = str(args.get("user_id", ""))
    records = [item for item in world.coupons if item[0] == user_id]
    payload = {
        "user_id": user_id,
        "claims": [{"campaign_id": item[1], "coupon_id": item[2], "at_ms": item[3]}
                   for item in records],
        "claim_count": len(records),
    }
    if not records:
        return ToolResult(tool="query_coupon_usage", observation=Observation.NORMAL,
                          summary=f"{user_id} 无领券记录", payload=payload)
    window_ms = max(item[3] for item in records) - min(item[3] for item in records)
    payload["claim_window_ms"] = window_ms
    anomalous = len(records) >= 5 or (len(records) >= 3 and window_ms <= 10 * 60_000)
    summary = (f"{user_id} 在 {window_ms // 60_000} 分钟内领取 {len(records)} 张券"
               if anomalous else f"{user_id} 领券 {len(records)} 张，节奏正常")
    return ToolResult(
        tool="query_coupon_usage",
        observation=Observation.ANOMALY if anomalous else Observation.NORMAL,
        summary=summary, payload=payload,
        evidence=[_draft(EvidenceKind.OBSERVED, summary, payload, "query_coupon_usage")],
    )


def query_ip_cluster(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    ip = str(args.get("ip", ""))
    accounts = world.accounts_on_ip(ip)
    payload = {"ip": ip, "accounts": accounts, "account_count": len(accounts)}
    anomalous = len(accounts) >= 10
    summary = (f"IP {ip} 聚集了 {len(accounts)} 个账号"
               if anomalous else f"IP {ip} 聚集 {len(accounts)} 个账号，属正常范围")
    return ToolResult(
        tool="query_ip_cluster",
        observation=Observation.ANOMALY if anomalous else Observation.NORMAL,
        summary=summary, payload=payload,
        evidence=[_draft(EvidenceKind.OBSERVED, summary, payload, "query_ip_cluster")],
    )


def query_risk_model_detail(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    """解释模型为什么这么打分。

    这个工具**不区分假设** —— 它回答的是「黑盒里发生了什么」，
    是调查的辅助信息而不是证据。规划器不会为了缩小不确定性选它（§4.4）。
    """
    score = float(args.get("model_score", 0.0))
    features = args.get("top_features", [])
    payload = {"model_score": score, "top_features": features}
    summary = (f"模型分 {score:.3f}，主要贡献：" +
               "、".join(f"{name}{value:+.3f}" for name, value in features))
    return ToolResult(
        tool="query_risk_model_detail", observation=Observation.INCONCLUSIVE,
        summary=summary, payload=payload,
        evidence=[_draft(EvidenceKind.OBSERVED, summary, payload,
                         "query_risk_model_detail")],
    )


def run_graph_analysis(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    """账号-设备-IP 关系图的连通分量分析。"""
    seed = str(args.get("device_id", ""))
    if not world.available("device_graph"):
        return _inconclusive("run_graph_analysis", "图计算所需的设备数据不可用",
                             {"device_id": seed})
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        root_l, root_r = find(left), find(right)
        if root_l != root_r:
            parent[root_r] = root_l

    for device_id, device in world.devices.items():
        for account in device.accounts:
            union(device_id, account)
    for ip, accounts in world.ips.items():
        for account in accounts:
            union(ip, account)

    component = [node for node in parent if find(node) == find(seed)] if seed in parent else []
    accounts_in_component = [node for node in component if node.startswith("u_")]
    devices_in_component = [node for node in component if node.startswith("dev_")]
    farm_devices = [item for item in devices_in_component if item in world.farm_devices]

    payload = {
        "seed": seed,
        "component_size": len(component),
        "accounts": sorted(accounts_in_component),
        "devices": sorted(devices_in_component),
        "farm_devices": sorted(farm_devices),
        "register_spread_ms": world.register_spread_ms(accounts_in_component),
    }
    anomalous = len(accounts_in_component) > 5 or bool(farm_devices)
    summary = (
        f"连通分量包含 {len(accounts_in_component)} 个账号 / "
        f"{len(devices_in_component)} 台设备"
        + (f"，其中 {len(farm_devices)} 台为已知群控设备" if farm_devices else "")
        + (f"，账号注册时间跨度 {payload['register_spread_ms'] // 60_000} 分钟"
           if len(accounts_in_component) > 1 else "")
    )
    return ToolResult(
        tool="run_graph_analysis",
        observation=Observation.ANOMALY if anomalous else Observation.NORMAL,
        summary=summary, payload=payload,
        evidence=[_draft(EvidenceKind.RAW, summary, payload, "run_graph_analysis")],
    )


def search_similar_cases(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    """检索历史案件。

    **它不区分假设** —— 历史案例提供的是「先例」，不是「这次是什么」。
    所以规划器不会为了缩小不确定性去选它；它出现的时机是
    「已经有一个假设被支持之后，去找同类案件的处置经验」。
    """
    keyword = str(args.get("keyword", ""))
    if not world.available("case_management"):
        return _inconclusive("search_similar_cases", "案件库不可用", {"keyword": keyword})
    matched: list[CaseRecord] = [
        case for case in world.cases
        if keyword and (keyword in case.pattern or keyword in case.description
                        or any(keyword in indicator for indicator in case.indicators))
    ] or list(world.cases)

    payload = {
        "keyword": keyword,
        "cases": [
            {"case_id": case.case_id, "pattern": case.pattern,
             "description": case.description, "confirmed": case.confirmed,
             "indicators": case.indicators, "resolution": case.resolution}
            for case in matched
        ],
    }
    if not matched:
        return ToolResult(
            tool="search_similar_cases", observation=Observation.INCONCLUSIVE,
            summary="未检索到相似案件 —— 可能是新形态",
            payload=payload,
        )
    confirmed = [case.case_id for case in matched if case.confirmed]
    summary = (f"检索到 {len(matched)} 条相似案件，其中已确认 {len(confirmed)} 条："
               + "、".join(case.pattern for case in matched[:3]))
    return ToolResult(
        tool="search_similar_cases",
        observation=Observation.ANOMALY if confirmed else Observation.INCONCLUSIVE,
        summary=summary, payload=payload,
        evidence=[_draft(EvidenceKind.OBSERVED, summary, payload, "search_similar_cases")],
    )


# ======================================================================
# 写类工具（会被网关按权限放行或拦下）
# ======================================================================
def create_case_ticket(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    alert_id = str(args.get("alert_id", ""))
    summary = str(args.get("summary", ""))
    ticket_id = f"TICKET_{abs(hash(alert_id)) % 100000:05d}"
    payload = {"ticket_id": ticket_id, "alert_id": alert_id, "summary": summary}
    return ToolResult(
        tool="create_case_ticket", observation=Observation.NORMAL,
        summary=f"已创建工单 {ticket_id}",
        payload=payload,
        evidence=[_draft(EvidenceKind.OBSERVED, f"工单 {ticket_id} 已创建",
                         payload, "create_case_ticket")],
    )


def apply_disposition(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    """执行处置。**正常情况下它永远不会被调用到**（网关会先拦下）。"""
    entity = str(args.get("entity_id", ""))
    action = str(args.get("action", ""))
    return ToolResult(
        tool="apply_disposition", observation=Observation.NORMAL,
        summary=f"已对 {entity} 执行 {action}",
        payload={"entity_id": entity, "action": action},
        evidence=[_draft(EvidenceKind.VERIFIED, f"{entity} 的处置 {action} 已执行",
                         {"entity_id": entity, "action": action}, "apply_disposition")],
    )


def release_block(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    entity = str(args.get("entity_id", ""))
    return ToolResult(
        tool="release_block", observation=Observation.NORMAL,
        summary=f"已解除 {entity} 的拦截",
        payload={"entity_id": entity},
    )


def update_rule_config(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    rule_id = str(args.get("rule_id", ""))
    threshold = args.get("threshold", 0.0)
    return ToolResult(
        tool="update_rule_config", observation=Observation.NORMAL,
        summary=f"规则 {rule_id} 阈值已改为 {threshold}",
        payload={"rule_id": rule_id, "threshold": threshold},
    )


def publish_strategy(world: RiskWorld, args: dict[str, Any]) -> ToolResult:
    candidate_id = str(args.get("candidate_id", ""))
    return ToolResult(
        tool="publish_strategy", observation=Observation.NORMAL,
        summary=f"策略 {candidate_id} 已发布",
        payload={"candidate_id": candidate_id},
    )


# ======================================================================
HANDLERS: dict[str, Callable[[RiskWorld, dict], ToolResult]] = {
    "query_account_profile": query_account_profile,
    "query_device_graph": query_device_graph,
    "query_transaction_history": query_transaction_history,
    "query_coupon_usage": query_coupon_usage,
    "query_ip_cluster": query_ip_cluster,
    "query_risk_model_detail": query_risk_model_detail,
    "run_graph_analysis": run_graph_analysis,
    "search_similar_cases": search_similar_cases,
    "create_case_ticket": create_case_ticket,
    "apply_disposition": apply_disposition,
    "release_block": release_block,
    "update_rule_config": update_rule_config,
    "publish_strategy": publish_strategy,
}


def resolve(name: str) -> Callable[[RiskWorld, dict], ToolResult]:
    handler = HANDLERS.get(name)
    if handler is None:
        raise KeyError(f"没有实现的调查工具：{name}")
    return handler


__all__ = ["HANDLERS", "resolve", "DAY_MS", "HOUR_MS"]
