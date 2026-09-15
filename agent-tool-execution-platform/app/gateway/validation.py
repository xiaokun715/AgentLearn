"""参数校验与参数自愈 —— §21 / §22 / §23 的落点。

对齐《第十二章：可靠工具执行系统设计说明书》：
- §21 Schema Validation：LLM 生成的参数**不可信**，先过 ``args_model``
- §22 Parameter Repair：校验失败不是直接失败，而是「自愈 -> 再校验」
- §23 自愈的边界：**只做形状/类型转换，绝不改变语义**
- §25 ``ParamRule``：自愈之后的业务约束（区间 / 白名单 / 前缀 / 正则）
- §41 状态机 ``CREATED → VALIDATING → QUEUED`` 里的 VALIDATING 阶段
- §65 场景：``{"timeout": "300", "test_cases": "TC001"}`` -> ``{"timeout": 300, "test_cases": ["TC001"]}``

流水线（§21 原文的四段式）::

    Schema Validation ──失败──► Parameter Repair ──► Validation ──► Execute
            │                          │                 │
            └──────────── 通过 ────────┴──── 仍失败 ──────┴──► REJECTED（§34）

三段职责分离（这是本模块最重要的设计决定）：

============================ ============================================================
组件                          负责什么
============================ ============================================================
Pydantic ``args_model``       形状：字段是否存在、类型对不对、默认值填不填
:class:`ParameterRepairer`    可枚举的**确定**修复（``"300"``->``300``、``"TC001"``->``["TC001"]``）
:class:`ArgumentValidator`    业务约束（§25 ``ParamRule``）+ 注入检测（§24）
============================ ============================================================

**为什么自愈必须与业务约束分离**：自愈能回答「这里应该是一个 int」，
但它**不能**回答「300000 这个值对业务是否合理」。把两者混在一起，
就会出现「LLM 说 timeout 应该是 300000，于是它就变成了 300000」——
即 §23 明令禁止的 *LLM Repair Only*。
"""
from __future__ import annotations

import logging
import os
import re
import types
from collections.abc import Collection, Iterable, MutableSequence, Sequence, Set
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from ..config import AppConfig
from ..domain.models import ToolCall
from ..domain.policy import ParamPolicy, ParamRule
from ..tools.registry import ToolRegistry
from .injection import InjectionDetector

logger = logging.getLogger(__name__)


# ======================================================================
# 结果值对象
# ======================================================================
@dataclass
class RepairStep:
    """一次参数修复的审计记录（§22）。

    :param field: 被修的字段名（数组元素用 ``test_cases[0]`` 形式）
    :param before: 修复前的值
    :param after: 修复后的值
    :param rule: 修复规则名，如 ``str->int`` / ``scalar->list`` / ``drop_unknown``
    """

    field: str
    before: Any
    after: Any
    rule: str

    def to_dict(self) -> dict[str, Any]:
        """序列化进 ``execution_event.payload`` —— 自愈必须是**可复盘**的。"""
        return {
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "rule": self.rule,
        }


@dataclass
class ValidationOutcome:
    """一次参数校验/自愈的完整结论。

    :param ok: 是否已经得到**可执行**的参数
    :param arguments: 修复后的参数（仅包含调用方真正传了的字段，不含默认值 ——
        默认值由 Pydantic 在执行前补齐，这样 ``arguments`` 与 Agent 的意图一一对应）
    :param repaired: 沿途做过的修复，供审计与「Agent 参数质量」度量
    :param errors: 失败原因（人类可读，会被喂给 LLM 修复提示词）
    :param repaired_by_llm: 是否动用了 LLM 兜底（用于观测「LLM 修复率」，
        这个比例长期偏高说明 Tool 的 schema 太苛刻或提示词太差）
    """

    ok: bool
    arguments: dict[str, Any]
    repaired: list[RepairStep] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    repaired_by_llm: bool = False

    @property
    def repair_count(self) -> int:
        return len(self.repaired)

    def describe(self) -> str:  # pragma: no cover - 展示用
        if self.ok:
            rules = ", ".join(step.rule for step in self.repaired) or "无修复"
            return f"ok=True repairs=[{rules}] llm={self.repaired_by_llm}"
        return "ok=False errors=" + "; ".join(self.errors)


