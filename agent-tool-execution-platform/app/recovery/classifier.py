"""错误分类器 —— 说明书 §34 Error Classification。

为什么需要分类
--------------
§13 反复强调一句话：**``FAILED`` 不能简单理解成 ``FAILED -> retry``。**

同一个 ``FAILED`` 背后至少藏着九种完全不同的处境：
``timeout`` 应该退避重试；``permission_error`` 重试一万次也还是被拒；
``validation_error`` 要去修参数而不是重试；``resource_exhausted`` 要降规格换节点。
如果不分类就统一重试，平台会做大量**注定失败**的尝试：
既浪费资源，又会把下游服务打死（§36 的「服务雪崩」）。

三层判定
--------
:meth:`ErrorClassifier.classify` 按下面的顺序判型，越靠前越可信：

1. **异常自带**：:class:`~app.domain.errors.ToolPlatformError` 的子类在抛出点就
   声明了自己的 ``error_type``（见 ``app/domain/errors.py`` 的模块 docstring）——
   这是最可靠的一层，因为它来自**唯一知道真相的那段代码**。
2. **异常类型表**：第三方的 ``ConnectionError`` / ``TimeoutError`` 之类，
   按类型查表（:data:`DEFAULT_RULES`，可用构造参数覆盖）。
3. **错误文本**：最后才退到 :meth:`ErrorClassifier.from_message`，
   从 ``"HTTP 503 Service Unavailable"`` 这种字符串里认（§13/§35 里点名的情形）。

**为什么顺序不能反**：文本匹配天生会误判（``"not found"`` 可能出现在
「查不到用户」这种业务错误里）。只有在拿不到结构化信息时才允许用文本兜底。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..domain.enums import ErrorType
from ..domain.errors import (
    BusinessError,
    InternalError,
    NetworkError,
    PermissionDenied,
    ResourceExhausted,
    SandboxError,
    ToolNotFound,
    ToolPlatformError,
    ToolTimeout,
    ValidationError,
)

#: 异常类型 -> 错误分类的**默认**查表（§34）。
#:
#: 表里出现的都是「平台无法插入自己异常子类」的场合：标准库异常、
#: 第三方 SDK 抛出的异常。走 MRO 查找，因此子类会命中最近的祖先。
DEFAULT_RULES: dict[type[BaseException], ErrorType] = {
    PermissionError: ErrorType.PERMISSION_ERROR,
    TimeoutError: ErrorType.TIMEOUT,
    asyncio.TimeoutError: ErrorType.TIMEOUT,
    ConnectionError: ErrorType.NETWORK_ERROR,
    ConnectionResetError: ErrorType.NETWORK_ERROR,
    ConnectionRefusedError: ErrorType.NETWORK_ERROR,
    OSError: ErrorType.NETWORK_ERROR,
    MemoryError: ErrorType.RESOURCE_EXHAUSTED,
    ValueError: ErrorType.VALIDATION_ERROR,
    TypeError: ErrorType.VALIDATION_ERROR,
    KeyError: ErrorType.TOOL_NOT_FOUND,
    NotImplementedError: ErrorType.INTERNAL_ERROR,
}

#: 分类 -> 用于 :meth:`ErrorClassifier.wrap` 包装的异常类。
#:
#: 包装时**不**统一用基类 而用对应子类，是为了让下游 ``except ToolTimeout``
#: 这类写法继续有效 —— 分类信息不该以牺牲可读的异常层次为代价。
_ERROR_CLASS_BY_TYPE: dict[ErrorType, type[ToolPlatformError]] = {
    ErrorType.VALIDATION_ERROR: ValidationError,
    ErrorType.PERMISSION_ERROR: PermissionDenied,
    ErrorType.TIMEOUT: ToolTimeout,
    ErrorType.NETWORK_ERROR: NetworkError,
    ErrorType.RESOURCE_EXHAUSTED: ResourceExhausted,
    ErrorType.TOOL_NOT_FOUND: ToolNotFound,
    ErrorType.BUSINESS_ERROR: BusinessError,
    ErrorType.SANDBOX_ERROR: SandboxError,
    ErrorType.INTERNAL_ERROR: InternalError,
}

#: §13/§35 点名的文本特征 -> 分类。**顺序即优先级**，先命中先返回。
#:
#: 每一行都对应说明书里的一条具体情形：
#: ``429`` -> Backoff（限流也是一种必须退避的网络类失败）；
#: ``503`` -> Retry；``timeout`` -> Retry；``permission denied`` -> Stop；
#: ``not found`` -> Fallback；``out of memory`` -> 降资源；``sandbox`` -> 新建 Sandbox。
_MESSAGE_RULES: tuple[tuple[tuple[str, ...], ErrorType], ...] = (
    # --- 权限类：拒绝就是拒绝，任何重试都没意义（§26） ---
    (
        (
            "permission denied",
            "permissiondenied",
            "access denied",
            "forbidden",
            "unauthorized",
            "not authorized",
            "403",
            "401",
            "权限不足",
            "无权限",
            "拒绝访问",
        ),
        ErrorType.PERMISSION_ERROR,
    ),
    # --- 资源类：不是「调用出错」而是「装不下」，重试前要先降规格（§35） ---
    (
        (
            "out of memory",
            "outofmemory",
            "oom",
            "no space left",
            "no space",
            "resource exhausted",
            "resourceexhausted",
            "quota exceeded",
            "too many open files",
            "disk full",
            "内存不足",
            "磁盘空间不足",
        ),
        ErrorType.RESOURCE_EXHAUSTED,
    ),
    # --- 超时：可重试，但要退避 / 提高 timeout（§35） ---
    (
        ("timeout", "timed out", "timedout", "deadline exceeded", "504", "超时"),
        ErrorType.TIMEOUT,
    ),
    # --- 网络类：429 与 503 都归到这里，语义是「退避后重试」 ---
    (
        (
            "429",
            "503",
            "502",
            "too many requests",
            "rate limit",
            "service unavailable",
            "connection reset",
            "connection refused",
            "connection",
            "network",
            "econnreset",
            "econnrefused",
            "dns",
            "网络",
            "连接",
        ),
        ErrorType.NETWORK_ERROR,
    ),
    # --- 沙箱类：隔离环境本身坏了，换个沙箱重来（§35 Sandbox Error） ---
    (("sandbox", "沙箱"), ErrorType.SANDBOX_ERROR),
    # --- 找不到：Fallback 或换等价 Tool（§35 Tool Not Found） ---
    (
        ("not found", "notfound", "404", "no such tool", "unknown tool", "未注册", "不存在"),
        ErrorType.TOOL_NOT_FOUND,
    ),
    # --- 参数类：修参数，不是重试（§22-§25） ---
    (
        (
            "validation",
            "invalid",
            "schema",
            "missing required",
            "typeerror",
            "valueerror",
            "unexpected keyword",
            "校验",
            "参数",
        ),
        ErrorType.VALIDATION_ERROR,
    ),
    # --- 业务类：Tool 跑通了但业务规则不允许（§13） ---
    (("business", "business rule", "业务"), ErrorType.BUSINESS_ERROR),
)


class ErrorClassifier:
    """把任意异常收敛成 :class:`~app.domain.enums.ErrorType`（§34）。

    :param rules: 覆盖/追加「异常类型 -> 分类」的查表；传 ``None`` 用
        :data:`DEFAULT_RULES`。传入的表**合并**到默认表之上，而不是替换 ——
        注册表语义比「全量替换」更难写错。
    """

    def __init__(self, rules: Optional[dict[type[BaseException], ErrorType]] = None) -> None:
        self._rules: dict[type[BaseException], ErrorType] = dict(DEFAULT_RULES)
        if rules:
            self._rules.update(rules)

    # ==================================================================
    # 判定
    # ==================================================================
    def classify(self, exc: BaseException) -> ErrorType:
        """判定异常属于哪一类（§34）。

        顺序（**不可调换**，理由见模块 docstring）：

        1. 平台异常直接读它自带的 ``error_type``；
        2. 按异常类型的 MRO 查表 —— 用 MRO 而不是 ``type(exc) in rules``，
           是为了让 ``ConnectionResetError`` 这类子类自动落到 ``ConnectionError``
           那一行，不必逐个枚举；
        3. 文本兜底 :meth:`from_message`；
        4. 都没有 -> ``INTERNAL_ERROR``（平台自己的锅，默认不重试，§35）。
        """
        # --- 1) 异常自带分类：最可信 ---
        if isinstance(exc, ToolPlatformError):
            return exc.error_type

        # --- 2) 类型查表（走 MRO，最近祖先优先） ---
        for klass in type(exc).__mro__:
            hit = self._rules.get(klass)
            if hit is not None:
                return hit

        # --- 3) 文本兜底 ---
        message = str(exc).strip()
        if message:
            return self.from_message(message)

        # --- 4) 兜底：平台自身缺陷 ---
        return ErrorType.INTERNAL_ERROR

    def wrap(self, exc: BaseException) -> ToolPlatformError:
        """把裸异常包成 :class:`ToolPlatformError`，供上层统一处理。

        已经是平台异常的原样返回（**不做二次包装**：包装层数越多，
        原始 traceback 越难读）。否则按 :meth:`classify` 的结果选一个
        对应的子类包起来，并把原异常放进 ``detail["cause"]``。

        为什么 ``cause`` 存 ``repr`` 字符串而不是异常对象本身：
        ``detail`` 会被写进 ``execution_event.payload`` 落库，必须是 JSON-safe 的。
        这里同时保留 ``cause_type``，排障时能一眼看出原始异常是什么。
        """
        if isinstance(exc, ToolPlatformError):
            return exc

        error_type = self.classify(exc)
        error_class = _ERROR_CLASS_BY_TYPE.get(error_type, InternalError)

        message = str(exc).strip() or type(exc).__name__
        return error_class(
            message,
            error_type=error_type,
            detail={
                "cause": repr(exc),
                "cause_type": type(exc).__name__,
                "classified_by": "ErrorClassifier",
            },
        )

    # ==================================================================
    # 文本判型
    # ==================================================================
    @staticmethod
    def from_message(message: str) -> ErrorType:
        """从错误文本判型 —— 覆盖说明书 §13 / §35 点名的全部情形。

        大小写不敏感（``"HTTP 503"`` 与 ``"http 503"`` 同样命中）。
        命中不了就返回 ``INTERNAL_ERROR``：
        **宁可把它当成平台缺陷交给人工，也不要猜一个「可以重试」的分类**，
        猜错会直接变成对下游的无效重放。
        """
        text = (message or "").lower()
        for needles, error_type in _MESSAGE_RULES:
            for needle in needles:
                if needle in text:
                    return error_type
        return ErrorType.INTERNAL_ERROR
