"""SimLedger —— 模拟“Agent 正在往数据库写临时表”产生的脏数据。

设计目标：制造一个“取消时必须回滚”的真实脏数据源，方便演示第十章第 4 道防线。

- :meth:`stage` 往 ``staged``（临时表）写行；只有 :meth:`commit` 才会真正生效。
- Agent 中途被取消 -> supervisor 调用 :meth:`rollback` 丢弃所有 ``staged`` 行，
  不留任何脏数据；这与事务回滚（Transaction Rollback）是同一件事。
"""
from __future__ import annotations


class SimLedger:
    def __init__(self) -> None:
        self._staged: list[dict] = []
        self._committed: list[dict] = []

    @property
    def staged_count(self) -> int:
        return len(self._staged)

    @property
    def committed_count(self) -> int:
        return len(self._committed)

    @property
    def in_transaction(self) -> bool:
        return self.staged_count > 0

    def stage(self, row: dict) -> dict:
        self._staged.append(dict(row))
        return {"index": len(self._staged) - 1, "staged_count": self.staged_count}

    def commit(self) -> int:
        """临时表 -> 正式表；返回本次提交行数。"""
        n = self.staged_count
        self._committed.extend(self._staged)
        self._staged.clear()
        return n

    def rollback(self) -> int:
        """丢弃临时表中所有未提交行；返回丢弃的行数（0 = 无脏数据可回滚）。"""
        n = self.staged_count
        self._staged.clear()
        return n

    def snapshot(self) -> dict:
        return {"staged": self.staged_count, "committed": self.committed_count}