# LLM 兜底修复回调：(原始参数, 错误列表, 约束描述) -> 建议参数
LLMRepairFn = Callable[[dict[str, Any], list[str], dict[str, Any]], dict[str, Any]]


# ======================================================================
# 类型识别 helper（Pydantic v2 的 annotation 是 typing 对象，要自己拆）
# ======================================================================
_SCALAR_NAMES: dict[Any, str] = {
    int: "integer",
    float: "number",
    bool: "boolean",
    str: "string",
}

_INT_RE = re.compile(r"[+-]?\d+")
_FLOAT_RE = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_NUM_WITH_UNIT_RE = re.compile(r"([+-]?(?:\d+\.\d*|\.\d+|\d+))\s*([A-Za-z]{0,12})")

# 字段名 -> 基准单位。**顺序敏感**：先 ``_ms`` 后 ``_s``，否则 ``timeout_ms`` 会被判成秒。
_UNIT_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("_ms", "_millis", "_milliseconds"), "ms"),
    (("timeout",), "ms"),  # §23 例子里 "300ms" -> 300 就是针对这个字段
    (("_s", "_sec", "_secs", "_second", "_seconds"), "s"),
)

_UNIT_ALIASES: dict[str, set[str]] = {
    "ms": {"ms", "msec", "msecs", "milli", "millis", "millisecond", "milliseconds"},
    "s": {"s", "sec", "secs", "second", "seconds"},
}


def _strip_annotated(annotation: Any) -> Any:
    """剥掉 ``Annotated[...]`` 外壳。"""
    while hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    return annotation


def _is_optional(annotation: Any) -> bool:
    """``Optional[X]`` / ``X | None`` 判定（两种写法都要认）。"""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return type(None) in get_args(annotation)
    return False


def _unwrap_optional(annotation: Any) -> Any:
    annotation = _strip_annotated(annotation)
    if _is_optional(annotation):
        for arg in get_args(annotation):
            if arg is not type(None):
                return _strip_annotated(arg)
    return annotation


def _scalar_name(annotation: Any) -> Optional[str]:
    """annotation 是否是标量类型，返回 ``integer`` / ``number`` / ``boolean`` / ``string``。"""
    return _SCALAR_NAMES.get(_unwrap_optional(annotation))


def _list_element(annotation: Any) -> Any:
    """annotation 是否是 ``list[T]`` 这类序列，返回元素类型 ``T``（不是则 None）。"""
    core = _unwrap_optional(annotation)
    origin = get_origin(core)
    if origin in (
        list,
        set,
        frozenset,
        tuple,
        Sequence,
        MutableSequence,
        Set,
        Iterable,
        Collection,
    ):
        args = get_args(core)
        if not args:
            return None
        if origin is tuple and len(args) > 1 and args[-1] is not Ellipsis:
            # 定长元组（Tuple[int, str]）元素位置有语义，不猜
            return None
        return args[0]
    return None


def _base_unit(field_name: str) -> Optional[str]:
    """字段的基准单位（``timeout`` -> ``ms``，``chunk_seconds`` -> ``s``）。

    只用于「去掉与基准单位一致的后缀」，**永远不用来换算**（§23）。
    """
    low = field_name.lower()
    for suffixes, unit in _UNIT_HINTS:
        if any(low.endswith(suffix) for suffix in suffixes):
            return unit
    return None


def _is_scalar_value(value: Any) -> bool:
    """是否是「一个值」而不是「一堆值」。"""
    return not isinstance(value, (list, tuple, set, frozenset, dict, bytes, bytearray))


