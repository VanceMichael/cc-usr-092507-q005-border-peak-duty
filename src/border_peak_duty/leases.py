"""岗位租约与等待队列。

租约是「人员占用岗位、岗位绑定通道单元」的唯一权威：

- 一名人员同一时间槽对同一岗位只能持有一份有效租约；
- 一个通道单元同一时间槽只能被一份有效租约占用；
- 调配（换人）、交接（班次要接）与设备恢复后的重新占位都走同一套获取/释放规则；
- 租约有到期时间，到期自动失效并释放人员与单元；
- 获取不到资源的请求进入等待队列，带升级期限，到期升级优先；
- 所有变更由调用方在事务中暂存，提交失败可整体回滚。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from .clock import Clock, SystemClock, iso, parse_ts
from .errors import LeaseConflictError
from .resources import POSITIONS, ResourcePool

DISPATCH = "DISPATCH"    # 调度发布
HANDOVER = "HANDOVER"    # 班次交接
RECOVERY = "RECOVERY"    # 设备恢复/租约恢复后补位
ESCALATED_REASSIGN = "ESCALATED_REASSIGN"

DEFAULT_TTL_MINUTES = 30
DEFAULT_ESCALATE_MINUTES = 10


class LeaseState(str, Enum):
    HELD = "HELD"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"  # 通道因设备检修等被强制停用


class WaitState(str, Enum):
    WAITING = "WAITING"
    ESCALATED = "ESCALATED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


_ACTIVE = frozenset({LeaseState.HELD})


@dataclass
class Lease:
    lease_id: str
    staff_id: str
    channel_id: str
    position: str
    slot: str
    acquired_at: str
    expires_at: str
    reason: str = DISPATCH
    state: LeaseState = LeaseState.HELD
    request_id: str = ""

    def active_at(self, ts) -> bool:
        return self.state == LeaseState.HELD and parse_ts(self.expires_at) > parse_ts(ts)


@dataclass
class WaitEntry:
    wait_id: str
    position: str
    slot: str
    enqueued_at: str
    escalate_at: str
    expires_at: str
    staff_id: str | None = None
    channel_id: str | None = None
    state: WaitState = WaitState.WAITING
    fulfilled_by: str = ""
    escalations: int = 0


class _Staged:
    """一次发布内的暂存变更，异常时丢弃即可回滚。"""

    def __init__(self) -> None:
        self.leases: dict[str, Lease] = {}
        self.entries: dict[str, WaitEntry] = {}
        self.order: list[str] = []
        self.request_ids: set[str] = set()
        self.releases: dict[str, LeaseState] = {}  # 待生效的旧租约状态


class LeaseRegistry:
    def __init__(self, pool: ResourcePool, clock: Clock | None = None) -> None:
        self.pool = pool
        self.clock = clock or SystemClock()
        self._leases: dict[str, Lease] = {}
        self._wait: dict[str, WaitEntry] = {}
        self._wait_order: list[str] = []
        self._request_ids: set[str] = set()
        self._stage: _Staged | None = None

    # ---- 事务 -----------------------------------------------------------

    def begin(self) -> None:
        if self._stage is not None:
            raise LeaseConflictError("已有未完成的租约事务")
        self._stage = _Staged()

    def commit(self) -> None:
        if self._stage is None:
            raise LeaseConflictError("没有可提交的租约事务")
        stage = self._stage
        self._stage = None
        for lid, state in stage.releases.items():
            if lid in self._leases:
                self._leases[lid].state = state
        for lid, lease in stage.leases.items():
            self._leases[lid] = lease
        self._request_ids.update(stage.request_ids)
        for wid, entry in stage.entries.items():
            self._wait[wid] = entry
            if wid not in self._wait_order:
                self._wait_order.append(wid)
        self._wait_order.extend(w for w in stage.order if w not in self._wait_order)
        self._wait_order = [w for w in self._wait_order if self._wait[w].state not in
                            {WaitState.FULFILLED, WaitState.CANCELLED, WaitState.EXPIRED}]

    def rollback(self) -> None:
        self._stage = None

    @property
    def in_transaction(self) -> bool:
        return self._stage is not None

    def _store(self) -> dict[str, Lease]:
        return self._stage.leases if self._stage is not None else self._leases

    def _request_store(self) -> set[str]:
        return self._stage.request_ids if self._stage is not None else self._request_ids

    def _all_leases(self) -> list[Lease]:
        if self._stage is not None:
            # 返回事务视图：暂存释放的租约按释放后状态参与校验；
            # 一旦回滚，暂存区丢弃，底层租约仍为 HELD。
            out = []
            for lid, lease in self._leases.items():
                view = lease
                if lid in self._stage.releases:
                    view = Lease(**{**lease.__dict__, "state": self._stage.releases[lid]})
                out.append(view)
            out.extend(self._stage.leases.values())
            return out
        return list(self._leases.values())

    def stage_release(self, lease_id: str, state: LeaseState = LeaseState.RELEASED) -> None:
        """事务内暂存释放，提交后才真正生效。"""
        if self._stage is None:
            raise LeaseConflictError("暂存释放必须在事务内")
        if lease_id not in self._leases:
            raise LeaseConflictError(f"未知租约：{lease_id}")
        self._stage.releases[lease_id] = state

    # ---- 校验与获取 ------------------------------------------------------

    def _validate_acquire(self, staff_id: str, channel_id: str, position: str,
                          slot: str, now, expires_at, request_id: str) -> None:
        if position not in POSITIONS:
            raise LeaseConflictError(f"未知岗位：{position}")
        if request_id and (
            request_id in self._request_ids or request_id in self._request_store()
        ):
            raise LeaseConflictError(f"请求 {request_id} 已处理，请勿重复占用")
        staff = self.pool.staff.get(staff_id)
        if staff is None:
            raise LeaseConflictError(f"未知人员：{staff_id}")
        if not staff.qualified_for(position):
            raise LeaseConflictError(f"人员 {staff_id} 不具备 {position} 资质")
        unit = self.pool.channels.get(channel_id)
        if unit is None:
            raise LeaseConflictError(f"未知通道单元：{channel_id}")
        if unit.serves() != position:
            raise LeaseConflictError(
                f"通道 {channel_id}（{unit.kind}）承载 {unit.serves()}，不能安排 {position}"
            )
        if not unit.is_openable():
            raise LeaseConflictError(f"通道 {channel_id} 暂不可用：{unit.status_reason()}")
        if parse_ts(expires_at) <= parse_ts(now):
            raise LeaseConflictError("租约到期时间必须晚于当前时间")
        for lease in self._all_leases():
            if not lease.active_at(now):
                continue
            # 普通查验、二线复核、最终放行人员相互隔离：一人一槽一岗
            if lease.staff_id == staff_id and lease.slot == slot:
                raise LeaseConflictError(
                    f"人员 {staff_id} 在 {slot} 已持有 {lease.position} 租约，"
                    "不得跨岗位重复占用"
                )
            if lease.channel_id == channel_id and lease.slot == slot:
                raise LeaseConflictError(
                    f"通道 {channel_id} 在 {slot} 已被人员 {lease.staff_id} 占用"
                )

    def acquire(self, *, staff_id: str, channel_id: str, position: str, slot: str,
                ttl_minutes: int = DEFAULT_TTL_MINUTES, reason: str = DISPATCH,
                request_id: str = "", now=None) -> Lease:
        ts = parse_ts(now) if now else self.clock.now()
        expires_at = ts + timedelta(minutes=ttl_minutes)
        self._validate_acquire(staff_id, channel_id, position, slot, ts, expires_at, request_id)
        lease = Lease(
            lease_id=f"lease-{uuid.uuid4().hex[:12]}",
            staff_id=staff_id,
            channel_id=channel_id,
            position=position,
            slot=slot,
            acquired_at=iso(ts),
            expires_at=iso(expires_at),
            reason=reason,
            request_id=request_id,
        )
        self._store()[lease.lease_id] = lease
        if request_id:
            self._request_store().add(request_id)
        return lease

    def release(self, lease_id: str, *, invalid: bool = False) -> Lease:
        target = self._leases.get(lease_id) or (
            self._stage.leases.get(lease_id) if self._stage else None
        )
        if target is None:
            raise LeaseConflictError(f"未知租约：{lease_id}")
        target.state = LeaseState.INVALIDATED if invalid else LeaseState.RELEASED
        return target

    def handover(self, lease_id: str, to_staff_id: str, *, request_id: str = "",
                 ttl_minutes: int | None = None) -> Lease:
        """班次交接：旧租约释放与新租约获取必须同时成功。"""
        old = self._leases.get(lease_id)
        if old is None or old.state != LeaseState.HELD:
            raise LeaseConflictError(f"租约 {lease_id} 不可交接")
        if old.staff_id == to_staff_id:
            raise LeaseConflictError("交接双方不能是同一人")
        now = self.clock.now()
        remaining = (parse_ts(old.expires_at) - now).total_seconds() / 60
        ttl = ttl_minutes if ttl_minutes is not None else max(1, int(remaining))
        staged = self._stage is not None
        if not staged:
            self.begin()
        try:
            # 先暂存释放旧租约，通道与人员在事务视图中空出
            self.stage_release(old.lease_id)
            new_lease = self.acquire(
                staff_id=to_staff_id,
                channel_id=old.channel_id,
                position=old.position,
                slot=old.slot,
                ttl_minutes=ttl,
                reason=HANDOVER,
                request_id=request_id,
                now=now,
            )
        except Exception:
            if not staged:
                self.rollback()
            raise
        if not staged:
            self.commit()
        return new_lease

    # ---- 等待队列 --------------------------------------------------------

    def enqueue(self, *, position: str, slot: str, staff_id: str | None = None,
                channel_id: str | None = None, escalate_after: int = DEFAULT_ESCALATE_MINUTES,
                ttl_minutes: int = DEFAULT_TTL_MINUTES, request_id: str = "", now=None) -> WaitEntry:
        ts = parse_ts(now) if now else self.clock.now()
        if request_id and (
            request_id in self._request_ids or request_id in self._request_store()
        ):
            raise LeaseConflictError(f"请求 {request_id} 已处理")
        entry = WaitEntry(
            wait_id=f"wait-{uuid.uuid4().hex[:12]}",
            position=position,
            slot=slot,
            staff_id=staff_id,
            channel_id=channel_id,
            enqueued_at=iso(ts),
            escalate_at=iso(ts + timedelta(minutes=escalate_after)),
            expires_at=iso(ts + timedelta(minutes=ttl_minutes)),
        )
        store = self._stage.entries if self._stage is not None else self._wait
        store[entry.wait_id] = entry
        if self._stage is None:
            self._wait_order.append(entry.wait_id)
        else:
            self._stage.order.append(entry.wait_id)
        if request_id:
            self._request_store().add(request_id)
        return entry

    def fulfill(self, wait_id: str, lease: Lease) -> None:
        entry = self._wait.get(wait_id)
        if entry is None or entry.state not in {WaitState.WAITING, WaitState.ESCALATED}:
            raise LeaseConflictError(f"等待请求 {wait_id} 不在可满足状态")
        entry.state = WaitState.FULFILLED
        entry.fulfilled_by = lease.lease_id

    def cancel_wait(self, wait_id: str) -> None:
        entry = self._wait[wait_id]
        if entry.state in {WaitState.WAITING, WaitState.ESCALATED}:
            entry.state = WaitState.CANCELLED

    def waiting(self, position: str | None = None, slot: str | None = None) -> list[WaitEntry]:
        out = []
        for wid in self._wait_order:
            e = self._wait[wid]
            if e.state not in {WaitState.WAITING, WaitState.ESCALATED}:
                continue
            if position is not None and e.position != position:
                continue
            if slot is not None and e.slot != slot:
                continue
            out.append(e)
        return out

    def tick(self, now=None) -> dict[str, list]:
        """推进时间：过期租约失效，等待项升级或过期。返回升级/过期清单。"""
        ts = parse_ts(now) if now else self.clock.now()
        expired_leases: list[Lease] = []
        escalations: list[WaitEntry] = []
        expired_waits: list[WaitEntry] = []
        for lease in self._leases.values():
            if lease.state == LeaseState.HELD and parse_ts(lease.expires_at) <= ts:
                lease.state = LeaseState.EXPIRED
                expired_leases.append(lease)
        for wid in self._wait_order:
            entry = self._wait[wid]
            if entry.state in {WaitState.FULFILLED, WaitState.CANCELLED}:
                continue
            if parse_ts(entry.expires_at) <= ts:
                entry.state = WaitState.EXPIRED
                expired_waits.append(entry)
            elif entry.state == WaitState.WAITING and parse_ts(entry.escalate_at) <= ts:
                entry.state = WaitState.ESCALATED
                entry.escalations += 1
                escalations.append(entry)
        return {
            "expired_leases": expired_leases,
            "escalations": escalations,
            "expired_waits": expired_waits,
        }

    # ---- 读取 -----------------------------------------------------------

    def active_leases(self, slot: str | None = None, now=None) -> list[Lease]:
        ts = parse_ts(now) if now else self.clock.now()
        out = [l for l in self._leases.values() if l.active_at(ts)]
        if slot is not None:
            out = [l for l in out if l.slot == slot]
        return sorted(out, key=lambda l: (l.position, l.channel_id))

    def lease_for(self, channel_id: str, slot: str, now=None) -> Lease | None:
        for lease in self.active_leases(slot, now):
            if lease.channel_id == channel_id:
                return lease
        return None

    def staff_lease(self, staff_id: str, slot: str, now=None) -> Lease | None:
        for lease in self.active_leases(slot, now):
            if lease.staff_id == staff_id:
                return lease
        return None

    def occupied_channels(self, slot: str, now=None) -> set[str]:
        return {l.channel_id for l in self.active_leases(slot, now)}

    def invalidated_channels(self) -> set[str]:
        return {l.channel_id for l in self._leases.values() if l.state == LeaseState.INVALIDATED}

    def invalidate_channel(self, channel_id: str, *, reason: str = "设备检修", now=None) -> list[Lease]:
        """通道被迫停用时强制失效其全部有效租约，释放被占用人员。"""
        ts = parse_ts(now) if now else self.clock.now()
        out = []
        for lease in self._leases.values():
            if lease.channel_id == channel_id and lease.active_at(ts):
                lease.state = LeaseState.INVALIDATED
                lease.reason = f"{lease.reason}+{reason}"
                out.append(lease)
        return out

    # ---- 快照 -----------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "leases": [l.__dict__ | {"state": l.state.value} for l in self._leases.values()],
            "wait": [w.__dict__ | {"state": w.state.value} for w in self._wait.values()],
            "wait_order": list(self._wait_order),
            "request_ids": sorted(self._request_ids),
        }

    def restore(self, data: dict) -> None:
        self._leases = {}
        for item in data.get("leases", []):
            item = dict(item)
            item["state"] = LeaseState(item["state"])
            self._leases[item["lease_id"]] = Lease(**item)
        self._wait = {}
        for item in data.get("wait", []):
            item = dict(item)
            item["state"] = WaitState(item["state"])
            self._wait[item["wait_id"]] = WaitEntry(**item)
        self._wait_order = [w for w in data.get("wait_order", []) if w in self._wait]
        self._request_ids = set(data.get("request_ids", []))
