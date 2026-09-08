"""记忆记录的数据模型。

对齐说明书（《第十一章：Agent 记忆系统设计说明书》§核心操作的 Python 工程实现）里的
``MemoryRecord`` 字段；另加 ``to_dict / from_dict`` 便于 API 序列化与 Checkpointer 快照。
只依赖 stdlib，便于核心逻辑脱离网络独立运行。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Literal

#: 记忆类型枚举 —— PROFILE(画像,确定性静态)、EPISODIC(情景,自传式经验)、SEMANTIC(语义/规则)
MemoryType = Literal["PROFILE", "EPISODIC", "SEMANTIC"]

PROFILE = "PROFILE"
EPISODIC = "EPISODIC"
SEMANTIC = "SEMANTIC"

MEMORY_TYPES: tuple[str, ...] = (PROFILE, EPISODIC, SEMANTIC)


def utcnow() -> datetime:
    """统一取当前 UTC 时间(带时区)，避免 naive/aware 混用导致衰减计算报错。"""
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


@dataclass
class MemoryRecord:
    """一条记忆的最小单元 —— “主语-谓词-宾语”三元组 + 生命周期元数据。

    对齐说明书 MemoryRecord：``memory_id, user_id, subject, predicate, object_value``；
    ``memory_type`` ∈ PROFILE / EPISODIC / SEMANTIC；``confidence`` 0~1；
    ``version`` 在 SCD2 换代时自增；``is_active`` 软失效标记(沉睡/归档)；
    ``access_count / last_accessed_at`` 支撑艾宾浩斯衰减与 Touch 强化。
    """

    memory_id: str
    user_id: str
    subject: str
    predicate: str
    object_value: str
    memory_type: MemoryType = SEMANTIC
    confidence: float = 1.0               # 0.0 ~ 1.0
    embedding: list[float] = field(default_factory=list)
    version: int = 1
    is_active: bool = True
    access_count: int = 0
    created_at: datetime = field(default_factory=utcnow)
    last_accessed_at: datetime = field(default_factory=utcnow)
    valid_to: datetime | None = None      # 软失效/沉睡的归档截止时间

    def __post_init__(self) -> None:
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError(
                f"未知记忆类型 {self.memory_type!r}，可选 {MEMORY_TYPES}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence 必须在 0~1 之间，收到 {self.confidence}")
        if self.access_count < 0:
            raise ValueError(f"access_count 不能为负，收到 {self.access_count}")

    # ------------------------------------------------------------------
    # 序列化 —— 供 /v1 观测 API、Checkpointer 快照、演示脚本使用
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """转成 JSON 友好的 dict(时间统一 ISO-8601 字符串)。"""
        d = asdict(self)
        d["created_at"] = _iso(self.created_at)
        d["last_accessed_at"] = _iso(self.last_accessed_at)
        d["valid_to"] = _iso(self.valid_to)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemoryRecord":
        """从 ``to_dict`` 产出的 dict 还原(时间串解析回 datetime)。"""
        raw = dict(raw)
        raw["created_at"] = datetime.fromisoformat(raw["created_at"])
        raw["last_accessed_at"] = datetime.fromisoformat(raw["last_accessed_at"])
        if raw.get("valid_to"):
            raw["valid_to"] = datetime.fromisoformat(raw["valid_to"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def snapshot(self) -> str:
        """人类可读的一行快照，demo / 日志输出用。"""
        state = "●活跃" if self.is_active else "○沉睡"
        return (
            f"{self.memory_id} [{self.memory_type}] v{self.version} {state} "
            f"conf={self.confidence:.2f} hit={self.access_count} "
            f"| {self.user_id} {self.subject}->{self.object_value}"
        )