# ======================================================================
# 确定性修复：单值类型转换（§21 的例子 + §23 的约束）
# ======================================================================
def _coerce_scalar(
    value: Any, target: str, *, field_name: str
) -> tuple[Any, Optional[str]]:
    """把一个值往目标标量类型上靠，返回 ``(新值, 规则名)``；不修则规则名为 None。

    **只做类型转换，绝不做量纲换算**（§23）：
    ``"300"`` -> ``300`` 可以；``"300ms"`` -> ``300`` 可以（单位已在值里，
    且单位与字段基准单位一致）；``300`` -> ``300000`` **永远不行** ——
    那是猜测业务语义，不是修复。
    """
    if target == "integer":
        if isinstance(value, bool):
            return value, None  # bool 是 int 的子类，但 True->1 会改语义，不碰
        if isinstance(value, int):
            return value, None
        if isinstance(value, float):
            if value.is_integer():
                return int(value), "float->int"
            return value, None  # 300.7 -> 300 是截断（改语义），交给校验报错
        if isinstance(value, str):
            text = value.strip()
            if _INT_RE.fullmatch(text):
                return int(text), "str->int"
            number = _parse_float(text)
            if number is not None and float(number).is_integer():
                # "300.0" -> 300：仍是同一个数量，只是写法不同
                return int(number), "str->int"
            stripped = _strip_unit(text, field_name)
            if stripped is not None:
                return stripped, "strip-unit"
            return value, None
        return value, None

    if target == "number":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (int, float)):
            return value, None
        if isinstance(value, str):
            text = value.strip()
            number = _parse_float(text)
            if number is not None:
                return number, "str->float"
            stripped = _strip_unit(text, field_name)
            if stripped is not None:
                return float(stripped), "strip-unit"
            return value, None
        return value, None

    if target == "boolean":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str):
            low = value.strip().lower()
            if low == "true":
                return True, "str->bool"
            if low == "false":
                return False, "str->bool"
            # 刻意不接受 "1"/"yes"/"on"：它们在不同语言/协议里含义不同，
            # 「猜」的代价（把 "1" 猜成 True）高于收益 —— §23 只允许确定的修复。
        return value, None

    if target == "string":
        if isinstance(value, str):
            return value, None
        if isinstance(value, bool):
            return value, None  # bool -> "True" 几乎总是参数写错了位置
        if isinstance(value, (int, float)):
            return str(value), "scalar->str"
        return value, None

    return value, None


def _parse_float(text: str) -> Optional[float]:
    if not _FLOAT_RE.fullmatch(text):
        return None
    try:
        return float(text)
    except ValueError:  # pragma: no cover - 正则已保证
        return None


def _strip_unit_for(text: str, base_unit: Optional[str]) -> Optional[Any]:
    """按指定基准单位去掉量纲后缀：``("300ms", "ms")`` -> ``300``。

    三条硬约束（§23）：

    1. 只有**单位与基准单位一致**才允许 —— ``timeout`` 的基准是 ms，
       所以 ``"300ms"`` 能修；``"300s"`` 修不了（那需要 ×1000，是量纲换算，禁止）。
    2. **永不**乘以/除以任何系数，取到的数字原样返回。
    3. 认不出基准单位（``None``）一律不修 —— 没有依据就不猜。
    """
    if base_unit is None:
        return None
    match = _NUM_WITH_UNIT_RE.fullmatch(text)
    if match is None:
        return None
    number_text, unit = match.group(1), match.group(2).lower()
    if not unit:
        return None  # 纯数字走 str->int / str->float 分支
    if unit not in _UNIT_ALIASES.get(base_unit, set()):
        return None
    value = float(number_text)
    return int(value) if value.is_integer() else value


def _strip_unit(text: str, field_name: str) -> Optional[Any]:
    """去掉与**该字段**基准单位一致的量纲后缀（见 :func:`_strip_unit_for`）。"""
    return _strip_unit_for(text, _base_unit(field_name))


