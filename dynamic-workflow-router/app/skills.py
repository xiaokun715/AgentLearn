"""子 Agent 的实现层 —— 每个 handler 就是那个 Agent 真正干的活。

这一层刻意做得很薄，因为它不是本 Demo 的主角。但有两个设计点是必须的：

1. **handler 只通过黑板书读写**，不接收「上游是谁」这种信息。
   子 Agent 之间不直接调用，只通过共享上下文耦合 —— 这样编排器才能
   自由地重排、并行、替换它们（§10.1）。
2. **产出的形状稳定**。下游 agent 依赖上游产出的键，键名是数据流契约的一部分；
   形状飘忽会让模板在第一次真的跑起来时才暴露问题。

所有 handler 都是纯函数式的：给定上下文与 query，产出确定的结果。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .domain.models import Intent

Handler = Callable[[dict[str, Any], "Intent"], dict[str, Any]]


def _topic(intent: Intent) -> str:
    """从意图的槽位里取一个「主题」，让各 agent 的输出看起来是针对同一个问题。"""
    return str(intent.entities.get("subject") or intent.entities.get("module") or "目标对象")


# ======================================================================
# 测试排障域
# ======================================================================
def log_collector(ctx: dict, intent: Intent) -> dict[str, Any]:
    return {
        "logs": [
            f"[INFO] {_topic(intent)}: 开始执行",
            f"[WARN] {_topic(intent)}: 检查到与预期不一致的状态",
            f"[ERROR] ERR_MEASUREMENT_ANOMALY {_topic(intent)} 结果异常",
        ]
    }


def log_analyzer(ctx: dict, intent: Intent) -> dict[str, Any]:
    logs = ctx.get("logs", [])
    errors = [line for line in logs if "[ERROR]" in line]
    return {
        "error_events": [
            {"code": "ERR_MEASUREMENT_ANOMALY", "line": line.split(" ", 2)[-1]}
            for line in errors
        ]
    }


def env_prober(ctx: dict, intent: Intent) -> dict[str, Any]:
    return {"env_state": {"status": "NOT_READY", "detail": f"{_topic(intent)} 的环境未完成初始化"}}


def config_inspector(ctx: dict, intent: Intent) -> dict[str, Any]:
    return {"config_diff": [{"key": "measurement_period_ms", "expected": 20, "actual": 5}]}


def root_cause_analyst(ctx: dict, intent: Intent) -> dict[str, Any]:
    """这一位是「汇总者」：它必须等到证据齐了才有意义。"""
    evidence = {
        "error_events": len(ctx.get("error_events", [])),
        "env_state": ctx.get("env_state", {}).get("status", "UNKNOWN"),
        "config_diff": len(ctx.get("config_diff", [])),
    }
    return {
        "hypotheses": [
            {
                "cause": f"{_topic(intent)} 的环境未就绪",
                "supporting": evidence["env_state"] == "NOT_READY",
                "confidence": 0.78,
            },
            {
                "cause": f"{_topic(intent)} 的配置取值与需求不一致",
                "supporting": evidence["config_diff"] > 0,
                "confidence": 0.41,
            },
        ]
    }


def report_writer(ctx: dict, intent: Intent) -> dict[str, Any]:
    hypotheses = ctx.get("hypotheses", [])
    top = max(hypotheses, key=lambda item: item["confidence"]) if hypotheses else None
    return {
        "report": (
            f"结论：{top['cause']}（置信度 {top['confidence']:.2f}）"
            if top else "结论：证据不足，未能定位"
        )
    }


# ======================================================================
# 代码审查域
# ======================================================================
def code_reader(ctx: dict, intent: Intent) -> dict[str, Any]:
    return {
        "code": [
            "def query(user_input):",
            "    sql = 'SELECT * FROM users WHERE name = %s' % user_input",
            "    return db.execute(sql)",
        ]
    }


def security_reviewer(ctx: dict, intent: Intent) -> dict[str, Any]:
    code = "\n".join(ctx.get("code", []))
    findings = []
    if "%s" in code and "+" in code or "%" in code:
        findings.append({"rule": "CWE-89", "severity": "HIGH", "detail": "疑似 SQL 注入"})
    return {"security_findings": findings or [{"rule": "none", "severity": "LOW", "detail": "未发现明显问题"}]}


def style_reviewer(ctx: dict, intent: Intent) -> dict[str, Any]:
    return {"style_findings": [{"rule": "N803", "detail": "函数名建议使用动词短语"}]}


# ======================================================================
# 数据分析域
# ======================================================================
def metric_query(ctx: dict, intent: Intent) -> dict[str, Any]:
    return {"metrics": {"throughput": [12.0, 11.4, 9.8, 6.1, 3.2], "window": "5m"}}


def trend_analyzer(ctx: dict, intent: Intent) -> dict[str, Any]:
    series = ctx.get("metrics", {}).get("throughput", [])
    if len(series) >= 2:
        delta = series[-1] - series[0]
        direction = "下降" if delta < 0 else "上升"
    else:
        delta, direction = 0.0, "无数据"
    return {"trend": {"direction": direction, "delta": round(delta, 2), "series": series}}


def chart_renderer(ctx: dict, intent: Intent) -> dict[str, Any]:
    series = ctx.get("metrics", {}).get("throughput", [])
    blocks = "▁▂▃▄▅▆▇█"
    if series:
        low, high = min(series), max(series)
        span = high - low or 1.0
        chart = "".join(blocks[min(7, int((v - low) / span * 8))] for v in series)
    else:
        chart = ""
    return {"chart": chart}


# ======================================================================
# 通用
# ======================================================================
def knowledge_searcher(ctx: dict, intent: Intent) -> dict[str, Any]:
    return {"references": [f"RFC/规范中关于 {_topic(intent)} 的条目", "历史相似案例 3 条"]}


def summarizer(ctx: dict, intent: Intent) -> dict[str, Any]:
    parts = [f"{key}: {len(value) if isinstance(value, list) else value}"
             for key, value in ctx.items() if not key.startswith("_")]
    return {"summary": "；".join(parts[:5]) or "无内容"}


HANDLERS: dict[str, Handler] = {
    "log_collector": log_collector,
    "log_analyzer": log_analyzer,
    "env_prober": env_prober,
    "config_inspector": config_inspector,
    "root_cause_analyst": root_cause_analyst,
    "report_writer": report_writer,
    "code_reader": code_reader,
    "security_reviewer": security_reviewer,
    "style_reviewer": style_reviewer,
    "metric_query": metric_query,
    "trend_analyzer": trend_analyzer,
    "chart_renderer": chart_renderer,
    "knowledge_searcher": knowledge_searcher,
    "summarizer": summarizer,
}


def resolve(name: str) -> Handler:
    handler = HANDLERS.get(name)
    if handler is None:
        raise KeyError(f"没有注册子 Agent 的实现：{name}")
    return handler
