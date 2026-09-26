"""HTTP 薄适配层：只做路由与 JSON 编解码，业务全部在 HostedApp。

路由（均为 JSON）：

- ``POST /events/flight``      录入航班动态
- ``POST /forecasts``          录入客流预测版本
- ``POST /devices``            更新设备状态
- ``POST /plans/publish``      规划并发布某时间槽
- ``POST /handovers``          岗位交接
- ``POST /recovery``           设备恢复后补位
- ``POST /cases/divert``       立案分流
- ``POST /cases/claim``        二线/放行接单
- ``POST /cases/review``       二线结论
- ``POST ``/cases/finalize``   最终放行
- ``POST /tick``               推进时间（租约到期、升级、案件重排队）
- ``GET  /board?slot=...``     值班看板（拥堵源/剩余能力/队列）
- ``GET  /cases/{id}/chain``   脱敏决定链
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .app import HostedApp
from .errors import PeakDutyError

COMMAND_ROUTES = {
    "/events/flight": "record_flight_event",
    "/forecasts": "record_forecast",
    "/devices": "set_device",
    "/plans/publish": "plan_and_publish",
    "/handovers": "handover",
    "/recovery": "recover_channel",
    "/cases/divert": "divert",
    "/cases/claim": "claim_case",
    "/cases/review": "review_case",
    "/cases/finalize": "finalize_case",
    "/tick": "tick",
}


def create_handler(app: HostedApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PeakDuty/0.1"

        def log_message(self, fmt, *args):  # 静默，避免污染测试输出
            return

        def _send(self, status: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            path = urlparse(self.path).path
            command = COMMAND_ROUTES.get(path)
            if command is None:
                self._send(404, {"error": "not_found", "path": path})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                self._send(400, {"error": "bad_json", "detail": str(exc)})
                return
            try:
                result = app.command(command, payload)
            except PeakDutyError as exc:
                self._send(409, {"error": type(exc).__name__, "detail": str(exc)})
                return
            except KeyError as exc:
                self._send(400, {"error": "missing_field", "detail": str(exc)})
                return
            self._send(200, result)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/board":
                slot = parse_qs(parsed.query).get("slot", [""])[0]
                if not slot:
                    self._send(400, {"error": "missing_field", "detail": "slot"})
                    return
                self._send(200, app.query("board", {"slot": slot}))
                return
            match = re.fullmatch(r"/cases/([A-Za-z0-9_-]+)/chain", parsed.path)
            if match:
                try:
                    self._send(200, app.query("case_chain", {"case_id": match.group(1)}))
                except KeyError:
                    self._send(404, {"error": "case_not_found"})
                return
            if parsed.path == "/health":
                self._send(200, {"status": "ok"})
                return
            self._send(404, {"error": "not_found", "path": parsed.path})

    return Handler


def serve(app: HostedApp, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), create_handler(app))
    return httpd
