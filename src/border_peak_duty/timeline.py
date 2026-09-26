"""统一时间线：航班动态、分时客流预测与按时间槽的需求汇总。

关键规则：

- 航班事件按 ``version`` 排序定序；同一动态（相同 ``event_id``）重发只更新
  对应版本，不产生第二条记录。
- 乱序到达的低版本事件在存在更高版本后一律拒绝；航班进入离场/取消终态后，
  任何非终态动态都不能让它重新占用资源。
- 预测按版本号管理，同版本重发只更新该版本序列；时间线始终使用最高版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clock import parse_slot, parse_ts, shift_slot, to_slot
from .errors import ForecastValidationError, StaleEventError

# 事件类型
SCHEDULED = "SCHEDULED"  # 航班计划
DELAYED = "DELAYED"      # 延误/时刻修订
ACTIVE = "ACTIVE"        # 旅客开始进入查验区（落地开舱/开始集结）
RELEASED = "RELEASED"    # 查验完成或航班离场，终态：释放资源
CANCELLED = "CANCELLED"  # 航班取消，终态：从未占用

TERMINAL_KINDS = frozenset({RELEASED, CANCELLED})
ARR = "ARR"
DEP = "DEP"


@dataclass(frozen=True)
class FlightEvent:
    flight_no: str
    version: int
    kind: str
    occurred_at: str                      # 业务发生时间（ISO）
    direction: str | None = None
    pax: int | None = None
    start: str | None = None              # 预计占用起点（ISO）
    end: str | None = None                # 预计占用终点（ISO）
    note: str = ""
    event_id: str = ""
    received_at: str = ""

    def identity(self) -> str:
        return self.event_id or f"{self.flight_no}:{self.kind}:v{self.version}"


@dataclass
class FlightView:
    """某个航班在当前全部已知版本下的投影。"""

    flight_no: str
    direction: str = ARR
    version: int = 0
    status: str = SCHEDULED
    planned_pax: int = 0
    planned_start: str | None = None
    planned_end: str | None = None
    actual_start: str | None = None
    actual_end: str | None = None
    delay_minutes: int = 0
    last_note: str = ""
    _events: dict[str, FlightEvent] = field(default_factory=dict, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_KINDS

    @property
    def released(self) -> bool:
        return self.status == RELEASED

    @property
    def cancelled(self) -> bool:
        return self.status == CANCELLED

    def occupancy_start(self) -> str | None:
        return self.actual_start or self.planned_start

    def occupancy_end(self) -> str | None:
        return self.actual_end or self.planned_end

    def window_slots(self, slot_minutes: int = 15) -> list[str]:
        """返回该航班当前占用（或将占用）的时间槽。"""
        start = self.occupancy_start()
        end = self.occupancy_end()
        if start is None or self.status == CANCELLED:
            return []
        if self.status == RELEASED and self.actual_end is not None:
            end = self.actual_end
        if end is None:
            return [to_slot(start, slot_minutes)]
        if parse_ts(end) <= parse_ts(start):
            return []
        slots: list[str] = []
        slot = to_slot(start, slot_minutes)
        last = to_slot(end, slot_minutes)
        # 终点恰落在槽边界时不含最后一个槽
        if parse_slot(last) == parse_ts(end):
            last = shift_slot(last, -1, slot_minutes)
        while slot <= last:
            slots.append(slot)
            slot = shift_slot(slot, 1, slot_minutes)
        return slots

    def demand_by_slot(self, slot_minutes: int = 15) -> dict[str, int]:
        """把航班旅客数均匀摊到占用窗口的各时间槽。"""
        slots = self.window_slots(slot_minutes)
        if not slots or self.planned_pax <= 0:
            return {}
        if self.status == CANCELLED:
            return {}
        n = len(slots)
        base, rem = divmod(self.planned_pax, n)
        result: dict[str, int] = {}
        for i, slot in enumerate(slots):
            result[slot] = base + (1 if i < rem else 0)
        return result


class FlightLog:
    """全部航班动态的权威记录与投影来源。"""

    def __init__(self) -> None:
        self._events: dict[str, FlightEvent] = {}
        self._flights: dict[str, FlightView] = {}

    # ---- 写入 -----------------------------------------------------------

    def ingest(self, event: FlightEvent) -> str:
        """录入一条航班动态，返回处置原因。

        :raises StaleEventError: 低版本迟到事件，或终态之后的占用性动态。
        """
        view = self._flights.get(event.flight_no)
        identity = event.identity()

        if view is None and event.kind != SCHEDULED:
            raise StaleEventError(
                f"航班 {event.flight_no} 缺少计划基线，不能先录入 {event.kind} 动态"
            )

        if view is not None and event.version < view.version:
            raise StaleEventError(
                f"航班 {event.flight_no} 已存在 v{view.version}，"
                f"迟到的 v{event.version}（{event.kind}）不予采纳"
            )

        if view is not None and view.terminal:
            same_version = event.version == view.version
            same_kind = view.status == event.kind
            if not (same_version and same_kind):
                raise StaleEventError(
                    f"航班 {event.flight_no} 已{view.status}离场终态，"
                    f"{event.kind} v{event.version} 不得重新占用资源"
                )

        existing = self._events.get(identity)
        if existing is None and view is not None and event.version == view.version:
            raise StaleEventError(
                f"航班 {event.flight_no} 的 v{event.version} 已被另一条动态占用，"
                "新动态必须递增版本号"
            )
        if existing is not None and existing.kind != event.kind:
            raise StaleEventError(f"动态标识 {identity} 的类型不可变更")

        self._events[identity] = event
        self._flights[event.flight_no] = self._project(event.flight_no)
        return "duplicate_updated" if existing is not None else "applied"

    def _project(self, flight_no: str) -> FlightView:
        events = sorted(
            (e for e in self._events.values() if e.flight_no == flight_no),
            key=lambda e: (e.version, e.received_at or e.occurred_at),
        )
        first = next(e for e in events if e.kind == SCHEDULED)
        view = FlightView(
            flight_no=flight_no,
            direction=first.direction or ARR,
            version=max(e.version for e in events),
            status=SCHEDULED,
            planned_pax=first.pax or 0,
            planned_start=first.start,
            planned_end=first.end,
        )
        original_start = parse_ts(first.start) if first.start else None
        for e in events:
            view.last_note = e.note or view.last_note
            if e.kind == SCHEDULED:
                continue
            if e.kind == DELAYED:
                if e.start:
                    view.planned_start = e.start
                if e.end:
                    view.planned_end = e.end
                if e.pax is not None:
                    view.planned_pax = e.pax
                if original_start is not None and e.start:
                    view.delay_minutes = max(
                        0, int((parse_ts(e.start) - original_start).total_seconds() // 60)
                    )
            elif e.kind == ACTIVE:
                view.actual_start = e.occurred_at
                view.planned_end = e.end or view.planned_end
                if e.pax is not None:
                    view.planned_pax = e.pax
                view.status = ACTIVE
            elif e.kind == RELEASED:
                view.actual_end = e.occurred_at
                view.status = RELEASED
            elif e.kind == CANCELLED:
                view.status = CANCELLED
        return view

    # ---- 读取 -----------------------------------------------------------

    def get(self, flight_no: str) -> FlightView | None:
        return self._flights.get(flight_no)

    def flights(self) -> dict[str, FlightView]:
        return dict(self._flights)

    def events(self, flight_no: str | None = None) -> list[FlightEvent]:
        out = [e for e in self._events.values() if flight_no is None or e.flight_no == flight_no]
        return sorted(out, key=lambda e: (e.flight_no, e.version, e.occurred_at))

    def demand_by_slot(self, slot_minutes: int = 15) -> dict[str, dict[str, int]]:
        """{航班: {槽: 人数}}，已离场窗口按实际时间截断，取消航班为 0。"""
        result: dict[str, dict[str, int]] = {}
        for no, view in self._flights.items():
            demand = view.demand_by_slot(slot_minutes)
            if demand:
                result[no] = demand
        return result


@dataclass(frozen=True)
class ForecastVersion:
    version: int
    issued_at: str
    series: dict[str, int]                    # {槽: 不含已接入动态航班的基础客流}
    note: str = ""


class ForecastBook:
    """分时客流预测的版本库。"""

    def __init__(self) -> None:
        self._versions: dict[int, ForecastVersion] = {}

    def ingest(self, forecast: ForecastVersion) -> str:
        if not forecast.series:
            raise ForecastValidationError(f"预测 v{forecast.version} 缺少分时序列")
        if forecast.version < 1:
            raise ForecastValidationError("预测版本号必须 >= 1")
        for slot, pax in forecast.series.items():
            if pax < 0:
                raise ForecastValidationError(f"槽 {slot} 预测量为负")
            parse_slot(slot)  # 校验槽格式
        existed = forecast.version in self._versions
        self._versions[forecast.version] = forecast
        return "duplicate_updated" if existed else "applied"

    def current(self) -> ForecastVersion | None:
        if not self._versions:
            return None
        return self._versions[max(self._versions)]

    def get(self, version: int) -> ForecastVersion | None:
        return self._versions.get(version)


@dataclass
class SlotDemand:
    slot: str
    forecast_pax: int
    flight_pax: int
    total_pax: int
    flights: dict[str, int]


class Timeline:
    """把预测、航班动态投影合并为同一时间线上的分时需求。"""

    def __init__(self, flights: FlightLog, forecasts: ForecastBook, slot_minutes: int = 15) -> None:
        self.flights = flights
        self.forecasts = forecasts
        self.slot_minutes = slot_minutes

    def slot_demand(self, slot: str) -> SlotDemand:
        current = self.forecasts.current()
        forecast_pax = current.series.get(slot, 0) if current else 0
        present: dict[str, int] = {}
        flight_total = 0
        for no, series in self.flights.demand_by_slot(self.slot_minutes).items():
            if slot in series:
                present[no] = series[slot]
                flight_total += series[slot]
        return SlotDemand(slot, forecast_pax, flight_total, forecast_pax + flight_total, present)

    def range(self, start_slot: str, steps: int) -> list[SlotDemand]:
        return [
            self.slot_demand(shift_slot(start_slot, i, self.slot_minutes))
            for i in range(steps)
        ]

    def all_slots(self) -> list[str]:
        slots = set()
        current = self.forecasts.current()
        if current:
            slots.update(current.series)
        for series in self.flights.demand_by_slot(self.slot_minutes).values():
            slots.update(series)
        return sorted(slots)
