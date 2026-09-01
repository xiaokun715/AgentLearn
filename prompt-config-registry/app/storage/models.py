"""存储序列化工具（设计说明书 §26~§29）。

统一约定：时间存 UTC ISO 字符串（SQLite TEXT / Postgres TIMESTAMPTZ 都接受），
JSONB 以 JSON 字符串存取。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_db(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def from_db(raw: Any) -> datetime:
    """把 DB 返回的时间统一成带 UTC 时区的 datetime。"""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(raw))


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw  # asyncpg 对 JSONB 已自动反序列化
    return json.loads(raw)
