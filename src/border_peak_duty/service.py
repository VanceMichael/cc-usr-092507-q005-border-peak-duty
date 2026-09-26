"""高峰保障服务：对外应用服务与值班接口。

把时间线、等待队列、分流案件、租约调度串成一条可重放的事件链；值班接口
``duty_snapshot`` 同时给出当前拥堵源、剩余能力，以及旅客为何被分流的脱敏
决定链。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from .cases import CaseStore, QueueEntry, WaitingQueue
from .journal import EventStore
from .models import (
    CaseState,
    CaseStage,
    FlightStatus,
    Lane,
    Qualification,
    QueueState,
    SeparationError,
)
from .scheduler import Scheduler
from .timeline import Timeline

DEFAULT_ESCALATION_MINUTES = 30


class PeakDutyService:
    def __init__(
        self,
        timeline: Timeline,
        journal_path: str | None = None,
        gate: Any | None = None,
        escalation_minutes: int = DEFAULT_ESCALATION_MINUTES,
    ) -> None:
        self.timeline = timeline
        self.journal = EventStore(journal_path)
        self.queue = WaitingQueue()
        self.cases = CaseStore(
            has_secondary=lambda staff_id: self._has_authority(staff_id, "secondary_review"),
            has_release=lambda staff_id: self._has_authority(staff_id, "release_authority"),
        )
        self.scheduler = Scheduler(timeline, self.queue, self.cases, self.journal, gate=gate)
        self.escalation_minutes = escalation_minutes
        self._counters: dict[str, int] = {}
        self._register_replay_handlers()

    # -- 通道与人员注册（同样落日志，支持仅靠日志重建） ----------------------

    def register_lane(self, lane: Lane) -> None:
        self.timeline.add_lane(lane)
        self.journal.append([{"type": "lane_registered", "lane": lane.to_dict()}])

    def register_staff(self, qualification: Qualification) -> None:
        self.timeline.roster.register(qualification)
        self.journal.append([{"type": "staff_registered", "staff": qualification.to_dict()}])

    # -- 受理与查验 ----------------------------------------------------------

    def admit_passenger(
        self,
        passenger_id: str,
        flight_no: str,
        eligible_kind: str,
        now: str,
    ) -> QueueEntry:
        """受理旅客进入等待队列。已离场航班不得再受理。"""
        if self.timeline.flights.is_terminal(flight_no):
            raise SeparationError(f"航班 {flight_no} 已离场/取消，不能再受理旅客")
        if self.queue.by_passenger(passenger_id) is not None:
            raise SeparationError(f"旅客 {passenger_id} 已受理，不得重复入队")
        entry_id = self._next_id("entry", passenger_id)
        event = {
            "type": "passenger_admitted",
            "entry": {
                "entry_id": entry_id,
                "passenger_id": passenger_id,
                "flight_no": flight_no,
                "eligible_kind": eligible_kind,
                "arrived_at": now,
            },
        }
        self.journal.append([event])
        return self.queue.admit(entry_id, passenger_id, flight_no, eligible_kind, now)

    def begin_service(self, entry_id: str, lane_id: str, now: str) -> QueueEntry:
        entry = self.queue.get(entry_id)
        if entry.state != QueueState.WAITING.value:
            raise SeparationError(f"旅客不在等待状态，无法开始查验：{entry.state}")
        self.journal.append([{
            "type": "service_began", "entry_id": entry_id, "lane_id": lane_id, "at": now,
        }])
        return self.queue.begin_service(entry_id, lane_id, now)

    def complete_service(self, entry_id: str, now: str) -> QueueEntry:
        entry = self.queue.get(entry_id)
        if entry.state != QueueState.SERVING.value:
            raise SeparationError(f"旅客不在查验中，无法放行：{entry.state}")
        self.journal.append([{"type": "service_completed", "entry_id": entry_id, "at": now}])
        return self.queue.finish(entry_id, now)

    # -- 分流案件 ------------------------------------------------------------

    def divert(
        self,
        entry_id: str,
        officer_id: str,
        reason_codes: list[str],
        now: str,
    ) -> dict[str, Any]:
        """普通查验发现证件失效/材料不足：发起分流案件（只存必要信息）。"""
        entry = self.queue.get(entry_id)
        if entry.state not in (QueueState.WAITING.value, QueueState.SERVING.value):
            raise SeparationError(f"当前状态不能发起分流：{entry.state}")
        case_id = self._next_id("case", entry.passenger_id)
        deadline = (
            datetime.fromisoformat(now) + timedelta(minutes=self.escalation_minutes)
        ).isoformat(timespec="minutes")
        # 旅客标识在案件中使用不透明代号，不落姓名、证件号。
        passenger_ref = f"PAX-{entry.passenger_id}"
        case = self.cases.open_case(
            case_id=case_id,
            passenger_ref=passenger_ref,
            flight_no=entry.flight_no,
            reason_codes=reason_codes,
            initiator=officer_id,
            at=now,
            escalation_deadline=deadline,
        )
        try:
            self.journal.append([
                {
                    "type": "case_opened",
                    "case": case.to_dict(),
                    "entry_id": entry_id,
                },
            ])
        except Exception:
            self.cases.remove(case_id)
            raise
        self.queue.divert(entry_id, case_id)
        return self.cases.masked_chain(case_id)

    def review_case(self, case_id: str, officer_id: str, decision: str, reason_code: str, now: str) -> dict[str, Any]:
        case = self.cases.decide(case_id, officer_id, decision, reason_code, now)
        events = [{
            "type": "case_decided", "case_id": case_id, "officer_id": officer_id,
            "decision": decision, "reason_code": reason_code, "at": now,
        }]
        if decision == "reject":
            events.append({"type": "case_terminal", "case_id": case_id, "outcome": "rejected"})
        self.journal.append(events)
        return self.cases.masked_chain(case_id)

    def release_case(self, case_id: str, officer_id: str, now: str) -> dict[str, Any]:
        case = self.cases.decide(case_id, officer_id, "release", "document_expired", now)
        self.journal.append([
            {
                "type": "case_decided", "case_id": case_id, "officer_id": officer_id,
                "decision": "release", "reason_code": "document_expired", "at": now,
            },
            {"type": "case_terminal", "case_id": case_id, "outcome": "resolved"},
        ])
        return self.cases.masked_chain(case_id)

    def resume_case(self, case_id: str, now: str) -> dict[str, Any]:
        case = self.cases.resume(case_id, now)
        self.journal.append([{"type": "case_resumed", "case_id": case_id, "at": now}])
        return self.cases.masked_chain(case_id)

    def close_flight(self, flight_no: str, now: str) -> dict[str, Any]:
        """航班离场：终态锁定，清扫未到场旅客，此后不再占用任何资源。"""
        from .models import FlightEvent

        event = FlightEvent(
            flight_no=flight_no,
            event_id=self._next_id("fclose", flight_no),
            revision=self.timeline.flights.max_revision(flight_no) + 1,
            status=FlightStatus.DEPARTED.value,
            occurred_at=now,
            scheduled_arrival=now,
            note="flight departed, terminal latch",
        )
        self.scheduler.apply_flight_event(event)
        abandoned = self.queue.abandon_flight(flight_no)
        self.journal.append([{
            "type": "flight_cleared", "flight_no": flight_no, "at": now,
            "abandoned_entry_ids": [e.entry_id for e in abandoned],
        }])
        return {"flight_no": flight_no, "abandoned": len(abandoned)}

    def tick_escalations(self, now: str) -> list[dict[str, Any]]:
        """升级期限巡检：超期案件升级，重启后期限仍以日志中的时间为准。"""
        overdue = self.cases.escalate_overdue(now)
        chains = []
        for case in overdue:
            self.journal.append([{"type": "case_escalated", "case_id": case.case_id, "at": now}])
            chains.append(self.cases.masked_chain(case.case_id))
        return chains

    # -- 值班接口 ------------------------------------------------------------

    def publish_bucket(self, bucket_start: str, now: str) -> dict[str, Any]:
        plan = self.scheduler.publish_bucket(bucket_start, now)
        return plan.to_dict()

    def duty_snapshot(self, bucket_start: str, now: str) -> dict[str, Any]:
        """值班长视图：拥堵源 + 剩余能力 + 脱敏决定链。"""
        plan = self.scheduler.planner().with_leases(self.scheduler.active_leases()).plan(bucket_start)
        queue_stats = self.queue.snapshot(now)
        demand = self.timeline.demand(bucket_start)

        congestion: list[dict[str, Any]] = []
        for trigger in plan.triggers:
            if trigger["code"] in (
                "capacity_shortfall", "device_unavailable", "staff_constrained",
                "carryover_backlog",
            ):
                congestion.append(trigger)
        open_capacity = sum(a.capacity for a in plan.open)
        escalated = [
            self.cases.masked_chain(c.case_id)
            for c in self.cases.open_cases()
            if c.state == CaseState.ESCALATED.value
        ]
        return {
            "as_of": now,
            "bucket": {"start": plan.bucket_start, "end": plan.bucket_end},
            "congestion_sources": congestion,
            "queue": queue_stats,
            "capacity": {
                "open_lanes": [a.to_dict() for a in plan.open],
                "open_capacity": open_capacity,
                "spare_capacity": plan.spare_capacity,
                "reserve_lanes": plan.reserve_lanes,
                "reserve_staff": plan.reserve_staff,
            },
            "demand": {
                "total": demand.total,
                "forecast_pax": demand.forecast_pax,
                "flight_pax": demand.flight_pax,
                "flights": demand.flights,
                "forecast_version": demand.forecast_version,
            },
            "open_cases": len(self.cases.open_cases()),
            "escalated_cases": escalated,
            "diversion_chains": [
                self.cases.masked_chain(c.case_id) for c in self.cases.open_cases()
            ],
        }

    # -- 恢复 ----------------------------------------------------------------

    def restore(self) -> None:
        """从事件日志重建队列、案件、租约、升级期限与计数器。"""
        self.queue = WaitingQueue()
        self.cases = CaseStore(
            has_secondary=lambda staff_id: self._has_authority(staff_id, "secondary_review"),
            has_release=lambda staff_id: self._has_authority(staff_id, "release_authority"),
        )
        self.scheduler.queue = self.queue
        self.scheduler.cases = self.cases
        self.scheduler.restore()

    def close(self) -> None:
        self.journal.close()

    # -- 内部 ----------------------------------------------------------------

    def _has_authority(self, staff_id: str, authority: str) -> bool:
        qualification = self.timeline.roster.get(staff_id)
        return bool(qualification is not None and getattr(qualification, authority))

    def _next_id(self, kind: str, seed: str) -> str:
        number = self._counters.get(kind, 0) + 1
        self._counters[kind] = number
        return f"{kind}-{number:06d}"

    def _register_replay_handlers(self) -> None:
        def on_admitted(event: dict[str, Any]) -> None:
            raw = event["entry"]
            self.queue.admit(
                raw["entry_id"], raw["passenger_id"], raw["flight_no"],
                raw["eligible_kind"], raw["arrived_at"],
            )
            self._bump_counter("entry")

        def on_service_began(event: dict[str, Any]) -> None:
            self.queue.begin_service(event["entry_id"], event["lane_id"], event["at"])

        def on_service_completed(event: dict[str, Any]) -> None:
            self.queue.finish(event["entry_id"], event["at"])

        def on_case_opened(event: dict[str, Any]) -> None:
            raw = event["case"]
            case = self.cases.open_case(
                case_id=raw["case_id"],
                passenger_ref=raw["passenger_ref"],
                flight_no=raw["flight_no"],
                reason_codes=raw["reason_codes"],
                initiator=raw["initiator"],
                at=raw["created_at"],
                escalation_deadline=raw["escalation_deadline"],
            )
            case.stage = raw["stage"]
            case.state = raw["state"]
            case.decisions = [
                __import__(
                    "border_peak_duty.cases", fromlist=["CaseDecision"]
                ).CaseDecision.from_dict(d)
                for d in raw["decisions"]
            ]
            self.queue.divert(event["entry_id"], raw["case_id"])
            self._bump_counter("case")

        def on_case_decided(event: dict[str, Any]) -> None:
            # 决定已在事发时通过回避与资质校验；重放只还原状态流转。
            from .cases import CaseDecision

            case = self.cases.get(event["case_id"])
            decision = event["decision"]
            stage = case.stage
            if decision == "approve":
                case.stage = CaseStage.RELEASE.value
                case.state = CaseState.OPEN.value
                role = "secondary_reviewer"
            elif decision == "hold":
                case.state = CaseState.HELD.value
                role = (
                    "secondary_reviewer" if stage == CaseStage.REVIEW.value
                    else "release_officer"
                )
            elif decision == "reject":
                case.state = CaseState.REJECTED.value
                role = (
                    "secondary_reviewer" if stage == CaseStage.REVIEW.value
                    else "release_officer"
                )
            elif decision == "release":
                case.state = CaseState.RESOLVED.value
                role = "release_officer"
            else:
                role = "unknown"
            case.decisions.append(
                CaseDecision(
                    at=event["at"], stage=stage, decision=decision,
                    reason_code=event["reason_code"], officer_id=event["officer_id"],
                    officer_role=role,
                )
            )

        def on_case_terminal(event: dict[str, Any]) -> None:
            case = self.cases.get(event["case_id"])
            case.state = (
                CaseState.RESOLVED.value if event["outcome"] == "resolved"
                else CaseState.REJECTED.value
            )

        def on_case_escalated(event: dict[str, Any]) -> None:
            self.cases.get(event["case_id"]).state = CaseState.ESCALATED.value

        def on_case_resumed(event: dict[str, Any]) -> None:
            self.cases.get(event["case_id"]).state = CaseState.OPEN.value

        def on_flight_cleared(event: dict[str, Any]) -> None:
            self.queue.abandon_flight(event["flight_no"])

        def on_publish_aborted(event: dict[str, Any]) -> None:
            return None

        def on_lane_registered(event: dict[str, Any]) -> None:
            raw = event["lane"]
            self.timeline.add_lane(
                Lane(
                    lane_id=raw["lane_id"], kind=raw["kind"], device_id=raw["device_id"],
                    base_capacity=raw["base_capacity"], required_position=raw["required_position"],
                )
            )

        def on_staff_registered(event: dict[str, Any]) -> None:
            self.timeline.roster.register(Qualification.from_dict(event["staff"]))

        self.scheduler.register_replay_handler("lane_registered", on_lane_registered)
        self.scheduler.register_replay_handler("staff_registered", on_staff_registered)
        self.scheduler.register_replay_handler("passenger_admitted", on_admitted)
        self.scheduler.register_replay_handler("service_began", on_service_began)
        self.scheduler.register_replay_handler("service_completed", on_service_completed)
        self.scheduler.register_replay_handler("case_opened", on_case_opened)
        self.scheduler.register_replay_handler("case_decided", on_case_decided)
        self.scheduler.register_replay_handler("case_terminal", on_case_terminal)
        self.scheduler.register_replay_handler("case_escalated", on_case_escalated)
        self.scheduler.register_replay_handler("case_resumed", on_case_resumed)
        self.scheduler.register_replay_handler("flight_cleared", on_flight_cleared)
        self.scheduler.register_replay_handler("publish_aborted", on_publish_aborted)

    def _bump_counter(self, kind: str) -> None:
        self._counters[kind] = self._counters.get(kind, 0) + 1
