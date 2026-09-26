"""值班服务：统一编排时间线、资源、租约、调度、分流案件。

这是值班接口与自动化测试共用的应用内核；HTTP 层只是它的薄适配。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict

from .cases import CaseBook
from .clock import Clock, SystemClock, iso
from .errors import PublishError
from .leases import LeaseRegistry
from .resources import (
    Channel,
    Device,
    DeviceStatus,
    ResourcePool,
    Staff,
)
from .scheduler import (
    TRIG_HANDOVER,
    TRIG_RECOVERY,
    Publisher,
    Scheduler,
)
from .timeline import (
    FlightEvent,
    FlightLog,
    ForecastBook,
    ForecastVersion,
    Timeline,
)


class RecordingPublisher(Publisher):
    """默认发布器：记录成功发布的计划；可被配置为对指定请求失败。"""

    def __init__(self, fail_requests: set[str] | None = None) -> None:
        self.published: list[dict] = []
        self.fail_requests = fail_requests or set()

    def publish(self, plan) -> None:
        if plan.plan_id in self.fail_requests:
            raise PublishError(f"发布通道拒收计划 {plan.plan_id}")
        self.published.append(plan.as_dict())


class PeakDutyService:
    def __init__(
        self,
        pool: ResourcePool,
        clock: Clock | None = None,
        *,
        secret: str = "unit-test-secret",
        slot_minutes: int = 15,
        publisher: Publisher | None = None,
        baseline_rate: int = 200,
    ) -> None:
        self.clock = clock or SystemClock()
        self.slot_minutes = slot_minutes
        self.pool = pool
        self.flights = FlightLog()
        self.forecasts = ForecastBook()
        self.timeline = Timeline(self.flights, self.forecasts, slot_minutes)
        self.registry = LeaseRegistry(pool, self.clock)
        self.scheduler = Scheduler(
            self.timeline, self.registry, self.clock, baseline_rate=baseline_rate
        )
        self.cases = CaseBook(pool, self.registry, secret, self.clock)
        self.publisher = publisher or RecordingPublisher()

    # ---- 输入：航班动态与预测 --------------------------------------------

    def record_flight_event(self, payload: dict, received_at: str | None = None) -> dict:
        event = FlightEvent(
            flight_no=payload["flight_no"],
            version=int(payload["version"]),
            kind=payload["kind"],
            occurred_at=payload["occurred_at"],
            direction=payload.get("direction"),
            pax=payload.get("pax"),
            start=payload.get("start"),
            end=payload.get("end"),
            note=payload.get("note", ""),
            event_id=payload.get("event_id", ""),
            received_at=received_at or iso(self.clock.now()),
        )
        outcome = self.flights.ingest(event)
        return {"event_id": event.identity(), "outcome": outcome}

    def record_forecast(self, payload: dict) -> dict:
        forecast = ForecastVersion(
            version=int(payload["version"]),
            issued_at=payload.get("issued_at", iso(self.clock.now())),
            series=dict(payload["series"]),
            note=payload.get("note", ""),
        )
        outcome = self.forecasts.ingest(forecast)
        return {"version": forecast.version, "outcome": outcome}

    # ---- 输入：设备状态 --------------------------------------------------

    def set_device(self, device_id: str, status: str, efficiency: float = 1.0) -> dict:
        new_status = DeviceStatus(status)
        device = self.pool.update_device(device_id, new_status, efficiency)
        invalidated: list[str] = []
        # 必需设备检修：该通道既有租约立即失效，人员释放回可用池
        channel = self.pool.channels[device.channel_id]
        if device.kind in channel.required_kinds() and not device.available:
            for lease in self.registry.invalidate_channel(
                channel.channel_id, reason="设备检修", now=self.clock.now()
            ):
                invalidated.append(lease.lease_id)
            # 从各时间槽的开放清单中摘除该通道
            for assignments in self.scheduler.openings.values():
                assignments[:] = [
                    a for a in assignments if a.channel_id != channel.channel_id
                ]
        recovered = (
            device.kind in channel.required_kinds() and new_status == DeviceStatus.UP
        )
        return {
            "device_id": device_id,
            "status": new_status.value,
            "invalidated_leases": invalidated,
            "recovered": recovered,
        }

    # ---- 调度发布 --------------------------------------------------------

    def plan_and_publish(
        self,
        slot: str,
        *,
        triggers: list[str] | None = None,
        request_id: str = "",
        ttl_minutes: int = 30,
        publisher: Publisher | None = None,
    ) -> dict:
        counts = self.cases.queue_counts()
        plan = self.scheduler.plan_slot(
            slot,
            secondary_queue=counts["AWAIT_SECONDARY"],
            release_queue=counts["AWAIT_RELEASE"],
            triggers=triggers,
        )
        published = self.scheduler.publish(
            plan, publisher or self.publisher,
            ttl_minutes=ttl_minutes, request_id=request_id,
        )
        return published.as_dict()

    def handover(self, lease_id: str, to_staff_id: str, *, request_id: str = "") -> dict:
        new_lease = self.registry.handover(lease_id, to_staff_id, request_id=request_id)
        # 交接后重建岗位组合（对外可见的触发原因）
        slot = new_lease.slot
        counts = self.cases.queue_counts()
        plan = self.scheduler.plan_slot(
            slot,
            secondary_queue=counts["AWAIT_SECONDARY"],
            release_queue=counts["AWAIT_RELEASE"],
            triggers=[TRIG_HANDOVER],
        )
        self.scheduler.publish(plan, self.publisher, request_id=request_id or f"handover-{uuid.uuid4().hex[:6]}")
        return {"new_lease_id": new_lease.lease_id, "slot": slot}

    def recover_channel(self, *, slot: str, request_id: str = "",
                        ttl_minutes: int = 30) -> dict:
        """设备恢复后，沿用同一套租约规则重新规划并补位发布。"""
        plan = self.scheduler.plan_slot(slot, triggers=[TRIG_RECOVERY])
        published = self.scheduler.publish(
            plan, self.publisher,
            ttl_minutes=ttl_minutes,
            request_id=request_id or f"recovery-{uuid.uuid4().hex[:6]}",
        )
        return {
            "slot": slot,
            "plan_id": published.plan_id,
            "opened": published.opened,
        }

    # ---- 分流案件委托 ----------------------------------------------------

    def divert(self, payload: dict) -> dict:
        case, created = self.cases.open_case(
            token=payload["token"],
            reason_code=payload["reason_code"],
            created_by=payload["created_by"],
            slot=payload["slot"],
            flight_no=payload.get("flight_no", ""),
            note=payload.get("note", ""),
        )
        return {"case_id": case.case_id, "created": created, "pseudonym": case.pseudonym}

    def claim_case(self, case_id: str, staff_id: str, lease_id: str) -> dict:
        lease = self.registry._leases[lease_id]  # noqa: SLF001
        case = self.cases.claim(case_id, staff_id, lease)
        return {"case_id": case_id, "status": case.status.value}

    def review_case(self, case_id: str, *, approved: bool, decided_by: str,
                    lease_id: str, note: str = "") -> dict:
        lease = self.registry._leases[lease_id]  # noqa: SLF001
        case = self.cases.review(
            case_id, approved=approved, decided_by=decided_by, lease=lease, note=note
        )
        return {"case_id": case_id, "status": case.status.value}

    def finalize_case(self, case_id: str, *, release: bool, decided_by: str,
                      lease_id: str, note: str = "") -> dict:
        lease = self.registry._leases[lease_id]  # noqa: SLF001
        case = self.cases.finalize(
            case_id, release=release, decided_by=decided_by, lease=lease, note=note
        )
        return {"case_id": case_id, "status": case.status.value}

    # ---- 时间推进 --------------------------------------------------------

    def tick(self) -> dict:
        now = self.clock.now()
        lease_events = self.registry.tick(now)
        case_events = self.cases.tick(now)
        return {
            "expired_leases": [l.lease_id for l in lease_events["expired_leases"]],
            "escalations": [
                {"wait_id": w.wait_id, "position": w.position, "slot": w.slot}
                for w in lease_events["escalations"]
            ],
            "expired_waits": [w.wait_id for w in lease_events["expired_waits"]],
            "requeued_cases": case_events["requeued"],
        }

    # ---- 值班看板 --------------------------------------------------------

    def board(self, slot: str) -> dict:
        now = self.clock.now()
        plan = self.scheduler.plans.get(slot)
        return {
            "as_of": iso(now),
            "slot": slot,
            "demand": self.timeline.slot_demand(slot).__dict__,
            "remaining_capacity": self.scheduler.remaining_capacity(slot, now),
            "congestion_sources": self.scheduler.congestion_sources(slot, now),
            "open_channels": self.scheduler.current_openings(slot, now),
            "plan": plan.as_dict() if plan else None,
            "case_queues": self.cases.queue_counts(),
            "waiting_requests": [
                {
                    "wait_id": w.wait_id,
                    "position": w.position,
                    "state": w.state.value,
                    "escalate_at": w.escalate_at,
                    "expires_at": w.expires_at,
                }
                for w in self.registry.waiting(slot=slot)
            ],
        }

    def case_chain(self, case_id: str) -> dict:
        return self.cases.decision_chain(case_id)

    # ---- 持久化快照 ------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "format": "peak-duty-snapshot",
            "version": 1,
            "slot_minutes": self.slot_minutes,
            "pool": _pool_snapshot(self.pool),
            "flights": [asdict(e) for e in self.flights.events()],
            "forecasts": [
                {"version": f.version, "issued_at": f.issued_at,
                 "series": f.series, "note": f.note}
                for f in [self.forecasts.get(v) for v in sorted(self.forecasts._versions)]  # noqa: SLF001
                if f is not None
            ],
            "leases": self.registry.snapshot(),
            "cases": self.cases.snapshot(),
            "scheduler": self.scheduler.snapshot(),
        }

    def restore(self, data: dict) -> None:
        pool = _pool_restore(data["pool"])
        self.pool = pool
        self.registry = LeaseRegistry(pool, self.clock)
        self.registry.restore(data["leases"])
        self.cases = CaseBook(pool, self.registry, self.cases.secret, self.clock)
        self.cases.restore(data["cases"])
        self.flights = FlightLog()
        for item in data["flights"]:
            self.flights.ingest(FlightEvent(**item))
        self.forecasts = ForecastBook()
        for item in data["forecasts"]:
            self.forecasts.ingest(ForecastVersion(**item))
        self.timeline = Timeline(self.flights, self.forecasts, self.slot_minutes)
        self.scheduler = Scheduler(
            self.timeline, self.registry, self.clock,
            baseline_rate=self.scheduler.baseline_rate,
        )
        self.scheduler.restore(data.get("scheduler", {}))


def _pool_snapshot(pool: ResourcePool) -> dict:
    return {
        "channels": [
            {
                "channel_id": c.channel_id,
                "name": c.name,
                "kind": c.kind,
                "base_rate": c.base_rate,
                "position": c.position,
                "devices": [
                    {"device_id": d.device_id, "kind": d.kind,
                     "channel_id": d.channel_id, "status": d.status.value,
                     "efficiency": d.efficiency}
                    for d in c.devices.values()
                ],
            }
            for c in pool.channels.values()
        ],
        "staff": [
            {"staff_id": s.staff_id, "name": s.name,
             "qualifications": sorted(s.qualifications), "team": s.team}
            for s in pool.staff.values()
        ],
    }


def _pool_restore(data: dict) -> ResourcePool:
    pool = ResourcePool()
    for item in data["channels"]:
        channel = Channel(
            channel_id=item["channel_id"],
            name=item["name"],
            kind=item["kind"],
            base_rate=item["base_rate"],
            position=item.get("position"),
        )
        for d in item["devices"]:
            channel.devices[d["device_id"]] = Device(
                device_id=d["device_id"],
                kind=d["kind"],
                channel_id=d["channel_id"],
                status=DeviceStatus(d["status"]),
                efficiency=d.get("efficiency", 1.0),
            )
        pool.add_channel(channel)
    for item in data["staff"]:
        pool.add_staff(Staff(
            staff_id=item["staff_id"],
            name=item["name"],
            qualifications=frozenset(item["qualifications"]),
            team=item.get("team", ""),
        ))
    return pool
