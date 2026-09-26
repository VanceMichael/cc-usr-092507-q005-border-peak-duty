# 口岸高峰勤务与异常分流

编排机场口岸高峰期的通道、人员、设备和异常查验。系统把航班计划及后续动态、
分时客流预测、通道能力、设备状态和人员资质汇入同一条分时时间线，自动产出每个
时段的开放通道、岗位组合、备用容量与触发原因；分流案件最小留痕、阶段隔离、
发起人回避；调配、交接、设备恢复共同遵守岗位租约，发布失败不留半生效状态；
重启后等待队列、租约与升级期限凭事件日志继续有效。

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `models.py` | 领域枚举与数据类：航班动态、预测版本、设备状态、通道、人员资质、租约、案件 |
| `timeline.py` | 统一时间线：乱序安全的版本化航班动态、分时预测、设备能力折减、时段需求合并 |
| `cases.py` | 等待队列（旅客唯一、不重不漏）与分流案件（最小信息、三阶段隔离、回避、升级期限） |
| `scheduler.py` | 规划器（开放/备用/触发原因）与岗位租约的原子发布、交接、检修收回、恢复重租 |
| `journal.py` | JSONL 事务事件日志：整批写入，重启重放 |
| `service.py` | 应用服务与值班接口：受理、查验、分流、发布、交接、离场清扫、`duty_snapshot` |
| `context.py` | 领域资料读取校验 |

## 关键规则

- **乱序航班**：动态按 `revision` 取最高版本，同一 `event_id` 重发幂等；
  `departed`/`cancelled` 终态锁定后，任何迟到动态（即使版本更高）都不能让已离场
  航班重新占用资源。已落地航班退出预测需求，旅客只存在于等待队列中，避免预测与
  队列双算；预测版本覆盖某时段时以预测为准、航班明细仅作成因。
- **不重不漏**：等待队列以旅客编号为唯一索引，同一旅客不能两次入队或同时存在于
  两个队列；航班离场时清扫从未到场的旅客，正在查验/已结案者不受影响。
- **分流案件**：只存不透明旅客代号与标准原因代码（证件失效、材料不足等），不落
  姓名、证件号、自由文本；普通查验 → 二线复核 → 最终放行三阶段隔离且分别留痕；
  发起人在复核与放行阶段一律回避；案件有升级期限，重启后期限继续有效。
- **岗位租约**：人员与通道按时段互斥；发布时先预占租约、再联动开通道、最后整批
  落日志——通道联动失败释放全部预占，日志失败撤回已开通道，绝不留下“人已占、
  通道未开”。交接班、设备检修收回、恢复后重租都走同一租约规则。
- **值班接口**：`duty_snapshot(bucket, now)` 返回拥堵源（能力缺口/设备不可用/
  人力受限/积压）、当前开放与备用能力、以及脱敏后的分流决定链。

## 使用示例

```python
from border_peak_duty import (
    PeakDutyService, Timeline, Lane, Qualification,
    FlightEvent, FlightStatus, ForecastVersion, DeviceState, DeviceStatus,
)

service = PeakDutyService(Timeline(bucket_minutes=60), journal_path="run/journal.jsonl")
service.register_lane(Lane("C1", "chinese", "DEV-C1", base_capacity=80))
service.register_staff(Qualification("S01", frozenset({"primary"}), frozenset({"chinese"})))

service.scheduler.apply_forecast(
    ForecastVersion(2, "2026-10-01T06:00", {"2026-10-01T09:00": 260}, "双节上调")
)
plan = service.publish_bucket("2026-10-01T09:00", "2026-10-01T08:55")
# plan: open_lanes / reserve_lanes / reserve_staff / spare_capacity / triggers

# 重启后：
service.close()
service = PeakDutyService(Timeline(bucket_minutes=60), journal_path="run/journal.jsonl")
service.restore()   # 等待队列、租约、案件、升级期限全部重建
```

## 开发命令

- 运行测试：`python3 -m unittest discover -s tests -v`
- 编译检查：`python3 -m compileall -q src`

上述命令只读取仓库内资料，不需要连接外部业务服务。28 个自动化测试覆盖乱序/幂等、
终态锁定、不重不漏、原子发布两类失败回滚、三阶段隔离与回避、升级期限，以及
“高峰一日 → 重启恢复”的端到端链路。
