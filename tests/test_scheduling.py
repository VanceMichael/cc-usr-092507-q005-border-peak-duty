"""岗位租约、调度产出与原子发布测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from border_peak_duty.clock import VirtualClock
from border_peak_duty.errors import LeaseConflictError, PublishError
from border_peak_duty.leases import (
    DISPATCH,
    HANDOVER,
    RECOVERY,
    LeaseState,
    WaitState,
)
from border_peak_duty.resources import DeviceStatus, PRIMARY, SECONDARY, RELEASE
from border_peak_duty.scenario import build_pool
from border_peak_duty.service import PeakDutyService
from border_peak_duty.scheduler import (
    TRIG_FORECAST,
    TRIG_FLIGHT_DELAY,
    TRIG_DEVICE_DEGRADED,
    TRIG_QUEUE_ESCALATE,
)

SLOT = "2026-10-01T18:45"


def make_service():
    clock = VirtualClock("2026-10-01T18:30:00Z")
    svc = PeakDutyService(build_pool(), clock)
    svc.record_flight_event({
        "flight_no": "CA900", "version": 1, "kind": "SCHEDULED",
        "occurred_at": "2026-10-01T08:00:00Z", "direction": "ARR", "pax": 360,
        "start": "2026-10-01T18:30:00Z", "end": "2026-10-01T19:15:00Z",
    })
    svc.record_flight_event({
        "flight_no": "CA900", "version": 2, "kind": "DELAYED",
        "occurred_at": "2026-10-01T17:00:00Z",
        "start": "2026-10-01T18:45:00Z", "end": "2026-10-01T19:30:00Z",
    })
    svc.record_forecast({"version": 1, "series": {SLOT: 500}})
    return svc, clock


class SchedulePublishTest(unittest.TestCase):
    def test_plan_opens_channels_with_positions_and_triggers(self):
        svc, _ = make_service()
        plan = svc.plan_and_publish(SLOT, request_id="plan-1")
        positions = plan["positions"]
        self.assertGreater(len(positions[PRIMARY]["opened"]), 0)
        self.assertGreaterEqual(len(positions[RELEASE]["opened"]), 1)
        # 需求与能力均给出，且能力覆盖需求
        self.assertGreaterEqual(plan["capacity_total"], plan["demand_total"] - 1)
        self.assertGreater(plan["reserve_capacity_pax"], 0)
        self.assertIn(TRIG_FORECAST, plan["triggers"])
        self.assertIn(TRIG_FLIGHT_DELAY, plan["triggers"])
        for item in plan["opened"]:
            self.assertTrue(item["lease_id"])

    def test_publish_is_atomic_failure_leaves_no_half_state(self):
        svc, _ = make_service()

        class FailingPublisher:
            def publish(self, plan):
                raise RuntimeError("广播通道离线")

        before = len(svc.registry.active_leases(SLOT))
        plan = svc.scheduler.plan_slot(SLOT)
        with self.assertRaises(PublishError):
            svc.scheduler.publish(plan, FailingPublisher(), request_id="boom")
        # 没有留下"人已占用、通道未启用"
        self.assertEqual(len(svc.registry.active_leases(SLOT)), before)
        self.assertEqual(svc.scheduler.current_openings(SLOT), [])

    def test_republish_keeps_baseline_leases(self):
        svc, _ = make_service()
        first = svc.plan_and_publish(SLOT, request_id="a")
        second = svc.plan_and_publish(SLOT, request_id="b")
        first_ids = {o["lease_id"] for o in first["opened"]}
        second_ids = {o["lease_id"] for o in second["opened"]}
        self.assertTrue(first_ids.issubset(second_ids))

    def test_staff_cannot_hold_two_positions_same_slot(self):
        svc, _ = make_service()
        plan = svc.plan_and_publish(SLOT)
        lease = svc.registry.active_leases(SLOT)[0]
        with self.assertRaises(LeaseConflictError):
            svc.registry.acquire(
                staff_id=lease.staff_id, channel_id="S01",
                position=SECONDARY, slot=SLOT, ttl_minutes=30,
            )

    def test_unqualified_staff_cannot_man_position(self):
        svc, _ = make_service()
        with self.assertRaises(LeaseConflictError):
            svc.registry.acquire(
                staff_id="P001", channel_id="S01",
                position=SECONDARY, slot=SLOT, ttl_minutes=30,
            )

    def test_idempotent_request_id_cannot_double_occupy(self):
        svc, _ = make_service()
        svc.plan_and_publish(SLOT, request_id="once")
        # 同样 request_id 的新槽发布：通道级幂等键不会误伤，
        # 但显式用相同键直接获取必须被拒
        plan = svc.scheduler.plan_slot("2026-10-01T19:00")
        with self.assertRaises(PublishError):
            svc.scheduler.publish(plan, svc.publisher, request_id="once")

    def test_device_down_invalidates_leases_and_recovery_reopens(self):
        svc, _ = make_service()
        svc.plan_and_publish(SLOT, request_id="d")
        lease = next(l for l in svc.registry.active_leases(SLOT)
                     if l.position == PRIMARY)
        channel_id = lease.channel_id
        gate = next(d.device_id for d in svc.pool.channels[channel_id].devices.values()
                    if d.kind == "GATE")
        result = svc.set_device(gate, DeviceStatus.DOWN)
        self.assertIn(lease.lease_id, result["invalidated_leases"])
        self.assertEqual(lease.state, LeaseState.INVALIDATED)
        self.assertIsNone(svc.registry.staff_lease(lease.staff_id, SLOT))
        board = svc.board(SLOT)
        self.assertTrue(all(o["channel_id"] != channel_id for o in board["open_channels"]))

        svc.set_device(gate, DeviceStatus.UP)
        recovered = svc.recover_channel(slot=SLOT)
        reopened = {o["channel_id"] for o in recovered["opened"]}
        self.assertIn(channel_id, reopened)
        # 恢复补位同样受租约约束：新持证人有资质
        new_holder = svc.registry.lease_for(channel_id, SLOT)
        self.assertIsNotNone(new_holder)
        self.assertEqual(new_holder.reason, DISPATCH)

    def test_handover_is_atomic_and_rebuilds_combo(self):
        svc, _ = make_service()
        svc.plan_and_publish(SLOT, request_id="h")
        lease = next(l for l in svc.registry.active_leases(SLOT))
        target = "S002" if lease.staff_id != "S002" else "S003"
        out = svc.handover(lease.lease_id, target)
        self.assertEqual(lease.state, LeaseState.RELEASED)
        new_lease = svc.registry._leases[out["new_lease_id"]]
        self.assertEqual(new_lease.state, LeaseState.HELD)
        self.assertEqual(new_lease.staff_id, target)
        self.assertEqual(new_lease.reason, HANDOVER)
        self.assertEqual(new_lease.channel_id, lease.channel_id)
        # 通道仍开放、岗位数不减少
        board = svc.board(SLOT)
        self.assertTrue(any(o["channel_id"] == lease.channel_id for o in board["open_channels"]))

    def test_failed_handover_keeps_old_lease(self):
        svc, _ = make_service()
        svc.plan_and_publish(SLOT)
        primary_lease = next(l for l in svc.registry.active_leases(SLOT)
                             if l.position == PRIMARY)
        # R 系列仅有 RELEASE 资质，不能接 PRIMARY
        with self.assertRaises(LeaseConflictError):
            svc.handover(primary_lease.lease_id, "R001")
        self.assertEqual(primary_lease.state, LeaseState.HELD)

    def test_waiting_queue_escalation_and_expiry(self):
        svc, clock = make_service()
        entry = svc.registry.enqueue(
            position=PRIMARY, slot=SLOT, escalate_after=10, ttl_minutes=30
        )
        clock.advance(minutes=11)
        events = svc.tick()
        self.assertEqual([e["wait_id"] for e in events["escalations"]], [entry.wait_id])
        self.assertEqual(svc.registry.waiting()[0].state, WaitState.ESCALATED)
        clock.advance(minutes=20)
        events = svc.tick()
        self.assertIn(entry.wait_id, events["expired_waits"])
        self.assertEqual(svc.registry.waiting(), [])

    def test_escalated_wait_surfaces_trigger_on_board(self):
        svc, clock = make_service()
        svc.plan_and_publish(SLOT)
        svc.registry.enqueue(position=SECONDARY, slot=SLOT,
                             escalate_after=5, ttl_minutes=30)
        clock.advance(minutes=6)
        svc.tick()
        plan = svc.scheduler.plan_slot(SLOT)
        self.assertIn(TRIG_QUEUE_ESCALATE, plan.triggers)

    def test_degraded_device_reduces_capacity(self):
        svc, _ = make_service()
        gate = next(d.device_id for d in svc.pool.channels["A01"].devices.values()
                    if d.kind == "GATE")
        svc.set_device(gate, DeviceStatus.DEGRADED, efficiency=0.5)
        plan = svc.scheduler.plan_slot(SLOT)
        self.assertIn(TRIG_DEVICE_DEGRADED, plan.triggers)
        rate = svc.pool.channels["A01"].effective_rate()
        self.assertAlmostEqual(rate, 60.0)


if __name__ == "__main__":
    unittest.main()
