"""原子快照存储：重启后等待队列、租约与升级期限继续有效。

写入采用「临时文件 + fsync + 原子 rename」，进程在任意时刻崩溃都只会留下
上一份完整快照或新的完整快照，不会读到半截 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JsonSnapshotStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=self.path.name, suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
