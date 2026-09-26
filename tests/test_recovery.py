"""重启恢复与值班长接口。"""

import tempfile
import unittest
from pathlib import Path

from support import build_service

from border_peak_duty.models import ForecastVersion
from border_peak_duty.scheduler import InMemoryLaneGate
from border_peak_duty.service import PeakDutyService
from border_peak_duty.timeline import Timeline

BUCKET = "2026-10-01T09:00"
JOURNAL = "journal.jsonl"


def fresh_service(directory: str, gate=None) -> PeakDutyService:
    timeline = Timeline(bucket_minutes=60)
    return PeakDutyService(timeline, journal_path=str(Path(directory) / JOURNAL), gate=gate)


class RestartRecoveryTest(unittest.TestCase):
    def test_queue_leases_and_deadlines_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = InMemoryLaneGate()
            service = build_service(journal_path=str(Path(directory) / JOURNAL), gate=gate)
            service.scheduler.apply_forecast(
                ForecastVersion(2, "2026-10-01T06:00", {BUCKET: 260}, "双节上调")
            )
            service.publish_bucket(BUCKET, "2026-10-01T08:55")
            service.admit_passenger("P200", "CA900", "chinese", "2026-10-01T09:05")
            entry = service.admit_passenger  # noqa
            e = service.queue.by_passenger("P200")
            chain = service.divert(e.entry_id, "S01", ["document_expired"], "2026-10-01T09:06")
            service.review_case(chain["case_id"], "S03", "approve", "document_expired", "2026-10-01T09:10")

            old_lease_ids = {l.lease_id for l in service.scheduler.active_leases()}

            # 服务重启：全新内存对象 + 空通道门，只靠事件日志重建。
            service.close()
            gate2 = InMemoryLaneGate()
            restored = fresh_service(directory, gate=gate2)
            # 先注册通道/人员不是必须的——日志里已有注册事件；但 build 用的
            # 通道与人员都已在原服务落过日志，这里直接 restore 即可。
            restored.restore()

            # 等待队列（含分流状态）恢复
            restored_entry = restored.queue.by_passenger("P200")
            self.assertIsNotNone(restored_entry)
            self.assertEqual(restored_entry.state, "diverted")
            self.assertEqual(restored.queue.waiting_count(), 0)

            # 租约继续有效
            new_lease_ids = {l.lease_id for l in restored.scheduler.active_leases()}
            self.assertEqual(new_lease_ids, old_lease_ids)
            self.assertEqual(len(restored.scheduler.active_leases()), 3)

            # 通道门恢复为开启
            self.assertEqual(gate2.opened, {"C1", "C2", "F1"})

            # 分流案件与决定链恢复，阶段停在 release
            case = restored.cases.get(chain["case_id"])
            self.assertEqual(case.stage, "release")
            self.assertEqual(len(case.decisions), 2)

            # 升级期限继续有效：按日志中的 deadline（09:36）触发升级
            overdue = restored.tick_escalations("2026-10-01T09:37")
            self.assertEqual(len(overdue), 1)
            self.assertEqual(overdue[0]["state"], "escalated")

            # 重启后可继续放行结案
            final_chain = restored.release_case(chain["case_id"], "S04", "2026-10-01T09:40")
            self.assertEqual(final_chain["state"], "resolved")

            restored.close()

    def test_failed_publish_leaves_nothing_to_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = InMemoryLaneGate(fail_for=lambda lanes: True)
            service = build_service(journal_path=str(Path(directory) / JOURNAL), gate=gate)
            service.scheduler.apply_forecast(
                ForecastVersion(2, "2026-10-01T06:00", {BUCKET: 260}, "双节上调")
            )
            try:
                service.publish_bucket(BUCKET, "2026-10-01T08:55")
            except Exception:
                pass
            service.close()

            restored = fresh_service(directory)
            restored.restore()
            self.assertEqual(restored.scheduler.active_leases(), [])
            restored.close()


class DutySnapshotTest(unittest.TestCase):
    def test_snapshot_reports_congestion_capacity_and_masked_chain(self):
        service = build_service()
        service.scheduler.apply_forecast(
            ForecastVersion(2, "2026-10-01T06:00", {BUCKET: 260}, "双节上调")
        )
        service.publish_bucket(BUCKET, "2026-10-01T08:55")
        service.admit_passenger("P300", "CA900", "chinese", "2026-10-01T09:05")
        entry = service.queue.by_passenger("P300")
        service.divert(entry.entry_id, "S01", ["insufficient_materials"], "2026-10-01T09:06")

        snapshot = service.duty_snapshot(BUCKET, "2026-10-01T09:20")
        self.assertEqual(snapshot["demand"]["total"], 260)
        self.assertEqual(snapshot["demand"]["forecast_version"], 2)
        self.assertGreaterEqual(snapshot["capacity"]["open_capacity"], 240)
        self.assertIn("reserve_staff", snapshot["capacity"])
        # 至少有一个分流案件及其脱敏决定链
        self.assertEqual(snapshot["open_cases"], 1)
        chain = snapshot["diversion_chains"][0]
        self.assertEqual(chain["reason_codes"], ["insufficient_materials"])
        actors = [link["actor"] for link in chain["chain"]]
        self.assertTrue(all(set(a) <= set("S0123456789*") for a in actors))
        self.assertNotIn("S01", actors)  # 发起人编号已脱敏

    def test_snapshot_flags_capacity_shortfall_as_congestion_source(self):
        service = build_service()
        service.scheduler.apply_forecast(
            ForecastVersion(2, "2026-10-01T06:00", {BUCKET: 2000}, "极端高峰")
        )
        service.publish_bucket(BUCKET, "2026-10-01T08:55")
        snapshot = service.duty_snapshot(BUCKET, "2026-10-01T09:00")
        codes = {c["code"] for c in snapshot["congestion_sources"]}
        self.assertIn("capacity_shortfall", codes)


if __name__ == "__main__":
    unittest.main()
