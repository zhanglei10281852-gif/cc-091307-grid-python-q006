"""系统端到端规则测试，直接覆盖需求中的每一条口径。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.errors import AuthzError, StateError, ValidationError
from src.models import (
    ActivityStatus,
    DisputeStatus,
    EntryKind,
    EntryStatus,
    Role,
)
from src.service import VolunteerService

# 2026-01-10 18:00 ~ 2026-01-11 02:00（跨午夜，8h）
DAY1 = 1_768_000_000.0
H = 3600.0


class Clock:
    def __init__(self):
        self.t = DAY1

    def __call__(self):
        return self.t


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    svc = VolunteerService(tmp_path / "v.db", clock=clock)
    admin = svc.register_user(None, "admin", "管理员", Role.ADMIN)
    reviewer_o1 = svc.register_user(
        admin, "rev1", "审核员甲", Role.REVIEWER,
        authorized_org_ids=["org-a"])
    reviewer_o2 = svc.register_user(
        admin, "rev2", "审核员乙", Role.REVIEWER,
        authorized_org_ids=["org-b"])
    v1 = svc.register_user(admin, "v1", "张三", Role.VOLUNTEER)
    v2 = svc.register_user(admin, "v2", "李四", Role.VOLUNTEER)
    svc.register_group(admin, "g1", "助老组", "org-a")
    svc.register_group(admin, "g2", "环保组", "org-b")
    return svc, clock, admin, reviewer_o1, reviewer_o2, v1, v2


def make_activity(svc, admin, aid="a1", status=ActivityStatus.ACTIVE,
                  start=DAY1 + 18 * H, end=DAY1 + 26 * H,
                  org="org-a", group="g1"):
    return svc.create_activity(admin, aid, f"活动-{aid}", org, group,
                               start, end, status)


# --------------------------------------------------------------------- #
# 1. 有效状态 + 跨午夜时长
# --------------------------------------------------------------------- #

def test_hours_only_for_effective_activity_and_cross_midnight(setup):
    svc, clock, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin)  # 18:00 次日 02:00，共 8h

    e = svc.submit_backfill(rev1, act.id, v1.id,
                            act.start_ts + H, act.end_ts - H,
                            reason="系统故障补录")
    assert e.status is EntryStatus.PENDING
    # 待复核不计入
    assert svc.volunteer_hours(v1, v1.id).hours == 0.0

    svc.approve_entry(admin, e.id)
    summary = svc.volunteer_hours(v1, v1.id)
    assert summary.hours == pytest.approx(6.0)          # 19:00–01:00 跨午夜 6h
    assert summary.components[0]["calc"]

    # 活动取消后，已通过的记录也不再计时长
    svc.set_activity_status(admin, act.id, ActivityStatus.CANCELLED,
                            reason="天气原因取消")
    summary = svc.volunteer_hours(v1, v1.id)
    assert summary.hours == 0.0
    assert summary.excluded[0]["excluded_reason"].startswith("活动状态为 cancelled")

    # planned 同样不计
    svc.set_activity_status(admin, act.id, ActivityStatus.PLANNED, reason="重排")
    assert svc.volunteer_hours(v1, v1.id).hours == 0.0


def test_service_window_clipped_to_activity_window(setup):
    svc, _, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin, start=DAY1 + 20 * H, end=DAY1 + 24 * H)
    # 报了 19:00–03:00，与活动窗口 20:00–24:00 求交 = 4h
    e = svc.submit_backfill(rev1, act.id, v1.id,
                            DAY1 + 19 * H, DAY1 + 27 * H, reason="补录")
    svc.approve_entry(admin, e.id)
    assert svc.volunteer_hours(v1, v1.id).hours == pytest.approx(4.0)


# --------------------------------------------------------------------- #
# 2. 重复签到 / 重复导入不累加
# --------------------------------------------------------------------- #

def test_duplicate_checkin_rejected(setup):
    svc, _, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin)
    svc.check_in(v1, act.id)
    with pytest.raises(StateError):
        svc.check_in(v1, act.id)
    assert len(svc.list_my_entries(v1)) == 1


def test_duplicate_import_idempotent(setup):
    svc, _, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin)
    rec = {"activity_id": act.id, "volunteer_id": v1.id,
           "check_in": act.start_ts, "check_out": act.start_ts + 2 * H,
           "import_key": "batch-1#row-7"}
    r1 = svc.import_entries(rev1, [rec], reason="从旧系统迁移")
    r2 = svc.import_entries(rev1, [rec], reason="从旧系统迁移（重跑）")
    assert len(r1["created"]) == 1 and r1["duplicates"] == []
    assert r2["created"] == [] and len(r2["duplicates"]) == 1
    entries = svc.list_my_entries(v1)
    assert len(entries) == 1
    svc.approve_entry(admin, entries[0].id)
    assert svc.volunteer_hours(v1, v1.id).hours == pytest.approx(2.0)


def test_import_requires_reason(setup):
    svc, _, _, rev1, _, v1, _ = setup
    act = make_activity(svc, _admin(svc))
    with pytest.raises(ValidationError):
        svc.import_entries(rev1, [], reason="  ")
    with pytest.raises(ValidationError):
        svc.import_entries(
            rev1, [{"activity_id": act.id, "volunteer_id": v1.id,
                    "check_in": act.start_ts, "check_out": act.start_ts + H}],
            reason="迁移")


# --------------------------------------------------------------------- #
# 3. 补录/修改须说明原因并经复核
# --------------------------------------------------------------------- #

def test_backfill_requires_reason_and_re_review_after_correction(setup):
    svc, _, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin)
    with pytest.raises(ValidationError):
        svc.submit_backfill(rev1, act.id, v1.id,
                            act.start_ts, act.start_ts + H, reason="")
    e = svc.submit_backfill(rev1, act.id, v1.id,
                            act.start_ts, act.start_ts + 3 * H,
                            reason="设备离线")
    svc.approve_entry(admin, e.id)
    assert svc.volunteer_hours(v1, v1.id).hours == pytest.approx(3.0)

    # 修正：原因必填，且修正后回退到 pending，需重新复核
    with pytest.raises(ValidationError):
        svc.correct_entry(rev1, e.id, act.start_ts, act.start_ts + 2 * H,
                          reason="  ")
    svc.correct_entry(rev1, e.id, act.start_ts, act.start_ts + 2 * H,
                      reason="核实签到表实为 2 小时")
    assert svc.get_entry(e.id).status is EntryStatus.PENDING
    assert svc.volunteer_hours(v1, v1.id).hours == 0.0
    svc.approve_entry(admin, e.id)
    assert svc.volunteer_hours(v1, v1.id).hours == pytest.approx(2.0)
    assert svc.get_entry(e.id).version == 2


def test_reviewer_cannot_self_approve(setup):
    svc, _, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin)
    e = svc.submit_backfill(rev1, act.id, v1.id,
                            act.start_ts, act.start_ts + H, reason="补录")
    with pytest.raises(AuthzError):
        svc.approve_entry(rev1, e.id)


def test_rejected_entry_not_counted(setup):
    svc, _, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin)
    e = svc.submit_backfill(rev1, act.id, v1.id,
                            act.start_ts, act.start_ts + H, reason="补录")
    svc.reject_entry(admin, e.id, reason="查无此人签到")
    assert svc.volunteer_hours(v1, v1.id).hours == 0.0
    assert svc.volunteer_hours(v1, v1.id).excluded[0]["entry_status"] == "rejected"


# --------------------------------------------------------------------- #
# 4. 角色权限
# --------------------------------------------------------------------- #

def _admin(svc):
    return svc.get_user("admin")


def test_reviewer_org_authorization(setup):
    svc, _, admin, rev1, rev2, v1, _ = setup
    act_b = make_activity(svc, admin, aid="ab", org="org-b", group="g2")
    # rev1 只有 org-a，不能碰 org-b 的记录
    e = svc.submit_backfill(rev2, act_b.id, v1.id,
                            act_b.start_ts, act_b.start_ts + H,
                            reason="补录")
    with pytest.raises(AuthzError):
        svc.approve_entry(rev1, e.id)
    rev2b = svc.register_user(admin, "rev2b", "审核员丙", Role.REVIEWER,
                              authorized_org_ids=["org-b"])
    svc.approve_entry(rev2b, e.id)  # 同组织另一审核员可复核（非提交人）
    # 提交人=复核人仍禁止
    assert svc.get_entry(e.id).status is EntryStatus.APPROVED


def test_self_approval_same_reviewer_blocked(setup):
    svc, _, admin, rev1, rev2, v1, _ = setup
    act_b = make_activity(svc, admin, aid="ab2", org="org-b", group="g2")
    e = svc.submit_backfill(rev2, act_b.id, v1.id,
                            act_b.start_ts, act_b.start_ts + H,
                            reason="补录")
    with pytest.raises(AuthzError):
        svc.approve_entry(rev2, e.id)


def test_volunteer_sees_only_own_details(setup):
    svc, _, admin, rev1, _, v1, v2 = setup
    act = make_activity(svc, admin)
    e1 = svc.submit_backfill(rev1, act.id, v1.id,
                             act.start_ts, act.start_ts + H, reason="补录")
    svc.submit_backfill(rev1, act.id, v2.id,
                        act.start_ts, act.start_ts + H, reason="补录")
    ids = {x.id for x in svc.list_my_entries(v1)}
    assert ids == {e1.id}
    with pytest.raises(AuthzError):
        svc.list_my_entries(v1, volunteer_id=v2.id)
    # 本人可看自己的复核链
    chain = svc.get_review_chain(v1, e1.id)
    assert [c.action for c in chain] == ["submit"]


def test_freeze_requires_admin_and_keeps_raw_data(setup):
    svc, _, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin)
    e = svc.submit_backfill(rev1, act.id, v1.id,
                            act.start_ts, act.start_ts + 2 * H, reason="补录")
    svc.approve_entry(admin, e.id)
    assert svc.volunteer_hours(v1, v1.id).hours == pytest.approx(2.0)

    with pytest.raises(AuthzError):
        svc.freeze_entry(rev1, e.id, reason="争议")
    svc.freeze_entry(admin, e.id, reason="表彰名单争议，待核")
    assert svc.volunteer_hours(v1, v1.id).hours == 0.0
    # 原始数据仍可读、可审计，且冻结期间禁止修改
    assert svc.get_entry(e.id).frozen is True
    with pytest.raises(StateError):
        svc.correct_entry(rev1, e.id, act.start_ts, act.start_ts + H,
                          reason="尝试修改冻结记录")
    # 解冻后恢复计时
    svc.unfreeze_entry(admin, e.id, reason="核实无误")
    assert svc.volunteer_hours(v1, v1.id).hours == pytest.approx(2.0)


# --------------------------------------------------------------------- #
# 5. 异议流程
# --------------------------------------------------------------------- #

def test_dispute_workflow(setup):
    svc, _, admin, rev1, _, v1, v2 = setup
    act = make_activity(svc, admin)
    e = svc.submit_backfill(rev1, act.id, v1.id,
                            act.start_ts, act.start_ts + 3 * H,
                            reason="补录")
    svc.approve_entry(admin, e.id)

    # v2 不能对 v1 的记录提异议
    with pytest.raises(AuthzError):
        svc.file_dispute(v2, "这条不是他的", entry_id=e.id)
    d = svc.file_dispute(v1, "实际只服务了 1 小时", entry_id=e.id)
    assert d.status is DisputeStatus.OPEN

    # 志愿者不能自己处理；处理时可联动修正
    with pytest.raises(AuthzError):
        svc.resolve_dispute(v1, d.id, DisputeStatus.UPHELD, note="x")
    svc.resolve_dispute(
        admin, d.id, DisputeStatus.UPHELD, note="查签到表确认 1 小时",
        correct_entry={"entry_id": e.id,
                       "check_in": act.start_ts,
                       "check_out": act.start_ts + H,
                       "reason": "异议成立，按签到表修正"})
    assert svc.get_entry(e.id).status is EntryStatus.PENDING
    svc.approve_entry(admin, e.id)
    assert svc.volunteer_hours(v1, v1.id).hours == pytest.approx(1.0)
    with pytest.raises(StateError):
        svc.resolve_dispute(admin, d.id, DisputeStatus.REJECTED, note="重复处理")


def test_dispute_list_scoped_by_role(setup):
    svc, _, admin, rev1, rev2, v1, v2 = setup
    act_a = make_activity(svc, admin, aid="aA")
    act_b = make_activity(svc, admin, aid="aB", org="org-b", group="g2")
    ea = svc.submit_backfill(rev1, act_a.id, v1.id,
                             act_a.start_ts, act_a.start_ts + H, reason="补录")
    eb = svc.submit_backfill(rev2, act_b.id, v2.id,
                             act_b.start_ts, act_b.start_ts + H, reason="补录")
    svc.file_dispute(v1, "异议A", entry_id=ea.id)
    svc.file_dispute(v2, "异议B", entry_id=eb.id)
    assert {d.volunteer_id for d in svc.list_disputes(rev1)} == {"v1"}
    assert {d.volunteer_id for d in svc.list_disputes(rev2)} == {"v2"}
    assert {d.volunteer_id for d in svc.list_disputes(v1)} == {"v1"}
    assert len(svc.list_disputes(admin)) == 2


# --------------------------------------------------------------------- #
# 6. 小组汇总
# --------------------------------------------------------------------- #

def test_activity_level_dispute_scoped_to_org(setup):
    svc, _, admin, rev1, rev2, v1, _ = setup
    act_b = make_activity(svc, admin, aid="ab9", org="org-b", group="g2")
    d = svc.file_dispute(v1, "整场活动被取消却仍有时长", activity_id=act_b.id)
    assert d.entry_id is None and d.activity_id == act_b.id
    assert svc.list_disputes(rev1) == []            # org-a 审核员不可见
    assert {x.id for x in svc.list_disputes(rev2)} == {d.id}
    with pytest.raises(AuthzError):
        svc.resolve_dispute(rev1, d.id, DisputeStatus.UPHELD, note="越权处理")
    svc.resolve_dispute(rev2, d.id, DisputeStatus.UPHELD, note="活动确已取消")
    assert svc._dispute_from_id(d.id).resolved_by == "rev2"


def test_group_hours_and_positions(setup):
    svc, _, admin, rev1, _, v1, v2 = setup
    act = make_activity(svc, admin)
    svc.assign_position(rev1, act.id, v1.id, "引导员")
    svc.assign_position(rev1, act.id, v2.id, "记录员")
    for v in (v1, v2):
        e = svc.submit_backfill(rev1, act.id, v.id,
                                act.start_ts, act.start_ts + 2 * H,
                                reason="补录")
        svc.approve_entry(admin, e.id)
    summary = svc.group_hours(rev1, "g1")
    assert summary.hours == pytest.approx(4.0)
    assert len(summary.components) == 2
    posts = svc.list_positions(v1, act.id)
    assert len(posts) == 1 and posts[0]["post"] == "引导员"
    with pytest.raises(AuthzError):
        svc.group_hours(_other_reviewer(svc), "g1")


def _other_reviewer(svc):
    return svc.get_user("rev2")


# --------------------------------------------------------------------- #
# 7. 审计与持久化恢复
# --------------------------------------------------------------------- #

def test_audit_trail_records_everything(setup):
    svc, _, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin)
    e = svc.submit_backfill(rev1, act.id, v1.id,
                            act.start_ts, act.start_ts + H, reason="补录")
    svc.approve_entry(admin, e.id)
    svc.freeze_entry(admin, e.id, reason="争议冻结")

    events = svc.query_audit(admin, entity_type="entry", entity_id=e.id)
    actions = [x.action for x in events]
    assert "entry.backfill" in actions
    assert "entry.approve" in actions
    assert "entry.freeze" in actions
    # 事件携带计算依据所需的原始字段
    assert events[0].detail["raw_hours"] == pytest.approx(1.0)

    # 志愿者只能查本人
    assert all(x.actor_id in {"rev1", "admin"} for x in
               svc.query_audit(v1))  # v1 自己无操作，但接口不允许查别人
    with pytest.raises(AuthzError):
        svc.query_audit(v1, actor_id=rev1.id)


def test_restart_recovers_freeze_and_review_chain(tmp_path):
    path = tmp_path / "persist.db"
    clock = Clock()
    svc = VolunteerService(path, clock=clock)
    admin = svc.register_user(None, "admin", "管理员", Role.ADMIN)
    rev = svc.register_user(admin, "rev", "审核员", Role.REVIEWER,
                            authorized_org_ids=["org-a"])
    vol = svc.register_user(admin, "vol", "志愿者", Role.VOLUNTEER)
    svc.register_group(admin, "g", "组", "org-a")
    act = make_activity(svc, admin, aid="aX", group="g")
    e = svc.submit_backfill(rev, act.id, vol.id,
                            act.start_ts, act.start_ts + 2 * H,
                            reason="补录")
    svc.correct_entry(rev, e.id, act.start_ts, act.start_ts + H,
                      reason="核减为1小时")
    svc.approve_entry(admin, e.id)
    svc.freeze_entry(admin, e.id, reason="争议")
    svc.close()

    # 模拟进程重启
    svc2 = VolunteerService(path, clock=clock)
    a2, r2, vv = (svc2.get_user("admin"), svc2.get_user("rev"),
                  svc2.get_user("vol"))
    e2 = svc2.get_entry(e.id)
    assert e2.frozen is True
    assert e2.version == 2
    assert e2.status is EntryStatus.APPROVED
    chain = [c.action for c in svc2.get_review_chain(a2, e.id)]
    assert chain == ["submit", "correct", "approve"]
    assert svc2.volunteer_hours(vv, vv.id).hours == 0.0  # 仍冻结
    # 审计链完整
    audits = svc2.query_audit(a2, entity_type="entry", entity_id=e.id)
    assert {x.action for x in audits} >= {"entry.backfill", "entry.correct",
                                          "entry.approve", "entry.freeze"}
    # 角色授权也恢复
    assert r2.authorized_org_ids == frozenset({"org-a"})
    svc2.close()


def test_checkin_checkout_lifecycle(setup):
    svc, clock, admin, rev1, _, v1, _ = setup
    act = make_activity(svc, admin, start=DAY1 + 20 * H, end=DAY1 + 24 * H)
    clock.t = act.start_ts
    e = svc.check_in(v1, act.id)
    assert e.check_out is None
    # 未签退不能复核
    with pytest.raises(StateError):
        svc.approve_entry(admin, e.id)
    clock.t = act.start_ts + 2.5 * H
    svc.check_out(v1, e.id)
    with pytest.raises(StateError):
        svc.check_out(v1, e.id)   # 重复签退
    svc.approve_entry(admin, e.id)
    assert svc.volunteer_hours(v1, v1.id).hours == pytest.approx(2.5)
