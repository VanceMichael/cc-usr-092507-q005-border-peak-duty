"""高峰调度器：每个时间槽产出开放通道、岗位组合、备用容量与触发原因。

发布事务顺序（任一步失败整体回滚，绝不出现「人已占用、通道未启用」）：

1. 在租约登记处开启事务，预占全部岗位租约；
2. 调用外部发布器（广播/看板/外设联动）；
3. 发布器成功后提交租约并登记开放状态；发布器抛错则回滚全部租约，
   开放状态保持原状。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

from .clock import Clock, SystemClock
from .errors import PublishError
from .leases import DISPATCH, Lease, LeaseRegistry
from .resources import (
    AUTO,
    PRIMARY,
    RELEASE,
    SECONDARY,
    Channel,
    DeviceStatus,
)
from .timeline import Timeline

# 触发原因
TRIG_FORECAST = "FORECAST_SURGE"          # 分时预测超过常态能力
TRIG_FLIGHT_DELAY = "FLIGHT_DELAY"        # 延误航班把客流推入该槽
TRIG_DEVICE_DEGRADED = "DEVICE_DEGRADED"  # 设备降效/检修导致能力收缩
TRIG_STAFF_SHORT = "STAFF_SHORTAGE"       # 资质人员不足
TRIG_QUEUE_ESCALATE = "QUEUE_ESCALATION"  # 等待队列升级
TRIG_HANDOVER = "HANDOVER_REBUILD"        # 交接后重建岗位组合
TRIG_RECOVERY = "DEVICE_RECOVERY"         # 设备恢复释放了新能力


@dataclass
class PositionNeed:
    position: str
    demand_pax: int
    required_units: int
    opened: list[str]
    reserve_units: list[str]
    shortage: int = 0


@dataclass
class SlotPlan:
    slot: str
    opened: list[dict]                     # [{channel_id, position, staff_id, lease_id}]
    positions: dict[str, PositionNeed]
    reserve_capacity_pax: float
    spare_channels: list[str]
    spare_staff: list[str]
    triggers: list[str]
    reasons: list[str]
    demand_total: int
    capacity_total: float
    generated_at: str
    plan_id: str = ""
    proposed: list = field(default_factory=list, repr=False)       # 拟新占分配
    surplus_lease_ids: list = field(default_factory=list, repr=False)  # 拟释放租约

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "slot": self.slot,
            "generated_at": self.generated_at,
            "demand_total": self.demand_total,
            "capacity_total": round(self.capacity_total, 1),
            "reserve_capacity_pax": round(self.reserve_capacity_pax, 1),
            "opened": list(self.opened),
            "positions": {
                p: {
                    "demand_pax": n.demand_pax,
                    "required_units": n.required_units,
                    "opened": list(n.opened),
                    "reserve_units": list(n.reserve_units),
                    "shortage": n.shortage,
                }
                for p, n in self.positions.items()
            },
            "spare_channels": list(self.spare_channels),
            "spare_staff": list(self.spare_staff),
            "triggers": list(self.triggers),
            "reasons": list(self.reasons),
        }


@dataclass
class _Assignment:
    channel_id: str
    position: str
    staff_id: str
    lease: Lease | None = None


class Publisher:
    """外部发布器接口；测试可令其抛错验证回滚。"""

    def publish(self, plan: SlotPlan) -> None:  # pragma: no cover - 接口
        raise NotImplementedError


class Scheduler:
    def __init__(
        self,
        timeline: Timeline,
        registry: LeaseRegistry,
        clock: Clock | None = None,
        *,
        baseline_rate: int = 200,
        secondary_ratio: int = 4,   # 每 4 个查验单元配 1 个二线台位
        min_release_units: int = 1,
        reserve_staff_ratio: float = 0.1,
    ) -> None:
        self.timeline = timeline
        self.registry = registry
        self.clock = clock or SystemClock()
        self.baseline_rate = baseline_rate
        self.secondary_ratio = secondary_ratio
        self.min_release_units = min_release_units
        self.reserve_staff_ratio = reserve_staff_ratio
        self.openings: dict[str, list[_Assignment]] = {}  # slot -> 已发布开放
        self.plans: dict[str, SlotPlan] = {}

    # ---- 规划 -----------------------------------------------------------

    def _units_for(self, position: str) -> list[Channel]:
        out = [c for c in self.registry.pool.channels.values() if c.serves() == position]
        return sorted(out, key=lambda c: (c.kind != AUTO, c.channel_id))

    def _available_staff(self, position: str, slot: str, now, freed: set[str] | None = None) -> list[str]:
        occupied = {l.staff_id for l in self.registry.active_leases(slot, now)}
        freed = freed or set()
        return sorted(
            s.staff_id for s in self.registry.pool.qualified_staff(position)
            if s.staff_id not in occupied or s.staff_id in freed
        )

    def plan_slot(
        self,
        slot: str,
        *,
        secondary_queue: int = 0,
        release_queue: int = 0,
        triggers: list[str] | None = None,
        rebuild: bool = False,
    ) -> SlotPlan:
        now = self.clock.now()
        demand = self.timeline.slot_demand(slot)
        triggers = list(triggers or [])
        reasons: list[str] = []

        # ---- 触发原因识别 ----
        if demand.total_pax > self.baseline_rate:
            triggers.append(TRIG_FORECAST)
            reasons.append(
                f"槽 {slot} 预测总客流 {demand.total_pax} 超过常态能力 {self.baseline_rate}"
            )
        delayed_flights = [
            no for no, view in self.timeline.flights.flights().items()
            if slot in view.window_slots(self.timeline.slot_minutes) and view.delay_minutes > 0
        ]
        if delayed_flights:
            triggers.append(TRIG_FLIGHT_DELAY)
            reasons.append("延误航班客流叠加：" + ",".join(sorted(delayed_flights)))
        degraded = [
            c.channel_id for c in self.registry.pool.channels.values()
            if any(not d.available or d.status == DeviceStatus.DEGRADED
                   for d in c.devices.values() if d.kind in c.required_kinds())
        ]
        if degraded:
            triggers.append(TRIG_DEVICE_DEGRADED)
            reasons.append("设备降效/检修：" + ",".join(sorted(degraded)))
        escalated = self.registry.waiting(slot=slot)
        escalated = [w for w in escalated if w.state.value == "ESCALATED"]
        if escalated:
            triggers.append(TRIG_QUEUE_ESCALATE)
            reasons.append(f"{len(escalated)} 个岗位等待请求超过升级期限")

        triggers = list(dict.fromkeys(triggers))

        # ---- 既有基线（同槽重规划时尽量保留，避免无谓换岗） ----
        existing = self.registry.active_leases(slot, now)
        baseline: dict[str, Lease] = {l.channel_id: l for l in existing}
        freed_staff: set[str] = set()
        surplus_lease_ids: list[str] = []
        proposed: list[_Assignment] = []
        kept_channels: set[str] = set()

        needs: dict[str, PositionNeed] = {}
        spare_channels: list[str] = []
        used_staff = {l.staff_id for l in existing}
        capacity_total = 0.0

        def fill(position: str, pax_demand: int, queue_extra: int = 0):
            units = [u for u in self._units_for(position) if u.is_openable()]
            rates = {u.channel_id: u.effective_rate() for u in units}
            required = 0
            if pax_demand > 0:
                covered = 0.0
                for u in units:
                    if covered >= pax_demand:
                        break
                    covered += rates[u.channel_id]
                    required += 1
            required = max(required, queue_extra)
            if position == RELEASE:
                required = max(required, self.min_release_units)

            chosen = units[:required]
            chosen_ids = {u.channel_id for u in chosen}
            opened: list[str] = []
            shortage = 0

            # 1) 保留基线中仍在目标集合内的租约
            for unit in chosen:
                lease = baseline.get(unit.channel_id)
                if lease is not None and lease.position == position:
                    opened.append(unit.channel_id)
                    kept_channels.add(unit.channel_id)

            # 2) 为没有有效基线的目标单元新占岗位
            staff_pool = self._available_staff(position, slot, now, freed_staff)
            for unit in chosen:
                if unit.channel_id in kept_channels:
                    continue
                candidates = [s for s in staff_pool if s not in used_staff]
                if not candidates:
                    shortage += 1
                    continue
                staff_id = candidates[0]
                used_staff.add(staff_id)
                opened.append(unit.channel_id)
                kept_channels.add(unit.channel_id)
                proposed.append(_Assignment(unit.channel_id, position, staff_id))

            # 3) 目标集合之外、属于本岗位的基线租约拟释放
            for cid, lease in baseline.items():
                if lease.position == position and cid not in chosen_ids:
                    surplus_lease_ids.append(lease.lease_id)
                    freed_staff.add(lease.staff_id)
                    used_staff.discard(lease.staff_id)

            nonlocal capacity_total
            capacity_total += sum(rates[c] for c in opened)

            reserve_units = [u.channel_id for u in units if u.channel_id not in chosen_ids]
            spare_channels.extend(reserve_units)
            if shortage:
                triggers.append(TRIG_STAFF_SHORT)
                reasons.append(f"{position} 缺少 {shortage} 名资质人员")
            needs[position] = PositionNeed(
                position=position,
                demand_pax=pax_demand,
                required_units=required,
                opened=sorted(opened),
                reserve_units=sorted(set(reserve_units)),
                shortage=shortage,
            )

        sec_extra = math.ceil(secondary_queue / max(1, self._desk_rate(SECONDARY)))
        rel_extra = math.ceil(release_queue / max(1, self._desk_rate(RELEASE)))
        fill(PRIMARY, demand.total_pax)
        fill(SECONDARY, secondary_queue, sec_extra)
        fill(RELEASE, release_queue, rel_extra)

        # 设备已停用却仍持有的租约必须释放
        for cid, lease in baseline.items():
            unit = self.registry.pool.channels.get(cid)
            if unit is not None and not unit.is_openable() and lease.lease_id not in surplus_lease_ids:
                surplus_lease_ids.append(lease.lease_id)
                freed_staff.add(lease.staff_id)
                triggers.append(TRIG_DEVICE_DEGRADED)
                reasons.append(f"通道 {cid} 设备检修，既有租约 {lease.lease_id} 须释放")

        kept_staff = {l.staff_id for cid, l in baseline.items()
                      if l.lease_id not in surplus_lease_ids}
        all_staff: set[str] = set()
        for position in needs:
            all_staff.update(s.staff_id for s in self.registry.pool.qualified_staff(position))
        spare_staff = sorted(all_staff - kept_staff - {a.staff_id for a in proposed})

        occupied_after = kept_channels
        reserve_capacity = sum(
            self.registry.pool.channels[cid].effective_rate()
            for cid in self.registry.pool.channels
            if cid not in occupied_after and self.registry.pool.channels[cid].is_openable()
        )
        spare_channel_list = sorted(
            cid for cid, c in self.registry.pool.channels.items()
            if c.is_openable() and cid not in occupied_after
        )

        plan = SlotPlan(
            slot=slot,
            opened=[],
            positions=needs,
            reserve_capacity_pax=reserve_capacity,
            spare_channels=spare_channel_list,
            spare_staff=spare_staff,
            triggers=triggers,
            reasons=reasons,
            demand_total=demand.total_pax,
            capacity_total=capacity_total,
            generated_at=self.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        plan.proposed = proposed
        plan.surplus_lease_ids = sorted(set(surplus_lease_ids))
        return plan

    def _desk_rate(self, position: str) -> int:
        rates = [c.base_rate for c in self._units_for(position)]
        return max(rates) if rates else 30

    # ---- 发布（原子） ----------------------------------------------------

    def publish(
        self,
        plan: SlotPlan,
        publisher: Publisher,
        *,
        ttl_minutes: int = 30,
        request_id: str = "",
    ) -> SlotPlan:
        # 同槽重规划是对"该槽当前计划"的修订，沿用稳定计划 ID
        if not plan.plan_id:
            previous = self.plans.get(plan.slot)
            plan.plan_id = previous.plan_id if previous else f"plan-{uuid.uuid4().hex[:12]}"
        self.registry.begin()
        acquired: list[_Assignment] = []
        try:
            # 先暂存释放多余租约（释放后人员/通道才在事务视图中可用）
            for lease_id in plan.surplus_lease_ids:
                self.registry.stage_release(lease_id)

            for a in plan.proposed:
                lease = self.registry.acquire(
                    staff_id=a.staff_id,
                    channel_id=a.channel_id,
                    position=a.position,
                    slot=plan.slot,
                    ttl_minutes=ttl_minutes,
                    reason=DISPATCH,
                    request_id=f"{request_id}:{a.channel_id}" if request_id else "",
                )
                a.lease = lease
                acquired.append(a)

            for need in plan.positions.values():
                for i in range(need.shortage):
                    self.registry.enqueue(
                        position=need.position,
                        slot=plan.slot,
                        escalate_after=10,
                        ttl_minutes=ttl_minutes,
                        request_id=f"{request_id}:wait:{need.position}:{i}" if request_id else "",
                    )

            # 外部发布可能失败：此刻租约全在暂存区，通道开放状态尚未变更
            publisher.publish(plan)
        except PublishError:
            self.registry.rollback()
            for a in acquired:
                a.lease = None
            raise
        except Exception as exc:
            self.registry.rollback()
            for a in acquired:
                a.lease = None
            raise PublishError(f"调度发布失败，已回滚全部岗位占用：{exc}") from exc

        self.registry.commit()

        # 发布后权威开放集合 = 本槽全部有效租约（含保留基线与新租约）
        now = self.clock.now()
        final = self.registry.active_leases(plan.slot, now)
        assignments = [
            _Assignment(l.channel_id, l.position, l.staff_id, l) for l in final
        ]
        self.openings[plan.slot] = assignments
        plan.opened = [
            {
                "channel_id": a.channel_id,
                "position": a.position,
                "staff_id": a.staff_id,
                "lease_id": a.lease.lease_id,
            }
            for a in assignments
        ]
        self.plans[plan.slot] = plan
        return plan

    # ---- 读取 -----------------------------------------------------------

    def current_openings(self, slot: str, now=None) -> list[dict]:
        return [
            {
                "channel_id": a.channel_id,
                "position": a.position,
                "staff_id": a.staff_id,
                "lease_id": a.lease.lease_id if a.lease else "",
            }
            for a in self.openings.get(slot, [])
            if a.lease and a.lease.active_at(now or self.clock.now())
        ]

    def remaining_capacity(self, slot: str, now=None) -> dict:
        """当前剩余能力：已开放能力 - 在队需求，以及未启用备用。"""
        ts = now or self.clock.now()
        occupied = self.registry.occupied_channels(slot, ts)
        open_capacity = sum(
            self.registry.pool.channels[cid].effective_rate()
            for cid in occupied if cid in self.registry.pool.channels
        )
        demand = self.timeline.slot_demand(slot)
        reserve = sum(
            c.effective_rate() for c in self.registry.pool.channels.values()
            if c.is_openable() and c.channel_id not in occupied
        )
        return {
            "slot": slot,
            "demand_pax": demand.total_pax,
            "open_capacity_pax": round(open_capacity, 1),
            "immediate_headroom_pax": round(max(0.0, open_capacity - demand.total_pax), 1),
            "reserve_capacity_pax": round(reserve, 1),
        }

    def congestion_sources(self, slot: str, now=None) -> list[dict]:
        """定位当前拥堵源：需求、设备、人员、队列。"""
        ts = now or self.clock.now()
        sources: list[dict] = []
        demand = self.timeline.slot_demand(slot)
        rem = self.remaining_capacity(slot, ts)
        if demand.total_pax > rem["open_capacity_pax"]:
            sources.append({
                "type": TRIG_FORECAST,
                "detail": f"需求 {demand.total_pax} 高于已开放能力 {rem['open_capacity_pax']}",
                "flights": demand.flights,
            })
        for c in self.registry.pool.channels.values():
            if not c.is_openable():
                sources.append({
                    "type": "DEVICE_DOWN",
                    "detail": f"通道 {c.channel_id} 不可用：{c.status_reason()}",
                })
        short = [w for w in self.registry.waiting(slot=slot)]
        if short:
            sources.append({
                "type": TRIG_QUEUE_ESCALATE if any(w.state.value == "ESCALATED" for w in short)
                        else TRIG_STAFF_SHORT,
                "detail": f"{len(short)} 个岗位请求等待中",
                "wait_ids": [w.wait_id for w in short],
            })
        return sources

    def snapshot(self) -> dict:
        return {
            "openings": {
                slot: [
                    {"channel_id": a.channel_id, "position": a.position,
                     "staff_id": a.staff_id, "lease_id": a.lease.lease_id if a.lease else ""}
                    for a in assignments
                ]
                for slot, assignments in self.openings.items()
            },
            "plans": {slot: plan.as_dict() for slot, plan in self.plans.items()},
        }

    def restore(self, data: dict) -> None:
        self.openings = {}
        leases = {l.lease_id: l for l in self.registry._leases.values()}  # noqa: SLF001
        for slot, items in data.get("openings", {}).items():
            assignments = []
            for item in items:
                assignments.append(_Assignment(
                    channel_id=item["channel_id"],
                    position=item["position"],
                    staff_id=item["staff_id"],
                    lease=leases.get(item["lease_id"]),
                ))
            self.openings[slot] = assignments

        self.plans = {}
        for slot, plan_dict in data.get("plans", {}).items():
            self.plans[slot] = _RestoredPlan(plan_dict)


class _RestoredPlan:
    """重启后只需对外只读视图的历史计划。"""

    def __init__(self, data: dict) -> None:
        self._data = data
        self.slot = data.get("slot", "")
        self.plan_id = data.get("plan_id", "")

    def as_dict(self) -> dict:
        return dict(self._data)