# ======================================================================
# 确定性修复：一个字段（含 list 装箱/拆箱/元素级修复）
# ======================================================================
def _coerce_value(
    value: Any,
    annotation: Any,
    *,
    field_name: str,
    steps: list[RepairStep],
) -> Any:
    """把单个字段的值修成 annotation 期待的形状，并在 ``steps`` 里记账。

    绝不抛异常、绝不猜测复杂结构：认不出来就原样返回，
    让 Pydantic 去报错（错误信息交回 LLM，比我们的猜测可靠）。
    """
    if value is None:
        # ``None`` 的含义是「这个参数没给」，不是「一个标量值」——
        # 若被 ``list[T]`` 分支装箱成 ``[None]``，就把「没传」变成了「传了个空值」，
        # 这是**改变语义**（§23 禁止）。是否允许 None 由模型的可选性决定。
        return value

    element = _list_element(annotation)
    if element is not None:
        if isinstance(value, dict):
            return value  # dict -> list 需要知道「包成哪个字段」，不猜
        if isinstance(value, (list, tuple, set, frozenset)):
            changed = False
            items: list[Any] = []
            for index, item in enumerate(value):
                new_item, rule = _coerce_value_inner(item, element, field_name)
                if rule is not None:
                    steps.append(
                        RepairStep(f"{field_name}[{index}]", item, new_item, rule)
                    )
                    changed = True
                items.append(new_item)
            return items if changed else value
        # §21 原例：``"TC001"`` -> ``["TC001"]``（标量装箱）
        boxed, inner_rule = _coerce_value_inner(value, element, field_name)
        steps.append(RepairStep(field_name, value, [boxed], "scalar->list"))
        if inner_rule is not None:
            steps.append(RepairStep(f"{field_name}[0]", value, boxed, inner_rule))
        return [boxed]

    target = _scalar_name(annotation)
    if target is None:
        # ``dict`` / 自定义模型 / ``Any``：形状复杂，交给 Pydantic（§23：不猜）
        return value

    if isinstance(value, (list, tuple, set, frozenset)):
        if len(value) == 1:
            # §21 原例：``[1]`` -> ``1``（单元素拆箱）
            inner = next(iter(value))
            new_value, _rule = _coerce_value_inner(inner, annotation, field_name)
            steps.append(RepairStep(field_name, value, new_value, "list->scalar"))
            return new_value
        # 多元素不拆：``[1, 2]`` 更可能是「参数传错了位置」，
        # 拆开等于替 Agent 做决定，应当报错让它重来。
        return value

    new_value, rule = _coerce_value_inner(value, annotation, field_name)
    if rule is not None:
        steps.append(RepairStep(field_name, value, new_value, rule))
    return new_value


def _coerce_value_inner(
    value: Any, annotation: Any, field_name: str
) -> tuple[Any, Optional[str]]:
    """元素级修复：如果元素本身是 ``list``（嵌套数组）则递归，否则做标量转换。"""
    if isinstance(value, (list, tuple, set, frozenset)):
        target = _list_element(annotation)
        if target is None:
            return value, None
        items = [*(value,)]
        changed = False
        for index, item in enumerate(items):
            new_item, rule = _coerce_value_inner(item, target, field_name)
            if rule is not None:
                items[index] = new_item
                changed = True
        return (items if changed else value), ("nested-list->list" if changed else None)

    target_name = _scalar_name(annotation)
    if target_name is None:
        return value, None
    return _coerce_scalar(value, target_name, field_name=field_name)


def _deterministic_pass(
    raw: dict[str, Any],
    model: type[BaseModel],
    steps: list[RepairStep],
) -> dict[str, Any]:
    """跑一遍确定性修复，返回「形状已就位」的参数 dict。

    职责边界：这里**不填默认值**（交给 Pydantic），也**不做业务判断**（交给 §25 策略）。
    """
    fields = model.model_fields
    extra = model.model_config.get("extra", "ignore")
    cleaned: dict[str, Any] = {}

    for key, value in (raw or {}).items():
        if key not in fields:
            if extra == "allow":
                cleaned[key] = value
                continue
            # 未知字段：``extra="forbid"`` 的模型会因此直接失败，
            # 但「多传了一个键」是典型的 LLM 输出瑕疵，丢掉比失败划算 ——
            # 前提是丢掉这件事必须留痕（drop_unknown）。
            steps.append(RepairStep(key, value, None, "drop_unknown"))
            continue
        cleaned[key] = _coerce_value(
            value, fields[key].annotation, field_name=key, steps=steps
        )
    return cleaned


def _model_errors(candidate: dict[str, Any], model: type[BaseModel]) -> list[str]:
    """用 Pydantic 校验一次，把错误压成人话列表。"""
    try:
        model.model_validate(candidate)
    except PydanticValidationError as exc:
        return [_format_pydantic_error(err) for err in exc.errors()]
    return []


def _format_pydantic_error(err: dict[str, Any]) -> str:
    location = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
    return f"{location}: {err.get('msg', 'invalid value')}"


# ======================================================================
# §25 业务约束检查（自愈**之后**执行的那一段）
# ======================================================================
_TYPE_CHECKERS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, (list, tuple, set, frozenset)),
    "object": lambda v: isinstance(v, dict),
}


