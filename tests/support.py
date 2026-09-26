"""测试共用构造：一套双通道类型、多资质人员的小型高峰现场。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from border_peak_duty.models import Lane, Qualification
from border_peak_duty.service import PeakDutyService
from border_peak_duty.timeline import Timeline


def build_service(journal_path: str | None = None, gate=None, escalation_minutes: int = 30) -> PeakDutyService:
    timeline = Timeline(bucket_minutes=60)
    service = PeakDutyService(
        timeline,
        journal_path=journal_path,
        gate=gate,
        escalation_minutes=escalation_minutes,
    )
    lanes = [
        Lane("C1", "chinese", "DEV-C1", base_capacity=80),
        Lane("C2", "chinese", "DEV-C2", base_capacity=80),
        Lane("F1", "foreigner", "DEV-F1", base_capacity=80),
        Lane("S1", "special", "DEV-S1", base_capacity=40),
    ]
    staff = [
        Qualification("S01", frozenset({"primary"}), frozenset({"chinese", "foreigner"})),
        Qualification("S02", frozenset({"primary"}), frozenset({"chinese", "foreigner"})),
        Qualification(
            "S03", frozenset({"primary"}), frozenset({"chinese"}),
            secondary_review=True,
        ),
        Qualification(
            "S04", frozenset({"primary"}), frozenset({"chinese", "foreigner", "special"}),
            secondary_review=True, release_authority=True,
        ),
        Qualification("S05", frozenset({"primary"}), frozenset({"special"})),
    ]
    for lane in lanes:
        service.register_lane(lane)
    for qualification in staff:
        service.register_staff(qualification)
    return service
