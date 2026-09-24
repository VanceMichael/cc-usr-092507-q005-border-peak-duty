"""读取并校验项目的领域资料。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = {"domain", "version", "actors", "facts", "constraints"}

def load_context(path: Path) -> dict[str, Any]:
    """返回字段完整、版本有效的领域资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not REQUIRED_FIELDS.issubset(value):
        raise ValueError("领域资料缺少必要字段")
    if value["version"] < 1:
        raise ValueError("领域资料版本无效")
    for field in ("actors", "facts", "constraints"):
        if not isinstance(value[field], list) or len(value[field]) < 2:
            raise ValueError(f"领域资料字段不完整：{field}")
    return value
