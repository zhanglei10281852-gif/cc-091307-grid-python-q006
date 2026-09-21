"""志愿服务登记系统的行为测试。

运行方式：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import (  # noqa: E402
    ActivityStatus,
    PermissionDeniedError,
    RecordStatus,
    Role,
    Service,
    StateError,
    ValidationError,
    VolunteerService,
)

UTC = timezone.utc


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    """2026 年 1 月某日的 UTC 时间，便于构造跨午夜场景。"""
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


class BaseCase(unittest.TestCase):
    """搭建标准夹具：管理员、两个小组、一名授权审核员、两名志愿者。"""

    def setUp(self) -> None:
        self.svc = VolunteerService(":memory:")
        self.admin = self.svc.create_user(None, "管理员", Role.ADMIN)
        self.org1 = self.svc.create_organization(self.admin, "服务一组")
        self.org2 = self.svc.create_organization(self.admin, "服务二组")
        self.rev1 = self.svc.create_user(self.admin, "审核员一", Role.REVIEWER)
        self.svc.grant_reviewer_scope(self.admin, self.rev1, self.org1)
        self.vol_a = self.svc.create_user(self.admin, "志愿者甲", Role.VOLUNTEER)
        self.vol_b = self.svc.create_user(self.admin, "志愿者乙", Role.VOLUNTEER)
        self.svc.add_membership(self.admin, self.vol_a, self.org1)
        self.svc.add_membership(self.admin, self.vol_b, self.org1)

    def tearDown(self) -> None:
        self.svc.close()

    def make_activity(self, org=None, title="夜间巡逻", start=None, end=None) -> str:
        org = org or self.org1
        actor = self.rev1 if org == self.org1 else self.admin
        return self.svc.create_activity(
            actor, org, title, start or dt(1, 22), end or dt(2, 2)
        )


class BootstrapTest(unittest.TestCase):
    def test_first_user_must_be_admin(self):
        svc = VolunteerService(":memory:")
        with self.assertRaises(ValidationError):
            svc.create_user(None, "普通人", Role.VOLUNTEER)
        admin = svc.create_user(None, "管理员", Role.ADMIN)
        with self.assertRaises(PermissionDeniedError):
            svc.create_user(None, "第二个", Role.VOLUNTEER)
        svc.create_user(admin, "第二个", Role.VOLUNTEER)
        svc.close()

    def test_service_alias_keeps_ready_flag(self):
        svc = Service()
        self.assertTrue(svc.ready)
        svc.close()
        self.assertFalse(svc.ready)


class AttendanceFlowTest(BaseCase):
    def test_cross_midnight_hours_by_actual_interval(self):
        """跨午夜活动按实际时段计算：23:00 签到、次日 01:30 签退 = 2.5 小时。"""
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1, 30))
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a)
        self.assertEqual(summary["total_hours"], 2.5)
        self.assertEqual(summary["excluded"], [])
        entry = summary["entries"][0]
        self.assertEqual(entry["intervals"][0]["start"], "2026-01-01T23:00:00+00:00")
        self.assertEqual(entry["intervals"][0]["end"], "2026-01-02T01:30:00+00:00")

    def test_open_record_is_incomplete_and_counts_zero(self):
        act = self.make_activity()
        self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a)
        self.assertEqual(summary["total_hours"], 0.0)
        self.assertEqual(summary["excluded"][0]["reason"], "incomplete")

    def test_check_out_must_be_after_check_in(self):
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        with self.assertRaises(ValidationError):
            self.svc.check_out(self.vol_a, rid, at=dt(1, 22))

    def test_duplicate_check_in_is_idempotent(self):
        """重复签到返回原记录，不产生第二条记录。"""
        act = self.make_activity()
        rid1 = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        rid2 = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23, 30))
        self.assertEqual(rid1, rid2)
        self.svc.check_out(self.vol_a, rid1, at=dt(2, 1))
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a)
        self.assertEqual(summary["total_hours"], 2.0)

    def test_overlapping_records_are_merged_not_accumulated(self):
        """同一志愿者同一活动的重叠时段合并：10-12 点与 11-13 点合并为 3 小时。"""
        act = self.make_activity(start=dt(3, 8), end=dt(3, 20))
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(3, 10))
        self.svc.check_out(self.vol_a, rid, at=dt(3, 12))
        self.svc.backfill_record(
            self.rev1, act, self.vol_a, dt(3, 11), dt(3, 13), "签到机故障补录"
        )
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a)
        self.assertEqual(summary["total_hours"], 3.0)
        intervals = summary["entries"][0]["intervals"]
        self.assertEqual(len(intervals), 1)
        self.assertEqual(len(intervals[0]["record_ids"]), 2)

    def test_summary_period_clamps_to_actual_overlap(self):
        """统计区间按实际交集计算：23:00-01:00 的记录在次日区间内只算 1 小时。"""
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a, start=dt(2, 0))
        self.assertEqual(summary["total_hours"], 1.0)

    def test_check_in_rejected_for_cancelled_or_completed_activity(self):
        act = self.make_activity()
        self.svc.set_activity_status(self.rev1, act, ActivityStatus.CANCELLED, "天气原因取消")
        with self.assertRaises(StateError):
            self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))


class ActivityStatusTest(BaseCase):
    def test_cancelled_activity_excluded_from_totals(self):
        """取消的活动不计入时长，排除原因随计算依据返回。"""
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        self.svc.set_activity_status(self.rev1, act, ActivityStatus.CANCELLED, "活动取消")
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a)
        self.assertEqual(summary["total_hours"], 0.0)
        self.assertEqual(summary["excluded"][0]["reason"], "activity_cancelled")

    def test_cancel_requires_reason_and_restore_is_admin_only(self):
        act = self.make_activity()
        with self.assertRaises(ValidationError):
            self.svc.set_activity_status(self.rev1, act, ActivityStatus.CANCELLED)
        self.svc.set_activity_status(self.rev1, act, ActivityStatus.CANCELLED, "取消")
        with self.assertRaises(PermissionDeniedError):
            self.svc.set_activity_status(self.rev1, act, ActivityStatus.PLANNED, "恢复")
        self.svc.set_activity_status(self.admin, act, ActivityStatus.PLANNED, "恢复活动")

    def test_invalid_transition_rejected(self):
        act = self.make_activity()
        with self.assertRaises(StateError):
            self.svc.set_activity_status(self.rev1, act, ActivityStatus.COMPLETED)


class BackfillReviewTest(BaseCase):
    def test_backfill_requires_reason(self):
        act = self.make_activity()
        with self.assertRaises(ValidationError):
            self.svc.backfill_record(self.vol_a, act, self.vol_a, dt(1, 23), dt(2, 1), "")

    def test_volunteer_backfill_pending_until_reviewed(self):
        """志愿者补录先进入待复核，不计入；授权审核员批准后计入。"""
        act = self.make_activity()
        rid = self.svc.backfill_record(
            self.vol_a, act, self.vol_a, dt(1, 23), dt(2, 1), "忘记签到"
        )
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a)
        self.assertEqual(summary["total_hours"], 0.0)
        self.assertEqual(summary["excluded"][0]["reason"], "pending_review")

        self.svc.approve_record(self.rev1, rid, "情况属实")
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a)
        self.assertEqual(summary["total_hours"], 2.0)

    def test_reviewer_scope_enforced_on_record_review(self):
        """审核员只能处理授权组织：二组的活动一组审核员无权复核。"""
        act2 = self.make_activity(org=self.org2, title="二组活动")
        rid = self.svc.backfill_record(
            self.admin, act2, self.vol_b, dt(1, 23), dt(2, 1), "补录"
        )
        # 管理员代录即生效；改由志愿者本人补录以产生待复核记录
        rid2 = self.svc.backfill_record(
            self.vol_b, act2, self.vol_b, dt(3, 10), dt(3, 12), "忘记签到"
        )
        with self.assertRaises(PermissionDeniedError):
            self.svc.approve_record(self.rev1, rid2)
        self.svc.approve_record(self.admin, rid2)
        rec = self.svc.get_record(self.admin, rid2)
        self.assertEqual(rec["record"]["status"], RecordStatus.EFFECTIVE.value)
        self.assertEqual(rec["record"]["reviewed_by"], self.admin)
        self.assertIsNotNone(rid)

    def test_reject_record_excludes_and_cannot_rereview(self):
        act = self.make_activity()
        rid = self.svc.backfill_record(
            self.vol_a, act, self.vol_a, dt(1, 23), dt(2, 1), "忘记签到"
        )
        self.svc.reject_record(self.rev1, rid, "查无此事")
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a)
        self.assertEqual(summary["excluded"][0]["reason"], "record_rejected")
        with self.assertRaises(StateError):
            self.svc.approve_record(self.rev1, rid)

    def test_reviewer_backfill_is_effective_immediately(self):
        act = self.make_activity()
        rid = self.svc.backfill_record(
            self.rev1, act, self.vol_a, dt(1, 23), dt(2, 1), "现场登记遗漏"
        )
        rec = self.svc.get_record(self.rev1, rid)
        self.assertEqual(rec["record"]["status"], RecordStatus.EFFECTIVE.value)


class AmendmentTest(BaseCase):
    def test_amendment_flow_with_reason_and_review(self):
        """修改必须说明原因并经复核；通过后时段更新且留痕前后快照。"""
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        with self.assertRaises(ValidationError):
            self.svc.request_amendment(self.vol_a, rid, dt(1, 22), dt(2, 1), "")
        aid = self.svc.request_amendment(
            self.vol_a, rid, dt(1, 22), dt(2, 1), "签到时间记错一小时"
        )
        # 复核前时长不变
        self.assertEqual(self.svc.volunteer_summary(self.vol_a, self.vol_a)["total_hours"], 2.0)
        self.svc.approve_amendment(self.rev1, aid, "核实通过")
        self.assertEqual(self.svc.volunteer_summary(self.vol_a, self.vol_a)["total_hours"], 3.0)
        chain = self.svc.get_record(self.vol_a, rid)
        apply_entries = [e for e in chain["audit_trail"] if e["action"] == "APPLY_AMENDMENT"]
        self.assertEqual(len(apply_entries), 1)
        self.assertEqual(apply_entries[0]["detail"]["before"]["check_in_at"],
                         "2026-01-01T23:00:00+00:00")
        self.assertEqual(apply_entries[0]["detail"]["after"]["check_in_at"],
                         "2026-01-01T22:00:00+00:00")

    def test_rejected_amendment_keeps_original(self):
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        aid = self.svc.request_amendment(self.vol_a, rid, dt(1, 20), dt(2, 1), "记错了")
        self.svc.reject_amendment(self.rev1, aid, "与签到机记录不符")
        self.assertEqual(self.svc.volunteer_summary(self.vol_a, self.vol_a)["total_hours"], 2.0)

    def test_reviewer_amendment_applies_immediately(self):
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        self.svc.request_amendment(self.rev1, rid, dt(1, 23), dt(2, 2), "签退漏记，按巡查表修正")
        self.assertEqual(self.svc.volunteer_summary(self.vol_a, self.vol_a)["total_hours"], 3.0)


class ImportTest(BaseCase):
    def rows(self, act: str) -> list[dict]:
        return [
            {"source_ref": "legacy-001", "activity_id": act, "volunteer_id": self.vol_a,
             "check_in": dt(1, 23), "check_out": dt(2, 1)},
            {"source_ref": "legacy-002", "activity_id": act, "volunteer_id": self.vol_b,
             "check_in": dt(1, 23), "check_out": dt(2, 0, 30)},
        ]

    def test_repeated_import_does_not_accumulate(self):
        """同一批数据重复导入：返回原批次结果，记录数与时长不变。"""
        act = self.make_activity()
        first = self.svc.import_records(self.rev1, self.rows(act))
        self.assertFalse(first["already_imported"])
        self.assertEqual(first["inserted_count"], 2)
        second = self.svc.import_records(self.rev1, self.rows(act))
        self.assertTrue(second["already_imported"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(self.svc.volunteer_summary(self.vol_a, self.vol_a)["total_hours"], 2.0)

    def test_duplicate_source_ref_skipped_within_and_across_batches(self):
        act = self.make_activity()
        rows = self.rows(act)
        rows.append(dict(rows[0]))  # 批次内重复 source_ref
        result = self.svc.import_records(self.rev1, rows)
        self.assertEqual(result["inserted_count"], 2)
        self.assertEqual(result["skipped_count"], 1)
        # 跨批次：一行重复 + 一行新增
        more = [dict(rows[0]),
                {"source_ref": "legacy-003", "activity_id": act,
                 "volunteer_id": self.vol_a, "check_in": dt(3, 10), "check_out": dt(3, 11)}]
        result2 = self.svc.import_records(self.rev1, more)
        self.assertEqual(result2["inserted_count"], 1)
        self.assertEqual(result2["skipped_count"], 1)

    def test_import_overlapping_onsite_record_merged_in_summary(self):
        """导入与现场记录时段重叠时，汇总合并而不是累加。"""
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        self.svc.import_records(self.rev1, self.rows(act))
        self.assertEqual(self.svc.volunteer_summary(self.vol_a, self.vol_a)["total_hours"], 2.0)

    def test_import_requires_reviewer_scope(self):
        act = self.make_activity()
        with self.assertRaises(PermissionDeniedError):
            self.svc.import_records(self.vol_a, self.rows(act))
        act2 = self.make_activity(org=self.org2)
        rows = [dict(r, activity_id=act2) for r in self.rows(act2)]
        with self.assertRaises(PermissionDeniedError):
            self.svc.import_records(self.rev1, rows)

    def test_import_row_validation(self):
        act = self.make_activity()
        with self.assertRaises(ValidationError):
            self.svc.import_records(self.rev1, [{"activity_id": act}])
        with self.assertRaises(ValidationError):
            self.svc.import_records(self.rev1, [])


class ObjectionTest(BaseCase):
    def make_counted_record(self) -> tuple[str, str]:
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        return act, rid

    def test_volunteer_files_and_reviewer_resolves(self):
        _, rid = self.make_counted_record()
        oid = self.svc.file_objection(self.vol_a, rid, "签退时间应为 02:00")
        rec = self.svc.get_record(self.vol_a, rid)
        self.assertTrue(rec["record"]["disputed"])
        self.svc.resolve_objection(self.rev1, oid, upheld=True, note="属实，已按修改流程更正")
        rec = self.svc.get_record(self.vol_a, rid)
        self.assertFalse(rec["record"]["disputed"])
        self.assertEqual(rec["objections"][0]["status"], "RESOLVED_UPHELD")
        with self.assertRaises(StateError):
            self.svc.resolve_objection(self.rev1, oid, upheld=False, note="重复处理")

    def test_cannot_object_to_others_record(self):
        _, rid = self.make_counted_record()
        with self.assertRaises(PermissionDeniedError):
            self.svc.file_objection(self.vol_b, rid, "不是我的记录")

    def test_objection_requires_reason(self):
        _, rid = self.make_counted_record()
        with self.assertRaises(ValidationError):
            self.svc.file_objection(self.vol_a, rid, "  ")

    def test_objection_visibility_scoped(self):
        _, rid = self.make_counted_record()
        self.svc.file_objection(self.vol_a, rid, "时长不对")
        self.assertEqual(len(self.svc.list_objections(self.rev1, org_id=self.org1)), 1)
        self.assertEqual(len(self.svc.list_objections(self.vol_a)), 1)
        self.assertEqual(self.svc.list_objections(self.vol_b), [])
        with self.assertRaises(PermissionDeniedError):
            self.svc.list_objections(self.rev1, org_id=self.org2)


class FreezeTest(BaseCase):
    def make_counted_record(self) -> str:
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        return rid

    def test_freeze_excludes_and_unfreeze_restores(self):
        rid = self.make_counted_record()
        self.svc.freeze_record(self.admin, rid, "年度表彰名单争议，待核查")
        summary = self.svc.volunteer_summary(self.vol_a, self.vol_a)
        self.assertEqual(summary["total_hours"], 0.0)
        self.assertEqual(summary["excluded"][0]["reason"], "frozen")
        self.svc.unfreeze_record(self.admin, rid, "核查完毕，恢复计入")
        self.assertEqual(self.svc.volunteer_summary(self.vol_a, self.vol_a)["total_hours"], 2.0)

    def test_freeze_requires_admin_and_reason(self):
        rid = self.make_counted_record()
        with self.assertRaises(PermissionDeniedError):
            self.svc.freeze_record(self.rev1, rid, "越权")
        with self.assertRaises(ValidationError):
            self.svc.freeze_record(self.admin, rid, "")
        self.svc.freeze_record(self.admin, rid, "争议")
        with self.assertRaises(StateError):
            self.svc.freeze_record(self.admin, rid, "重复冻结")

    def test_frozen_record_blocks_changes(self):
        rid = self.make_counted_record()
        self.svc.freeze_record(self.admin, rid, "争议核查中")
        with self.assertRaises(StateError):
            self.svc.request_amendment(self.rev1, rid, dt(1, 23), dt(2, 2), "修正")
        with self.assertRaises(StateError):
            self.svc.check_out(self.admin, rid, at=dt(2, 2))

    def test_original_data_survives_freeze(self):
        """冻结不删除原始数据：明细与审核链完整可查。"""
        rid = self.make_counted_record()
        self.svc.freeze_record(self.admin, rid, "争议")
        rec = self.svc.get_record(self.admin, rid)
        self.assertEqual(rec["record"]["check_in_at"], "2026-01-01T23:00:00+00:00")
        actions = [e["action"] for e in rec["audit_trail"]]
        self.assertIn("CHECK_IN", actions)
        self.assertIn("FREEZE", actions)


class GroupSummaryTest(BaseCase):
    def test_group_summary_aggregates_valid_records(self):
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        rid_b = self.svc.check_in(self.vol_b, act, self.vol_b, at=dt(1, 23))
        self.svc.check_out(self.vol_b, rid_b, at=dt(2, 0, 30))
        # 一条待复核补录不计入小组时长
        self.svc.backfill_record(self.vol_a, act, self.vol_a, dt(3, 10), dt(3, 12), "补录")
        summary = self.svc.group_summary(self.rev1, self.org1)
        self.assertEqual(summary["total_hours"], 3.5)
        by_vol = {v["volunteer_id"]: v["hours"] for v in summary["by_volunteer"]}
        self.assertEqual(by_vol[self.vol_a], 2.0)
        self.assertEqual(by_vol[self.vol_b], 1.5)
        self.assertEqual(len(summary["excluded"]), 1)
        self.assertEqual(summary["excluded"][0]["reason"], "pending_review")

    def test_group_summary_permission(self):
        with self.assertRaises(PermissionDeniedError):
            self.svc.group_summary(self.rev1, self.org2)
        # 小组成员可以查看本组汇总
        summary = self.svc.group_summary(self.vol_a, self.org1)
        self.assertEqual(summary["org_id"], self.org1)


class AuditQueryTest(BaseCase):
    def test_audit_query_admin_only_with_reasons(self):
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.freeze_record(self.admin, rid, "争议冻结")
        entries = self.svc.audit_query(self.admin, entity_type="attendance_record",
                                       entity_id=rid)
        actions = [e["action"] for e in entries]
        self.assertEqual(actions, ["CHECK_IN", "FREEZE"])
        self.assertEqual(entries[1]["reason"], "争议冻结")
        with self.assertRaises(PermissionDeniedError):
            self.svc.audit_query(self.rev1)

    def test_audit_query_filters(self):
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        entries = self.svc.audit_query(self.admin, action="CHECK_OUT")
        self.assertEqual(len(entries), 1)
        entries = self.svc.audit_query(self.admin, actor=self.vol_a)
        self.assertTrue(all(e["actor_id"] == self.vol_a for e in entries))


class PersistenceTest(unittest.TestCase):
    """进程重启后冻结标记与审核链仍可恢复。"""

    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "volunteer.db"
            svc = VolunteerService(db)
            admin = svc.create_user(None, "管理员", Role.ADMIN)
            org = svc.create_organization(admin, "服务一组")
            rev = svc.create_user(admin, "审核员", Role.REVIEWER)
            svc.grant_reviewer_scope(admin, rev, org)
            vol = svc.create_user(admin, "志愿者", Role.VOLUNTEER)
            svc.add_membership(admin, vol, org)
            act = svc.create_activity(rev, org, "夜巡", dt(1, 22), dt(2, 2))
            rid = svc.backfill_record(vol, act, vol, dt(1, 23), dt(2, 1), "忘记签到")
            svc.approve_record(rev, rid, "属实")
            svc.freeze_record(admin, rid, "表彰争议冻结")
            oid = svc.file_objection(vol, rid, "签退时间有出入")
            svc.close()

            # 模拟进程重启：重新打开同一数据文件
            svc2 = VolunteerService(db)
            rec = svc2.get_record(admin, rid)
            self.assertTrue(rec["record"]["frozen"])
            self.assertEqual(rec["record"]["status"], RecordStatus.EFFECTIVE.value)
            self.assertEqual(rec["record"]["reviewed_by"], rev)
            actions = [e["action"] for e in rec["audit_trail"]]
            self.assertEqual(actions, ["BACKFILL", "APPROVE_RECORD", "FREEZE", "FILE_OBJECTION"])
            # 冻结标记仍生效：不计入时长
            self.assertEqual(svc2.volunteer_summary(vol, vol)["total_hours"], 0.0)
            # 异议仍可继续处理
            svc2.resolve_objection(rev, oid, upheld=False, note="维持原记录")
            svc2.unfreeze_record(admin, rid, "核查完毕")
            self.assertEqual(svc2.volunteer_summary(vol, vol)["total_hours"], 2.0)
            svc2.close()


class PositionTest(BaseCase):
    def test_position_recorded_on_check_in(self):
        act = self.make_activity()
        pos = self.svc.add_position(self.rev1, act, "引导岗", quota=2)
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, position_id=pos, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        rec = self.svc.get_record(self.vol_a, rid)
        self.assertEqual(rec["position"]["name"], "引导岗")

    def test_position_must_belong_to_activity(self):
        act = self.make_activity()
        act2 = self.make_activity(title="另一活动")
        pos = self.svc.add_position(self.rev1, act2, "签到岗")
        with self.assertRaises(ValidationError):
            self.svc.check_in(self.vol_a, act, self.vol_a, position_id=pos, at=dt(1, 23))
        with self.assertRaises(ValidationError):
            self.svc.add_position(self.rev1, act2, "签到岗")


class PermissionTest(BaseCase):
    def test_volunteer_cannot_review_or_create_activity(self):
        act = self.make_activity()
        rid = self.svc.backfill_record(
            self.vol_a, act, self.vol_a, dt(1, 23), dt(2, 1), "补录"
        )
        with self.assertRaises(PermissionDeniedError):
            self.svc.approve_record(self.vol_b, rid)
        with self.assertRaises(PermissionDeniedError):
            self.svc.create_activity(self.vol_a, self.org1, "私自活动", dt(1, 8), dt(1, 9))

    def test_volunteer_summary_visibility(self):
        act = self.make_activity()
        rid = self.svc.check_in(self.vol_a, act, self.vol_a, at=dt(1, 23))
        self.svc.check_out(self.vol_a, rid, at=dt(2, 1))
        # 本人、授权审核员、管理员可查；其他志愿者不可查
        self.assertEqual(self.svc.volunteer_summary(self.vol_a, self.vol_a)["total_hours"], 2.0)
        self.assertEqual(self.svc.volunteer_summary(self.rev1, self.vol_a)["total_hours"], 2.0)
        self.assertEqual(self.svc.volunteer_summary(self.admin, self.vol_a)["total_hours"], 2.0)
        with self.assertRaises(PermissionDeniedError):
            self.svc.volunteer_summary(self.vol_b, self.vol_a)

    def test_grant_scope_admin_only(self):
        with self.assertRaises(PermissionDeniedError):
            self.svc.grant_reviewer_scope(self.rev1, self.rev1, self.org2)


if __name__ == "__main__":
    unittest.main()
