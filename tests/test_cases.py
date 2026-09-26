"""等待队列不重不漏、分流案件最小信息/三阶段隔离/发起人回避/升级期限。"""

import unittest

from support import build_service

from border_peak_duty.cases import DIVERSION_REASONS
from border_peak_duty.models import SeparationError

NOW = "2026-10-01T09:05"


class WaitingQueueTest(unittest.TestCase):
    def test_passenger_counted_once_and_flight_departure_cleans_up(self):
        service = build_service()
        service.admit_passenger("P001", "CA100", "chinese", NOW)
        # 同一旅客不能在两个队列/两次受理中重复计算。
        with self.assertRaisesRegex(SeparationError, "不得重复入队"):
            service.admit_passenger("P001", "CA100", "chinese", NOW)
        service.admit_passenger("P002", "CA100", "foreigner", NOW)
        self.assertEqual(service.queue.waiting_count(), 2)
        self.assertEqual(service.queue.waiting_count("chinese"), 1)

        result = service.close_flight("CA100", "2026-10-01T11:30")
        self.assertEqual(result["abandoned"], 2)
        self.assertEqual(service.queue.waiting_count(), 0)
        # 已离场航班不能再受理旅客。
        with self.assertRaisesRegex(SeparationError, "已离场"):
            service.admit_passenger("P003", "CA100", "chinese", "2026-10-01T11:31")

    def test_admitted_passenger_not_double_counted_in_forecast(self):
        service = build_service()
        from border_peak_duty.models import FlightEvent, FlightStatus

        service.timeline.flights.apply(
            FlightEvent(
                flight_no="CA100", event_id="plan", revision=1,
                status=FlightStatus.SCHEDULED.value, occurred_at="2026-10-01T08:00",
                scheduled_arrival="2026-10-01T09:20", pax=100,
            )
        )
        service.admit_passenger("P001", "CA100", "chinese", NOW)
        # 落地后航班退出预测需求，旅客只存在于等待队列。
        service.timeline.flights.apply(
            FlightEvent(
                flight_no="CA100", event_id="arr", revision=2,
                status=FlightStatus.ARRIVED.value, occurred_at=NOW,
                scheduled_arrival="2026-10-01T09:20", pax=100,
            )
        )
        demand = service.timeline.demand("2026-10-01T09:00")
        self.assertEqual(demand.flight_pax, 0)
        self.assertEqual(service.queue.waiting_count(), 1)


class DiversionCaseTest(unittest.TestCase):
    def _open_case(self, service=None, reasons=("document_expired",)):
        service = service or build_service()
        entry = service.admit_passenger("P100", "CA900", "chinese", NOW)
        chain = service.divert(entry.entry_id, "S01", list(reasons), NOW)
        return service, chain

    def test_case_keeps_only_minimum_information(self):
        _, chain = self._open_case()
        self.assertEqual(chain["passenger_ref"], "PA*****0")
        self.assertIn("document_expired", chain["reason_codes"])
        # 决定链不包含姓名、证件号等字段。
        serialized = repr(chain)
        for forbidden in ("name", "passport", "id_number", "P100"):
            self.assertNotIn(forbidden, serialized)
        for link in chain["chain"]:
            self.assertIn("actor_role", link)
            self.assertIn("actor", link)

    def test_reason_must_be_standard_code(self):
        service = build_service()
        entry = service.admit_passenger("P101", "CA900", "chinese", NOW)
        with self.assertRaisesRegex(SeparationError, "白名单"):
            service.divert(entry.entry_id, "S01", ["looks_suspicious_free_text"], NOW)
        self.assertEqual(set(DIVERSION_REASONS) >= {"document_expired", "insufficient_materials"}, True)

    def test_stage_isolation_and_initiator_recusal(self):
        service, chain = self._open_case()
        case_id = chain["case_id"]
        # 发起人 S01 不能在二线复核批准自己的结论。
        with self.assertRaisesRegex(SeparationError, "发起人"):
            service.review_case(case_id, "S01", "approve", "document_expired", NOW)
        # 无复核权的人员不能操作二线复核。
        with self.assertRaisesRegex(SeparationError, "二线复核权限"):
            service.review_case(case_id, "S02", "approve", "document_expired", NOW)
        # 具备复核权的 S03（非发起人）批准，进入最终放行阶段。
        chain = service.review_case(case_id, "S03", "approve", "document_expired", NOW)
        self.assertEqual(chain["stage"], "release")
        # S03 无最终放行权。
        with self.assertRaisesRegex(SeparationError, "最终放行权限"):
            service.release_case(case_id, "S03", NOW)
        # 发起人同样不能做最终放行。
        with self.assertRaisesRegex(SeparationError, "发起人"):
            service.release_case(case_id, "S01", NOW)
        chain = service.release_case(case_id, "S04", NOW)
        self.assertEqual(chain["state"], "resolved")
        # 三阶段各有不同角色的决定，相互隔离留痕。
        roles = [link["actor_role"] for link in chain["chain"]]
        self.assertEqual(
            roles, ["screening_officer", "secondary_reviewer", "release_officer"]
        )

    def test_insufficient_materials_hold_then_resume_flow(self):
        service, chain = self._open_case(reasons=("insufficient_materials",))
        case_id = chain["case_id"]
        chain = service.review_case(case_id, "S03", "hold", "insufficient_materials", NOW)
        self.assertEqual(chain["state"], "held")
        # 挂起期间不能重复作复核决定。
        with self.assertRaisesRegex(SeparationError, "已终结|挂起|阶段"):
            service.review_case(case_id, "S03", "approve", "insufficient_materials", NOW)
        chain = service.resume_case(case_id, "2026-10-01T09:40")
        self.assertEqual(chain["state"], "open")
        self.assertEqual(chain["stage"], "review")

    def test_escalation_deadline_fires(self):
        service, chain = self._open_case()
        self.assertEqual(chain["escalation_deadline"], "2026-10-01T09:35")
        self.assertEqual(service.tick_escalations("2026-10-01T09:34"), [])
        escalated = service.tick_escalations("2026-10-01T09:36")
        self.assertEqual(len(escalated), 1)
        self.assertEqual(escalated[0]["state"], "escalated")
        # 已升级的不会重复升级。
        self.assertEqual(service.tick_escalations("2026-10-01T09:50"), [])


if __name__ == "__main__":
    unittest.main()
