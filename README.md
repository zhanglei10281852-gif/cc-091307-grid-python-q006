# 社区志愿服务时长登记系统

志愿者协会换届时统一服务时长口径的登记与审核系统。纯 Python 3.11 +
标准库 `sqlite3`，无第三方运行依赖。

## 口径（计算规则）

- **按活动有效状态计时**：仅 `active` / `completed` 活动下、且记录状态为
  `approved` 的时段计入汇总；`planned`、`cancelled`（含取消后已审核的旧记录）、
  待复核、被驳回、被冻结的记录一律不计，并在结果的 `excluded` 中说明原因。
- **跨午夜按实际时段**：时间一律使用 epoch 时间戳，有效时长取
  `[签到, 签退]` 与 `[活动开始, 活动结束]` 的交集
  `min(签退,活动结束) − max(签到,活动开始)`，跨午夜无需特判，超出活动窗口的
  上报部分自动裁剪。
- **不累加**：同活动同志愿者的重复现场签到直接拒绝；批量导入以
  `(活动, 志愿者, import_key)` 为幂等键，重复导入返回 `duplicates` 而不产生
  新时长。
- **补录与修改留痕**：补录、导入、修正都必须填写原因；修正后记录回退到
  `pending` 须重新复核，版本号 +1；提交人与复核人不得为同一人。
- **冻结不删除**：管理员可冻结/解冻争议记录（须填原因），冻结期间移出汇总且
  禁止修改，但原始数据、复核链、审计全部保留；系统不提供任何删除接口。

## 角色

| 角色 | 权限 |
| --- | --- |
| 志愿者 | 自助签到签退、本人补录、查看本人明细/复核链/时长、对本人记录提异议 |
| 审核员 | 仅能处理 `authorized_org_ids` 内组织的活动：岗位、补录、导入、复核、异议 |
| 管理员 | 用户/小组/活动建档、活动状态变更、冻结争议记录、全量审计查询 |

## 主要接口（`src.service.VolunteerService`）

- 档案：`register_user` / `register_group` / `create_activity` /
  `set_activity_status` / `assign_position`
- 打卡：`check_in` / `check_out`
- 补录导入：`submit_backfill` / `import_entries` / `correct_entry`
- 复核：`approve_entry` / `reject_entry` / `get_review_chain`
- 异议：`file_dispute` / `list_disputes` / `resolve_dispute`
- 冻结：`freeze_entry` / `unfreeze_entry`
- 汇总（返回 `HoursBreakdown`，含 `components` 计算依据与 `excluded` 排除原因）：
  `volunteer_hours` / `group_hours`
- 明细与审计：`list_my_entries` / `query_audit`

汇总结果示例：

```json
{
  "hours": 6.0,
  "components": [{
    "entry_id": "...", "activity_status": "active",
    "check_in": 1768003600.0, "check_out": 1768025200.0,
    "raw_hours": 6.0, "effective_hours": 6.0,
    "calc": "min(签退,活动结束)-max(签到,活动开始)"
  }],
  "excluded": [{ "entry_id": "...", "excluded_reason": "活动状态为 cancelled，仅 active/completed 计时长" }]
}
```

## 持久化与恢复

所有数据落在单个 SQLite 文件（构造时传入路径，默认 `:memory:`）：
`entries.frozen`、`reviews`（复核链）、`disputes`、`audit_log` 均已落盘，
进程重启后重新打开同一文件即可恢复冻结标记、审核链、异议状态与授权范围
（见 `tests/test_service.py::test_restart_recovers_freeze_and_review_chain`）。

## 运行测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install pytest
python -m pytest tests/ -q
```
