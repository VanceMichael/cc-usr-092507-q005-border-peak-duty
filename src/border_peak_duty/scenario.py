"""标准高峰场景构造器：通道、设备与资质人员。

所有数据均为虚构，不含真实身份信息。
"""

from __future__ import annotations

from .resources import (
    AUTO,
    BIO_DEVICE,
    DESK,
    DOC_SCANNER,
    GATE,
    MANUAL,
    MANUAL_BOOTH,
    PRIMARY,
    RELEASE,
    SECONDARY,
    Channel,
    Device,
    ResourcePool,
    Staff,
)


def _auto_channel(idx: int, rate: int = 120) -> Channel:
    cid = f"A{idx:02d}"
    channel = Channel(cid, f"自助通道{idx}", AUTO, rate)
    for kind in (GATE, DOC_SCANNER, BIO_DEVICE):
        did = f"D-{cid}-{kind}"
        channel.devices[did] = Device(did, kind, cid)
    return channel


def _manual_channel(idx: int, rate: int = 90) -> Channel:
    cid = f"M{idx:02d}"
    channel = Channel(cid, f"人工通道{idx}", MANUAL, rate)
    for kind in (DOC_SCANNER, MANUAL_BOOTH):
        did = f"D-{cid}-{kind}"
        channel.devices[did] = Device(did, kind, cid)
    return channel


def _desk(position: str, idx: int, rate: int = 40) -> Channel:
    cid = f"{position[0]}{idx:02d}"
    return Channel(cid, f"{position}台位{idx}", DESK, rate, position=position)


def build_pool(
    *,
    auto: int = 6,
    manual: int = 3,
    secondary: int = 2,
    release_desks: int = 1,
    primary_officers: int = 12,
    secondary_officers: int = 4,
    release_officers: int = 3,
) -> ResourcePool:
    pool = ResourcePool()
    for i in range(1, auto + 1):
        pool.add_channel(_auto_channel(i))
    for i in range(1, manual + 1):
        pool.add_channel(_manual_channel(i))
    for i in range(1, secondary + 1):
        pool.add_channel(_desk(SECONDARY, i))
    for i in range(1, release_desks + 1):
        pool.add_channel(_desk(RELEASE, i))

    for i in range(1, primary_officers + 1):
        pool.add_staff(Staff(f"P{i:03d}", f"查验员{i:02d}", frozenset({PRIMARY}), team="PRIMARY"))
    for i in range(1, secondary_officers + 1):
        pool.add_staff(Staff(
            f"S{i:03d}", f"复核员{i:02d}", frozenset({PRIMARY, SECONDARY}), team="SECONDARY"
        ))
    for i in range(1, release_officers + 1):
        pool.add_staff(Staff(
            f"R{i:03d}", f"放行员{i:02d}", frozenset({RELEASE}), team="RELEASE"
        ))
    return pool