def normalize_path(value: str) -> str:
    """把路径归一化到「POSIX 风格 + 已折叠 ``..`` / ``.`` / 重复分隔符」的坐标系。

    为什么要同时做两件事（§25 的 ``allowed_prefix`` 在 Windows 上必须可用）：

    1. ``os.path.normpath`` —— ``"/workspace/tests/../x"`` 与 ``"/workspace/x"``
       必须判成同一个路径，否则前缀检查可以被 ``..`` 绕过；
    2. 统一分隔符 —— 策略里写的是 ``/workspace/tests/``，而 Windows 上
       ``normpath`` 会返回 ``\\workspace\\tests``，不统一就永远匹配不上。
    """
    return os.path.normpath(value.replace("\\", "/")).replace("\\", "/")


def _match_prefix(path: str, prefix: str) -> bool:
    """按**路径分段**匹配前缀，避免 ``/workspace/tests-evil`` 命中 ``/workspace/tests``。"""
    norm_path = normalize_path(path)
    norm_prefix = normalize_path(prefix)
    if norm_prefix.endswith("/"):
        norm_prefix = norm_prefix[:-1]
    if norm_path == norm_prefix:
        return True
    # Windows 盘符 / UNC 路径大小写不敏感，其它路径保持大小写敏感
    if re.match(r"^[a-zA-Z]:", norm_prefix) or norm_prefix.startswith("//"):
        return norm_path.lower().startswith(norm_prefix.lower() + "/")
    return norm_path.startswith(norm_prefix + "/")


def check_against_rule(field_name: str, value: Any, rule: ParamRule) -> list[str]:
    """按一条 :class:`ParamRule` 检查一个值，返回错误列表（§25）。

    这是「确定性验证器」的核心：**LLM 修复的产物也必须过这里**，
    否则 §23 的 ``LLM Repair + Deterministic Validator`` 就退化成
    ``LLM Repair Only``（LLM 说多少就是多少）。
    """
    errors: list[str] = []

    if rule.type is not None:
        checker = _TYPE_CHECKERS.get(rule.type)
        if checker is not None and not checker(value):
            return [
                f"参数 {field_name} 类型应为 {rule.type}，实际为 "
                f"{type(value).__name__}（值 {value!r}）"
            ]

    # 数值区间：错误信息里必须带上合法区间，否则 Agent 只能靠猜重试
    if rule.minimum is not None or rule.maximum is not None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if rule.minimum is not None and value < rule.minimum:
                errors.append(
                    f"参数 {field_name}={value} 小于最小值 {rule.minimum}"
                    f"（允许区间 [{rule.minimum}, {rule.maximum}]）"
                )
            if rule.maximum is not None and value > rule.maximum:
                errors.append(
                    f"参数 {field_name}={value} 超过最大值 {rule.maximum}"
                    f"（允许区间 [{rule.minimum}, {rule.maximum}]）"
                )

    # 长度约束：字符串按字符数，数组按元素个数
    if rule.min_length is not None or rule.max_length is not None:
        if isinstance(value, (str, list, tuple, set, frozenset)):
            length = len(value)
            if rule.min_length is not None and length < rule.min_length:
                errors.append(
                    f"参数 {field_name} 长度 {length} 小于最小长度 {rule.min_length}"
                )
            if rule.max_length is not None and length > rule.max_length:
                errors.append(
                    f"参数 {field_name} 长度 {length} 超过最大长度 {rule.max_length}"
                )

    # 白名单：标量直接比对；列表要求**每个元素**都在白名单里
    if rule.allowed is not None:
        if rule.type == "array" or isinstance(value, (list, tuple, set, frozenset)):
            for index, item in enumerate(value):
                if item not in rule.allowed:
                    errors.append(
                        f"参数 {field_name}[{index}]={item!r} 不在允许列表 {rule.allowed} 内"
                    )
        elif value not in rule.allowed:
            errors.append(f"参数 {field_name}={value!r} 不在允许列表 {rule.allowed} 内")

    # 正则白名单：数组按元素逐个匹配（test_cases 的 "^TC[0-9]{3}$" 就是这种）
    if rule.pattern:
        targets = (
            list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]
        )
        try:
            compiled = re.compile(rule.pattern)
        except re.error as exc:  # 策略写错了：报错而不是静默放过
            errors.append(f"参数 {field_name} 的正则配置非法 {rule.pattern!r}: {exc}")
        else:
            for index, item in enumerate(targets):
                if isinstance(item, dict):  # pragma: no cover - 病态输入
                    continue
                if not isinstance(item, str) or compiled.fullmatch(item) is None:
                    label = f"{field_name}[{index}]" if len(targets) > 1 else field_name
                    errors.append(
                        f"参数 {label}={item!r} 不匹配模式 {rule.pattern}"
                    )

    # 路径前缀（§24 路径穿越的业务约束侧）
    if rule.allowed_prefix or rule.denied_prefix:
        targets = (
            list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]
        )
        for index, item in enumerate(targets):
            if not isinstance(item, str):
                continue
            label = f"{field_name}[{index}]" if len(targets) > 1 else field_name
            if rule.allowed_prefix and not any(
                _match_prefix(item, prefix) for prefix in rule.allowed_prefix
            ):
                errors.append(
                    f"参数 {label}={item!r} 不在允许的路径前缀 {rule.allowed_prefix} 之下"
                )
            if rule.denied_prefix:
                for prefix in rule.denied_prefix:
                    if _match_prefix(item, prefix):
                        errors.append(
                            f"参数 {label}={item!r} 命中禁止的路径前缀 {prefix!r}"
                        )

    return errors


