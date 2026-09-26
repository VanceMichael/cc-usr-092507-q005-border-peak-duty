"""航班动态乱序/幂等/终态 与 客流预测版本测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from border_peak_duty.clock import VirtualClock
from border_peak_duty.errors import ForecastValidationError, StaleEventError
from border_peak_duty.service import PeakDutyService
from border_peak_duty.scenario import build_pool


def service():
    return PeakDutyService(build_pool(), VirtualClock("2026-10-01T12:00:00Z"))


SCHEDULED = {
    "flight_no": "CA123", "version": 1, "kind": "SCHEDULED",
    "occurred_at": "2026-10-01T08:00:00Z", "direction": "ARR", "pax": 300,
    "start": "2026-10-01T18:30:00Z", "end": "2026-10-01T19:15:00Z",
}


class FlightTimelineTest(unittest.TestCase):
    def test_rejects_dynamic_without_schedule_baseline(self):
        svc = service()
        with self.assertRaisesRegex(StaleEventError, "缺少计划基线"):
            svc.record_flight_event({**SCHEDULED, "version": 5, "kind": "DELAYED",
                                     "start": "2026-10-01T20:00:00Z"})

    def test_out_of_order_low_version_is_rejected(self):
        svc = service()
        svc.record_flight_event(SCHEDULED)
        svc.record_flight_event({**SCHEDULED, "version": 3, "kind": "DELAYED",
                                 "start": "2026-10-01T19:00:00Z",
                                 "end": "2026-10-01T19:45:00Z"})
        # v2 迟到（乱序）：不得覆盖 v3 的投影
        with self.assertRaisesRegex(StaleEventError, "迟到的 v2"):
            svc.record_flight_event({**SCHEDULED, "version": 2, "kind": "DELAYED",
                                     "start": "2026-10-01T18:45:00Z"})
        view = svc.flights.get("CA123")
        self.assertEqual(view.version, 3)
        self.assertEqual(view.planned_start, "2026-10-01T19:00:00Z")

    def test_same_version_resend_only_updates_that_version(self):
        svc = service()
        svc.record_flight_event(SCHEDULED)
        payload = {**SCHEDULED, "version": 2, "kind": "DELAYED",
                   "start": "2026-10-01T19:00:00Z", "end": "2026-10-01T19:45:00Z",
                   "event_id": "CA123-DELAY-v2", "note": "流控"}
        first = svc.record_flight_event(payload)
        second = svc.record_flight_event({**payload, "note": "流控修订"})
        self.assertEqual(first["outcome"], "applied")
        self.assertEqual(second["outcome"], "duplicate_updated")
        # 只有一条动态记录，且备注被更新
        events = svc.flights.events("CA123")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1].note, "流控修订")

    def test_released_flight_cannot_reoccupy_resources(self):
        svc = service()
        svc.record_flight_event(SCHEDULED)
        svc.record_flight_event({**SCHEDULED, "version": 2, "kind": "ACTIVE",
                                 "occurred_at": "2026-10-01T18:35:00Z"})
        svc.record_flight_event({**SCHEDULED, "version": 3, "kind": "RELEASED",
                                 "occurred_at": "2026-10-01T19:10:00Z"})
        self.assertTrue(svc.flights.get("CA123").released)
        with self.assertRaisesRegex(StaleEventError, "离场终态"):
            svc.record_flight_event({**SCHEDULED, "version": 4, "kind": "DELAYED",
                                     "occurred_at": "2026-10-01T19:20:00Z",
                                     "start": "2026-10-01T20:00:00Z",
                                     "end": "2026-10-01T20:30:00Z", "pax": 300})
        # 同版本同终态的重发可以幂等更新
        again = svc.record_flight_event({**SCHEDULED, "version": 3, "kind": "RELEASED",
                                         "occurred_at": "2026-10-01T19:10:00Z",
                                         "note": "确认离场"})
        self.assertEqual(again["outcome"], "duplicate_updated")

    def test_cancelled_flight_contributes_zero_demand(self):
        svc = service()
        svc.record_flight_event(SCHEDULED)
        svc.record_flight_event({**SCHEDULED, "version": 2, "kind": "CANCELLED",
                                 "occurred_at": "2026-10-01T16:00:00Z"})
        self.assertEqual(svc.flights.get("CA123").window_slots(), [])
        demand = svc.timeline.slot_demand("2026-10-01T18:30")
        self.assertEqual(demand.flight_pax, 0)

    def test_delay_moves_pax_into_later_slots(self):
        svc = service()
        svc.record_flight_event(SCHEDULED)
        before = svc.timeline.slot_demand("2026-10-01T18:30").flight_pax
        svc.record_flight_event({**SCHEDULED, "version": 2, "kind": "DELAYED",
                                 "start": "2026-10-01T19:30:00Z",
                                 "end": "2026-10-01T20:15:00Z"})
        self.assertEqual(svc.flights.get("CA123").delay_minutes, 60)
        self.assertEqual(svc.timeline.slot_demand("2026-10-01T18:30").flight_pax, 0)
        after = svc.timeline.slot_demand("2026-10-01T19:30").flight_pax
        self.assertGreater(after, 0)
        self.assertGreater(before, 0)


class ForecastTest(unittest.TestCase):
    def test_highest_version_is_used_and_resend_updates(self):
        svc = service()
        svc.record_forecast({"version": 1, "series": {"2026-10-01T18:30": 100}})
        svc.record_forecast({"version": 2, "series": {"2026-10-01T18:30": 260}})
        r = svc.record_forecast({"version": 2, "series": {"2026-10-01T18:30": 280}})
        self.assertEqual(r["outcome"], "duplicate_updated")
        self.assertEqual(svc.forecasts.current().version, 2)
        self.assertEqual(svc.timeline.slot_demand("2026-10-01T18:30").forecast_pax, 280)

    def test_invalid_forecast_rejected(self):
        svc = service()
        with self.assertRaises(ForecastValidationError):
            svc.record_forecast({"version": 0, "series": {"2026-10-01T18:30": 1}})
        with self.assertRaises(ForecastValidationError):
            svc.record_forecast({"version": 1, "series": {}})
        with self.assertRaises(ForecastValidationError):
            svc.record_forecast({"version": 1, "series": {"2026-10-01T18:30": -1}})


if __name__ == "__main__":
    unittest.main()
