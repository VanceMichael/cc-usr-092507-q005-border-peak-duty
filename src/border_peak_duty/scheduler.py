"""岗位租约、通道开启规划与原子调度。

租约是调配、交接、设备恢复共同遵守的互斥单元：

- 同一位人员在重叠时段只能持有一个有效租约；
- 同一条通道同一时段只能有一个有效岗位租约；
- 人员占用与通道开启在同一个发布事务里生效：通道发布器失败时，预留的人员
  立即释放，绝不留下“人已占、通道未开”的中间状态；
- 交接班先原子撤销旧租约、再立新租约；设备检修释放租约、恢复后才能重新承租。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .cases import CaseStore
from .journal import EventStore
from .models import (
    DeviceStatus,
    Lane,
    LaneKind,
    PublishConflict,
    QualificationError,
)
from .timeline import Timeline


# ---------------------------------------------------------------------------
# 租约
# ---------------------------------------------------------------------------


@dataclass
class Lease:
    lease_id: str
    staff_id: str
    lane_id: str
    position: str
    bucket_start: str
    bucket_end: str
    acquired_at: str
    status: str = "active"          # active / released
    released_at: str | None = None
    release_reason: str | None = None

    def overlaps(self, bucket_start: str, bucket_end: str) -> bool:
        return self.status == "active" and bucket_start < self.bucket_end and bucket_end > self.bucket_start

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "staff_id": self.staff_id,
            "lane_id": self.lane_id,
            "position": self.position,
            "bucket_start": self.bucket_start,
            "bucket_end": self.bucket_end,
            "acquired_at": self.acquired_at,
            "status": self.status,
            "released_at": self.released_at,
            "release_reason": self.release_reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Lease":
        return cls(**value)


class LaneGate(Protocol):
    """通道开启执行器（勤务/硬件联动接口）。"""

    def open(self, lane_ids: list[str]) -> None: ...


class InMemoryLaneGate:
    """默认通道门：记录已开启通道；测试可注入失败。"""

    def __init__(self, fail_for: Callable[[list[str]], bool] | None = None) -> None:
        self.opened: set[str] = set()
        self._fail_for = fail_for

    def open(self, lane_ids: list[str]) -> None:
        if self._fail_for is not None and self._fail_for(lane_ids):
            raise PublishConflict(f"通道开启联动失败：{lane_ids}")
        self.opened.update(lane_ids)

    def close(self, lane_ids: list[str]) -> None:
        self.opened.difference_update(lane_ids)


# ---------------------------------------------------------------------------
# 规划产物
# ---------------------------------------------------------------------------


@dataclass
class LaneAssignment:
    lane: Lane
    staff_id: str
    position: str
    capacity: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane.lane_id,
            "kind": self.lane.kind,
            "staff_id": self.staff_id,
            "position": self.position,
            "capacity": self.capacity,
        }


@dataclass
class BucketPlan:
    bucket_start: str
    bucket_end: str
    open: list[LaneAssignment] = field(default_factory=list)
    reserve_lanes: list[str] = field(default_factory=list)
    reserve_staff: list[str] = field(default_factory=list)
    spare_capacity: int = 0
    triggers: list[dict[str, str]] = field(default_factory=list)
    demand_by_kind: dict[str, int] = field(default_factory=dict)
    waiting_by_kind: dict[str, int] = field(default_factory=dict)
    forecast_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket_start": self.bucket_start,
            "bucket_end": self.bucket_end,
            "open_lanes": [a.to_dict() for a in self.open],
            "reserve_lanes": list(self.reserve_lanes),
            "reserve_staff": list(self.reserve_staff),
            "spare_capacity": self.spare_capacity,
            "triggers": list(self.triggers),
            "demand_by_kind": dict(self.demand_by_kind),
            "waiting_by_kind": dict(self.waiting_by_kind),
            "forecast_version": self.forecast_version,
        }


class Planner:
    """纯计算：根据时间线与队列产出某时段的开放方案（不落任何状态）。"""

    def __init__(
        self,
        timeline: Timeline,
        waiting_count_by_kind: Callable[[], dict[str, int]],
        demand_shares: dict[str, float] | None = None,
    ) -> None:
        self.timeline = timeline
        self.waiting_count_by_kind = waiting_count_by_kind
        self.demand_shares = demand_shares or {
            # 预测客流只在常规通道间分摊；特殊通道仅在有该类排队旅客时开放，
            # 平时作为备用容量。
            LaneKind.CHINESE.value: 2 / 3,
            LaneKind.FOREIGNER.value: 1 / 3,
        }

    def plan(self, bucket_start: str) -> BucketPlan:
        start, end = self.timeline.bucket_bounds(bucket_start)
        demand = self.timeline.demand(bucket_start)
        waiting = self.waiting_count_by_kind()

        demand_by_kind: dict[str, int] = {}
        kinds = set(self.demand_shares) | set(waiting)
        for kind in kinds:
            projected = int(demand.total * self.demand_shares.get(kind, 0.0))
            demand_by_kind[kind] = projected + waiting.get(kind, 0)

        plan = BucketPlan(
            bucket_start=start,
            bucket_end=end,
            demand_by_kind=demand_by_kind,
            waiting_by_kind=dict(waiting),
            forecast_version=demand.forecast_version,
        )
        if demand.flights:
            plan.triggers.append({
                "code": "flight_arrival_window",
                "detail": f"时段内到场航班 {','.join(demand.flights)}，合计 {demand.flight_pax} 人",
            })
        if demand.forecast_pax:
            plan.triggers.append({
                "code": "forecast_demand",
                "detail": f"预测版本 v{demand.forecast_version}：{demand.forecast_pax} 人",
            })
        if waiting:
            plan.triggers.append({
                "code": "carryover_backlog",
                "detail": f"上时段积压 {sum(waiting.values())} 人",
            })

        active_leases: list[Lease] = getattr(self, "_active_leases", [])
        # 只排除与目标时段重叠的租约；其它时段的有效租约不占用本时段。
        overlapping = [
            l for l in active_leases
            if l.status == "active" and l.overlaps(start, end)
        ]
        busy_staff = {l.staff_id for l in overlapping}
        open_lane_ids = {l.lane_id for l in overlapping}

        reported_unavailable: set[str] = set()
        for kind, workload in sorted(demand_by_kind.items()):
            if workload <= 0:
                continue
            lanes = sorted(
                (lane for lane in self.timeline.lanes.values() if lane.kind == kind),
                key=lambda x: x.lane_id,
            )
            cumulative = 0
            for lane in lanes:
                capacity, reason = self.timeline.devices.effective_capacity(lane)
                if capacity <= 0:
                    if lane.lane_id not in reported_unavailable:
                        reported_unavailable.add(lane.lane_id)
                        plan.triggers.append({
                            "code": "device_unavailable",
                            "detail": f"通道 {lane.lane_id} 因 {reason} 不可开放",
                        })
                    continue
                if lane.lane_id in open_lane_ids:
                    lease = next(l for l in active_leases if l.lane_id == lane.lane_id)
                    plan.open.append(
                        LaneAssignment(
                            lane=lane, staff_id=lease.staff_id,
                            position=lease.position, capacity=capacity,
                        )
                    )
                    cumulative += capacity
                    continue
                if cumulative >= workload:
                    plan.reserve_lanes.append(lane.lane_id)
                    plan.spare_capacity += capacity
                    continue
                staff = self._pick_staff(lane, busy_staff)
                if staff is None:
                    plan.reserve_lanes.append(lane.lane_id)
                    plan.triggers.append({
                        "code": "staff_constrained",
                        "detail": f"通道 {lane.lane_id} 可开但无空闲合格人员，转入备用",
                    })
                    continue
                plan.open.append(
                    LaneAssignment(lane=lane, staff_id=staff, position=lane.required_position, capacity=capacity)
                )
                busy_staff.add(staff)
                cumulative += capacity
            if cumulative < workload:
                plan.triggers.append({
                    "code": "capacity_shortfall",
                    "detail": f"{kind} 通道能力 {cumulative} 低于需求 {workload}，缺口 {workload - cumulative} 人",
                })

        # 未被需求循环触及的健康通道（如无客流类型的特殊通道）统一归入备用。
        touched = {a.lane.lane_id for a in plan.open} | set(plan.reserve_lanes)
        for lane in sorted(self.timeline.lanes.values(), key=lambda x: x.lane_id):
            if lane.lane_id in touched:
                continue
            capacity, reason = self.timeline.devices.effective_capacity(lane)
            if capacity <= 0:
                if lane.lane_id not in reported_unavailable:
                    reported_unavailable.add(lane.lane_id)
                    plan.triggers.append({
                        "code": "device_unavailable",
                        "detail": f"通道 {lane.lane_id} 因 {reason} 不可开放",
                    })
            else:
                plan.reserve_lanes.append(lane.lane_id)
                plan.spare_capacity += capacity

        # 未被使用且设备正常的人员为备勤
        assigned = {a.staff_id for a in plan.open} | busy_staff
        plan.reserve_staff = [
            q.staff_id
            for q in self.timeline.roster.all()
            if q.staff_id not in assigned
        ]
        return plan

    def _pick_staff(self, lane: Lane, busy_staff: set[str]) -> str | None:
        for qualification in self.timeline.roster.all():
            if qualification.staff_id in busy_staff:
                continue
            if qualification.can_serve(lane.required_position, lane.kind):
                return qualification.staff_id
        return None

    def with_leases(self, active_leases: list[Lease]) -> "Planner":
        """绑定当前有效租约后再计算，避免把已开通道/已占人员重新规划。"""
        self._active_leases = active_leases
        return self


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


class Scheduler:
    """持有全部运行状态，以事件日志保证原子发布与重启恢复。"""

    def __init__(
        self,
        timeline: Timeline,
        queue: Any,
        cases: CaseStore,
        journal: EventStore,
        gate: LaneGate | None = None,
    ) -> None:
        self.timeline = timeline
        self.queue = queue
        self.cases = cases
        self.journal = journal
        self.gate = gate or InMemoryLaneGate()
        self.leases: dict[str, Lease] = {}
        self._replay_handlers: dict[str, Callable[[dict[str, Any]], None]] = {}

    # -- 输入事件（落日志，重放时重建时间线） --------------------------------

    def apply_flight_event(self, event: Any) -> str:
        outcome = self.timeline.flights.apply(event)
        self.journal.append([{"type": "flight_event", "event": event.to_dict(), "outcome": outcome}])
        return outcome

    def apply_forecast(self, forecast: Any) -> str:
        outcome = self.timeline.forecasts.apply(forecast)
        self.journal.append([{"type": "forecast", "forecast": forecast.to_dict(), "outcome": outcome}])
        return outcome

    def apply_device(self, state: Any) -> str:
        outcome = self.timeline.devices.apply(state)
        self.journal.append([{"type": "device", "state": state.to_dict(), "outcome": outcome}])
        # 检修/离线立即收回该设备上通道的租约。
        if outcome == "new" and state.status in (
            DeviceStatus.MAINTENANCE.value,
            DeviceStatus.OFFLINE.value,
        ):
            self._revoke_lanes_for_device(state.device_id, state.updated_at, f"device_{state.status}")
        return outcome

    # -- 规划与发布 ----------------------------------------------------------

    def planner(self) -> Planner:
        return Planner(self.timeline, self._waiting_by_kind)

    def _waiting_by_kind(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for entry in self.queue.waiting():
            result[entry.eligible_kind] = result.get(entry.eligible_kind, 0) + 1
        return result

    def active_leases(self) -> list[Lease]:
        return [l for l in self.leases.values() if l.status == "active"]

    def publish_bucket(self, bucket_start: str, now: str) -> BucketPlan:
        """计算方案并原子发布：通道联动失败则回滚全部人员预留。"""
        start, end = self.timeline.bucket_bounds(bucket_start)
        self._expire_leases(now)
        plan = Planner(self.timeline, self._waiting_by_kind).with_leases(self.active_leases()).plan(bucket_start)

        new_assignments = [a for a in plan.open if a.lane.lane_id not in {l.lane_id for l in self.active_leases()}]
        if not new_assignments:
            return plan

        # 1) 预校验：资质与互斥（双重保险，Planner 已尽量避开）。
        held: list[Lease] = []
        for index, assignment in enumerate(new_assignments):
            qualification = self.timeline.roster.get(assignment.staff_id)
            if qualification is None or not qualification.can_serve(assignment.position, assignment.lane.kind):
                self._release_held(held, now, "qualification_failed")
                raise QualificationError(
                    f"人员 {assignment.staff_id} 不具备 {assignment.lane.kind} 通道 {assignment.position} 资质"
                )
            if self._has_overlap(assignment.staff_id, assignment.lane.lane_id, start, end):
                self._release_held(held, now, "overlap")
                raise PublishConflict("人员或通道在该时段已有有效租约")
            held.append(
                Lease(
                    lease_id=f"lease-{self.journal.tx_seq + 1}-{index}",
                    staff_id=assignment.staff_id,
                    lane_id=assignment.lane.lane_id,
                    position=assignment.position,
                    bucket_start=start,
                    bucket_end=end,
                    acquired_at=now,
                    status="held",
                )
            )

        # 2) 执行通道联动；失败则不留任何占用。
        lane_ids = [a.lane.lane_id for a in new_assignments]
        try:
            self.gate.open(lane_ids)
        except Exception:
            self._release_held(held, now, "gate_failed")
            raise

        # 3) 联动成功后整批落日志并生效；日志失败则撤回通道联动，
        #    保证人员占用与通道启用同生共死。
        events = [
            {"type": "lanes_opened", "bucket_start": start, "bucket_end": end, "lane_ids": lane_ids}
        ]
        for lease in held:
            lease.status = "active"
            self.leases[lease.lease_id] = lease
            events.append({"type": "lease_acquired", "lease": lease.to_dict()})
        try:
            self.journal.append(events)
        except Exception:
            self.gate.close(lane_ids)
            self._release_held(held, now, "journal_failed")
            raise
        return plan

    def handover(self, lease_id: str, to_staff: str, at: str) -> Lease:
        """交接班：旧租约释放与新租约建立在同一事务中。"""
        old = self.leases.get(lease_id)
        if old is None or old.status != "active":
            raise PublishConflict("租约不存在或已释放，无法交接")
        lane = self.timeline.lanes[old.lane_id]
        qualification = self.timeline.roster.get(to_staff)
        if qualification is None or not qualification.can_serve(old.position, lane.kind):
            raise QualificationError(f"接替人员 {to_staff} 资质不符")
        if self._has_overlap(to_staff, old.lane_id, old.bucket_start, old.bucket_end, exclude=lease_id):
            raise PublishConflict("接替人员在该时段已有有效租约")

        new = Lease(
            lease_id=f"lease-{self.journal.tx_seq + 1}-ho",
            staff_id=to_staff,
            lane_id=old.lane_id,
            position=old.position,
            bucket_start=old.bucket_start,
            bucket_end=old.bucket_end,
            acquired_at=at,
        )
        # 先落原子事务，再改内存，保证日志写失败时双方状态不变。
        self.journal.append([
            {"type": "lease_released", "lease_id": old.lease_id, "at": at, "reason": "handover"},
            {"type": "lease_acquired", "lease": new.to_dict()},
        ])
        old.status = "released"
        old.released_at = at
        old.release_reason = "handover"
        self.leases[new.lease_id] = new
        return new

    def release_lease(self, lease_id: str, at: str, reason: str = "manual") -> None:
        lease = self.leases.get(lease_id)
        if lease is None or lease.status != "active":
            raise PublishConflict("租约不存在或已释放")
        self.journal.append([
            {"type": "lease_released", "lease_id": lease_id, "at": at, "reason": reason},
        ])
        lease.status = "released"
        lease.released_at = at
        lease.release_reason = reason
        self.gate.close([lease.lane_id])
        self.journal.append([{"type": "lanes_closed", "lane_ids": [lease.lane_id]}])

    def device_recovered(self, state: Any) -> str:
        """设备恢复：状态进入时间线后，下一时段发布即可重新承租该通道。"""
        outcome = self.timeline.devices.apply(state)
        self.journal.append([{"type": "device", "state": state.to_dict(), "outcome": outcome}])
        return outcome

    # -- 内部 ---------------------------------------------------------------

    def _has_overlap(
        self, staff_id: str, lane_id: str, start: str, end: str, exclude: str | None = None
    ) -> bool:
        for lease in self.leases.values():
            if lease.status != "active" or lease.lease_id == exclude:
                continue
            if lease.staff_id == staff_id and lease.overlaps(start, end):
                return True
            if lease.lane_id == lane_id and lease.overlaps(start, end):
                return True
        return False

    def _release_held(self, held: list[Lease], at: str, reason: str) -> None:
        for lease in held:
            if lease.lease_id in self.leases:
                del self.leases[lease.lease_id]
        # held 租约从未进入 leases/日志，无需补偿事件；记录一条失败审计。
        if held:
            self.journal.append([{
                "type": "publish_aborted",
                "at": at,
                "reason": reason,
                "lane_ids": [l.lane_id for l in held],
            }])

    def _revoke_lanes_for_device(self, device_id: str, at: str, reason: str) -> None:
        events: list[dict[str, Any]] = []
        closed: list[str] = []
        for lease in self.active_leases():
            lane = self.timeline.lanes.get(lease.lane_id)
            if lane is not None and lane.device_id == device_id:
                lease.status = "released"
                lease.released_at = at
                lease.release_reason = reason
                closed.append(lease.lane_id)
                events.append(
                    {"type": "lease_released", "lease_id": lease.lease_id, "at": at, "reason": reason}
                )
        if closed:
            self.gate.close(closed)
            events.append({"type": "lanes_closed", "lane_ids": closed})
        if events:
            self.journal.append(events)

    def _expire_leases(self, now: str) -> None:
        """时段结束的租约自动到期释放，供下一发布周期重新调配。"""
        events: list[dict[str, Any]] = []
        closed: list[str] = []
        for lease in self.active_leases():
            if now >= lease.bucket_end:
                lease.status = "released"
                lease.released_at = now
                lease.release_reason = "bucket_end"
                closed.append(lease.lane_id)
                events.append(
                    {"type": "lease_released", "lease_id": lease.lease_id, "at": now, "reason": "bucket_end"}
                )
        if closed:
            self.gate.close(closed)
            events.append({"type": "lanes_closed", "lane_ids": closed})
        if events:
            self.journal.append(events)

    # -- 重启恢复 ------------------------------------------------------------

    def restore(self) -> None:
        """重放事件日志，恢复时间线、队列、案件、租约与通道门状态。"""
        from .cases import DiversionCase, CaseDecision
        from .models import DeviceState, FlightEvent, ForecastVersion

        self.leases.clear()
        for event in self.journal.replay():
            etype = event["type"]
            if etype == "flight_event":
                self.timeline.flights.apply(FlightEvent.from_dict(event["event"]))
            elif etype == "forecast":
                f = event["forecast"]
                self.timeline.forecasts.apply(
                    ForecastVersion(f["version"], f["issued_at"], f["buckets"], f["reason"])
                )
            elif etype == "device":
                s = event["state"]
                self.timeline.devices.apply(DeviceState(**s))
            elif etype == "lease_acquired":
                lease = Lease.from_dict(event["lease"])
                self.leases[lease.lease_id] = lease
            elif etype == "lease_released":
                lease = self.leases.get(event["lease_id"])
                if lease is not None:
                    lease.status = "released"
                    lease.released_at = event["at"]
                    lease.release_reason = event["reason"]
            elif etype == "lanes_opened":
                self.gate.open(event["lane_ids"])
            elif etype == "lanes_closed":
                self.gate.close(event["lane_ids"])
            else:
                # 队列/案件类事件由 Service 层注册的重放器处理。
                handler = self._replay_handlers.get(etype)
                if handler is not None:
                    handler(event)

    _replay_handlers: dict[str, Callable[[dict[str, Any]], None]]

    def register_replay_handler(self, etype: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._replay_handlers[etype] = handler