def check_constraints(
    values: dict[str, Any], constraints: dict[str, ParamRule]
) -> list[str]:
    """按约束表检查一批值（只检查**出现**在 values 里的字段）。"""
    errors: list[str] = []
    for name, value in (values or {}).items():
        rule = constraints.get(name)
        if rule is None:
            continue
        errors.extend(check_against_rule(name, value, rule))
    return errors


def _numeric_fingerprint(value: Any) -> Any:
    """把值压成「数量指纹」—— 专门用来抓「LLM 悄悄放大数值」（§23）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        number = _parse_float(text)
        if number is not None:
            return number
        # ``"300ms"`` 与 ``300`` 是同一个数量（只是写法不同），指纹必须相等，
        # 否则 LLM 的一次合法类型转换会被误判成「篡改数量级」。
        for base in ("ms", "s"):
            stripped = _strip_unit_for(text, base)
            if stripped is not None:
                return stripped
        return text
    return value


# ======================================================================
# §22 Parameter Repair
# ======================================================================
class ParameterRepairer:
    """确定性参数修复器（§22 的核心），LLM 只做**兜底**。

    ``llm_repair`` 的签名是 ``(raw, errors, constraints) -> dict``：
    平台把「原始参数 + 失败原因 + 约束描述」交给 LLM，拿回一份**建议**参数。
    建议随后要**再过一遍确定性校验**（§23）：

    * 仍然过不了 ``args_model`` -> 失败
    * 过不了 ``ParamRule``（区间/白名单/前缀/正则）-> 失败
    * 动了 ``repair_forbidden`` 字段的**数量级**（``300`` -> ``300000``）-> 直接拒绝

    最后一条是整套设计的关键：LLM 最擅长的恰恰是「把 300 编成 300000 看起来更合理」，
    而这类改动在生产里意味着删错数据、跑爆机器。宁可直接 REJECTED。
    """

    def __init__(self, *, llm_repair: Optional[LLMRepairFn] = None) -> None:
        self._llm_repair = llm_repair

    # ------------------------------------------------------------------
    def repair(
        self,
        raw: dict[str, Any],
        model: type[BaseModel],
        *,
        allow_llm: bool = True,
        constraints: Optional[dict[str, ParamRule]] = None,
    ) -> ValidationOutcome:
        """把 ``raw`` 往 ``model`` 的形状上修。

        :param allow_llm: 是否允许 LLM 兜底（§25 ``ParamPolicy.allow_llm_repair``）。
            关掉它就是「纯确定性自愈」—— 可复现、可压测，适合生产默认值。
        :param constraints: 该 Tool 的参数策略；用于给 LLM 提示词提供约束，
            也用于**复核 LLM 的产物**。
        """
        original = dict(raw or {})
        steps: list[RepairStep] = []
        candidate = _deterministic_pass(original, model, steps)

        errors = _model_errors(candidate, model)
        if constraints:
            # 约束违例也走自愈流程（例如 query 超长可以截断），但**绝不允许**
            # 靠猜测数量级来满足约束 —— 那条线由 repair_forbidden 守着。
            errors.extend(check_constraints(candidate, constraints))
        errors = list(dict.fromkeys(errors))

        if not errors:
            return ValidationOutcome(ok=True, arguments=candidate, repaired=steps)

        if allow_llm and self._llm_repair is not None:
            outcome, reason = self._llm_fallback(
                original, candidate, model, constraints, steps, errors
            )
            if outcome is not None:
                return outcome
            if reason:
                errors.append(reason)

        return ValidationOutcome(
            ok=False, arguments=candidate, repaired=steps, errors=errors
        )

    # ------------------------------------------------------------------
    def _llm_fallback(
        self,
        original: dict[str, Any],
        candidate: dict[str, Any],
        model: type[BaseModel],
        constraints: Optional[dict[str, ParamRule]],
        steps: list[RepairStep],
        errors: list[str],
    ) -> tuple[Optional[ValidationOutcome], str]:
        """调用 LLM 兜底修复，并把它的建议**重新过一遍确定性校验**。

        返回 ``(outcome, reason)``：``outcome`` 非空表示已给出结论；
        为空时 ``reason`` 说明为什么没用 LLM 的建议（会补进 errors 里）。
        """
        payload = {
            name: rule.model_dump() for name, rule in (constraints or {}).items()
        }
        try:
            suggestion = self._llm_repair(dict(original), list(errors), payload)
        except Exception as exc:  # noqa: BLE001 - 兜底路径本身失败不能拖垮主流程
            logger.warning("LLM 参数修复调用失败: %s", exc)
            return None, f"LLM 修复调用失败: {exc}"

        if not isinstance(suggestion, dict):
            return None, f"LLM 修复返回了非 dict（{type(suggestion).__name__}），已忽略"

        # --- 第一道闸：不许动 repair_forbidden 字段的**数量级**（§23 核心）---
        violated = _forbidden_magnitude_edits(original, suggestion, constraints or {})
        if violated:
            return (
                ValidationOutcome(
                    ok=False,
                    arguments=candidate,
                    repaired=steps,
                    errors=errors + violated,
                    repaired_by_llm=False,
                ),
                "",
            )

        # --- 第二道闸：LLM 的产物同样要过确定性修复 + Pydantic + 策略 ---
        llm_steps: list[RepairStep] = []
        reshaped = _deterministic_pass(suggestion, model, llm_steps)
        remaining = _model_errors(reshaped, model)
        if not remaining and constraints:
            remaining = check_constraints(reshaped, constraints)
        if remaining:
            return (
                None,
                "LLM 修复未通过确定性校验"
                f"（§23 要求 LLM Repair + Deterministic Validator）: "
                f"{'; '.join(remaining[:3])}",
            )

        # --- 采纳：记账「LLM 到底改了哪些字段」，与形状修复分开记录 ---
        diff_steps = [
            RepairStep(name, candidate.get(name), value, "llm_repair")
            for name, value in reshaped.items()
            if name not in candidate or candidate[name] != value
        ]
        return (
            ValidationOutcome(
                ok=True,
                arguments=reshaped,
                repaired=steps + diff_steps,
                errors=[],
                repaired_by_llm=True,
            ),
            "",
        )


def _has_magnitude(value: Any) -> bool:
    """指纹是否是一个**真实存在的数量**（而不是 ``"bad"`` / ``None`` 这类无数量值）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _forbidden_magnitude_edits(
    original: dict[str, Any],
    suggestion: dict[str, Any],
    constraints: dict[str, ParamRule],
) -> list[str]:
    """检查 LLM 是否篡改了 ``repair_forbidden`` 字段的数量。

    允许它做**类型转换**（``"300"`` -> ``300`` 是同一数量的不同写法），
    禁止它改数字本身 —— ``300`` -> ``300000`` 就是 §23 明令禁止的量纲推测。

    原值本身不含数量时（例如 ``{"timeout": "bad"}``）不在这里判：
    没有基准就无从谈「改没改数量级」，此时改由 §25 的区间约束兜底
    （``check_constraints`` 会拿 ``minimum``/``maximum`` 卡住 LLM 的产物）。
    **这也是为什么规范要求 repair_forbidden 字段必须同时声明区间** ——
    只标 ``repair_forbidden`` 却不给区间，等于把这道闸门拆了。
    """
    violations: list[str] = []
    for name, rule in constraints.items():
        if not rule.repair_forbidden or name not in suggestion:
            continue
        if name not in original:
            # 凭空补出一个「禁止推测」的字段 —— 更是猜测，直接拒
            violations.append(
                f"LLM 自行补出了 repair_forbidden 字段 {name}={suggestion[name]!r}，已拒绝"
            )
            continue
        before = _numeric_fingerprint(original[name])
        if not _has_magnitude(before):
            continue
        after = _numeric_fingerprint(suggestion[name])
        if before != after:
            violations.append(
                f"LLM 修改了禁止自愈字段 {name} 的数量级"
                f"（{original[name]!r} -> {suggestion[name]!r}），"
                "§23 禁止量纲/数量级推测，已拒绝整个 LLM 建议"
            )
    return violations


