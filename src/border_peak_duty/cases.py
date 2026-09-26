"""等待队列与分流案件。

两条计数边界：

- 已受理旅客进入等待队列后，其航班不再出现在时间线的到场需求中，因此同一位
  旅客不可能同时被预测需求和等待队列计数；队列内按旅客编号去重，也不可能在
  两个队列中重复计算。
- 分流案件只保留必要信息：旅客用不透明代号、原因只用标准代码，不存姓名、
  证件号或自由文本材料。普通查验、二线复核、最终放行三阶段严格隔离，案件
  发起人不能批准自己提出的分流结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .models import CaseStage, CaseState, QueueState, SeparationError

# 允许进入分流案件的最小原因代码白名单——其它字段一律不收。
DIVERSION_REASONS = frozenset(
    {
        "document_expired",       # 证件失效
        "document_missing",       # 证件缺失
        "insufficient_materials",  # 材料不足
        "visa_issue",             # 签证/签注问题
        "identity_mismatch",      # 人证不一致
    }
)


# ---------------------------------------------------------------------------
# 等待队列
# ---------------------------------------------------------------------------


@dataclass
class QueueEntry:
    entry_id: str
    passenger_id: str
    flight_no: str
    eligible_kind: str
    arrived_at: str
    state: str = QueueState.WAITING.value
    lane_id: str | None = None
    service_started_at: str | None = None
    finished_at: str | None = None
    case_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "passenger_id": self.passenger_id,
            "flight_no": self.flight_no,
            "eligible_kind": self.eligible_kind,
            "arrived_at": self.arrived_at,
            "state": self.state,
            "lane_id": self.lane_id,
            "service_started_at": self.service_started_at,
            "finished_at": self.finished_at,
            "case_id": self.case_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QueueEntry":
        return cls(**value)


class WaitingQueue:
    """已受理旅客的单一事实来源。"""

    def __init__(self) -> None:
        self._entries: dict[str, QueueEntry] = {}
        # passenger_id -> entry_id，保证一位旅客全程只入队一次
        self._passenger_index: dict[str, str] = {}

    def admit(
        self,
        entry_id: str,
        passenger_id: str,
        flight_no: str,
        eligible_kind: str,
        arrived_at: str,
    ) -> QueueEntry:
        if passenger_id in self._passenger_index:
            existing = self._entries[self._passenger_index[passenger_id]]
            raise SeparationError(
                f"旅客 {passenger_id} 已受理（编号 {existing.entry_id}），不得重复入队"
            )
        entry = QueueEntry(
            entry_id=entry_id,
            passenger_id=passenger_id,
            flight_no=flight_no,
            eligible_kind=eligible_kind,
            arrived_at=arrived_at,
        )
        self._entries[entry_id] = entry
        self._passenger_index[passenger_id] = entry_id
        return entry

    def get(self, entry_id: str) -> QueueEntry:
        return self._entries[entry_id]

    def by_passenger(self, passenger_id: str) -> QueueEntry | None:
        entry_id = self._passenger_index.get(passenger_id)
        return self._entries[entry_id] if entry_id else None

    def waiting(self, kind: str | None = None) -> list[QueueEntry]:
        result = [
            e
            for e in self._entries.values()
            if e.state == QueueState.WAITING.value
            and (kind is None or e.eligible_kind == kind)
        ]
        result.sort(key=lambda e: (e.arrived_at, e.entry_id))
        return result

    def waiting_count(self, kind: str | None = None) -> int:
        return len(self.waiting(kind))

    def begin_service(self, entry_id: str, lane_id: str, at: str) -> QueueEntry:
        entry = self._entries[entry_id]
        if entry.state != QueueState.WAITING.value:
            raise SeparationError(f"旅客不在等待状态，无法开始查验：{entry.state}")
        entry.state = QueueState.SERVING.value
        entry.lane_id = lane_id
        entry.service_started_at = at
        return entry

    def finish(self, entry_id: str, at: str) -> QueueEntry:
        entry = self._entries[entry_id]
        if entry.state != QueueState.SERVING.value:
            raise SeparationError(f"旅客不在查验中，无法放行：{entry.state}")
        entry.state = QueueState.DONE.value
        entry.finished_at = at
        return entry

    def divert(self, entry_id: str, case_id: str) -> QueueEntry:
        entry = self._entries[entry_id]
        if entry.state in (QueueState.DONE.value, QueueState.DIVERTED.value):
            raise SeparationError(f"旅客已结案，不能再次分流：{entry.state}")
        entry.state = QueueState.DIVERTED.value
        entry.case_id = case_id
        return entry

    def abandon_flight(self, flight_no: str) -> list[QueueEntry]:
        """航班离场时清扫：仍在等待（从未到场）的旅客标记为放弃。

        正在查验或已结案的旅客不动。被清扫的旅客此后不再计入任何队列。
        """
        changed = []
        for entry in self._entries.values():
            if (
                entry.flight_no == flight_no
                and entry.state == QueueState.WAITING.value
            ):
                entry.state = QueueState.ABANDONED.value
                changed.append(entry)
        return changed

    def snapshot(self, now: str) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        oldest: str | None = None
        for entry in self.waiting():
            by_kind[entry.eligible_kind] = by_kind.get(entry.eligible_kind, 0) + 1
            if oldest is None or entry.arrived_at < oldest:
                oldest = entry.arrived_at
        serving = sum(
            1 for e in self._entries.values() if e.state == QueueState.SERVING.value
        )
        return {
            "waiting_total": self.waiting_count(),
            "waiting_by_kind": by_kind,
            "serving": serving,
            "oldest_waiting_since": oldest,
            "oldest_wait_seconds": _seconds_between(oldest, now),
        }

    def to_dict(self) -> dict[str, Any]:
        return {eid: e.to_dict() for eid, e in self._entries.items()}

    def load_dict(self, value: dict[str, Any]) -> None:
        for eid, raw in value.items():
            entry = QueueEntry.from_dict(raw)
            self._entries[eid] = entry
            self._passenger_index[entry.passenger_id] = eid


# ---------------------------------------------------------------------------
# 分流案件
# ---------------------------------------------------------------------------


@dataclass
class CaseDecision:
    at: str
    stage: str
    decision: str   # divert / approve / hold / reject / release
    reason_code: str
    officer_id: str
    officer_role: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "stage": self.stage,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "officer_id": self.officer_id,
            "officer_role": self.officer_role,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CaseDecision":
        return cls(**value)


@dataclass
class DiversionCase:
    case_id: str
    passenger_ref: str           # 不透明旅客代号，非姓名/证件号
    flight_no: str
    reason_codes: list[str]
    created_at: str
    escalation_deadline: str
    initiator: str
    stage: str = CaseStage.REVIEW.value  # 发起即在普通查验完成后，待二线复核
    state: str = CaseState.OPEN.value
    decisions: list[CaseDecision] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passenger_ref": self.passenger_ref,
            "flight_no": self.flight_no,
            "reason_codes": list(self.reason_codes),
            "created_at": self.created_at,
            "escalation_deadline": self.escalation_deadline,
            "initiator": self.initiator,
            "stage": self.stage,
            "state": self.state,
            "decisions": [d.to_dict() for d in self.decisions],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiversionCase":
        value = dict(value)
        value["decisions"] = [CaseDecision.from_dict(d) for d in value["decisions"]]
        return cls(**value)


def mask_ref(ref: str) -> str:
    """脱敏：保留前两位与后一位，其余打码。"""
    if len(ref) <= 3:
        return ref[0] + "**"
    return ref[:2] + "*" * (len(ref) - 3) + ref[-1]


class CaseStore:
    """分流案件库：阶段隔离 + 发起人回避 + 最小信息。"""

    def __init__(self, has_secondary: Callable[[str], bool], has_release: Callable[[str], bool]):
        self._cases: dict[str, DiversionCase] = {}
        self._has_secondary = has_secondary
        self._has_release = has_release

    def open_case(
        self,
        case_id: str,
        passenger_ref: str,
        flight_no: str,
        reason_codes: list[str],
        initiator: str,
        at: str,
        escalation_deadline: str,
    ) -> DiversionCase:
        if case_id in self._cases:
            raise SeparationError(f"分流案件已存在：{case_id}")
        reasons = frozenset(reason_codes)
        if not reasons or not reasons <= DIVERSION_REASONS:
            raise SeparationError(
                f"分流原因必须取自标准代码白名单：{sorted(reasons - DIVERSION_REASONS)}"
            )
        case = DiversionCase(
            case_id=case_id,
            passenger_ref=passenger_ref,
            flight_no=flight_no,
            reason_codes=sorted(reasons),
            created_at=at,
            escalation_deadline=escalation_deadline,
            initiator=initiator,
            decisions=[
                CaseDecision(
                    at=at,
                    stage=CaseStage.SCREEN.value,
                    decision="divert",
                    reason_code=sorted(reasons)[0],
                    officer_id=initiator,
                    officer_role="screening_officer",
                )
            ],
        )
        self._cases[case_id] = case
        return case

    def get(self, case_id: str) -> DiversionCase:
        return self._cases[case_id]

    def remove(self, case_id: str) -> None:
        """撤销尚未落日志的新建案件（仅用于日志写入失败的补偿）。"""
        self._cases.pop(case_id, None)

    def decide(
        self,
        case_id: str,
        officer_id: str,
        decision: str,
        reason_code: str,
        at: str,
    ) -> DiversionCase:
        case = self._cases[case_id]
        if case.state in (CaseState.RESOLVED.value, CaseState.REJECTED.value):
            raise SeparationError("案件已终结，不能再作决定")
        if case.state == CaseState.HELD.value:
            raise SeparationError("案件挂起中，补齐材料并恢复后才能继续")
        if case.state == CaseState.ESCALATED.value:
            # 升级不阻断处置，但只允许更高权限阶段的操作，这里不额外限制。
            pass
        if reason_code not in DIVERSION_REASONS and decision != "release":
            raise SeparationError("决定原因必须取自标准代码白名单")

        stage = CaseStage(case.stage)
        if stage is CaseStage.REVIEW:
            # 二线复核：需要复核资质，且发起人不能批准自己的结论。
            if officer_id == case.initiator:
                raise SeparationError("案件发起人不能批准自己提出的分流结论")
            if not self._has_secondary(officer_id):
                raise SeparationError("该人员不具备二线复核权限")
            role = "secondary_reviewer"
            if decision == "approve":
                case.stage = CaseStage.RELEASE.value
                case.state = CaseState.OPEN.value
            elif decision == "hold":
                case.state = CaseState.HELD.value
            elif decision == "reject":
                case.state = CaseState.REJECTED.value
                case.decisions.append(self._record(case, at, stage, decision, reason_code, officer_id, role))
                return case
            else:
                raise SeparationError("二线复核阶段只接受 approve/hold/reject")
        elif stage is CaseStage.RELEASE:
            if officer_id == case.initiator:
                raise SeparationError("案件发起人不能批准自己提出的分流结论")
            if not self._has_release(officer_id):
                raise SeparationError("该人员不具备最终放行权限")
            role = "release_officer"
            if decision == "release":
                case.state = CaseState.RESOLVED.value
            elif decision == "reject":
                case.state = CaseState.REJECTED.value
            elif decision == "hold":
                case.state = CaseState.HELD.value
            else:
                raise SeparationError("放行阶段只接受 release/reject/hold")
        else:
            raise SeparationError(f"案件处于不可操作阶段：{case.stage}")

        case.decisions.append(
            self._record(case, at, stage, decision, reason_code, officer_id, role)
        )
        return case

    @staticmethod
    def _record(
        case: DiversionCase,
        at: str,
        stage: CaseStage,
        decision: str,
        reason_code: str,
        officer_id: str,
        role: str,
    ) -> CaseDecision:
        return CaseDecision(
            at=at, stage=stage.value, decision=decision,
            reason_code=reason_code, officer_id=officer_id, officer_role=role,
        )

    def resume(self, case_id: str, at: str) -> DiversionCase:
        """补材料完成后，挂起案件回到当前阶段继续流转。"""
        case = self._cases[case_id]
        if case.state != CaseState.HELD.value:
            raise SeparationError("只有挂起中的案件可以恢复")
        case.state = CaseState.OPEN.value
        case.decisions.append(
            CaseDecision(
                at=at, stage=case.stage, decision="resume",
                reason_code="insufficient_materials",
                officer_id=case.initiator, officer_role="system_resume",
            )
        )
        return case

    def escalate_overdue(self, now: str) -> list[DiversionCase]:
        """超过升级期限仍未终结的案件标记升级。重启后期限继续有效。"""
        changed = []
        for case in self._cases.values():
            if (
                case.state == CaseState.OPEN.value
                and now > case.escalation_deadline
            ):
                case.state = CaseState.ESCALATED.value
                changed.append(case)
        return changed

    def masked_chain(self, case_id: str) -> dict[str, Any]:
        """脱敏决定链：只向值班接口暴露必要信息。"""
        case = self._cases[case_id]
        return {
            "case_id": case.case_id,
            "passenger_ref": mask_ref(case.passenger_ref),
            "flight_no": case.flight_no,
            "reason_codes": list(case.reason_codes),
            "stage": case.stage,
            "state": case.state,
            "created_at": case.created_at,
            "escalation_deadline": case.escalation_deadline,
            "chain": [
                {
                    "at": d.at,
                    "stage": d.stage,
                    "decision": d.decision,
                    "reason_code": d.reason_code,
                    "actor_role": d.officer_role,
                    "actor": mask_ref(d.officer_id),
                }
                for d in case.decisions
            ],
        }

    def open_cases(self) -> list[DiversionCase]:
        return [
            c
            for c in self._cases.values()
            if c.state in (CaseState.OPEN.value, CaseState.HELD.value, CaseState.ESCALATED.value)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {cid: c.to_dict() for cid, c in self._cases.items()}

    def load_dict(self, value: dict[str, Any]) -> None:
        for cid, raw in value.items():
            self._cases[cid] = DiversionCase.from_dict(raw)


def _seconds_between(start: str | None, end: str) -> int | None:
    if start is None:
        return None
    from datetime import datetime

    return int(
        (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    )
