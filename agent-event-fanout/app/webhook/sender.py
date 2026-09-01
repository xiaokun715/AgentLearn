"""Webhook HTTP 发送器（设计说明书 §12, §18, §37）。

- 用 ``httpx.AsyncClient`` 发 POST，携带签名请求头（§12）。
- 把一次投递结果归类为：成功 / 可重试失败 / 永久失败（§18）。
- 超时（§37）与连接错误本质上也是一种失败 → 可重试。
- 解析 ``Retry-After``（§38）供重试策略使用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..domain.event import utcnow

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WebhookResponse:
    status_code: int | None = None
    body: str = ""
    headers: dict[str, str] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


@dataclass(slots=True)
class DeliveryOutcome:
    """一次投递的归类结果。``retryable=True`` 才进入重试路径（§18）。"""

    success: bool
    retryable: bool
    status_code: int | None = None
    error: str | None = None
    retry_after: int | None = None

    @property
    def summary(self) -> str:
        if self.success:
            return f"HTTP {self.status_code}"
        if self.retryable:
            return f"可重试: {self.error or f'HTTP {self.status_code}'}"
        return f"永久失败: {self.error or f'HTTP {self.status_code}'}"


class WebhookSender:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout = timeout
        # 允许注入 httpx.MockTransport（测试）或真实 client
        self._client = client

    async def _acquire_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # trust_env=False：Webhook 投递应直连客户端点，不要隐式走系统代理
            # （例如 Windows 注册表里的本地代理会把 localhost 请求拦成 503）。
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def post(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: bytes,
    ) -> DeliveryOutcome:
        """发送一次 Webhook 并归类结果。"""
        client = await self._acquire_client()
        try:
            resp: httpx.Response = await client.post(
                url, content=body, headers=headers, timeout=self.timeout
            )
        except httpx.TimeoutException as exc:
            return DeliveryOutcome(
                success=False, retryable=True,
                error=f"timeout after {self.timeout}s: {exc.__class__.__name__}",
            )
        except httpx.HTTPError as exc:
            return DeliveryOutcome(
                success=False, retryable=True,
                error=f"connection error: {exc.__class__.__name__}: {exc}",
            )

        status = resp.status_code
        if 200 <= status < 300:
            return DeliveryOutcome(success=True, retryable=False, status_code=status)

        retry_after = self._parse_retry_after(resp.headers)
        retryable = status in (408, 429) or status >= 500
        return DeliveryOutcome(
            success=False,
            retryable=retryable,
            status_code=status,
            error=f"HTTP {status}",
            retry_after=retry_after,
        )

    @staticmethod
    def _parse_retry_after(headers: Any) -> int | None:
        """§38：解析 ``Retry-After``（支持秒数或 HTTP-date）。"""
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is None:
            return None
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)
        # HTTP-date 形式：返回距 now 的秒数
        try:
            from email.utils import parsedate_to_datetime

            deadline = parsedate_to_datetime(raw)
            if deadline.tzinfo is None:
                from datetime import timezone

                deadline = deadline.replace(tzinfo=timezone.utc)
            delta = (deadline - utcnow()).total_seconds()
            return max(0, int(delta))
        except (ValueError, TypeError):
            return None
