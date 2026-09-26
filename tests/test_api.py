"""值班接口 HTTP 冒烟：看板、拥堵源、脱敏决定链与命令路由。"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from border_peak_duty import HostedApp, JsonSnapshotStore, VirtualClock, build_pool
from border_peak_duty.api import serve

SLOT = "2026-10-01T18:45"


class ApiSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = VirtualClock("2026-10-01T18:30:00Z")
        self.app = HostedApp(
            JsonSnapshotStore(Path(self.tmp.name) / "snap.json"),
            build_pool(), self.clock,
        )
        self.httpd = serve(self.app, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.tmp.cleanup()

    def _post(self, path: str, payload: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_health_and_board_after_rush_plan(self):
        status, _ = self._get("/health")
        self.assertEqual(status, 200)

        self._post("/events/flight", {
            "flight_no": "CA900", "version": 1, "kind": "SCHEDULED",
            "occurred_at": "2026-10-01T08:00:00Z", "direction": "ARR", "pax": 360,
            "start": "2026-10-01T18:30:00Z", "end": "2026-10-01T19:15:00Z",
        })
        self._post("/forecasts", {"version": 1, "series": {SLOT: 500}})
        status, plan = self._post("/plans/publish", {"slot": SLOT, "request_id": "http-1"})
        self.assertEqual(status, 200)
        self.assertGreater(len(plan["opened"]), 0)

        status, board = self._get(f"/board?slot={SLOT}")
        self.assertEqual(status, 200)
        self.assertIn("congestion_sources", board)
        self.assertIn("remaining_capacity", board)
        self.assertIn("open_channels", board)
        self.assertIn("case_queues", board)
        self.assertEqual(len(board["open_channels"]), len(plan["opened"]))

    def test_divert_chain_endpoint_is_masked(self):
        self._post("/forecasts", {"version": 1, "series": {SLOT: 300}})
        self._post("/plans/publish", {"slot": SLOT, "request_id": "http-2"})
        _, divert = self._post("/cases/divert", {
            "token": "E-SECRET-99", "reason_code": "DOC_EXPIRED",
            "created_by": "P001", "slot": SLOT, "flight_no": "CA900",
        })
        self._post("/plans/publish", {"slot": SLOT, "request_id": "http-2b"})
        cid = divert["case_id"]

        _, board = self._get(f"/board?slot={SLOT}")
        self.assertEqual(board["case_queues"]["DIVERTED"], 1)

        _, chain = self._get(f"/cases/{cid}/chain")
        self.assertTrue(chain["passenger"].startswith("PAX-"))
        self.assertNotIn("E-SECRET-99", json.dumps(chain))
        self.assertNotIn("P001", json.dumps(chain))
        self.assertEqual(chain["reason"], "证件已过有效期")

    def test_stale_flight_event_returns_conflict(self):
        self._post("/events/flight", {
            "flight_no": "CA900", "version": 1, "kind": "SCHEDULED",
            "occurred_at": "2026-10-01T08:00:00Z", "direction": "ARR", "pax": 100,
            "start": "2026-10-01T18:30:00Z", "end": "2026-10-01T19:00:00Z",
        })
        status, body = self._post("/events/flight", {
            "flight_no": "CA900", "version": 1, "kind": "DELAYED",
            "occurred_at": "2026-10-01T17:00:00Z",
            "start": "2026-10-01T19:30:00Z",
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "StaleEventError")

    def test_unknown_route_404(self):
        try:
            self._get("/nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
