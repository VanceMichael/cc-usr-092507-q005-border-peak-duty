"""JSONL 事件日志：重启后通过重放恢复等待队列、租约与升级期限。

一次调度发布（多个租约 + 多条通道开启）作为同一事务写入：要么整批落盘，
要么一条都不写，不会出现半生效状态。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable


class EventStore:
    """每行一个事务：``{"tx": 序号, "events": [...]}``。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._file = None
        self.tx_seq = 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a+", encoding="utf-8")
            self._file.seek(0)
            for line in self._file:
                line = line.strip()
                if line:
                    self.tx_seq = max(self.tx_seq, json.loads(line)["tx"])
            self._file.seek(0, os.SEEK_END)

    def append(self, events: Iterable[dict[str, Any]]) -> int:
        """原子追加一个事务。磁盘写入失败时异常上抛，不产生部分写入。"""
        events = list(events)
        if not events:
            return self.tx_seq
        self.tx_seq += 1
        record = json.dumps(
            {"tx": self.tx_seq, "events": events}, ensure_ascii=False, sort_keys=True
        )
        if self._file is not None:
            self._file.write(record + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())
        return self.tx_seq

    def replay(self) -> list[dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.extend(json.loads(line)["events"])
        return events

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
