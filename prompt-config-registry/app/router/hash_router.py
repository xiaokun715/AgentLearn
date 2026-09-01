"""Hash Router —— 把 user_id 稳定映射到 0~99 的 bucket（设计说明书 §15~§16）。

为什么用 Hash 而不是 random（§16）：
  同一个用户必须**稳定命中同一个版本**（Sticky Assignment），
  否则实验数据会非常混乱（第一次 v12 / 第二次 v13 / 第三次 v12 ...）。

用 SHA256 而不是 Python 内置 hash()：内置 hash 每次进程启动会加随机 salt，
会导致同一用户跨重启后落入不同 bucket —— 实验分组就被打散了。
"""
from __future__ import annotations

import hashlib


def bucket(user_id: str, *, salt: str = "") -> int:
    """返回 0~99 的确定性 bucket。

    salt 用于在同一个 user 上开多个互不相关的实验时打散分组
    （实验 1 用 ''，实验 2 用 'experiment_name'，避免两组永远同分）。
    """
    raw = f"{salt}:{user_id}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return int(digest[:8], 16) % 100


def is_selected(user_id: str, percent: int, *, salt: str = "") -> bool:
    """给定流量百分比（如 canary 5%），判断该用户是否命中。"""
    return bucket(user_id, salt=salt) < percent
