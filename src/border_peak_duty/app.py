"""托管应用：命令分发、变更后自动快照、重启恢复。

HTTP 层与自动化测试都通过这一层访问领域服务，确保两条路径行为一致。
"""

from __future__ import annotations

from typing import Any

from .clock import Clock, VirtualClock
from .resources import ResourcePool
from .service import PeakDutyService
from .store import JsonSnapshotStore


class HostedApp:
    def __init__(
        self,
        store: JsonSnapshotStore | None = None,
        pool: ResourcePool | None = None,
        clock: Clock | None = None,
        *,
        secret: str = "unit-test-secret",
        slot_minutes: int = 15,
        autosnapshot: bool = True,
        baseline_rate: int = 200,
    ) -> None:
        self.store = store
        self.clock = clock or VirtualClock()
        self.secret = secret
        self.slot_minutes = slot_minutes
        self.autosnapshot = autosnapshot
        self.baseline_rate = baseline_rate
        self.service = self._build(pool)
        if self.store is not None and self.store.exists():
            data = self.store.load()
            if data:
                self.service.restore(data)

    def _build(self, pool: ResourcePool | None) -> PeakDutyService:
        return PeakDutyService(
            pool or ResourcePool(),
            self.clock,
            secret=self.secret,
            slot_minutes=self.slot_minutes,
            baseline_rate=self.baseline_rate,
        )

    def save(self) -> None:
        if self.store is not None:
            self.store.save(self.service.snapshot())

    def restart(self) -> None:
        """模拟服务重启：从最近快照完整恢复（同一时钟继续走）。"""
        if self.store is None or not self.store.exists():
            raise RuntimeError("未配置持久化存储，无法重启恢复")
        data = self.store.load()
        self.service = self._build(None)
        self.service.restore(data)

    def command(self, name: str, payload: dict | None = None) -> dict[str, Any]:
        payload = payload or {}
        handler = self._commands()[name]
        result = handler(payload)
        if self.autosnapshot:
            self.save()
        return result if isinstance(result, dict) else {"result": result}

    def query(self, name: str, payload: dict | None = None) -> dict[str, Any]:
        payload = payload or {}
        return self._queries()[name](payload)

    # ---- 命令/查询表 -----------------------------------------------------

    def _commands(self) -> dict:
        s = self.service

        def record_flight(p):
            return s.record_flight_event(p, received_at=p.get("received_at"))

        return {
            "record_flight_event": record_flight,
            "record_forecast": s.record_forecast,
            "set_device": lambda p: s.set_device(
                p["device_id"], p["status"], float(p.get("efficiency", 1.0))
            ),
            "plan_and_publish": lambda p: s.plan_and_publish(
                p["slot"],
                triggers=p.get("triggers"),
                request_id=p.get("request_id", ""),
                ttl_minutes=int(p.get("ttl_minutes", 30)),
            ),
            "handover": lambda p: s.handover(
                p["lease_id"], p["to_staff_id"], request_id=p.get("request_id", "")
            ),
            "recover_channel": lambda p: s.recover_channel(
                slot=p["slot"],
                request_id=p.get("request_id", ""),
                ttl_minutes=int(p.get("ttl_minutes", 30)),
            ),
            "divert": s.divert,
            "claim_case": lambda p: s.claim_case(p["case_id"], p["staff_id"], p["lease_id"]),
            "review_case": lambda p: s.review_case(
                p["case_id"],
                approved=bool(p["approved"]),
                decided_by=p["decided_by"],
                lease_id=p["lease_id"],
                note=p.get("note", ""),
            ),
            "finalize_case": lambda p: s.finalize_case(
                p["case_id"],
                release=bool(p["release"]),
                decided_by=p["decided_by"],
                lease_id=p["lease_id"],
                note=p.get("note", ""),
            ),
            "tick": lambda p: s.tick(),
        }

    def _queries(self) -> dict:
        s = self.service
        return {
            "board": lambda p: s.board(p["slot"]),
            "case_chain": lambda p: s.case_chain(p["case_id"]),
            "snapshot": lambda p: s.snapshot(),
        }
