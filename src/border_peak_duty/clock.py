"""时钟与时间槽工具。

系统内部统一使用带 UTC 时区的 ``datetime``；对外的时间槽用 15 分钟（可配置）
对齐到整点的 ISO 字符串 ``YYYY-MM-DDTHH:MM`` 表示。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

UTC = timezone.utc


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class VirtualClock:
    """测试用时钟：时间只随测试显式推进。"""

    def __init__(self, start: datetime | str | None = None) -> None:
        self._now = parse_ts(start) if start else datetime(2026, 10, 1, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime | str) -> None:
        self._now = parse_ts(value)

    def advance(self, minutes: int = 0, seconds: int = 0) -> datetime:
        self._now += timedelta(minutes=minutes, seconds=seconds)
        return self._now


def parse_ts(value: datetime | str) -> datetime:
    """接受 ISO 字符串或 datetime；朴素时间视为 UTC。"""
    if isinstance(value, datetime):
        ts = value
    else:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def iso(ts: datetime) -> str:
    return parse_ts(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def slot_start(ts: datetime | str, slot_minutes: int = 15) -> datetime:
    ts = parse_ts(ts)
    midnight = datetime.combine(ts.date(), datetime.min.time(), tzinfo=UTC)
    delta = ts - midnight
    snapped = (delta // timedelta(minutes=slot_minutes)) * timedelta(minutes=slot_minutes)
    return midnight + snapped


def to_slot(ts: datetime | str, slot_minutes: int = 15) -> str:
    return slot_start(ts, slot_minutes).strftime("%Y-%m-%dT%H:%M")


def parse_slot(slot: str) -> datetime:
    return parse_ts(slot + ":00Z")


def shift_slot(slot: str, steps: int = 1, slot_minutes: int = 15) -> str:
    ts = parse_slot(slot) + timedelta(minutes=slot_minutes * steps)
    return ts.strftime("%Y-%m-%dT%H:%M")


def iter_slots(start: datetime | str, end: datetime | str, slot_minutes: int = 15):
    """生成覆盖 ``[start, end)`` 的对齐时间槽字符串。"""
    cur = slot_start(start, slot_minutes)
    end_ts = parse_ts(end)
    while cur < end_ts:
        yield cur.strftime("%Y-%m-%dT%H:%M")
        cur += timedelta(minutes=slot_minutes)
