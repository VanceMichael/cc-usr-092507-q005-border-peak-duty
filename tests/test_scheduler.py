"""规划产出、岗位租约、原子发布、交接班与设备恢复。"""

import unittest

from support import build_service

from border_peak_duty.models import (
    DeviceState,
    DeviceStatus,
    FlightEvent,
    FlightStatus,
    ForecastVersion,
    PublishConflict,
    QualificationError,
)
from border_peak_duty.scheduler import InMemoryLaneGate

BUCKET = "2026-10-01T09:00"
NOW = "2026-10-01T08:55"


def forecast(service, pax=260):
    service.scheduler.apply_forecast(
        ForecastVersion(2, "2026-10-01T06:00", {BUCKET: pax}, "双节上调")
    )


class PlanningTest(unittest.TestCase):
    def test_plan_produces_open_lanes_reserve_and_triggers(self):
        service = build_service()
        forecast(service, pax=260)
        plan = service.publish_bucket(BUCKET, NOW)
        self.assertTrue(plan["open_lanes"])
        # 60/30/10 分摊：中文 156 人需要两条 C 通道（80*2>=156），
        # 外籍 78 人需要一条 F 通道。
        open_ids = {a["lane_id"] for a in plan["open_lanes"]}
        self.assertEqual(open_ids, {"C1", "C2", "F1"})
        # S1 与未排人员进入备用，且给出触发原因。
        self.assertIn("S1", plan["reserve_lanes"])
        self.assertTrue(plan["reserve_staff"])
        codes = {t["code"] for t in plan["triggers"]}
        self.assertIn("forecast_demand", codes)

    def test_publish_is_idempotent_within_bucket(self):
        service = build_service()
        forecast(service)
        first = service.publish_bucket(BUCKET, NOW)
        second = service.publish_bucket(BUCKET, "2026-10-01T08:57")
        self.assertEqual(
            {a["lane_id"] for a in first["open_lanes"]},
            {a["lane_id"] for a in second["open_lanes"]},
        )
        active = [l for l in service.scheduler.leases.values() if l.status == "active"]
        self.assertEqual(len(active), 3)

    def test_gate_failure_leaves_no_half_state(self):
        gate = InMemoryLaneGate(fail_for=lambda lanes: "C2" in lanes)
        service = build_service(gate=gate)
        forecast(service)
        with self.assertRaises(PublishConflict):
            service.publish_bucket(BUCKET, NOW)
        # 没有任何人员占用残留，也没有通道被启用。
        self.assertEqual(service.scheduler.active_leases(), [])
        self.assertEqual(gate.opened, set())
        # 发布失败后修复联动，同方案可以重试成功。
        gate._fail_for = None
        plan = service.publish_bucket(BUCKET, NOW)
        self.assertEqual({a["lane_id"] for a in plan["open_lanes"]}, {"C1", "C2", "F1"})
        self.assertEqual(gate.opened, {"C1", "C2", "F1"})

    def test_staff_constrained_lane_is_not_counted_as_spare(self):
        service = build_service()
        # 仅留一名能上中文通道的人员；S02/S03 移出花名册不现实，改为制造
        # 极高外籍需求，使 F 通道人力不足时验证备用口径。
        forecast(service, pax=260)
        service.scheduler.apply_device(
            DeviceState("DEV-C2", DeviceStatus.MAINTENANCE.value, 1.0, "2026-10-01T08:30", "检修")
        )
        plan = service.publish_bucket(BUCKET, NOW)
        self.assertNotIn("C2", [a["lane_id"] for a in plan["open_lanes"]])
        self.assertNotIn("C2", plan["reserve_lanes"])
        codes = {t["code"] for t in plan["triggers"]}
        self.assertIn("device_unavailable", codes)
        self.assertIn("capacity_shortfall", codes)

    def test_handover_and_device_recovery_share_lease_rules(self):
        service = build_service()
        forecast(service)
        service.publish_bucket(BUCKET, NOW)
        lease = next(
            l for l in service.scheduler.active_leases() if l.lane_id == "C1"
        )
        self.assertEqual(lease.staff_id, "S01")
        # 无相应资质的人员不能接班（S05 只有特殊通道资质）。
        with self.assertRaises(QualificationError):
            service.scheduler.handover(lease.lease_id, "S05", "2026-10-01T09:30")
        new_lease = service.scheduler.handover(lease.lease_id, "S03", "2026-10-01T09:30")
        self.assertEqual(new_lease.staff_id, "S03")
        old = service.scheduler.leases[lease.lease_id]
        self.assertEqual(old.status, "released")
        # 同一人不能在重叠时段持两个租约：S03 已接 C1，不能再接 C2。
        with self.assertRaises(PublishConflict):
            service.scheduler.handover(
                next(l.lease_id for l in service.scheduler.active_leases() if l.lane_id == "C2"),
                "S03",
                "2026-10-01T09:31",
            )

        # 设备检修立即收回租约；恢复后下一时段可重新承租。
        service.scheduler.apply_device(
            DeviceState("DEV-F1", DeviceStatus.MAINTENANCE.value, 1.0, "2026-10-01T09:40", "读证故障")
        )
        self.assertFalse(
            any(l.lane_id == "F1" for l in service.scheduler.active_leases())
        )
        service.scheduler.device_recovered(
            DeviceState("DEV-F1", DeviceStatus.ONLINE.value, 1.0, "2026-10-01T10:05", "修复")
        )
        service.scheduler.apply_forecast(
            ForecastVersion(2, "2026-10-01T06:00", {"2026-10-01T10:00": 260}, "双节上调")
        )
        plan = service.publish_bucket("2026-10-01T10:00", "2026-10-01T10:06")
        self.assertIn("F1", [a["lane_id"] for a in plan["open_lanes"]])


if __name__ == "__main__":
    unittest.main()
