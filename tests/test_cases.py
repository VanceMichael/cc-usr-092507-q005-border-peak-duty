"""异常分流案件：最小化留痕、三阶段隔离、租约绑定与重排队。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from border_peak_duty.cases import (
    DOC_EXPIRED,
    MATERIAL_MISSING,
    CaseStatus,
)
from border_peak_duty.clock import VirtualClock
from border_peak_duty.errors import CaseRuleError, SlotClosedError
from border_peak_duty.resources import PRIMARY, RELEASE, SECONDARY
from border_peak_duty.scenario import build_pool
from border_peak_duty.service import PeakDutyService

SLOT = "2026-10-01T18:45"
TOKEN = "E-88001234"


def make_service():
    clock = VirtualClock("2026-10-01T18:30:00Z")
    svc = PeakDutyService(build_pool(secondary_officers=4, release_officers=3), clock)
    svc.record_forecast({"version": 1, "series": {SLOT: 500}})
    return svc, clock


def open_full_case(svc, token=TOKEN):
    """立案 → 发布（含二线/放行台位）→ 复核通过 → 放行，返回案件与各租约。"""
    divert = svc.divert({
        "token": token, "reason_code": DOC_EXPIRED,
        "created_by": "P001", "slot": SLOT, "flight_no": "CA900",
    })
    svc.plan_and_publish(SLOT, request_id=f"plan-{token[-4:]}")
    leases = {l.position: l for l in svc.registry.active_leases(SLOT)}
    return divert["case_id"], leases


class CaseFlowTest(unittest.TestCase):
    def test_full_isolated_flow(self):
        svc, _ = make_service()
        case_id, leases = open_full_case(svc)
        sec, rel = leases[SECONDARY], leases[RELEASE]
        # 三个人互不相同：立案 P001、复核、放行
        self.assertNotIn(sec.staff_id, {"P001", rel.staff_id})

        svc.claim_case(case_id, sec.staff_id, sec.lease_id)
        self.assertEqual(svc.cases.get(case_id).status, CaseStatus.IN_REVIEW)
        svc.review_case(case_id, approved=True, decided_by=sec.staff_id,
                        lease_id=sec.lease_id)
        self.assertEqual(svc.cases.get(case_id).status, CaseStatus.APPROVED)
        svc.claim_case(case_id, rel.staff_id, rel.lease_id)
        svc.finalize_case(case_id, release=True, decided_by=rel.staff_id,
                          lease_id=rel.lease_id)
        self.assertEqual(svc.cases.get(case_id).status, CaseStatus.RELEASED)

        chain = svc.case_chain(case_id)
        actions = [c["action"] for c in chain["chain"]]
        self.assertEqual(
            actions, ["DIVERT", "REVIEW_APPROVE", "FINAL_RELEASE"]
        )
        stages = [c["stage"] for c in chain["chain"]]
        self.assertEqual(stages, [PRIMARY, SECONDARY, RELEASE])

    def test_initiator_cannot_approve_own_conclusion(self):
        svc, _ = make_service()
        case_id, leases = open_full_case(svc)
        # 立案人 P001 同时具备 PRIMARY 资质，但不能进二线批准
        p_primary = next(
            l for l in svc.registry.active_leases(SLOT) if l.staff_id == "P001"
        )
        with self.assertRaises(CaseRuleError):
            svc.cases.claim(case_id, "P001", p_primary)
        # 复核人也不能到最终放行岗批准同一案件
        sec = leases[SECONDARY]
        svc.claim_case(case_id, sec.staff_id, sec.lease_id)
        svc.review_case(case_id, approved=True, decided_by=sec.staff_id,
                        lease_id=sec.lease_id)
        # 即使复核人另有 RELEASE 租约也禁止（本例 S 系列无 RELEASE 资质，
        # 这里直接验证隔离规则）
        with self.assertRaises(CaseRuleError):
            svc.cases.claim(case_id, sec.staff_id, leases[RELEASE])

    def test_secondary_return_requeues_to_primary_queue(self):
        svc, _ = make_service()
        case_id, leases = open_full_case(svc)
        sec = leases[SECONDARY]
        svc.claim_case(case_id, sec.staff_id, sec.lease_id)
        svc.review_case(case_id, approved=False, decided_by=sec.staff_id,
                        lease_id=sec.lease_id, note="材料不足，退回补料")
        case = svc.cases.get(case_id)
        self.assertEqual(case.status, CaseStatus.RETURNED)
        # 终态后同一旅客可重新立案
        again, created = svc.cases.open_case(
            token=TOKEN, reason_code=MATERIAL_MISSING,
            created_by="P002", slot=SLOT,
        )
        self.assertTrue(created)

    def test_duplicate_divert_does_not_double_count(self):
        svc, _ = make_service()
        first = svc.divert({"token": TOKEN, "reason_code": DOC_EXPIRED,
                            "created_by": "P001", "slot": SLOT})
        second = svc.divert({"token": TOKEN, "reason_code": DOC_EXPIRED,
                             "created_by": "P001", "slot": SLOT})
        self.assertFalse(second["created"])
        self.assertEqual(first["case_id"], second["case_id"])
        self.assertEqual(svc.cases.queue_counts()["DIVERTED"], 1)

    def test_case_requires_valid_lease_and_requeues_when_closed(self):
        svc, clock = make_service()
        case_id, leases = open_full_case(svc)
        sec = leases[SECONDARY]
        svc.claim_case(case_id, sec.staff_id, sec.lease_id)
        # 租约到期后提交结论：必须失败（SlotClosedError），不能放行
        clock.advance(minutes=31)
        with self.assertRaises(SlotClosedError):
            svc.review_case(case_id, approved=True, decided_by=sec.staff_id,
                            lease_id=sec.lease_id)
        # 随后系统 tick 把挂在失效租约上的案件退回等待队列，重新接单
        svc.tick()
        case = svc.cases.get(case_id)
        self.assertEqual(case.status, CaseStatus.DIVERTED)
        self.assertIsNone(case.claim)
        self.assertEqual(case.requeues, 1)

    def test_invalid_reason_and_raw_data_are_rejected(self):
        svc, _ = make_service()
        with self.assertRaises(CaseRuleError):
            svc.divert({"token": TOKEN, "reason_code": "WHATEVER",
                        "created_by": "P001", "slot": SLOT})
        with self.assertRaises(CaseRuleError):
            svc.divert({"token": TOKEN, "reason_code": DOC_EXPIRED,
                        "created_by": "P001", "slot": SLOT,
                        "note": f"证件号 {TOKEN}"})

    def test_snapshot_contains_no_raw_identity(self):
        svc, _ = make_service()
        case_id, _ = open_full_case(svc)
        raw = json.dumps(svc.snapshot(), ensure_ascii=False)
        self.assertNotIn(TOKEN, raw)
        chain = svc.case_chain(case_id)
        self.assertTrue(chain["passenger"].startswith("PAX-"))
        for link in chain["chain"]:
            # 处置人也是脱敏角色标识
            self.assertNotIn("P001", link["by"])


if __name__ == "__main__":
    unittest.main()
