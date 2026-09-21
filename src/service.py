"""志愿服务登记系统核心服务。

业务规则（需求映射）：
- 时长只按活动的**有效状态**（active/completed）计算；cancelled/planned 一律为 0。
- 跨午夜活动按 epoch 时间戳求实际时段交集，无需特殊处理。
- 重复签到（同活动同志愿者已有现场记录）直接拒绝；重复导入按
  (活动, 志愿者, import_key) 幂等跳过，均不累加。
- 补录/批量导入/修正必须填写原因，且经复核通过后才计入汇总。
- 志愿者只能查看本人明细并就本人记录提异议；审核员只能处理被授权组织；
  管理员可冻结/解冻争议记录，但系统不提供任何删除原始数据的接口。
- 所有写操作写入 audit_log；冻结标记与复核链均持久化，重启可恢复。
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Sequence

from .errors import AuthzError, NotFoundError, StateError, ValidationError
from .models import (
    Activity,
    ActivityStatus,
    AuditEvent,
    Dispute,
    DisputeStatus,
    EntryKind,
    EntryStatus,
    Group,
    HoursBreakdown,
    Review,
    Role,
    ServiceEntry,
    User,
)
from .storage import Database

_EFFECTIVE_STATUSES = {ActivityStatus.ACTIVE.value, ActivityStatus.COMPLETED.value}
_SECONDS_PER_HOUR = 3600.0


def _overlap_hours(start_a: float, end_a: float,
                   start_b: float, end_b: float) -> float:
    """两个时段交集的小时数；无交集（含跨午夜自然支持）为 0。"""
    lo = max(start_a, start_b)
    hi = min(end_a, end_b)
    return max(0.0, hi - lo) / _SECONDS_PER_HOUR


class VolunteerService:
    """系统对外门面。所有方法均线程外串行使用（单 SQLite 连接）。"""

    def __init__(self, db: str | Database = ":memory:", *, clock=time.time):
        self.db = db if isinstance(db, Database) else Database(db)
        self._clock = clock

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ #
    # 基础档案
    # ------------------------------------------------------------------ #

    def register_user(self, actor: User | None, user_id: str, name: str,
                      role: Role, *, org_id: str | None = None,
                      authorized_org_ids: Iterable[str] = ()) -> User:
        if actor is not None and actor.role is not Role.ADMIN:
            raise AuthzError("只有管理员可以登记用户")
        auth = frozenset(authorized_org_ids)
        if role is Role.REVIEWER and not auth:
            raise ValidationError("审核员必须至少授权一个组织")
        self.db.conn.execute(
            "INSERT INTO users(id,name,role,org_id,authorized_orgs) VALUES(?,?,?,?,?)",
            (user_id, name, role.value, org_id, json.dumps(sorted(auth))),
        )
        self._audit(actor.id if actor is not None else user_id,
                    "user.register", "user", user_id,
                    {"name": name, "role": role.value, "org_id": org_id,
                     "authorized_orgs": sorted(auth)})
        self.db.commit()
        return self.get_user(user_id)

    def register_group(self, actor: User, group_id: str, name: str,
                       org_id: str) -> Group:
        self._require_admin(actor)
        self.db.conn.execute(
            "INSERT INTO groups(id,name,org_id) VALUES(?,?,?)",
            (group_id, name, org_id))
        self._audit(actor.id, "group.register", "group", group_id,
                    {"name": name, "org_id": org_id})
        self.db.commit()
        return Group(group_id, name, org_id)

    def create_activity(self, actor: User, activity_id: str, title: str,
                        org_id: str, group_id: str,
                        start_ts: float, end_ts: float,
                        status: ActivityStatus = ActivityStatus.PLANNED) -> Activity:
        """创建活动。跨午夜完全允许：只要 end_ts > start_ts。"""
        self._require_admin(actor)
        if end_ts <= start_ts:
            raise ValidationError("活动结束时间必须晚于开始时间")
        self._get_group_row(group_id)
        self.db.conn.execute(
            "INSERT INTO activities(id,title,org_id,group_id,start_ts,end_ts,status)"
            " VALUES(?,?,?,?,?,?,?)",
            (activity_id, title, org_id, group_id, start_ts, end_ts, status.value))
        self._audit(actor.id, "activity.create", "activity", activity_id,
                    {"title": title, "org_id": org_id, "group_id": group_id,
                     "start_ts": start_ts, "end_ts": end_ts,
                     "status": status.value})
        self.db.commit()
        return self.get_activity(activity_id)

    def set_activity_status(self, actor: User, activity_id: str,
                            status: ActivityStatus, *, reason: str) -> Activity:
        """变更活动有效状态（如取消活动）。取消后其下所有记录不再计时长。"""
        self._require_admin(actor)
        if not reason or not reason.strip():
            raise ValidationError("活动状态变更必须说明原因")
        before = self._get_activity_row(activity_id)
        self.db.conn.execute("UPDATE activities SET status=? WHERE id=?",
                             (status.value, activity_id))
        self._audit(actor.id, "activity.set_status", "activity", activity_id,
                    {"from": before["status"], "to": status.value,
                     "reason": reason})
        self.db.commit()
        return self.get_activity(activity_id)

    def assign_position(self, actor: User, activity_id: str,
                        volunteer_id: str, post: str) -> None:
        """登记服务岗位。审核员限本组织，管理员不限。"""
        activity = self.get_activity(activity_id)
        self._require_org_access(actor, activity.org_id)
        volunteer = self.get_user(volunteer_id)
        if volunteer.role is Role.ADMIN:
            raise ValidationError("不能给管理员安排服务岗位")
        self.db.conn.execute(
            "INSERT INTO positions(id,activity_id,volunteer_id,post,assigned_by,assigned_ts)"
            " VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, activity_id, volunteer_id, post,
             actor.id, self._clock()))
        self._audit(actor.id, "position.assign", "activity", activity_id,
                    {"volunteer_id": volunteer_id, "post": post})
        self.db.commit()

    def list_positions(self, actor: User, activity_id: str) -> list[dict]:
        activity = self.get_activity(activity_id)
        # 本活动参与者本人或有组织权限者可查
        if actor.role is Role.VOLUNTEER:
            rows = self.db.conn.execute(
                "SELECT * FROM positions WHERE activity_id=? AND volunteer_id=?",
                (activity_id, actor.id)).fetchall()
        else:
            self._require_org_access(actor, activity.org_id)
            rows = self.db.conn.execute(
                "SELECT * FROM positions WHERE activity_id=?",
                (activity_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 签到 / 签退
    # ------------------------------------------------------------------ #

    def check_in(self, actor: User, activity_id: str,
                 volunteer_id: str | None = None,
                 *, ts: float | None = None) -> ServiceEntry:
        """现场签到。重复签到不累加、不产生第二条记录。

        志愿者自助签到时可省略 ``volunteer_id``（默认本人）；有组织权限的
        审核员可代签，须显式指定。
        """
        activity = self.get_activity(activity_id)
        if volunteer_id is None:
            volunteer_id = actor.id
        self._require_self_or_org(actor, volunteer_id, activity.org_id)
        ts = self._clock() if ts is None else ts

        row = self.db.conn.execute(
            "SELECT * FROM entries WHERE activity_id=? AND volunteer_id=?"
            " AND kind='checkin'",
            (activity_id, volunteer_id)).fetchone()
        if row is not None:
            raise StateError("该活动已有签到记录，重复签到不予累加")

        entry_id = uuid.uuid4().hex
        self.db.conn.execute(
            "INSERT INTO entries(id,activity_id,volunteer_id,kind,check_in,check_out,"
            "raw_hours,status,created_by,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (entry_id, activity_id, volunteer_id, EntryKind.CHECKIN.value,
             ts, None, 0.0, EntryStatus.PENDING.value, actor.id, self._clock()))
        self._add_review(entry_id, "submit", actor.id,
                         {"check_in": ts, "check_out": None})
        self._audit(actor.id, "entry.checkin", "entry", entry_id,
                    {"activity_id": activity_id,
                     "volunteer_id": volunteer_id, "ts": ts})
        self.db.commit()
        return self.get_entry(entry_id)

    def check_out(self, actor: User, entry_id: str,
                  *, ts: float | None = None) -> ServiceEntry:
        entry = self.get_entry(entry_id)
        activity = self.get_activity(entry.activity_id)
        self._require_self_or_org(actor, entry.volunteer_id, activity.org_id)
        if entry.frozen:
            raise StateError("记录已冻结，签退前须由管理员解冻")
        if entry.check_out is not None:
            raise StateError("该签到已签退，重复签退不予累加")
        ts = self._clock() if ts is None else ts
        if ts <= entry.check_in:
            raise ValidationError("签退时间必须晚于签到时间")

        raw_hours = (ts - entry.check_in) / _SECONDS_PER_HOUR
        self.db.conn.execute(
            "UPDATE entries SET check_out=?, raw_hours=? WHERE id=?",
            (ts, raw_hours, entry_id))
        self._add_review(entry_id, "checkout", actor.id,
                         {"check_in": entry.check_in, "check_out": ts})
        self._audit(actor.id, "entry.checkout", "entry", entry_id,
                    {"check_in": entry.check_in, "check_out": ts,
                     "raw_hours": raw_hours})
        self.db.commit()
        return self.get_entry(entry_id)

    # ------------------------------------------------------------------ #
    # 补录 / 批量导入
    # ------------------------------------------------------------------ #

    def submit_backfill(self, actor: User, activity_id: str,
                        volunteer_id: str, check_in: float, check_out: float,
                        *, reason: str) -> ServiceEntry:
        """补录一条服务时段，必须填写原因，提交后进入复核队列。"""
        return self._create_interval_entry(
            actor, activity_id, volunteer_id, check_in, check_out,
            reason=reason, kind=EntryKind.BACKFILL, import_key=None,
            action="entry.backfill")

    def import_entries(self, actor: User, records: Sequence[dict],
                       *, reason: str) -> dict:
        """批量导入。每条记录须带幂等键 ``import_key``；重复键跳过不累加。

        记录字段：activity_id, volunteer_id, check_in, check_out, import_key。
        返回 ``{"created": [...], "duplicates": [...]}``。
        """
        if not reason or not reason.strip():
            raise ValidationError("批量导入必须说明原因")
        if actor.role is Role.VOLUNTEER:
            raise AuthzError("志愿者无权批量导入")
        created, duplicates = [], []
        for rec in records:
            try:
                key = rec["import_key"]
            except KeyError:
                raise ValidationError("导入记录缺少 import_key，无法保证幂等")
            exists = self.db.conn.execute(
                "SELECT id FROM entries WHERE activity_id=? AND volunteer_id=?"
                " AND import_key=?",
                (rec["activity_id"], rec["volunteer_id"], key)).fetchone()
            if exists is not None:
                duplicates.append({"import_key": key, "entry_id": exists["id"]})
                continue
            entry = self._create_interval_entry(
                actor, rec["activity_id"], rec["volunteer_id"],
                rec["check_in"], rec["check_out"],
                reason=reason, kind=EntryKind.IMPORT, import_key=key,
                action="entry.import", commit=False)
            created.append(entry.id)
        self._audit(actor.id, "entry.import_batch", "import_batch",
                     uuid.uuid4().hex,
                    {"reason": reason, "created": created,
                     "duplicates": duplicates})
        self.db.commit()
        return {"created": created, "duplicates": duplicates}

    def _create_interval_entry(self, actor: User, activity_id: str,
                               volunteer_id: str, check_in: float,
                               check_out: float, *, reason: str,
                               kind: EntryKind, import_key: str | None,
                               action: str, commit: bool = True) -> ServiceEntry:
        if not reason or not reason.strip():
            raise ValidationError("补录/导入必须说明原因")
        if check_out <= check_in:
            raise ValidationError("结束时间必须晚于开始时间")
        activity = self.get_activity(activity_id)
        if actor.role is Role.VOLUNTEER and actor.id != volunteer_id:
            raise AuthzError("志愿者只能为本人补录")
        if actor.role is Role.REVIEWER and \
                activity.org_id not in actor.authorized_org_ids:
            raise AuthzError("审核员无权为该组织补录")
        self.get_user(volunteer_id)

        entry_id = uuid.uuid4().hex
        raw_hours = (check_out - check_in) / _SECONDS_PER_HOUR
        self.db.conn.execute(
            "INSERT INTO entries(id,activity_id,volunteer_id,kind,check_in,check_out,"
            "raw_hours,status,reason,import_key,created_by,created_ts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, activity_id, volunteer_id, kind.value,
             check_in, check_out, raw_hours, EntryStatus.PENDING.value,
             reason.strip(), import_key, actor.id, self._clock()))
        self._add_review(entry_id, "submit", actor.id,
                         {"check_in": check_in, "check_out": check_out,
                          "reason": reason.strip()})
        self._audit(actor.id, action, "entry", entry_id,
                    {"activity_id": activity_id,
                     "volunteer_id": volunteer_id, "kind": kind.value,
                     "check_in": check_in, "check_out": check_out,
                     "raw_hours": raw_hours, "reason": reason.strip(),
                     "import_key": import_key})
        if commit:
            self.db.commit()
        return self.get_entry(entry_id)

    # ------------------------------------------------------------------ #
    # 组织复核
    # ------------------------------------------------------------------ #

    def approve_entry(self, actor: User, entry_id: str,
                      *, reason: str | None = None) -> ServiceEntry:
        entry, activity = self._entry_for_review(actor, entry_id)
        if entry.created_by == actor.id:
            raise AuthzError("提交人与复核人不能为同一人")
        if entry.check_out is None:
            raise StateError("尚未签退，无法复核通过")
        self.db.conn.execute(
            "UPDATE entries SET status=? WHERE id=?",
            (EntryStatus.APPROVED.value, entry_id))
        self._add_review(entry_id, "approve", actor.id,
                         {"reason": reason})
        self._audit(actor.id, "entry.approve", "entry", entry_id,
                    {"reason": reason})
        self.db.commit()
        return self.get_entry(entry_id)

    def reject_entry(self, actor: User, entry_id: str, *, reason: str) -> ServiceEntry:
        if not reason or not reason.strip():
            raise ValidationError("驳回必须说明理由")
        entry, activity = self._entry_for_review(actor, entry_id)
        self.db.conn.execute(
            "UPDATE entries SET status=? WHERE id=?",
            (EntryStatus.REJECTED.value, entry_id))
        self._add_review(entry_id, "reject", actor.id, {"reason": reason.strip()})
        self._audit(actor.id, "entry.reject", "entry", entry_id,
                    {"reason": reason.strip()})
        self.db.commit()
        return self.get_entry(entry_id)

    def correct_entry(self, actor: User, entry_id: str,
                      new_check_in: float, new_check_out: float,
                      *, reason: str) -> ServiceEntry:
        """修正时段。必须说明原因；修正后状态回到 pending，须重新复核。"""
        if not reason or not reason.strip():
            raise ValidationError("修正必须说明原因")
        if new_check_out <= new_check_in:
            raise ValidationError("结束时间必须晚于开始时间")
        entry, activity = self._entry_for_review(actor, entry_id)
        if entry.frozen:
            raise StateError("记录已冻结，不能修改；如需修正请先由管理员解冻")

        before = {"check_in": entry.check_in, "check_out": entry.check_out,
                  "raw_hours": entry.raw_hours, "status": entry.status.value}
        raw_hours = (new_check_out - new_check_in) / _SECONDS_PER_HOUR
        self.db.conn.execute(
            "UPDATE entries SET check_in=?, check_out=?, raw_hours=?,"
            " status=?, version=version+1 WHERE id=?",
            (new_check_in, new_check_out, raw_hours,
             EntryStatus.PENDING.value, entry_id))
        self._add_review(entry_id, "correct", actor.id,
                         {"from": before,
                          "to": {"check_in": new_check_in,
                                 "check_out": new_check_out,
                                 "raw_hours": raw_hours},
                          "reason": reason.strip()})
        self._audit(actor.id, "entry.correct", "entry", entry_id,
                    {"from": before,
                     "to": {"check_in": new_check_in,
                            "check_out": new_check_out,
                            "raw_hours": raw_hours},
                     "reason": reason.strip(), "version": entry.version + 1})
        self.db.commit()
        return self.get_entry(entry_id)

    def _entry_for_review(self, actor: User, entry_id: str):
        if actor.role is Role.VOLUNTEER:
            raise AuthzError("志愿者无复核权限")
        entry = self.get_entry(entry_id)
        activity = self.get_activity(entry.activity_id)
        if actor.role is Role.REVIEWER and \
                activity.org_id not in actor.authorized_org_ids:
            raise AuthzError("该活动不属于你被授权的组织")
        return entry, activity

    def get_review_chain(self, actor: User, entry_id: str) -> list[Review]:
        """返回复核链（提交/签退/通过/驳回/修正），按时间顺序。"""
        entry = self.get_entry(entry_id)
        activity = self.get_activity(entry.activity_id)
        if actor.role is Role.VOLUNTEER and actor.id != entry.volunteer_id:
            raise AuthzError("只能查看本人记录的复核链")
        if actor.role is Role.REVIEWER and \
                activity.org_id not in actor.authorized_org_ids:
            raise AuthzError("该活动不属于你被授权的组织")
        rows = self.db.conn.execute(
            "SELECT * FROM reviews WHERE entry_id=? ORDER BY seq",
            (entry_id,)).fetchall()
        return [Review(id=r["id"], entry_id=r["entry_id"], action=r["action"],
                       actor_id=r["actor_id"], ts=r["ts"], reason=r["reason"],
                       check_in=r["check_in"], check_out=r["check_out"])
                for r in rows]

    # ------------------------------------------------------------------ #
    # 异议
    # ------------------------------------------------------------------ #

    def file_dispute(self, actor: User, reason: str, *,
                     entry_id: str | None = None,
                     activity_id: str | None = None) -> Dispute:
        """志愿者就本人记录提出异议。"""
        if actor.role is not Role.VOLUNTEER:
            raise AuthzError("只有志愿者可以提出异议")
        if not reason or not reason.strip():
            raise ValidationError("异议必须说明理由")
        if entry_id is None and activity_id is None:
            raise ValidationError("异议须关联一条记录或一个活动")
        if entry_id is not None:
            entry = self.get_entry(entry_id)
            if entry.volunteer_id != actor.id:
                raise AuthzError("只能对本人的记录提出异议")
        dispute_id = uuid.uuid4().hex
        now = self._clock()
        self.db.conn.execute(
            "INSERT INTO disputes(id,volunteer_id,entry_id,activity_id,"
            "reason,status,created_ts) VALUES(?,?,?,?,?,?,?)",
            (dispute_id, actor.id, entry_id, activity_id,
             reason.strip(), DisputeStatus.OPEN.value, now))
        self._audit(actor.id, "dispute.file", "dispute", dispute_id,
                    {"entry_id": entry_id, "activity_id": activity_id,
                     "reason": reason.strip()})
        self.db.commit()
        return self._dispute_from_id(dispute_id)

    def list_disputes(self, actor: User, *,
                      status: DisputeStatus | None = None) -> list[Dispute]:
        sql = "SELECT * FROM disputes"
        params: list = []
        if actor.role is Role.VOLUNTEER:
            sql += " WHERE volunteer_id=?"
            params.append(actor.id)
        elif actor.role is Role.REVIEWER:
            # 审核员只见授权组织下记录/活动关联的异议
            sql += (" WHERE COALESCE("
                    " (SELECT a.org_id FROM entries e JOIN activities a"
                    "   ON a.id=e.activity_id WHERE e.id=disputes.entry_id),"
                    " (SELECT a.org_id FROM activities a"
                    "   WHERE a.id=disputes.activity_id)"
                    " ) IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(sorted(actor.authorized_org_ids)))
        if status is not None:
            sql += " AND" if "WHERE" in sql else " WHERE"
            sql += " status=?"
            params.append(status.value)
        sql += " ORDER BY created_ts"
        rows = self.db.conn.execute(sql, params).fetchall()
        return [self._dispute_from_row(r) for r in rows]

    def resolve_dispute(self, actor: User, dispute_id: str,
                        outcome: DisputeStatus, *, note: str,
                        correct_entry: dict | None = None) -> Dispute:
        """处理异议。成立时可同时给出修正；审核员限授权组织。

        ``correct_entry`` 形如 ``{"entry_id", "check_in", "check_out", "reason"}``，
        修正仍须走重新复核流程。
        """
        if outcome is DisputeStatus.OPEN:
            raise ValidationError("处理结果不能是 open")
        if not note or not note.strip():
            raise ValidationError("异议处理必须写明结论")
        dispute = self._dispute_from_id(dispute_id)
        if dispute.status is not DisputeStatus.OPEN:
            raise StateError("该异议已处理")
        org_ids = self._dispute_orgs(dispute)
        if actor.role is Role.VOLUNTEER:
            raise AuthzError("志愿者无异议处理权限")
        if actor.role is Role.REVIEWER and not org_ids <= actor.authorized_org_ids:
            raise AuthzError("异议涉及非授权组织，无权处理")

        if correct_entry is not None:
            self.correct_entry(
                actor, correct_entry["entry_id"],
                correct_entry["check_in"], correct_entry["check_out"],
                reason=correct_entry["reason"])

        self.db.conn.execute(
            "UPDATE disputes SET status=?, resolved_by=?, resolved_ts=?,"
            " resolution_note=? WHERE id=?",
            (outcome.value, actor.id, self._clock(), note.strip(), dispute_id))
        self._audit(actor.id, "dispute.resolve", "dispute", dispute_id,
                    {"outcome": outcome.value, "note": note.strip(),
                     "corrected": correct_entry})
        self.db.commit()
        return self._dispute_from_id(dispute_id)

    def _dispute_orgs(self, dispute: Dispute) -> frozenset[str]:
        orgs: set[str] = set()
        if dispute.entry_id:
            row = self.db.conn.execute(
                "SELECT a.org_id FROM entries e JOIN activities a"
                " ON a.id=e.activity_id WHERE e.id=?",
                (dispute.entry_id,)).fetchone()
            if row is not None:
                orgs.add(row["org_id"])
        if dispute.activity_id:
            orgs.add(self.get_activity(dispute.activity_id).org_id)
        return frozenset(orgs)

    # ------------------------------------------------------------------ #
    # 冻结（管理员）
    # ------------------------------------------------------------------ #

    def freeze_entry(self, actor: User, entry_id: str, *, reason: str) -> ServiceEntry:
        """冻结争议记录：立即移出汇总，但原始数据保留、可审计、可解冻。"""
        return self._set_frozen(actor, entry_id, True, reason)

    def unfreeze_entry(self, actor: User, entry_id: str, *, reason: str) -> ServiceEntry:
        return self._set_frozen(actor, entry_id, False, reason)

    def _set_frozen(self, actor: User, entry_id: str, frozen: bool,
                    reason: str) -> ServiceEntry:
        self._require_admin(actor)
        if not reason or not reason.strip():
            raise ValidationError("冻结/解冻必须说明原因")
        entry = self.get_entry(entry_id)
        if entry.frozen == frozen:
            raise StateError(f"记录冻结状态已经是 {frozen}")
        self.db.conn.execute("UPDATE entries SET frozen=? WHERE id=?",
                             (1 if frozen else 0, entry_id))
        self._audit(actor.id,
                    "entry.freeze" if frozen else "entry.unfreeze",
                    "entry", entry_id, {"reason": reason.strip()})
        self.db.commit()
        return self.get_entry(entry_id)

    # ------------------------------------------------------------------ #
    # 查询：明细 / 汇总（含计算依据）
    # ------------------------------------------------------------------ #

    def list_my_entries(self, actor: User, *,
                        volunteer_id: str | None = None) -> list[ServiceEntry]:
        """志愿者查看本人明细；审核员/管理员可显式指定志愿者。"""
        target = volunteer_id
        if actor.role is Role.VOLUNTEER:
            if target is not None and target != actor.id:
                raise AuthzError("只能查看本人的服务明细")
            target = actor.id
        return self._entries_for_volunteer(target)

    def _entries_for_volunteer(self, volunteer_id: str) -> list[ServiceEntry]:
        rows = self.db.conn.execute(
            "SELECT * FROM entries WHERE volunteer_id=? ORDER BY check_in",
            (volunteer_id,)).fetchall()
        return [self._entry_from_row(r) for r in rows]

    def volunteer_hours(self, actor: User, volunteer_id: str) -> HoursBreakdown:
        """个人时长汇总，返回逐条计算依据与被排除记录。"""
        if actor.role is Role.VOLUNTEER and actor.id != volunteer_id:
            raise AuthzError("只能查询本人的时长汇总")
        return self._summarize(
            self._entries_for_volunteer(volunteer_id))

    def group_hours(self, actor: User, group_id: str) -> HoursBreakdown:
        """小组时长汇总（按活动的小组归属），同样给出计算依据。"""
        group = self.get_group(group_id)
        if actor.role is Role.REVIEWER and \
                group.org_id not in actor.authorized_org_ids:
            raise AuthzError("该小组不属于你被授权的组织")
        rows = self.db.conn.execute(
            "SELECT e.* FROM entries e JOIN activities a"
            " ON a.id=e.activity_id WHERE a.group_id=? ORDER BY e.check_in",
            (group_id,)).fetchall()
        return self._summarize([self._entry_from_row(r) for r in rows])

    def _summarize(self, entries: list[ServiceEntry]) -> HoursBreakdown:
        components, excluded, total = [], [], 0.0
        for e in entries:
            activity = self.get_activity(e.activity_id)
            basis = self._component(e, activity)
            if e.status is not EntryStatus.APPROVED:
                excluded.append({**basis, "excluded_reason":
                                 f"记录状态为 {e.status.value}，尚未复核通过"})
                continue
            if e.frozen:
                excluded.append({**basis, "excluded_reason": "记录已被冻结，存在争议"})
                continue
            if activity.status not in _EFFECTIVE_STATUSES:
                excluded.append({**basis, "excluded_reason":
                                 f"活动状态为 {activity.status.value}，"
                                 "仅 active/completed 计时长"})
                continue
            if e.check_out is None:
                excluded.append({**basis, "excluded_reason": "尚未签退"})
                continue
            eff = _overlap_hours(e.check_in, e.check_out,
                                 activity.start_ts, activity.end_ts)
            if eff <= 0:
                excluded.append({**basis, "effective_hours": 0.0,
                                 "excluded_reason": "服务时段与活动有效时段无交集"})
                continue
            total += eff
            components.append({**basis, "effective_hours": round(eff, 6),
                               "calc": "min(签退,活动结束)-max(签到,活动开始)"})
        return HoursBreakdown(hours=round(total, 6),
                              components=tuple(components),
                              excluded=tuple(excluded))

    def _component(self, e: ServiceEntry, activity: Activity) -> dict:
        return {
            "entry_id": e.id,
            "activity_id": activity.id,
            "activity_title": activity.title,
            "activity_status": activity.status.value,
            "activity_window": [activity.start_ts, activity.end_ts],
            "volunteer_id": e.volunteer_id,
            "kind": e.kind.value,
            "check_in": e.check_in,
            "check_out": e.check_out,
            "raw_hours": round(e.raw_hours, 6),
            "entry_status": e.status.value,
            "frozen": e.frozen,
            "version": e.version,
        }

    # ------------------------------------------------------------------ #
    # 审计查询
    # ------------------------------------------------------------------ #

    def query_audit(self, actor: User, *, entity_type: str | None = None,
                    entity_id: str | None = None, actor_id: str | None = None,
                    action: str | None = None) -> list[AuditEvent]:
        """审计 trail 查询。志愿者只能看与本人相关的事件。"""
        if actor.role is Role.VOLUNTEER:
            if actor_id not in (None, actor.id):
                raise AuthzError("志愿者只能查询本人的审计记录")
            actor_id = actor.id
        rows = self.db.query_audit(entity_type=entity_type, entity_id=entity_id,
                                   actor_id=actor_id, action=action)
        return [AuditEvent(id=r["id"], ts=r["ts"], actor_id=r["actor_id"],
                           action=r["action"], entity_type=r["entity_type"],
                           entity_id=r["entity_id"],
                           detail=json.loads(r["detail"])) for r in rows]

    # ------------------------------------------------------------------ #
    # 对象读取
    # ------------------------------------------------------------------ #

    def get_user(self, user_id: str) -> User:
        r = self.db.conn.execute("SELECT * FROM users WHERE id=?",
                                 (user_id,)).fetchone()
        if r is None:
            raise NotFoundError(f"用户不存在: {user_id}")
        return User(id=r["id"], name=r["name"], role=Role(r["role"]),
                    org_id=r["org_id"],
                    authorized_org_ids=frozenset(json.loads(r["authorized_orgs"])))

    def get_group(self, group_id: str) -> Group:
        r = self._get_group_row(group_id)
        return Group(r["id"], r["name"], r["org_id"])

    def _get_group_row(self, group_id: str):
        r = self.db.conn.execute("SELECT * FROM groups WHERE id=?",
                                 (group_id,)).fetchone()
        if r is None:
            raise NotFoundError(f"小组不存在: {group_id}")
        return r

    def get_activity(self, activity_id: str) -> Activity:
        return self._activity_from_row(self._get_activity_row(activity_id))

    def _get_activity_row(self, activity_id: str):
        r = self.db.conn.execute("SELECT * FROM activities WHERE id=?",
                                 (activity_id,)).fetchone()
        if r is None:
            raise NotFoundError(f"活动不存在: {activity_id}")
        return r

    def _activity_from_row(self, r) -> Activity:
        return Activity(id=r["id"], title=r["title"], org_id=r["org_id"],
                        group_id=r["group_id"], start_ts=r["start_ts"],
                        end_ts=r["end_ts"], status=ActivityStatus(r["status"]))

    def get_entry(self, entry_id: str) -> ServiceEntry:
        r = self.db.conn.execute("SELECT * FROM entries WHERE id=?",
                                 (entry_id,)).fetchone()
        if r is None:
            raise NotFoundError(f"记录不存在: {entry_id}")
        return self._entry_from_row(r)

    def _entry_from_row(self, r) -> ServiceEntry:
        return ServiceEntry(
            id=r["id"], activity_id=r["activity_id"],
            volunteer_id=r["volunteer_id"], kind=EntryKind(r["kind"]),
            check_in=r["check_in"], check_out=r["check_out"],
            raw_hours=r["raw_hours"],
            effective_hours=0.0,  # 有效时长依赖活动状态，由汇总时计算
            status=EntryStatus(r["status"]), reason=r["reason"],
            import_key=r["import_key"], frozen=bool(r["frozen"]),
            created_by=r["created_by"], created_ts=r["created_ts"],
            version=r["version"])

    def _dispute_from_id(self, dispute_id: str) -> Dispute:
        r = self.db.conn.execute("SELECT * FROM disputes WHERE id=?",
                                 (dispute_id,)).fetchone()
        if r is None:
            raise NotFoundError(f"异议不存在: {dispute_id}")
        return self._dispute_from_row(r)

    def _dispute_from_row(self, r) -> Dispute:
        return Dispute(id=r["id"], volunteer_id=r["volunteer_id"],
                       entry_id=r["entry_id"],
                       activity_id=r["activity_id"],
                       reason=r["reason"],
                       status=DisputeStatus(r["status"]),
                       created_ts=r["created_ts"],
                       resolved_by=r["resolved_by"],
                       resolved_ts=r["resolved_ts"],
                       resolution_note=r["resolution_note"])

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _add_review(self, entry_id: str, action: str, actor_id: str,
                    detail: dict) -> None:
        row = self.db.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next FROM reviews WHERE entry_id=?",
            (entry_id,)).fetchone()
        self.db.conn.execute(
            "INSERT INTO reviews(id,entry_id,seq,action,actor_id,ts,reason,"
            "check_in,check_out) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, entry_id, row["next"], action, actor_id,
             self._clock(), detail.get("reason"),
             detail.get("check_in"), detail.get("check_out")))

    def _audit(self, actor_id: str, action: str, entity_type: str,
               entity_id: str, detail: dict) -> None:
        self.db.write_audit(self._clock(), actor_id, action,
                            entity_type, entity_id, detail)

    def _require_admin(self, actor: User) -> None:
        if actor.role is not Role.ADMIN:
            raise AuthzError("需要管理员权限")

    def _require_org_access(self, actor: User, org_id: str) -> None:
        if actor.role is Role.ADMIN:
            return
        if actor.role is Role.REVIEWER:
            if org_id not in actor.authorized_org_ids:
                raise AuthzError("该组织不在你的授权范围内")
            return
        raise AuthzError("需要组织管理权限")

    def _require_self_or_org(self, actor: User, volunteer_id: str,
                             org_id: str) -> None:
        if actor.role is Role.VOLUNTEER:
            if actor.id != volunteer_id:
                raise AuthzError("只能操作本人的签到")
            return
        self._require_org_access(actor, org_id)
