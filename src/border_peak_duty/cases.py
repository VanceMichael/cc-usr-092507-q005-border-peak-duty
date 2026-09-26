"""异常分流案件。

设计约束：

- **最小化留痕**：案件只保存假名（HMAC）、原因代码、必要的业务槽位与航班号，
  不保存姓名、证件号等原始身份信息；备注中出现原始身份令牌会被拒绝。
- **三阶段隔离**：普通查验（PRIMARY）只能立案分流；二线复核（SECONDARY）
  只能给出复核结论；最终放行（RELEASE）只能放行或拒绝。各阶段处置人必须
  互不相同，立案人永远不能批准自己的结论。
- **租约绑定**：处置案件必须持有对应岗位的有效租约；租约失效（通道关闭、
  到期、交接）后提交结论将抛出 :class:`SlotClosedError`，案件重新排队。
- **一人一计数**：同一旅客令牌在有未结案件时重复立案，返回原案件，
  不会在两个队列中重复计算。
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .clock import Clock, SystemClock, iso, parse_ts
from .errors import CaseRuleError, SlotClosedError
from .leases import Lease, LeaseRegistry
from .resources import PRIMARY, RELEASE, SECONDARY, ResourcePool

# 原因代码（不保存自由文本形式的证件细节）
DOC_EXPIRED = "DOC_EXPIRED"
DOC_DAMAGED = "DOC_DAMAGED"
DOC_MISMATCH = "DOC_MISMATCH"
MATERIAL_MISSING = "MATERIAL_MISSING"
VISA_INVALID = "VISA_INVALID"
REASON_CODES = frozenset({
    DOC_EXPIRED, DOC_DAMAGED, DOC_MISMATCH, MATERIAL_MISSING, VISA_INVALID,
})

REASON_TEXT = {
    DOC_EXPIRED: "证件已过有效期",
    DOC_DAMAGED: "证件损坏、无法机读",
    DOC_MISMATCH: "证件信息与乘机信息不一致",
    MATERIAL_MISSING: "申报材料不完整",
    VISA_INVALID: "签证或停留凭据失效",
}

# 案件允许持久化的全部字段（白名单即最小化）
CASE_FIELDS = frozenset({
    "case_id", "pseudonym", "reason_code", "slot", "flight_no",
    "created_at", "created_by", "status", "claim", "decisions",
    "requeues", "token_hash",
})


class CaseStatus(str, Enum):
    DIVERTED = "DIVERTED"        # 已分流，等待二线
    IN_REVIEW = "IN_REVIEW"      # 二线正在复核
    RETURNED = "RETURNED"        # 二线退回普通查验（重新排队）
    APPROVED = "APPROVED"        # 二线通过，等待最终放行
    FINALIZING = "FINALIZING"    # 放行岗正在处置
    RELEASED = "RELEASED"        # 最终放行
    DENIED = "DENIED"            # 最终拒绝放行


_TERMINAL = frozenset({CaseStatus.RETURNED, CaseStatus.RELEASED, CaseStatus.DENIED})
_STAGE_POSITION = {
    CaseStatus.IN_REVIEW: SECONDARY,
    CaseStatus.FINALIZING: RELEASE,
}


@dataclass(frozen=True)
class Decision:
    stage: str
    action: str
    decided_by: str
    at: str
    lease_id: str
    note: str = ""


@dataclass
class Claim:
    staff_id: str
    lease_id: str
    position: str
    at: str


@dataclass
class Case:
    case_id: str
    pseudonym: str
    token_hash: str
    reason_code: str
    slot: str
    flight_no: str
    created_at: str
    created_by: str
    status: CaseStatus = CaseStatus.DIVERTED
    claim: Claim | None = None
    decisions: list[Decision] = field(default_factory=list)
    requeues: int = 0


def pseudonymize(secret: str, token: str) -> tuple[str, str]:
    """返回 (对外假名, 内部去重哈希)；原始令牌不落盘。"""
    digest = hmac.new(secret.encode(), token.strip().encode(), hashlib.sha256).hexdigest()
    return f"PAX-{digest[:12]}", digest


class CaseBook:
    def __init__(self, pool: ResourcePool, registry: LeaseRegistry, secret: str,
                 clock: Clock | None = None) -> None:
        self.pool = pool
        self.registry = registry
        self.secret = secret
        self.clock = clock or SystemClock()
        self._cases: dict[str, Case] = {}
        self._open_by_token: dict[str, str] = {}  # token_hash -> case_id（未结案件）

    # ---- 立案 -----------------------------------------------------------

    def open_case(self, *, token: str, reason_code: str, created_by: str, slot: str,
                  flight_no: str = "", note: str = "", now=None) -> tuple[Case, bool]:
        """立案分流。返回 (案件, 是否新建)；重复立案返回原案件。"""
        if reason_code not in REASON_CODES:
            raise CaseRuleError(f"未知分流原因：{reason_code}")
        staff = self.pool.staff.get(created_by)
        if staff is None or not staff.qualified_for(PRIMARY):
            raise CaseRuleError(f"立案人 {created_by} 不具备普通查验资质")
        token = token.strip()
        if not token:
            raise CaseRuleError("旅客令牌为空")
        if note and token in note:
            raise CaseRuleError("备注中不得包含原始证件信息")

        _, token_hash = pseudonymize(self.secret, token)
        existing_id = self._open_by_token.get(token_hash)
        if existing_id is not None:
            return self._cases[existing_id], False

        ts = parse_ts(now) if now else self.clock.now()
        pseudonym, _ = pseudonymize(self.secret, token)
        case = Case(
            case_id=f"case-{uuid.uuid4().hex[:12]}",
            pseudonym=pseudonym,
            token_hash=token_hash,
            reason_code=reason_code,
            slot=slot,
            flight_no=flight_no,
            created_at=iso(ts),
            created_by=created_by,
        )
        case.decisions.append(Decision(
            stage=PRIMARY, action="DIVERT", decided_by=created_by,
            at=iso(ts), lease_id="", note=note[:80],
        ))
        self._cases[case.case_id] = case
        self._open_by_token[token_hash] = case.case_id
        return case, True

    # ---- 阶段处置 --------------------------------------------------------

    def _require_qualification(self, staff_id: str, position: str) -> None:
        staff = self.pool.staff.get(staff_id)
        if staff is None or not staff.qualified_for(position):
            raise CaseRuleError(f"人员 {staff_id} 不具备 {position} 资质")

    def _require_isolation(self, case: Case, staff_id: str) -> None:
        actors = {case.created_by, *(d.decided_by for d in case.decisions)}
        if staff_id in actors:
            raise CaseRuleError(
                f"案件 {case.case_id} 发起人/前序处置人不能再批准本阶段结论"
            )

    def _require_lease(self, case: Case, staff_id: str, position: str,
                       lease: Lease, now) -> None:
        if lease.position != position:
            raise CaseRuleError(f"租约 {lease.lease_id} 岗位不是 {position}")
        if not lease.active_at(now):
            raise SlotClosedError(
                f"{position} 岗位租约 {lease.lease_id} 已失效，案件须重新排队"
            )
        if lease.staff_id != staff_id:
            raise CaseRuleError("租约与处置人不一致")

    def claim(self, case_id: str, staff_id: str, lease: Lease, now=None) -> Case:
        """二线或放行岗接单；通道/租约已关闭则抛 SlotClosedError。"""
        case = self._cases[case_id]
        ts = parse_ts(now) if now else self.clock.now()
        if case.status == CaseStatus.DIVERTED:
            position = SECONDARY
            target = CaseStatus.IN_REVIEW
        elif case.status == CaseStatus.APPROVED:
            position = RELEASE
            target = CaseStatus.FINALIZING
        else:
            raise CaseRuleError(f"案件 {case_id} 当前状态 {case.status} 不接单")
        self._require_qualification(staff_id, position)
        self._require_isolation(case, staff_id)
        self._require_lease(case, staff_id, position, lease, ts)
        case.status = target
        case.claim = Claim(staff_id, lease.lease_id, position, iso(ts))
        return case

    def review(self, case_id: str, *, approved: bool, decided_by: str,
               lease: Lease, note: str = "", now=None) -> Case:
        case = self._cases[case_id]
        ts = parse_ts(now) if now else self.clock.now()
        if case.status != CaseStatus.IN_REVIEW:
            raise CaseRuleError(f"案件 {case_id} 不在二线复核中（{case.status}）")
        self._require_qualification(decided_by, SECONDARY)
        self._require_isolation(case, decided_by)
        self._require_lease(case, decided_by, SECONDARY, lease, ts)
        if case.claim is None or case.claim.staff_id != decided_by:
            raise SlotClosedError("案件未由当前处置人接单，请重新排队接单")
        action = "REVIEW_APPROVE" if approved else "REVIEW_RETURN"
        case.decisions.append(Decision(
            stage=SECONDARY, action=action, decided_by=decided_by,
            at=iso(ts), lease_id=lease.lease_id, note=note[:80],
        ))
        case.status = CaseStatus.APPROVED if approved else CaseStatus.RETURNED
        case.claim = None
        self._close_token_if_terminal(case)
        return case

    def finalize(self, case_id: str, *, release: bool, decided_by: str,
                 lease: Lease, note: str = "", now=None) -> Case:
        case = self._cases[case_id]
        ts = parse_ts(now) if now else self.clock.now()
        if case.status != CaseStatus.FINALIZING:
            raise CaseRuleError(f"案件 {case_id} 不在最终放行环节（{case.status}）")
        self._require_qualification(decided_by, RELEASE)
        self._require_isolation(case, decided_by)
        self._require_lease(case, decided_by, RELEASE, lease, ts)
        if case.claim is None or case.claim.staff_id != decided_by:
            raise SlotClosedError("案件未由当前处置人接单，请重新排队接单")
        action = "FINAL_RELEASE" if release else "FINAL_DENY"
        case.decisions.append(Decision(
            stage=RELEASE, action=action, decided_by=decided_by,
            at=iso(ts), lease_id=lease.lease_id, note=note[:80],
        ))
        case.status = CaseStatus.RELEASED if release else CaseStatus.DENIED
        case.claim = None
        self._close_token_if_terminal(case)
        return case

    def _close_token_if_terminal(self, case: Case) -> None:
        if case.status in _TERMINAL:
            self._open_by_token.pop(case.token_hash, None)

    # ---- 时间推进：阶段关闭后重新排队 ------------------------------------

    def tick(self, now=None) -> dict[str, list[str]]:
        """租约失效后，挂在其上的案件退回等待队列。"""
        ts = parse_ts(now) if now else self.clock.now()
        returned: list[str] = []
        for case in self._cases.values():
            if case.claim is None:
                continue
            lease = self.registry._leases.get(case.claim.lease_id)  # noqa: SLF001
            if lease is None or not lease.active_at(ts):
                case.status = (
                    CaseStatus.DIVERTED if case.status == CaseStatus.IN_REVIEW
                    else CaseStatus.APPROVED
                )
                case.claim = None
                case.requeues += 1
                returned.append(case.case_id)
        return {"requeued": returned}

    # ---- 读取 -----------------------------------------------------------

    def get(self, case_id: str) -> Case:
        return self._cases[case_id]

    def queue_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in CaseStatus}
        for case in self._cases.values():
            counts[case.status.value] += 1
        counts["AWAIT_SECONDARY"] = counts[CaseStatus.DIVERTED.value]
        counts["AWAIT_RELEASE"] = counts[CaseStatus.APPROVED.value]
        return counts

    def awaiting_secondary(self) -> list[Case]:
        return [c for c in self._cases.values() if c.status == CaseStatus.DIVERTED]

    def awaiting_release(self) -> list[Case]:
        return [c for c in self._cases.values() if c.status == CaseStatus.APPROVED]

    def decision_chain(self, case_id: str) -> dict:
        """脱敏决定链：只含假名、原因与各阶段处置（无任何原始身份信息）。"""
        case = self._cases[case_id]
        return {
            "case_id": case.case_id,
            "passenger": case.pseudonym,
            "reason_code": case.reason_code,
            "reason": REASON_TEXT[case.reason_code],
            "flight_no": case.flight_no,
            "slot": case.slot,
            "status": case.status.value,
            "requeues": case.requeues,
            "chain": [
                {
                    "stage": d.stage,
                    "action": d.action,
                    "by": self._staff_pseudonym(d.decided_by),
                    "at": d.at,
                    "note": d.note,
                }
                for d in case.decisions
            ],
        }

    def _staff_pseudonym(self, staff_id: str) -> str:
        digest = hmac.new(
            b"staff", staff_id.encode(), hashlib.sha256
        ).hexdigest()[:8]
        name = self.pool.staff.get(staff_id)
        role = name.team if name and name.team else "OFFICER"
        return f"{role}-{digest}"

    # ---- 快照 -----------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "cases": [
                {
                    "case_id": c.case_id,
                    "pseudonym": c.pseudonym,
                    "token_hash": c.token_hash,
                    "reason_code": c.reason_code,
                    "slot": c.slot,
                    "flight_no": c.flight_no,
                    "created_at": c.created_at,
                    "created_by": c.created_by,
                    "status": c.status.value,
                    "claim": c.claim.__dict__ if c.claim else None,
                    "decisions": [d.__dict__ for d in c.decisions],
                    "requeues": c.requeues,
                }
                for c in self._cases.values()
            ],
        }

    def restore(self, data: dict) -> None:
        self._cases = {}
        self._open_by_token = {}
        for item in data.get("cases", []):
            claim_data = item.get("claim")
            case = Case(
                case_id=item["case_id"],
                pseudonym=item["pseudonym"],
                token_hash=item["token_hash"],
                reason_code=item["reason_code"],
                slot=item["slot"],
                flight_no=item.get("flight_no", ""),
                created_at=item["created_at"],
                created_by=item["created_by"],
                status=CaseStatus(item["status"]),
                claim=Claim(**claim_data) if claim_data else None,
                decisions=[Decision(**d) for d in item.get("decisions", [])],
                requeues=item.get("requeues", 0),
            )
            self._cases[case.case_id] = case
            if case.status not in _TERMINAL:
                self._open_by_token[case.token_hash] = case.case_id
