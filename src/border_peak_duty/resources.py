"""资源台账：通道、通道设备与人员资质。

通道能力受设备状态约束：任一必需设备停修，通道能力降为 0（停用）；
人员能否上某岗位由其资质集合决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# 岗位
PRIMARY = "PRIMARY"        # 普通查验岗
SECONDARY = "SECONDARY"    # 二线复核岗
RELEASE = "RELEASE"        # 最终放行岗
POSITIONS = (PRIMARY, SECONDARY, RELEASE)

# 设备
GATE = "GATE"              # 自助闸机
DOC_SCANNER = "DOC_SCANNER"
BIO_DEVICE = "BIO_DEVICE"  # 生物信息采集
MANUAL_BOOTH = "MANUAL_BOOTH"

# 单元类型
AUTO = "AUTO"
MANUAL = "MANUAL"
DESK = "DESK"              # 二线复核 / 最终放行台位（无通道设备）

REQUIRED_DEVICES: dict[str, frozenset[str]] = {
    AUTO: frozenset({GATE, DOC_SCANNER, BIO_DEVICE}),
    MANUAL: frozenset({DOC_SCANNER, MANUAL_BOOTH}),
    DESK: frozenset(),
}

# 各类单元默认承载的岗位
UNIT_POSITION = {
    AUTO: PRIMARY,
    MANUAL: PRIMARY,
    DESK: None,  # 台位在构造时显式指定 SECONDARY / RELEASE
}


class DeviceStatus(str, Enum):
    UP = "UP"
    DEGRADED = "DEGRADED"  # 降效运行：能力打折但不停用
    DOWN = "DOWN"          # 检修停用


@dataclass(frozen=True)
class Device:
    device_id: str
    kind: str
    channel_id: str
    status: DeviceStatus = DeviceStatus.UP
    efficiency: float = 1.0  # DEGRADED 时 < 1

    @property
    def available(self) -> bool:
        return self.status is not DeviceStatus.DOWN

    def capacity_factor(self) -> float:
        if self.status is DeviceStatus.DOWN:
            return 0.0
        if self.status is DeviceStatus.DEGRADED:
            return max(0.1, min(1.0, self.efficiency))
        return 1.0


@dataclass
class Channel:
    channel_id: str
    name: str
    kind: str                       # AUTO / MANUAL / DESK
    base_rate: int                  # 每槽基础通过能力（人次/槽）
    devices: dict[str, Device] = field(default_factory=dict)
    position: str | None = None     # DESK 台位指定 SECONDARY / RELEASE；查验通道为 PRIMARY

    def serves(self) -> str:
        """该单元承载的岗位。"""
        if self.kind == DESK:
            if self.position not in (SECONDARY, RELEASE):
                raise ValueError(f"台位 {self.channel_id} 未指定合法岗位")
            return self.position
        return PRIMARY

    def required_kinds(self) -> frozenset[str]:
        return REQUIRED_DEVICES.get(self.kind, frozenset())

    def missing_required(self) -> list[str]:
        present = {d.kind for d in self.devices.values()}
        return [k for k in sorted(self.required_kinds()) if k not in present]

    def is_openable(self) -> bool:
        """设备齐全且没有必需设备处于检修停用状态。"""
        if self.missing_required():
            return False
        return all(
            d.available for d in self.devices.values() if d.kind in self.required_kinds()
        )

    def effective_rate(self) -> float:
        """按设备状态折算的实际能力。"""
        factor = 1.0
        for d in self.devices.values():
            if d.kind in self.required_kinds():
                factor = min(factor, d.capacity_factor())
        return self.base_rate * factor

    def status_reason(self) -> str | None:
        missing = self.missing_required()
        if missing:
            return f"缺少必需设备：{','.join(missing)}"
        down = [d.kind for d in self.devices.values()
                if d.kind in self.required_kinds() and not d.available]
        if down:
            return f"设备检修停用：{','.join(sorted(down))}"
        return None


@dataclass(frozen=True)
class Staff:
    staff_id: str
    name: str
    qualifications: frozenset[str]
    team: str = ""

    def qualified_for(self, position: str) -> bool:
        return position in self.qualifications


class ResourcePool:
    """通道、设备、人员的内存台账。"""

    def __init__(
        self,
        channels: dict[str, Channel] | None = None,
        staff: dict[str, Staff] | None = None,
    ) -> None:
        self.channels: dict[str, Channel] = dict(channels or {})
        self.staff: dict[str, Staff] = dict(staff or {})

    def add_channel(self, channel: Channel) -> None:
        self.channels[channel.channel_id] = channel

    def add_staff(self, staff_member: Staff) -> None:
        self.staff[staff_member.staff_id] = staff_member

    def update_device(self, device_id: str, status: DeviceStatus, efficiency: float = 1.0) -> Device:
        for channel in self.channels.values():
            if device_id in channel.devices:
                old = channel.devices[device_id]
                new = Device(old.device_id, old.kind, old.channel_id, status, efficiency)
                channel.devices[device_id] = new
                return new
        raise KeyError(f"未知设备：{device_id}")

    def get_device(self, device_id: str) -> Device | None:
        for channel in self.channels.values():
            if device_id in channel.devices:
                return channel.devices[device_id]
        return None

    def qualified_staff(self, position: str) -> list[Staff]:
        return [s for s in self.staff.values() if s.qualified_for(position)]
