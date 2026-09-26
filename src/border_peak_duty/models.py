"""高峰保障调度系统的领域模型与异常。

时间一律使用可比较的时间戳字符串（如 ``2026-10-01T08:00``）或可转为
``datetime`` 的对象；系统内部统一存 ISO 字符串，按字典序即可正确排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """所有可预期的领域规则冲突的基类。"""


class StaleEventError(DomainError):
    """事件版本过期或航班已处于终态，拒绝应用。"""


class DuplicateEventError(DomainError):
    """同一动态（相同来源版本）重复送达，调用方应视为幂等成功而非错误。

    系统仍会记录送达事实，但不产生新的状态版本。
    """


class QualificationError(DomainError):
    """人员资质不满足岗位要求。"""


class PublishConflict(DomainError):
    """岗位租约发布冲突（通道不可用、租约被占、发布器失败等）。"""


class SeparationError(DomainError):
    """违反查验/复核/放行阶段隔离或发起人回避规则。"""


class EscalationError(DomainError):
    """分流案件超过升级期限仍未处置。"""


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class FlightStatus(str, Enum):
    SCHEDULED = "scheduled"      # 计划到达
    DELAYED = "delayed"          # 延误（新 ETA）
    ARRIVED = "arrived"          # 已落地
    CLOSED = "closed"            # 航班保障关闭，旅客清空
    DEPARTED = "departed"        # 已离场（终态）
    CANCELLED = "cancelled"      # 取消（终态）

    @property
    def is_terminal(self) -> bool:
        return self in (FlightStatus.DEPARTED, FlightStatus.CANCELLED)


class DeviceStatus(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"        # 降效，能力按比例折减
    MAINTENANCE = "maintenance"  # 检修，通道不可用
    OFFLINE = "offline"          # 故障，通道不可用


class CaseStage(str, Enum):
    SCREEN = "screen"     # 普通查验
    REVIEW = "review"     # 二线复核
    RELEASE = "release"   # 最终放行

    def next(self) -> "CaseStage":
        if self is CaseStage.SCREEN:
            return CaseStage.REVIEW
        if self is CaseStage.REVIEW:
            return CaseStage.RELEASE
        raise SeparationError("放行阶段之后没有下一阶段")


class CaseState(str, Enum):
    OPEN = "open"
    HELD = "held"             # 等待补材料
    ESCALATED = "escalated"   # 超升级期限
    RESOLVED = "resolved"     # 放行
    REJECTED = "rejected"     # 拒绝入境/退运


class LaneKind(str, Enum):
    CHINESE = "chinese"
    FOREIGNER = "foreigner"
    SPECIAL = "special"   # 外交/特殊通道，也作分流备用
    STAFF = "staff"


class QueueState(str, Enum):
    WAITING = "waiting"
    SERVING = "serving"
    DONE = "done"
    DIVERTED = "diverted"   # 转入分流案件
    ABANDONED = "abandoned"  # 航班离场时未到场旅客


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class Qualification:
    """人员资质：可上岗的岗位集合与通道类型集合。"""

    staff_id: str
    positions: frozenset[str]          # 如 primary / verifier / escort
    lane_kinds: frozenset[str]         # 可服务的通道类型
    secondary_review: bool = False     # 是否具备二线复核权
    release_authority: bool = False    # 是否具备最终放行权

    def can_serve(self, position: str, lane_kind: str) -> bool:
        return position in self.positions and lane_kind in self.lane_kinds

    def to_dict(self) -> dict[str, Any]:
        return {
            "staff_id": self.staff_id,
            "positions": sorted(self.positions),
            "lane_kinds": sorted(self.lane_kinds),
            "secondary_review": self.secondary_review,
            "release_authority": self.release_authority,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Qualification":
        return cls(
            staff_id=value["staff_id"],
            positions=frozenset(value["positions"]),
            lane_kinds=frozenset(value["lane_kinds"]),
            secondary_review=value.get("secondary_review", False),
            release_authority=value.get("release_authority", False),
        )


@dataclass
class Lane:
    """查验通道及其绑定设备。"""

    lane_id: str
    kind: str
    device_id: str
    base_capacity: int                 # 每时段满负荷查验人数
    required_position: str = "primary"  # 开放所需的主岗位

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "kind": self.kind,
            "device_id": self.device_id,
            "base_capacity": self.base_capacity,
            "required_position": self.required_position,
        }


@dataclass
class FlightEvent:
    """航班动态事件。``event_id`` 相同的重发只更新同一版本，绝不新增。"""

    flight_no: str
    event_id: str
    revision: int                     # 同一航班内严格递增
    status: str
    occurred_at: str
    scheduled_arrival: str
    pax: int = 0
    revised_arrival: str | None = None  # 延误后的新 ETA
    delay_minutes: int = 0
    source: str = "airport"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "flight_no": self.flight_no,
            "event_id": self.event_id,
            "revision": self.revision,
            "status": self.status,
            "occurred_at": self.occurred_at,
            "scheduled_arrival": self.scheduled_arrival,
            "pax": self.pax,
            "revised_arrival": self.revised_arrival,
            "delay_minutes": self.delay_minutes,
            "source": self.source,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FlightEvent":
        return cls(**value)


@dataclass
class ForecastVersion:
    """分时客流预测的一个版本。"""

    version: int
    issued_at: str
    buckets: dict[str, int] = field(default_factory=dict)  # bucket_start -> 人数
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "issued_at": self.issued_at,
            "buckets": dict(self.buckets),
            "reason": self.reason,
        }


@dataclass
class DeviceState:
    device_id: str
    status: str
    capacity_ratio: float = 1.0
    updated_at: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "status": self.status,
            "capacity_ratio": self.capacity_ratio,
            "updated_at": self.updated_at,
            "reason": self.reason,
        }
