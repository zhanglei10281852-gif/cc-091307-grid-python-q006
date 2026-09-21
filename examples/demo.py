"""端到端演示：从“口径不一的表彰争议”到“可审计的时长口径”。

场景还原：
1. 两个小组各自开展活动，其中一组把已取消活动的时长也报了上去；
2. 系统按活动有效状态统一口径：取消的活动不计入；
3. 志愿者补录必须说明原因并经授权审核员复核；
4. 重复签到、重复导入不累加；
5. 志愿者对明细提出异议，管理员冻结争议记录（原始数据保留）；
6. 异议处理完毕解冻，时长恢复计入；
7. 进程重启后冻结标记与审核链仍可恢复。

运行：python3 examples/demo.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import ActivityStatus, Role, VolunteerService  # noqa: E402

UTC = timezone.utc


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    if isinstance(payload, (dict, list)):
        import json

        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)


def main() -> None:
    db_path = Path(tempfile.gettempdir()) / "volunteer_demo.db"
    if db_path.exists():
        db_path.unlink()
    svc = VolunteerService(db_path)

    # ---- 初始化：协会、两个小组、审核员、志愿者 --------------------------
    admin = svc.create_user(None, "协会管理员", Role.ADMIN)
    org1 = svc.create_organization(admin, "巡防一组")
    org2 = svc.create_organization(admin, "巡防二组")
    rev1 = svc.create_user(admin, "一组审核员", Role.REVIEWER)
    svc.grant_reviewer_scope(admin, rev1, org1)
    xiaoli = svc.create_user(admin, "小李", Role.VOLUNTEER)
    xiaowang = svc.create_user(admin, "小王", Role.VOLUNTEER)
    svc.add_membership(admin, xiaoli, org1)
    svc.add_membership(admin, xiaowang, org1)

    # ---- 一组的夜巡活动（跨午夜），小李实际服务 --------------------------
    night = svc.create_activity(rev1, org1, "春节夜巡", dt(1, 22), dt(2, 2))
    guide = svc.add_position(rev1, night, "路口引导岗", quota=2)
    rid = svc.check_in(xiaoli, night, xiaoli, position_id=guide, at=dt(1, 23))
    svc.check_out(xiaoli, rid, at=dt(2, 1, 30))
    show("跨午夜活动按实际时段计算（23:00 - 次日 01:30）",
         svc.volunteer_summary(xiaoli, xiaoli))

    # ---- 一组的取消活动：历史系统把它计入了，本系统不计 ------------------
    cancelled = svc.create_activity(rev1, org1, "因雪取消的植树", dt(5, 9), dt(5, 11))
    svc.import_records(rev1, [
        {"source_ref": "old-sys-0001", "activity_id": cancelled, "volunteer_id": xiaoli,
         "check_in": dt(5, 9), "check_out": dt(5, 11)},
    ])
    svc.set_activity_status(rev1, cancelled, ActivityStatus.CANCELLED, "暴雪橙色预警")
    summary = svc.volunteer_summary(xiaoli, xiaoli)
    show("取消活动的导入记录被排除（计算依据随汇总返回）", summary["excluded"])

    # ---- 重复导入同一批历史数据：幂等，不累加 ----------------------------
    again = svc.import_records(rev1, [
        {"source_ref": "old-sys-0001", "activity_id": cancelled, "volunteer_id": xiaoli,
         "check_in": dt(5, 9), "check_out": dt(5, 11)},
    ])
    show("重复导入返回原批次", {"already_imported": again["already_imported"],
                              "inserted_count": again["inserted_count"]})

    # ---- 小王补录：说明原因 + 审核员复核 ---------------------------------
    pending = svc.backfill_record(xiaowang, night, xiaowang, dt(1, 23), dt(2, 1),
                                  "签到机故障，现场负责人已确认")
    print(f"\n补录复核前小王总时长: {svc.volunteer_summary(xiaowang, xiaowang)['total_hours']}h")
    svc.approve_record(rev1, pending, "与值班日志核对一致")
    print(f"补录复核后小王总时长: {svc.volunteer_summary(xiaowang, xiaowang)['total_hours']}h")

    # ---- 年度表彰名单争议：小李对取消活动的口径提出异议 -------------------
    obj = svc.file_objection(xiaoli, svc.volunteer_summary(xiaoli, xiaoli)["excluded"][0]["record_id"],
                             "该活动已取消，历史系统却计入了 2 小时，请复核口径")
    # 管理员冻结争议记录（原始数据保留，冻结期间不计入）
    svc.freeze_record(admin, rid, "表彰名单争议，冻结夜巡记录待核查")
    show("冻结期间小李时长汇总", svc.volunteer_summary(xiaoli, xiaoli))

    # 核查结论：异议成立，先解冻，再由审核员按复核链更正签退时间
    svc.resolve_objection(rev1, obj, upheld=True, note="口径已统一：取消活动一律不计入")
    svc.unfreeze_record(admin, rid, "核查完毕，恢复计入")
    svc.request_amendment(rev1, rid, dt(1, 23), dt(2, 2), "按巡查签到表更正签退时间")
    show("解冻并更正后小李时长汇总", svc.volunteer_summary(xiaoli, xiaoli))

    # ---- 小组汇总与审计 --------------------------------------------------
    show("一组时长汇总（成员/活动两个维度）", svc.group_summary(rev1, org1))
    show("争议记录的完整审核链",
         [(e["action"], e["reason"]) for e in svc.get_record(admin, rid)["audit_trail"]])
    svc.close()

    # ---- 进程重启：冻结/复核链从 SQLite 恢复 ------------------------------
    svc2 = VolunteerService(db_path)
    svc2.freeze_record(admin, rid, "换届复核抽查")
    svc2.close()
    svc3 = VolunteerService(db_path)
    rec = svc3.get_record(admin, rid)
    show("重启后冻结标记与审核链仍可恢复",
         {"frozen": bool(rec["record"]["frozen"]),
          "audit_actions": [e["action"] for e in rec["audit_trail"]]})
    svc3.close()
    print(f"\n演示数据文件: {db_path}")


if __name__ == "__main__":
    main()