# ======================================================================
# §21 完整校验流水线
# ======================================================================
class ArgumentValidator:
    """Tool Gateway 的参数校验流水线（§21）。

    顺序固定为 ``Schema -> Repair -> Schema + Policy + Injection``：
    **先修形状、再判业务**。反过来（先判业务再修形状）会让
    ``"300"`` 这种字符串直接在区间检查里报「不是数字」，
    把一次本可自愈的调用变成一次失败。

    所有检查都返回 :class:`ValidationOutcome`，不抛异常 —— 由 Gateway 决定
    是 ``REJECTED`` 还是丢进 §35 的恢复流程（``ParameterRepairError`` /
    ``InjectionDetected`` 由调用方在需要硬失败语义时抛出）。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        config: AppConfig,
        *,
        repairer: Optional[ParameterRepairer] = None,
    ) -> None:
        self._registry = registry
        # 保留 config 引用：Tool 的策略来自它（registry 由它装配），
        # 后续若要按环境收紧自愈（例如生产强制 allow_llm_repair=False）也在这里读。
        self._config = config
        if registry.config is not config:
            logger.warning(
                "ArgumentValidator 与 ToolRegistry 使用的不是同一个 AppConfig，"
                "参数策略可能不一致"
            )
        self.repairer = repairer or ParameterRepairer()
        self.injection_detector = InjectionDetector(enabled=True)

    # ------------------------------------------------------------------
    def validate(self, call: ToolCall) -> ValidationOutcome:
        """校验一次 :class:`ToolCall` 的参数。

        未注册的 Tool 会抛 :class:`~app.domain.errors.ToolNotFound`（**不**塞进
        ``errors``）—— 「工具不存在」与「参数不合法」在 §35 里走的是两条恢复路径
        （``FALLBACK`` vs ``REPAIR``），压成同一个 ``ok=False`` 会让调用方失去判断依据。
        """
        spec = self._registry.get(call.tool_name)
        policy = spec.param_policy

        outcome = self.repairer.repair(
            call.arguments,
            spec.args_model,
            allow_llm=policy.allow_llm_repair,
            constraints=policy.rules,
        )

        # 自愈之后的第二段：业务约束 + 注入检测（无论自愈成功与否都跑，
        # 这样 errors 是一次「完整的诊断」，而不是逐次暴露问题）。
        policy_errors = self.validate_against_policy(outcome.arguments, policy)

        errors = list(dict.fromkeys([*outcome.errors, *policy_errors]))
        return ValidationOutcome(
            ok=outcome.ok and not policy_errors,
            arguments=outcome.arguments,
            repaired=outcome.repaired,
            errors=errors,
            repaired_by_llm=outcome.repaired_by_llm,
        )

    # ------------------------------------------------------------------
    def validate_against_policy(
        self, values: dict[str, Any], policy: ParamPolicy
    ) -> list[str]:
        """按 §25 策略检查参数：类型/区间/长度/白名单/正则/路径前缀 + 注入。

        注入检测在这里只**产出错误字符串**，不抛异常 —— 注入是安全事件，
        调用方若需要「命中即中断 + 审计」的硬失败语义，应显式调用
        :meth:`InjectionDetector.assert_clean`（它会抛 ``InjectionDetected``）。
        """
        errors: list[str] = []
        for name, value in (values or {}).items():
            rule = policy.rule_for(name)
            if rule is None:
                continue
            errors.extend(check_against_rule(name, value, rule))

        if policy.injection_guard:
            for finding in self.injection_detector.scan_arguments(
                values, rules=policy.rules
            ):
                errors.append(
                    f"参数注入命中[{finding.category}/{finding.severity}] "
                    f"{finding.field}: {finding.detail} | evidence={finding.evidence!r}"
                )
        return errors
