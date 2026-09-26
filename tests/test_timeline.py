"""乱序航班动态、版本化预测、设备状态与不重复计数。"""

import unittest

from support import build_service

from border_peak_duty.models import DeviceState, DeviceStatus, FlightEvent, FlightStatus, ForecastVersion
from border_peak_duty.timeline import FlightTimeline


def flight(event_id, revision, status, eta="2026-10-01T09:00", pax=100, **kw):
    delayed = status == FlightStatus.DELAYED.value
    return FlightEvent(
        flight_no="CA100",
        event_id=event_id,
        revision=revision,
        status=status,
        occurred_at=kw.pop("occurred_at", eta),
        scheduled_arrival="2026-10-01T09:00" if delayed else kw.pop("scheduled_arrival", eta),
        revised_arrival=eta if delayed else None,
        pax=pax,
        **kw,
    )


class FlightTimelineTest(unittest.TestCase):
    def setUp(self):
        self.tl = FlightTimeline()

    def test_out_of_order_events_take_highest_revision(self):
        self.assertEqual(self.tl.apply(flight("e2", 2, "delayed", "2026-10-01T10:30")), "new")
        self.assertEqual(self.tl.apply(flight("e1", 1, "scheduled")), "late")
        effective = self.tl.effective("CA100")
        self.assertEqual(effective.event_id, "e2")
        self.assertEqual(effective.revised_arrival, "2026-10-01T10:30")

    def test_duplicate_delivery_is_idempotent(self):
        self.tl.apply(flight("e1", 1, "scheduled"))
        event = flight("e1", 1, "scheduled")
        self.assertEqual(self.tl.apply(event), "duplicate")
        self.assertEqual(len(self.tl._events["CA100"]), 1)

    def test_departed_flight_never_reoccupies_timeline(self):
        self.tl.apply(flight("e1", 1, "scheduled"))
        self.tl.apply(flight("e2", 2, "arrived", "2026-10-01T09:05", occurred_at="2026-10-01T09:05"))
        departed = flight("e3", 3, "departed", "2026-10-01T11:00", occurred_at="2026-10-01T11:00", pax=0)
        self.assertEqual(self.tl.apply(departed), "new")
        self.assertTrue(self.tl.is_terminal("CA100"))
        # 乱序迟到的“再次延误”，哪怕 revision 更高也不能让航班复活。
        late = flight("e4", 4, "delayed", "2026-10-01T12:30", occurred_at="2026-10-01T08:00")
        self.assertEqual(self.tl.apply(late), "late")
        self.assertEqual(self.tl.effective("CA100").status, FlightStatus.DEPARTED.value)
        self.assertEqual(self.tl.active_flights(), [])
        self.assertEqual(self.tl.projected_for_bucket("2026-10-01T12:00", "2026-10-01T13:00"), [])

    def test_arrived_flight_leaves_demand_to_waiting_queue(self):
        self.tl.apply(flight("e1", 1, "scheduled"))
        self.assertEqual(
            [f.flight_no for f in self.tl.projected_for_bucket("2026-10-01T09:00", "2026-10-01T10:00")],
            ["CA100"],
        )
        self.tl.apply(flight("e2", 2, "arrived", occurred_at="2026-10-01T09:02"))
        self.assertEqual(
            self.tl.projected_for_bucket("2026-10-01T09:00", "2026-10-01T10:00"), []
        )


class ForecastAndDemandTest(unittest.TestCase):
    def test_forecast_versions_and_no_double_count(self):
        service = build_service()
        service.timeline.flights.apply(flight("plan", 1, "scheduled", pax=100))

        service.scheduler.apply_forecast(
            ForecastVersion(1, "2026-09-30T20:00", {"2026-10-01T09:00": 100}, "初版")
        )
        d1 = service.timeline.demand("2026-10-01T09:00")
        # 预测覆盖该时段：以预测为准，航班只作成因，不双算。
        self.assertEqual(d1.total, 100)
        self.assertEqual(d1.forecast_pax, 100)
        self.assertEqual(d1.flight_pax, 100)

        # 双节预测突然上调为新版本。
        outcome = service.scheduler.apply_forecast(
            ForecastVersion(2, "2026-10-01T06:00", {"2026-10-01T09:00": 260}, "双节上调")
        )
        self.assertEqual(outcome, "new")
        self.assertEqual(service.timeline.demand("2026-10-01T09:00").total, 260)

        # 同一版本重发只更新对应版本，不产生新版本。
        again = service.scheduler.apply_forecast(
            ForecastVersion(2, "2026-10-01T06:05", {"2026-10-01T09:00": 260}, "双节上调重发")
        )
        self.assertEqual(again, "duplicate")
        self.assertEqual(service.timeline.forecasts.latest().version, 2)

    def test_uncovered_bucket_falls_back_to_flight_totals(self):
        service = build_service()
        service.timeline.flights.apply(
            flight("plan", 1, "scheduled", eta="2026-10-01T10:20", pax=50)
        )
        service.scheduler.apply_forecast(
            ForecastVersion(1, "2026-09-30T20:00", {"2026-10-01T09:00": 10}, "初版")
        )
        demand = service.timeline.demand("2026-10-01T10:00")
        self.assertFalse(demand.forecast_covered)
        self.assertEqual(demand.total, 50)


class DeviceTimelineTest(unittest.TestCase):
    def test_stale_device_update_ignored_and_unavailable_has_zero_capacity(self):
        service = build_service()
        service.scheduler.apply_device(
            DeviceState("DEV-C1", DeviceStatus.MAINTENANCE.value, 1.0, "2026-10-01T09:10", "检修")
        )
        stale = service.scheduler.apply_device(
            DeviceState("DEV-C1", DeviceStatus.ONLINE.value, 1.0, "2026-10-01T09:00", "迟到状态")
        )
        self.assertEqual(stale, "late")
        lane = service.timeline.lanes["C1"]
        capacity, reason = service.timeline.devices.effective_capacity(lane)
        self.assertEqual((capacity, reason), (0, "device_maintenance"))

    def test_degraded_device_reduces_capacity(self):
        service = build_service()
        service.scheduler.apply_device(
            DeviceState("DEV-C1", DeviceStatus.DEGRADED.value, 0.5, "2026-10-01T09:10", "读证偏慢")
        )
        capacity, reason = service.timeline.devices.effective_capacity(service.timeline.lanes["C1"])
        self.assertEqual((capacity, reason), (40, "device_degraded"))


if __name__ == "__main__":
    unittest.main()
