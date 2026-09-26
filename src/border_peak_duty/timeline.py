"""统一时间线：航班动态、分时预测、通道设备、人员资质。

所有输入都按业务版本归并，乱序送达不会改写更高版本的事实：

- 航班事件按 ``revision`` 排序取最新；``departed``/``cancelled`` 一旦出现即
  终态锁定，迟到的低版本延误动态不能让已离场航班重新进入需求时间线。
- 同一 ``event_id`` 重发视为幂等重放，不产生新版本。
- 预测按版本号归并，同一版本重发只更新该版本，不同版本互不覆盖。
- 设备状态按 ``updated_at`` 取最新，检修/离线通道能力为 0。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    DeviceState,
    DeviceStatus,
    FlightEvent,
    FlightStatus,
    ForecastVersion,
    Lane,
    Qualification,
)

TERMINAL_FLIGHT_STATUSES = {FlightStatus.DEPARTED.value, FlightStatus.CANCELLED.value}
# 未到场状态：这些航班的旅客计入预测需求；落地后旅客进入等待队列，
# 不再计入预测需求——任何人只可能在预测需求或等待队列之一中被计数。
PRE_ARRIVAL_STATUSES = {FlightStatus.SCHEDULED.value, FlightStatus.DELAYED.value}


class FlightTimeline:
    """版本化、乱序安全的航班动态时间线。"""

    def __init__(self) -> None:
        # flight_no -> {event_id: event}
        self._events: dict[str, dict[str, FlightEvent]] = {}
        # flight_no -> True 表示已观察到终态事件（终态锁）
        self._terminal_latched: dict[str, bool] = {}

    # -- 写入 ---------------------------------------------------------------

    def apply(self, event: FlightEvent) -> str:
        """归入一条航班动态。

        返回 ``new``（新事件）、``duplicate``（同一动态重发）或
        ``late``（乱序迟到、对当前有效状态无影响）。
        """
        per_flight = self._events.setdefault(event.flight_no, {})
        if event.event_id in per_flight:
            # 同一动态重发：只确认同一版本，绝不重复计数。
            return "duplicate"

        # 终态锁已合上：任何非终态动态（哪怕版本号更高）一律按迟到处理，
        # 已离场航班不得重新进入需求时间线。事件留档备查，但不影响有效状态。
        if self._terminal_latched.get(event.flight_no) and event.status not in TERMINAL_FLIGHT_STATUSES:
            per_flight[event.event_id] = event
            return "late"

        per_flight[event.event_id] = event
        if event.status in TERMINAL_FLIGHT_STATUSES:
            self._terminal_latched[event.flight_no] = True
        current = self.effective(event.flight_no)
        if current is not None and current.event_id != event.event_id:
            return "late" if event.revision < current.revision else "new"
        return "new"

    # -- 读取 ---------------------------------------------------------------

    def effective(self, flight_no: str) -> FlightEvent | None:
        """返回当前有效（最高 revision，且尊重终态锁）的动态。"""
        per_flight = self._events.get(flight_no)
        if not per_flight:
            return None
        if self._terminal_latched.get(flight_no):
            # 终态锁：在所有终态事件中取最高 revision；忽略其余一切。
            terminals = [
                e for e in per_flight.values() if e.status in TERMINAL_FLIGHT_STATUSES
            ]
            return max(terminals, key=lambda e: e.revision)
        return max(per_flight.values(), key=lambda e: e.revision)

    def is_terminal(self, flight_no: str) -> bool:
        return bool(self._terminal_latched.get(flight_no))

    def max_revision(self, flight_no: str) -> int:
        per_flight = self._events.get(flight_no)
        if not per_flight:
            return 0
        return max(e.revision for e in per_flight.values())

    def flight_numbers(self) -> list[str]:
        return sorted(self._events)

    def active_flights(self) -> list[FlightEvent]:
        """所有未离场/未取消航班的有效动态，按预计到场时间排序。"""
        result = []
        for flight_no in self._events:
            if self._terminal_latched.get(flight_no):
                continue
            event = self.effective(flight_no)
            if event is not None:
                result.append(event)
        result.sort(key=lambda e: (e.revised_arrival or e.scheduled_arrival))
        return result

    def projected_for_bucket(self, bucket_start: str, bucket_end: str) -> list[FlightEvent]:
        """预计在给定时段到场、且尚未落地的航班。

        已落地航班不在这里——其旅客已进入等待队列，避免两个队列重复计算。
        """
        result = []
        for event in self.active_flights():
            if event.status not in PRE_ARRIVAL_STATUSES:
                continue
            eta = event.revised_arrival or event.scheduled_arrival
            if bucket_start <= eta < bucket_end:
                result.append(event)
        return result


class ForecastTimeline:
    """分时客流预测，仅以版本号归并。"""

    def __init__(self) -> None:
        self._versions: dict[int, ForecastVersion] = {}

    def apply(self, forecast: ForecastVersion) -> str:
        existing = self._versions.get(forecast.version)
        self._versions[forecast.version] = forecast
        return "duplicate" if existing is not None else "new"

    def latest(self) -> ForecastVersion | None:
        if not self._versions:
            return None
        return self._versions[max(self._versions)]

    def version(self, number: int) -> ForecastVersion | None:
        return self._versions.get(number)


class DeviceRegistry:
    """通道设备状态，按状态更新时间取最新（乱序安全）。"""

    def __init__(self) -> None:
        self._states: dict[str, DeviceState] = {}

    def apply(self, state: DeviceState) -> str:
        old = self._states.get(state.device_id)
        if old is not None and state.updated_at < old.updated_at:
            return "late"
        if old is not None and state.updated_at == old.updated_at:
            return "duplicate"
        self._states[state.device_id] = state
        return "new"

    def state_of(self, device_id: str) -> DeviceState:
        return self._states.get(
            device_id,
            DeviceState(device_id, DeviceStatus.ONLINE.value, 1.0),
        )

    def effective_capacity(self, lane: Lane) -> tuple[int, str]:
        """返回通道当前有效能力与原因。"""
        state = self.state_of(lane.device_id)
        if state.status in (DeviceStatus.MAINTENANCE.value, DeviceStatus.OFFLINE.value):
            return 0, f"device_{state.status}"
        ratio = state.capacity_ratio if state.status == DeviceStatus.DEGRADED.value else 1.0
        return int(lane.base_capacity * ratio), (
            "device_degraded" if ratio < 1.0 else "nominal"
        )


class Roster:
    """在岗人员及其资质。"""

    def __init__(self) -> None:
        self._staff: dict[str, Qualification] = {}

    def register(self, qualification: Qualification) -> None:
        self._staff[qualification.staff_id] = qualification

    def get(self, staff_id: str) -> Qualification | None:
        return self._staff.get(staff_id)

    def all(self) -> list[Qualification]:
        return [self._staff[k] for k in sorted(self._staff)]


@dataclass
class BucketDemand:
    bucket_start: str
    bucket_end: str
    forecast_pax: int
    flight_pax: int
    flights: list[str] = field(default_factory=list)
    forecast_version: int | None = None
    forecast_covered: bool = False

    @property
    def total(self) -> int:
        # 最新预测版本已纳入航班计划时（含该时段键即视为覆盖），以预测为准，
        # 航班明细只用于解释触发原因；未覆盖时回退为航班加总，两者绝不双算。
        return self.forecast_pax if self.forecast_covered else self.flight_pax


class Timeline:
    """聚合全部输入的统一时间线。"""

    def __init__(self, bucket_minutes: int = 60) -> None:
        self.bucket_minutes = bucket_minutes
        self.flights = FlightTimeline()
        self.forecasts = ForecastTimeline()
        self.devices = DeviceRegistry()
        self.roster = Roster()
        self.lanes: dict[str, Lane] = {}

    def add_lane(self, lane: Lane) -> None:
        self.lanes[lane.lane_id] = lane

    def bucket_bounds(self, bucket_start: str) -> tuple[str, str]:
        from datetime import datetime, timedelta

        start = datetime.fromisoformat(bucket_start)
        end = start + timedelta(minutes=self.bucket_minutes)
        return bucket_start, end.isoformat(timespec="minutes")

    def demand(self, bucket_start: str) -> BucketDemand:
        """合并预测客流与未到场航班客流，得到时段总需求。"""
        start, end = self.bucket_bounds(bucket_start)
        forecast = self.forecasts.latest()
        covered = forecast is not None and bucket_start in forecast.buckets
        forecast_pax = int(forecast.buckets.get(bucket_start, 0)) if forecast else 0
        flights = self.flights.projected_for_bucket(start, end)
        return BucketDemand(
            bucket_start=start,
            bucket_end=end,
            forecast_pax=forecast_pax,
            flight_pax=sum(f.pax for f in flights),
            flights=[f.flight_no for f in flights],
            forecast_version=forecast.version if forecast else None,
            forecast_covered=covered,
        )
