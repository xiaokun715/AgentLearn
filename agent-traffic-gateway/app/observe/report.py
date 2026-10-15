"""终端报表 —— 把仿真结果画成人能一眼看懂的图。

流量治理的结论全是比例和曲线，用「一行一个数」打印出来是看不出问题的。
所以这里提供三种视图：

- :func:`table`      对比表（A/B 实验的主力）
- :func:`sparkline`  时间序列（队列深度/在途数的形状 —— 雪崩长什么样）
- :func:`bar_row`    横向条形图（容量曲线，一眼看出饱和点）

全部只用 ASCII/Unicode 块字符，不引入画图依赖 —— 这样它能出现在
CI 日志里，也能出现在 SSH 会话里（§16 的可观测性在演示里也要成立）。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from ..domain.enums import RejectReason

WIDTH = 82

#: 八级块字符。用它们而不是等宽柱状字符，是因为它能在**单行**里
#: 表达几百个采样点，而柱状图几百列就折行了。
_BLOCKS = "▁▂▃▄▅▆▇█"


# ======================================================================
# 基本格式
# ======================================================================
def ms(value: float) -> str:
    """毫秒 → 人读的字符串。"""
    if value <= 0:
        return "0"
    if value < 1000:
        return f"{int(value)}ms"
    return f"{value / 1000:.1f}s"


def pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def banner(text: str) -> None:
    print()
    print("=" * WIDTH)
    print(f"  {text}")
    print("=" * WIDTH)


def section(index: int, title: str, ref: str = "") -> None:
    suffix = f"  [{ref}]" if ref else ""
    print(f"\n── {index}. {title}{suffix}")
    print("─" * WIDTH)


def step(text: str) -> None:
    print(f"   → {text}")


def info(key: str, value: object) -> None:
    print(f"     {key:<26} {value}")


def ok(text: str) -> None:
    print(f"   ✓ {text}")


def warn(text: str) -> None:
    print(f"   ! {text}")


def brief(text: str, limit: int = 130) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ======================================================================
# 对比表
# ======================================================================
def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    indent: int = 5,
    aligns: Sequence[str] | None = None,
) -> None:
    """对齐的等宽表。``aligns`` 用 ``"l"`` / ``"r"`` 指定每列对齐。"""
    rows = [[str(cell) for cell in row] for row in rows]
    columns = len(headers)
    aligns = list(aligns or ["l"] * columns)
    widths = [len(str(headers[i])) for i in range(columns)]
    for row in rows:
        for i, cell in enumerate(row[:columns]):
            widths[i] = max(widths[i], len(cell))

    prefix = " " * indent
    head = "  ".join(
        str(headers[i]).ljust(widths[i]) if aligns[i] == "l"
        else str(headers[i]).rjust(widths[i])
        for i in range(columns)
    )
    print(f"{prefix}{head}")
    print(f"{prefix}{'-' * len(head)}")
    for row in rows:
        cells = []
        for i in range(columns):
            cell = row[i] if i < len(row) else ""
            cells.append(cell.ljust(widths[i]) if aligns[i] == "l" else cell.rjust(widths[i]))
        print(f"{prefix}{'  '.join(cells)}")


# ======================================================================
# 时间序列
# ======================================================================
def sparkline(values: Sequence[float], width: int = 68, label: str = "") -> str:
    """把一条时间序列压成一行块字符。

    适合看**形状**：队列深度是「一直贴着 0」还是「一路往上爬」，
    这个小图比任何数字都快。
    """
    if not values:
        return f"{label}（无数据）"
    data = list(values)
    if len(data) > width:                       # 等距抽稀
        step_size = len(data) / width
        data = [data[int(i * step_size)] for i in range(width)]
    low, high = min(data), max(data)
    span = high - low
    if span <= 0:
        chars = _BLOCKS[0] * len(data)
    else:
        chars = "".join(
            _BLOCKS[min(len(_BLOCKS) - 1, int((value - low) / span * len(_BLOCKS)))]
            for value in data
        )
    return f"{label}{chars}  min={_num(low)} max={_num(high)}"


def _num(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def bar_row(label: str, value: float, *, max_value: float, width: int = 34,
            suffix: str = "") -> str:
    """一行横向条形图。"""
    ratio = (value / max_value) if max_value > 0 else 0.0
    filled = int(round(ratio * width))
    return f"{label:<22} {'█' * filled}{'·' * (width - filled)} {suffix}"


def timeline_panel(series: dict[str, list[float]], keys: Sequence[tuple[str, str]]) -> None:
    """把几条时间序列画成一组 sparkline。"""
    for key, label in keys:
        print(f"     {sparkline(series.get(key, []), label=label)}")


# ======================================================================
# 结果解读
# ======================================================================
def result_row(label: str, result) -> list[str]:
    """把一次仿真压成对比表里的一行。"""
    summary = result.summary
    return [
        label,
        f"{summary['achieved_rps']:.2f}",
        pct(summary["success_ratio"]),
        ms(result.p50),
        ms(result.p99),
        f"{summary['retry_amplification']:.2f}",
        pct(summary["cache_offload"], 0),
    ]


RESULT_HEADERS = ["场景", "吞吐(RPS)", "成功", "P50", "P99", "重试放大", "减负"]
RESULT_ALIGNS = ["l", "r", "r", "r", "r", "r", "r"]


def rejection_breakdown(result, *, indent: int = 5) -> None:
    """§16.1：拒绝率必须**按原因分开**。混在一起看什么都判断不出来。"""
    reasons = result.summary["reject_reasons"]
    if not reasons:
        print(f"{' ' * indent}（没有被拒绝的请求）")
        return
    total = sum(reasons.values())
    rows = []
    for name, count in sorted(reasons.items(), key=lambda item: -item[1]):
        try:
            note = RejectReason(name).actionable
        except ValueError:
            note = ""
        rows.append([name, str(count), pct(count / total), note])
    table(["原因", "数量", "占比", "对调用方意味着什么"], rows, indent=indent,
          aligns=["l", "r", "r", "l"])


def outcome_breakdown(result, *, indent: int = 5) -> None:
    summary = result.summary
    rows = [
        ["OK", str(summary["succeeded"] - summary["degraded"])],
        ["DEGRADED（降级）", str(summary["degraded"])],
        ["REJECTED（我方拒绝）", str(summary["rejected"])],
        ["TIMEOUT（超期）", str(summary["timed_out"])],
        ["FAILED（上游失败）", str(summary["failed"])],
    ]
    table(["结果", "数量"], rows, indent=indent, aligns=["l", "r"])


def offload_breakdown(result, *, indent: int = 5) -> None:
    served = result.summary["served_by"]
    total = result.metrics.total or 1
    order = [
        ("upstream", "真正打到上游"),
        ("coalesced", "搭车（singleflight）"),
        ("cache_l1", "L1 进程内缓存"),
        ("cache_l2", "L2 共享缓存"),
        ("cache_semantic", "语义缓存"),
    ]
    rows = [
        [label, str(served.get(key, 0)), pct(served.get(key, 0) / total, 1)]
        for key, label in order
    ]
    table(["结果来源", "数量", "占比"], rows, indent=indent, aligns=["l", "r", "r"])


def note_outcome(result, *, indent: int = 5) -> str:
    """一行结论。"""
    summary = result.summary
    if summary["success_ratio"] >= 0.99 and result.p99 <= 2000:
        text = "健康：几乎全部成功，且 P99 在两次服务时间内"
    elif summary["success_ratio"] >= 0.9:
        text = "可用：有少量拒绝/超期，但主体请求正常"
    elif summary["success_ratio"] >= 0.7:
        text = "降级运行：相当一部分请求没有拿到结果"
    else:
        text = "不可用：多数请求失败，且失败原因主要是排队而非上游"
    return " " * indent + text
