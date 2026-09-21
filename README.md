# 社区志愿服务时长登记系统

面向志愿者协会/社区网格的志愿服务时长登记与审计系统，解决换届时“各小组时长口径不一、
取消活动计入总时长、表彰名单争议”的问题：统一按**活动有效状态**计算时长，全部变更
留痕可审计，争议记录可冻结但原始数据不可删除。

运行环境：Python 3.11（仅标准库，无第三方依赖）。代码位于 `src` 目录。

## 快速开始

```bash
python3 -m unittest discover -s tests -v   # 运行测试（43 个用例）
python3 examples/demo.py                   # 端到端演示（含争议处理与重启恢复）
```

```python
from src import VolunteerService, Role, ActivityStatus

svc = VolunteerService("data/volunteer.db")          # SQLite 持久化，重启后状态恢复
admin = svc.create_user(None, "管理员", Role.ADMIN)   # 首个用户引导为管理员
org = svc.create_organization(admin, "巡防一组")
rev = svc.create_user(admin, "审核员", Role.REVIEWER)
svc.grant_reviewer_scope(admin, rev, org)            # 审核员按组织授权
vol = svc.create_user(admin, "小李", Role.VOLUNTEER)
svc.add_membership(admin, vol, org)

act = svc.create_activity(rev, org, "春节夜巡",
                          "2026-01-01T22:00:00+08:00", "2026-01-02T02:00:00+08:00")
rid = svc.check_in(vol, act, vol, at="2026-01-01T23:00:00+08:00")
svc.check_out(vol, rid, at="2026-01-02T01:30:00+08:00")   # 跨午夜按实际时段计 2.5h
svc.volunteer_summary(vol, vol)["total_hours"]            # -> 2.5
```

## 时长口径（统一规则）

计入时长的记录必须同时满足：活动未取消、记录已生效（`EFFECTIVE`）、未冻结、已签退。
汇总返回 `entries`（计入明细：合并后的时段、来源记录、小时数）与 `excluded`
（排除明细及原因码），原因码包括：

| 原因码 | 含义 |
| --- | --- |
| `activity_cancelled` | 活动已取消 |
| `pending_review` | 补录待复核 |
| `record_rejected` | 复核驳回 |
| `frozen` | 管理员冻结中 |
| `incomplete` | 已签到未签退 |

- **跨午夜**：按实际签到/签退时间戳计算；统计区间与记录时段取交集。
- **重复签到**：同一志愿者在同一活动有未签退记录时，重复签到幂等返回原记录。
- **重复导入**：批次按内容哈希幂等（重复导入返回原批次结果），行级按 `source_ref`
  唯一约束去重；同一志愿者同一活动的重叠时段在汇总时合并，绝不累加。

## 角色与权限

| 能力 | 志愿者 | 审核员 | 管理员 |
| --- | --- | --- | --- |
| 签到签退、查看本人明细、提出异议、申请补录/修改 | ✔（本人） | ✔（代录） | ✔ |
| 复核补录/修改、处理异议、批量导入、管理活动与岗位 | — | ✔（仅授权组织） | ✔ |
| 冻结/解冻争议记录、授权审核员、审计查询 | — | — | ✔ |

- 补录/修改**必须填写原因**；志愿者提交后进入待复核，授权审核员或管理员
  提交则创建即复核（均留痕）。
- 审核员只能处理被授权组织（`reviewer_scopes`）内的记录，越权抛出
  `PermissionDeniedError`。
- 系统不提供任何删除接口：驳回、冻结、取消都只是状态标记，原始数据与
  审计日志（`audit_log`，追加式）永久保留。

## 主要接口

- 活动与岗位：`create_activity` / `set_activity_status`（取消需原因）/ `add_position`
- 考勤：`check_in` / `check_out` / `backfill_record` / `import_records` /
  `request_amendment` / `approve_record` / `reject_record` /
  `approve_amendment` / `reject_amendment`
- 异议：`file_objection` / `resolve_objection` / `list_objections`
- 冻结：`freeze_record` / `unfreeze_record`（需原因，仅管理员）
- 查询：`volunteer_summary` / `group_summary`（成员与活动两个维度）/
  `get_record`（记录 + 修改申请 + 异议 + 审计轨迹的完整审核链）/
  `audit_query`（按操作者/动作/实体/时间过滤，仅管理员）

## 持久化与重启恢复

全部状态（含冻结标记、复核状态、异议、审计日志）存储于 SQLite
（`db_path`，`:memory:` 可用于测试）。进程重启后用同一路径重新实例化
`VolunteerService` 即可恢复，无需额外流程；`PersistenceTest` 覆盖了该场景。

## 目录结构

```
src/
  __init__.py   # 公共导出
  models.py     # 状态/角色枚举与排除原因码
  errors.py     # 业务异常体系
  db.py         # SQLite 连接与表结构
  service.py    # VolunteerService 外观（全部业务逻辑）
tests/test_volunteer_service.py  # 43 个行为测试
examples/demo.py                 # 争议场景端到端演示
```
