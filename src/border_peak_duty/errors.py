"""领域错误：调度系统对外抛出的全部异常。"""

from __future__ import annotations


class PeakDutyError(Exception):
    """所有领域错误的基类。"""


class StaleEventError(PeakDutyError):
    """事件版本过旧，或航班已离场终态后收到占用性动态。"""


class DuplicateEventError(PeakDutyError):
    """同一事件版本重复投递（幂等忽略时由调用方决定是否抛出）。"""


class ForecastValidationError(PeakDutyError):
    """预测版本缺少时间槽或版本不合法。"""


class LeaseConflictError(PeakDutyError):
    """岗位租约冲突：人员冲突、通道停用、资质不符或租约不可变。"""


class PublishError(PeakDutyError):
    """调度发布失败，所有拟议变更必须回滚。"""


class CaseRuleError(PeakDutyError):
    """分流案件违反隔离或批准规则。"""


class SlotClosedError(PeakDutyError):
    """案件所在查验阶段已经关闭，旅客必须重新排队。"""
