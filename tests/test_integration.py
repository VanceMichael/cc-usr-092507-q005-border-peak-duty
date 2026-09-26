"""端到端：双节高峰一日保障的关键路径，以及发布环节两类失败的回滚。"""

import tempfile
import unittest
from pathlib import Path

from support import build_service

from border_peak_duty.models import (
    DeviceState,
    DeviceStatus,
    FlightEvent,
    FlightStatus,
    ForecastVersion,
    PublishConflict,
)
from border_peak_duty.scheduler import InMemoryLaneGate
from border_peak_duty.service import PeakDutyService
from border_peak_duty.timeline import Timeline

BUCKET = "2026-10-01T09:00"
JOURNAL = "journal.jsonl"


def fresh_service(directory: str, gate=None) -> PeakDutyService:
    timeline = Timeline(bucket_minutes=60)
    return PeakDutyService(timeline, journal_path=str(Path(directory) / JOURNAL), gate=gate)


class EndToEndPeakDayTest(unittest.TestCase):
    def test_full_peak_day_then_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = InMemoryLaneGate()
            service = build_service(journal_path=str(Path(directory) / JOURNAL), gate=gate)

            # 1) 预测先出初版，双节临近突然上调。
            service.scheduler.apply_forecast(
                ForecastVersion(1, "2026-09-30T20:00", {BUCKET: 120}, "初版")
            )
            service.scheduler.apply_forecast(
                ForecastVersion(2, "2026-10-01T06:00", {BUCKET: 260}, "双节上调")
            )

            # 2) 航班计划与乱序延误动态（延误事件先到，计划事件后到）。
            service.scheduler.apply_flight_event(
                FlightEvent("CA100", "delay", 2, FlightStatus.DELAYED.value,
                            "2026-10-01T08:40", "2026-10-01T09:00", pax=180,
                            revised_arrival="2026-10-01T09:40", delay_minutes=40)
            )
            service.scheduler.apply_flight_event(
                FlightEvent("CA100", "plan", 1, FlightStatus.SCHEDULED.value,
                            "2026-10-01T08:00", "2026-10-01T09:00", pax=180)
            )
            effective = service.timeline.flights.effective("CA100")
            self.assertEqual(effective.status, FlightStatus.DELAYED.value)

            # 3) 系统产出 09:00 时段方案：开放通道、岗位组合、备用与触发原因。
            plan = service.publish_bucket(BUCKET, "2026-10-01T08:55")
            self.assertEqual({a["lane_id"] for a in plan["open_lanes"]}, {"C1", "C2", "F1"})
            self.assertTrue(all(a["staff_id"] for a in plan["open_lanes"]))
            self.assertIn("S1", plan["reserve_lanes"])
            codes = {t["code"] for t in plan["triggers"]}
            self.assertIn("forecast_demand", codes)

            # 4) 设备检修叠加：C2 通道立即收回租约并关闭。
            service.scheduler.apply_device(
                DeviceState("DEV-C2", DeviceStatus.MAINTENANCE.value, 1.0,
                            "2026-10-01T09:10", "读证设备检修")
            )
            self.assertFalse(
                any(l.lane_id == "C2" for l in service.scheduler.active_leases())
            )
            self.assertNotIn("C2", gate.opened)

            # 5) 旅客陆续受理：正常查验放行；证件失效的走分流案件。
            e1 = service.admit_passenger("P501", "CA100", "chinese", "2026-10-01T09:12")
            service.begin_service(e1.entry_id, "C1", "2026-10-01T09:13")
            service.complete_service(e1.entry_id, "2026-10-01T09:15")

            e2 = service.admit_passenger("P502", "CA100", "chinese", "2026-10-01T09:13")
            service.begin_service(e2.entry_id, "C1", "2026-10-01T09:14")
            chain = service.divert(
                e2.entry_id, "S01", ["document_expired"], "2026-10-01T09:16"
            )
            case_id = chain["case_id"]
            # 发起人不能自批；由有复核权的另一人复核，再有放行权者放行。
            service.review_case(case_id, "S03", "approve", "document_expired", "2026-10-01T09:20")
            service.release_case(case_id, "S04", "2026-10-01T09:25")
            self.assertEqual(
                service.queue.by_passenger("P502").state, "diverted"
            )

            # 6) 交接班遵循同一租约。
            c1_lease = next(l for l in service.scheduler.active_leases() if l.lane_id == "C1")
            service.scheduler.handover(c1_lease.lease_id, "S03", "2026-10-01T09:30")

            # 7) 航班离场：终态锁定，迟到的延误动态不能让其复活。
            service.close_flight("CA100", "2026-10-01T11:10")
            outcome = service.scheduler.apply_flight_event(
                FlightEvent("CA100", "zombie-delay", 99, FlightStatus.DELAYED.value,
                            "2026-10-01T11:20", "2026-10-01T11:10", pax=180,
                            revised_arrival="2026-10-01T12:30")
            )
            self.assertEqual(outcome, "late")
            self.assertEqual(
                service.timeline.flights.effective("CA100").status,
                FlightStatus.DEPARTED.value,
            )

            # 8) 值班接口：拥堵源与剩余能力齐备，分流链脱敏。
            snapshot = service.duty_snapshot(BUCKET, "2026-10-01T09:40")
            self.assertTrue(
                {c["code"] for c in snapshot["congestion_sources"]}
                & {"device_unavailable", "capacity_shortfall"}
            )
            self.assertIn("spare_capacity", snapshot["capacity"])
            # 已结案分流不占 open_cases；决定链中无真实人员/旅客标识。
            self.assertEqual(snapshot["open_cases"], 0)

            # 9) 重启恢复：租约（含交接后的新租约）与已完成队列继续有效。
            lease_keys_before = {
                (l.lease_id, l.staff_id, l.lane_id, l.status)
                for l in service.scheduler.leases.values()
            }
            service.close()
            restored = fresh_service(directory, gate=InMemoryLaneGate())
            restored.restore()
            lease_keys_after = {
                (l.lease_id, l.staff_id, l.lane_id, l.status)
                for l in restored.scheduler.leases.values()
            }
            self.assertEqual(lease_keys_after, lease_keys_before)
            self.assertEqual(restored.queue.by_passenger("P501").state, "done")
            self.assertEqual(restored.queue.by_passenger("P502").state, "diverted")
            self.assertEqual(
                restored.cases.get(case_id).state, "resolved"
            )
            restored.close()


class PublishFailureRollbackTest(unittest.TestCase):
    def test_journal_failure_after_gate_open_rolls_back(self):
        service = build_service()
        service.scheduler.apply_forecast(
            ForecastVersion(2, "2026-10-01T06:00", {BUCKET: 260}, "双节上调")
        )
        real_append = service.journal.append

        def fail_on_publish(events):
            if any(e.get("type") == "lease_acquired" for e in events):
                raise OSError("磁盘不可写")
            return real_append(events)

        service.journal.append = fail_on_publish
        gate = service.scheduler.gate
        with self.assertRaises(OSError):
            service.publish_bucket(BUCKET, "2026-10-01T08:55")
        # 不留“人已占用、通道已开”的状态。
        self.assertEqual(service.scheduler.active_leases(), [])
        self.assertEqual(gate.opened, set())


if __name__ == "__main__":
    unittest.main()
