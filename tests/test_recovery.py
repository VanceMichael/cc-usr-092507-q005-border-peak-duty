"""重启恢复：等待队列、租约与升级期限在服务重启后继续有效。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from border_peak_duty import HostedApp, JsonSnapshotStore, VirtualClock, build_pool
from border_peak_duty.cases import DOC_EXPIRED, CaseStatus
from border_peak_duty.leases import WaitState

SLOT = "2026-10-01T18:45"


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = VirtualClock("2026-10-01T18:30:00Z")
        self.app = HostedApp(
            JsonSnapshotStore(Path(self.tmp.name) / "snap.json"),
            build_pool(), self.clock,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _build_rush(self):
        self.app.command("record_flight_event", {
            "flight_no": "CA900", "version": 1, "kind": "SCHEDULED",
            "occurred_at": "2026-10-01T08:00:00Z", "direction": "ARR", "pax": 360,
            "start": "2026-10-01T18:30:00Z", "end": "2026-10-01T19:15:00Z",
        })
        self.app.command("record_forecast", {"version": 1, "series": {SLOT: 500}})
        plan = self.app.command("plan_and_publish", {"slot": SLOT, "request_id": "p"})
        divert = self.app.command("divert", {
            "token": "E-88001234", "reason_code": DOC_EXPIRED,
            "created_by": "P001", "slot": SLOT, "flight_no": "CA900",
        })
        self.app.command("plan_and_publish", {"slot": SLOT, "request_id": "p2"})
        return plan["plan_id"], divert["case_id"]

    def test_leases_wait_queue_and_deadlines_survive_restart(self):
        plan_id, case_id = self._build_rush()
        leases_before = {
            (l.channel_id, l.staff_id, l.position)
            for l in self.app.service.registry.active_leases(SLOT)
        }
        # 挂一个等待请求，升级期限落在重启之后
        wait = self.app.service.registry.enqueue(
            position="SECONDARY", slot=SLOT,
            escalate_after=10, ttl_minutes=40,
        )
        self.app.save()  # 直接操作领域对象后显式落盘

        self.app.restart()

        leases_after = {
            (l.channel_id, l.staff_id, l.position)
            for l in self.app.service.registry.active_leases(SLOT)
        }
        self.assertEqual(leases_before, leases_after)

        # 等待队列与升级期限保留：推进到升级点后确实升级
        waiting = self.app.service.registry.waiting()
        self.assertEqual([w.wait_id for w in waiting], [wait.wait_id])
        self.assertEqual(waiting[0].state, WaitState.WAITING)
        self.clock.advance(minutes=11)
        events = self.app.command("tick", {})
        self.assertEqual([e["wait_id"] for e in events["escalations"]], [wait.wait_id])

        # 开放通道与看板一致
        board = self.app.query("board", {"slot": SLOT})
        self.assertEqual(len(board["open_channels"]), len(leases_after))
        self.assertEqual(board["plan"]["plan_id"], plan_id)

        # 案件继续可处理
        self.assertEqual(self.app.service.cases.get(case_id).status, CaseStatus.DIVERTED)
        sec = next(l for l in self.app.service.registry.active_leases(SLOT)
                   if l.position == "SECONDARY")
        self.app.command("claim_case", {
            "case_id": case_id, "staff_id": sec.staff_id, "lease_id": sec.lease_id,
        })

    def test_flight_terminal_state_survives_restart(self):
        self.app.command("record_flight_event", {
            "flight_no": "CA900", "version": 1, "kind": "SCHEDULED",
            "occurred_at": "2026-10-01T08:00:00Z", "direction": "ARR", "pax": 360,
            "start": "2026-10-01T18:30:00Z", "end": "2026-10-01T19:15:00Z",
        })
        self.app.command("record_flight_event", {
            "flight_no": "CA900", "version": 2, "kind": "RELEASED",
            "occurred_at": "2026-10-01T19:05:00Z",
        })
        self.app.restart()
        self.assertEqual(self.app.service.flights.get("CA900").status, "RELEASED")
        from border_peak_duty.errors import StaleEventError
        with self.assertRaises(StaleEventError):
            self.app.command("record_flight_event", {
                "flight_no": "CA900", "version": 3, "kind": "DELAYED",
                "occurred_at": "2026-10-01T19:10:00Z",
                "start": "2026-10-01T20:00:00Z", "end": "2026-10-01T20:30:00Z",
            })

    def test_snapshot_file_is_atomic_single_file(self):
        self._build_rush()
        path = Path(self.tmp.name) / "snap.json"
        self.assertTrue(path.exists())
        leftovers = list(Path(self.tmp.name).glob("*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
