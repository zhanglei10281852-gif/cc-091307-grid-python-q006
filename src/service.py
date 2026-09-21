"""志愿服务登记系统——领域服务入口。

职责覆盖：活动与岗位登记、签到签退、补录/修改复核、组织审核、
异议处理、冻结管理、时长汇总与审计查询。

设计要点：
- 全部状态持久化在 SQLite（含冻结标记与复核链），进程重启后原样恢复；
- 原始数据只增不改不删，所有变更写入追加式审计日志；
- 时长按活动有效状态计算：取消的活动、待复核/已驳回/被冻结/未签退的记录均不计入；
- 跨午夜活动按实际签到签退时段计算；同一志愿者同一活动的重叠时段合并去重，
  重复签到与重复导入都不会累加。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .db import connect
from .errors import (
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from .models import (
    EXCLUDE_ACTIVITY_CANCELLED,
    EXCLUDE_FROZEN,
    EXCLUDE_INCOMPLETE,
    EXCLUDE_PENDING_REVIEW,
    EXCLUDE_RECORD_REJECTED,
    ActivityStatus,
    AmendmentStatus,
    ObjectionStatus,
    RecordSource,
    RecordStatus,
    Role,
)

# 允许的活动状态流转
_ACTIVITY_TRANSITIONS = {
    ActivityStatus.PLANNED: {ActivityStatus.ONGOING, ActivityStatus.CANCELLED},
    ActivityStatus.ONGOING: {ActivityStatus.COMPLETED, ActivityStatus.CANCELLED},
    ActivityStatus.COMPLETED: set(),
    ActivityStatus.CANCELLED: {ActivityStatus.PLANNED},  # 仅管理员可恢复，见 set_activity_status
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _to_utc(value: datetime | str, field: str) -> datetime:
    """把输入统一成带时区的 UTC datetime。"""
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 不是合法的 ISO-8601 时间: {value!r}") from exc
    if not isinstance(value, datetime):
        raise ValidationError(f"{field} 必须是 datetime 或 ISO-8601 字符串")
    if value.tzinfo is None:
        #  naive 时间按 UTC 处理，避免跨午夜/跨时区口径不一致
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _hours(seconds: float) -> float:
    return round(seconds / 3600.0, 2)


class VolunteerService:
    """志愿服务登记系统的外观（Facade）。

    参数：
        db_path: SQLite 文件路径，``":memory:"`` 表示纯内存（测试用）。
        clock:   可选的时钟注入，返回 aware datetime，便于测试。
    """

    def __init__(self, db_path: str | Path, clock: Callable[[], datetime] | None = None):
        self._conn: sqlite3.Connection = connect(db_path)
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.ready = True

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()
            self.ready = False

    def __enter__(self) -> "VolunteerService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _now(self) -> datetime:
        return _to_utc(self._clock(), "now")

    def _audit(
        self,
        actor_id: str | None,
        action: str,
        entity_type: str,
        entity_id: str,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO audit_log (ts, actor_id, action, entity_type, entity_id, reason, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(self._now()),
                actor_id,
                action,
                entity_type,
                entity_id,
                reason,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _get(self, table: str, row_id: str, label: str) -> sqlite3.Row:
        row = self._conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{label}不存在: {row_id}")
        return row

    def _require_role(self, user_id: str, *roles: Role) -> sqlite3.Row:
        user = self._get("users", user_id, "用户")
        if user["role"] not in {r.value for r in roles}:
            raise PermissionDeniedError(
                f"用户 {user_id} 的角色 {user['role']} 无权执行该操作，需要: "
                + "/".join(r.value for r in roles)
            )
        return user

    def _is_admin(self, user_id: str) -> bool:
        row = self._conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        return row is not None and row["role"] == Role.ADMIN.value

    def _has_scope(self, user_id: str, org_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM reviewer_scopes WHERE user_id = ? AND org_id = ?",
            (user_id, org_id),
        ).fetchone()
        return row is not None

    def _require_org_reviewer(self, user_id: str, org_id: str) -> None:
        """审核员只能处理授权组织；管理员不受范围限制。"""
        if self._is_admin(user_id):
            return
        user = self._get("users", user_id, "用户")
        if user["role"] == Role.REVIEWER.value and self._has_scope(user_id, org_id):
            return
        raise PermissionDeniedError(f"用户 {user_id} 不是组织 {org_id} 的授权审核员")

    def _activity_org(self, activity_id: str) -> str:
        return self._get("activities", activity_id, "活动")["org_id"]

    def _record_org(self, record: sqlite3.Row) -> str:
        return self._activity_org(record["activity_id"])

    @staticmethod
    def _require_reason(reason: str | None, what: str) -> str:
        if reason is None or not str(reason).strip():
            raise ValidationError(f"{what}必须说明原因")
        return str(reason).strip()

    @staticmethod
    def _check_window(check_in: datetime, check_out: datetime) -> None:
        if check_out <= check_in:
            raise ValidationError("签退时间必须晚于签到时间")

    # ------------------------------------------------------------------
    # 组织与账号
    # ------------------------------------------------------------------
    def create_user(self, actor_id: str | None, name: str, role: str | Role) -> str:
        """创建用户。系统内没有任何用户时允许匿名引导创建首个管理员。"""
        role_value = Role(role).value if not isinstance(role, Role) else role.value
        with self._lock, self._conn:
            user_count = self._conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            if user_count == 0:
                if role_value != Role.ADMIN.value:
                    raise ValidationError("首个用户必须是管理员（引导初始化）")
            else:
                if actor_id is None:
                    raise PermissionDeniedError("创建用户需要管理员身份")
                self._require_role(actor_id, Role.ADMIN)
            uid = _new_id("usr")
            self._conn.execute(
                "INSERT INTO users (id, name, role, created_at) VALUES (?, ?, ?, ?)",
                (uid, name.strip(), role_value, _iso(self._now())),
            )
            self._audit(actor_id or uid, "CREATE_USER", "user", uid,
                        detail={"name": name, "role": role_value})
            return uid

    def create_organization(
        self, actor_id: str, name: str, parent_id: str | None = None
    ) -> str:
        with self._lock, self._conn:
            self._require_role(actor_id, Role.ADMIN)
            if parent_id is not None:
                self._get("organizations", parent_id, "上级组织")
            oid = _new_id("org")
            self._conn.execute(
                "INSERT INTO organizations (id, name, parent_id, created_at) VALUES (?, ?, ?, ?)",
                (oid, name.strip(), parent_id, _iso(self._now())),
            )
            self._audit(actor_id, "CREATE_ORG", "organization", oid,
                        detail={"name": name, "parent_id": parent_id})
            return oid

    def add_membership(self, actor_id: str, user_id: str, org_id: str) -> None:
        """把志愿者加入小组。管理员或该组织的授权审核员可操作。"""
        with self._lock, self._conn:
            self._require_org_reviewer(actor_id, org_id)
            self._get("users", user_id, "用户")
            self._get("organizations", org_id, "组织")
            self._conn.execute(
                "INSERT OR IGNORE INTO memberships (user_id, org_id, created_at) VALUES (?, ?, ?)",
                (user_id, org_id, _iso(self._now())),
            )
            self._audit(actor_id, "ADD_MEMBERSHIP", "organization", org_id,
                        detail={"user_id": user_id})

    def grant_reviewer_scope(self, actor_id: str, reviewer_id: str, org_id: str) -> None:
        """授权审核员负责某个组织。仅管理员可操作。"""
        with self._lock, self._conn:
            self._require_role(actor_id, Role.ADMIN)
            self._require_role(reviewer_id, Role.REVIEWER)
            self._get("organizations", org_id, "组织")
            self._conn.execute(
                "INSERT OR IGNORE INTO reviewer_scopes (user_id, org_id, granted_by, created_at)"
                " VALUES (?, ?, ?, ?)",
                (reviewer_id, org_id, actor_id, _iso(self._now())),
            )
            self._audit(actor_id, "GRANT_SCOPE", "organization", org_id,
                        detail={"reviewer_id": reviewer_id})

    # ------------------------------------------------------------------
    # 活动与岗位
    # ------------------------------------------------------------------
    def create_activity(
        self,
        actor_id: str,
        org_id: str,
        title: str,
        planned_start: datetime | str,
        planned_end: datetime | str,
    ) -> str:
        start = _to_utc(planned_start, "planned_start")
        end = _to_utc(planned_end, "planned_end")
        self._check_window(start, end)
        with self._lock, self._conn:
            self._require_org_reviewer(actor_id, org_id)
            self._get("organizations", org_id, "组织")
            aid = _new_id("act")
            self._conn.execute(
                "INSERT INTO activities (id, org_id, title, planned_start, planned_end, status,"
                " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (aid, org_id, title.strip(), _iso(start), _iso(end),
                 ActivityStatus.PLANNED.value, actor_id, _iso(self._now())),
            )
            self._audit(actor_id, "CREATE_ACTIVITY", "activity", aid,
                        detail={"org_id": org_id, "title": title,
                                "planned_start": _iso(start), "planned_end": _iso(end)})
            return aid

    def set_activity_status(
        self,
        actor_id: str,
        activity_id: str,
        status: str | ActivityStatus,
        reason: str | None = None,
    ) -> None:
        """活动状态流转。取消必须说明原因；取消后恢复仅管理员可操作。"""
        target = status if isinstance(status, ActivityStatus) else ActivityStatus(status)
        with self._lock, self._conn:
            activity = self._get("activities", activity_id, "活动")
            self._require_org_reviewer(actor_id, activity["org_id"])
            current = ActivityStatus(activity["status"])
            if target not in _ACTIVITY_TRANSITIONS[current]:
                raise StateError(f"活动状态不允许从 {current.value} 变更为 {target.value}")
            if target == ActivityStatus.CANCELLED:
                reason = self._require_reason(reason, "取消活动")
            if current == ActivityStatus.CANCELLED and not self._is_admin(actor_id):
                raise PermissionDeniedError("已取消的活动只能由管理员恢复")
            self._conn.execute(
                "UPDATE activities SET status = ? WHERE id = ?",
                (target.value, activity_id),
            )
            self._audit(actor_id, "ACTIVITY_STATUS", "activity", activity_id, reason=reason,
                        detail={"from": current.value, "to": target.value})

    def add_position(
        self, actor_id: str, activity_id: str, name: str, quota: int | None = None
    ) -> str:
        with self._lock, self._conn:
            activity = self._get("activities", activity_id, "活动")
            self._require_org_reviewer(actor_id, activity["org_id"])
            pid = _new_id("pos")
            try:
                self._conn.execute(
                    "INSERT INTO positions (id, activity_id, name, quota, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (pid, activity_id, name.strip(), quota, _iso(self._now())),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationError(f"活动 {activity_id} 下已存在岗位 {name!r}") from exc
            self._audit(actor_id, "CREATE_POSITION", "position", pid,
                        detail={"activity_id": activity_id, "name": name, "quota": quota})
            return pid

    # ------------------------------------------------------------------
    # 签到签退
    # ------------------------------------------------------------------
    def _check_attendance_actor(self, actor_id: str, volunteer_id: str, org_id: str) -> None:
        if actor_id == volunteer_id:
            return
        self._require_org_reviewer(actor_id, org_id)

    def _validate_position(self, activity_id: str, position_id: str | None) -> None:
        if position_id is None:
            return
        position = self._get("positions", position_id, "岗位")
        if position["activity_id"] != activity_id:
            raise ValidationError(f"岗位 {position_id} 不属于活动 {activity_id}")

    def check_in(
        self,
        actor_id: str,
        activity_id: str,
        volunteer_id: str,
        position_id: str | None = None,
        at: datetime | str | None = None,
    ) -> str:
        """现场签到。同一志愿者在同一活动已有未签退记录时，返回原记录（幂等，不重复计时）。"""
        moment = _to_utc(at, "at") if at is not None else self._now()
        with self._lock, self._conn:
            activity = self._get("activities", activity_id, "活动")
            if activity["status"] == ActivityStatus.CANCELLED.value:
                raise StateError("活动已取消，不能签到")
            if activity["status"] == ActivityStatus.COMPLETED.value:
                raise StateError("活动已结束，请使用补录流程")
            self._check_attendance_actor(actor_id, volunteer_id, activity["org_id"])
            self._require_role(volunteer_id, Role.VOLUNTEER)
            self._validate_position(activity_id, position_id)
            open_rec = self._conn.execute(
                "SELECT id FROM attendance_records"
                " WHERE activity_id = ? AND volunteer_id = ? AND check_out_at IS NULL"
                " AND status != ?",
                (activity_id, volunteer_id, RecordStatus.REJECTED.value),
            ).fetchone()
            if open_rec is not None:
                return open_rec["id"]  # 重复签到：幂等返回原记录
            rid = _new_id("rec")
            now_iso = _iso(self._now())
            self._conn.execute(
                "INSERT INTO attendance_records (id, activity_id, volunteer_id, position_id,"
                " check_in_at, source, status, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, activity_id, volunteer_id, position_id, _iso(moment),
                 RecordSource.ONSITE.value, RecordStatus.EFFECTIVE.value,
                 actor_id, now_iso, now_iso),
            )
            self._audit(actor_id, "CHECK_IN", "attendance_record", rid,
                        detail={"activity_id": activity_id, "volunteer_id": volunteer_id,
                                "position_id": position_id, "check_in_at": _iso(moment)})
            return rid

    def check_out(
        self,
        actor_id: str,
        record_id: str,
        at: datetime | str | None = None,
    ) -> None:
        moment = _to_utc(at, "at") if at is not None else self._now()
        with self._lock, self._conn:
            record = self._get("attendance_records", record_id, "签到记录")
            org_id = self._record_org(record)
            self._check_attendance_actor(actor_id, record["volunteer_id"], org_id)
            if record["frozen"]:
                raise StateError("记录已被冻结，解冻后才能签退")
            if record["status"] == RecordStatus.REJECTED.value:
                raise StateError("记录已被驳回，不能签退")
            if record["check_out_at"] is not None:
                raise StateError("记录已签退，如需调整请走修改申请流程")
            check_in = _to_utc(record["check_in_at"], "check_in_at")
            self._check_window(check_in, moment)
            self._conn.execute(
                "UPDATE attendance_records SET check_out_at = ?, updated_at = ? WHERE id = ?",
                (_iso(moment), _iso(self._now()), record_id),
            )
            self._audit(actor_id, "CHECK_OUT", "attendance_record", record_id,
                        detail={"check_out_at": _iso(moment)})

    # ------------------------------------------------------------------
    # 补录与修改（必须说明原因并经过复核）
    # ------------------------------------------------------------------
    def backfill_record(
        self,
        actor_id: str,
        activity_id: str,
        volunteer_id: str,
        check_in: datetime | str,
        check_out: datetime | str,
        reason: str,
        position_id: str | None = None,
    ) -> str:
        """事后补录。志愿者补录进入待复核；授权审核员/管理员补录视同已复核。"""
        reason = self._require_reason(reason, "补录")
        start = _to_utc(check_in, "check_in")
        end = _to_utc(check_out, "check_out")
        self._check_window(start, end)
        with self._lock, self._conn:
            activity = self._get("activities", activity_id, "活动")
            if activity["status"] == ActivityStatus.CANCELLED.value:
                raise StateError("活动已取消，不能补录")
            org_id = activity["org_id"]
            self._check_attendance_actor(actor_id, volunteer_id, org_id)
            self._require_role(volunteer_id, Role.VOLUNTEER)
            self._validate_position(activity_id, position_id)
            reviewed = actor_id != volunteer_id  # 审核员/管理员代录，创建即复核
            rid = _new_id("rec")
            now_iso = _iso(self._now())
            self._conn.execute(
                "INSERT INTO attendance_records (id, activity_id, volunteer_id, position_id,"
                " check_in_at, check_out_at, source, status, reason, review_comment,"
                " reviewed_by, reviewed_at, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, activity_id, volunteer_id, position_id, _iso(start), _iso(end),
                 RecordSource.BACKFILL.value,
                 RecordStatus.EFFECTIVE.value if reviewed else RecordStatus.PENDING.value,
                 reason, "审核员/管理员补录，创建即复核" if reviewed else None,
                 actor_id if reviewed else None, now_iso if reviewed else None,
                 actor_id, now_iso, now_iso),
            )
            self._audit(actor_id, "BACKFILL", "attendance_record", rid, reason=reason,
                        detail={"activity_id": activity_id, "volunteer_id": volunteer_id,
                                "check_in_at": _iso(start), "check_out_at": _iso(end),
                                "auto_reviewed": reviewed})
            return rid

    def approve_record(self, actor_id: str, record_id: str, comment: str | None = None) -> None:
        self._review_record(actor_id, record_id, approve=True, comment=comment)

    def reject_record(self, actor_id: str, record_id: str, comment: str | None = None) -> None:
        self._review_record(actor_id, record_id, approve=False, comment=comment)

    def _review_record(
        self, actor_id: str, record_id: str, approve: bool, comment: str | None
    ) -> None:
        with self._lock, self._conn:
            record = self._get("attendance_records", record_id, "签到记录")
            self._require_org_reviewer(actor_id, self._record_org(record))
            if record["status"] != RecordStatus.PENDING.value:
                raise StateError(f"记录当前状态为 {record['status']}，不能复核")
            new_status = RecordStatus.EFFECTIVE if approve else RecordStatus.REJECTED
            now_iso = _iso(self._now())
            self._conn.execute(
                "UPDATE attendance_records SET status = ?, review_comment = ?,"
                " reviewed_by = ?, reviewed_at = ?, updated_at = ? WHERE id = ?",
                (new_status.value, comment, actor_id, now_iso, now_iso, record_id),
            )
            self._audit(actor_id,
                        "APPROVE_RECORD" if approve else "REJECT_RECORD",
                        "attendance_record", record_id,
                        reason=comment, detail={"from": RecordStatus.PENDING.value,
                                                "to": new_status.value})

    def request_amendment(
        self,
        actor_id: str,
        record_id: str,
        check_in: datetime | str,
        check_out: datetime | str,
        reason: str,
    ) -> str:
        """申请修改签到签退时段。授权审核员/管理员提交的修改立即生效并留痕。"""
        reason = self._require_reason(reason, "修改记录")
        start = _to_utc(check_in, "check_in")
        end = _to_utc(check_out, "check_out")
        self._check_window(start, end)
        with self._lock, self._conn:
            record = self._get("attendance_records", record_id, "签到记录")
            org_id = self._record_org(record)
            self._check_attendance_actor(actor_id, record["volunteer_id"], org_id)
            if record["frozen"]:
                raise StateError("记录已被冻结，解冻后才能修改")
            if record["status"] == RecordStatus.REJECTED.value:
                raise StateError("记录已被驳回，不能修改")
            auto = actor_id != record["volunteer_id"]  # 审核员/管理员修改立即生效
            aid = _new_id("amd")
            now_iso = _iso(self._now())
            self._conn.execute(
                "INSERT INTO amendments (id, record_id, proposed_check_in, proposed_check_out,"
                " reason, status, requested_by, reviewed_by, created_at, reviewed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (aid, record_id, _iso(start), _iso(end), reason,
                 AmendmentStatus.APPROVED.value if auto else AmendmentStatus.PENDING.value,
                 actor_id, actor_id if auto else None, now_iso, now_iso if auto else None),
            )
            self._audit(actor_id, "REQUEST_AMENDMENT", "amendment", aid, reason=reason,
                        detail={"record_id": record_id, "proposed_check_in": _iso(start),
                                "proposed_check_out": _iso(end), "auto_approved": auto})
            if auto:
                self._apply_amendment(actor_id, record, start, end, aid)
            return aid

    def _apply_amendment(
        self,
        actor_id: str,
        record: sqlite3.Row,
        start: datetime,
        end: datetime,
        amendment_id: str,
    ) -> None:
        before = {"check_in_at": record["check_in_at"], "check_out_at": record["check_out_at"]}
        self._conn.execute(
            "UPDATE attendance_records SET check_in_at = ?, check_out_at = ?, updated_at = ?"
            " WHERE id = ?",
            (_iso(start), _iso(end), _iso(self._now()), record["id"]),
        )
        self._audit(actor_id, "APPLY_AMENDMENT", "attendance_record", record["id"],
                    detail={"amendment_id": amendment_id, "before": before,
                            "after": {"check_in_at": _iso(start), "check_out_at": _iso(end)}})

    def approve_amendment(self, actor_id: str, amendment_id: str, comment: str | None = None) -> None:
        with self._lock, self._conn:
            amendment = self._get("amendments", amendment_id, "修改申请")
            record = self._get("attendance_records", amendment["record_id"], "签到记录")
            self._require_org_reviewer(actor_id, self._record_org(record))
            if amendment["status"] != AmendmentStatus.PENDING.value:
                raise StateError(f"修改申请当前状态为 {amendment['status']}，不能复核")
            if record["frozen"]:
                raise StateError("记录已被冻结，解冻后才能应用修改")
            now_iso = _iso(self._now())
            self._conn.execute(
                "UPDATE amendments SET status = ?, reviewed_by = ?, review_comment = ?,"
                " reviewed_at = ? WHERE id = ?",
                (AmendmentStatus.APPROVED.value, actor_id, comment, now_iso, amendment_id),
            )
            self._audit(actor_id, "APPROVE_AMENDMENT", "amendment", amendment_id,
                        reason=comment, detail={"record_id": record["id"]})
            start = _to_utc(amendment["proposed_check_in"], "proposed_check_in")
            end = _to_utc(amendment["proposed_check_out"], "proposed_check_out")
            self._apply_amendment(actor_id, record, start, end, amendment_id)

    def reject_amendment(self, actor_id: str, amendment_id: str, comment: str | None = None) -> None:
        with self._lock, self._conn:
            amendment = self._get("amendments", amendment_id, "修改申请")
            record = self._get("attendance_records", amendment["record_id"], "签到记录")
            self._require_org_reviewer(actor_id, self._record_org(record))
            if amendment["status"] != AmendmentStatus.PENDING.value:
                raise StateError(f"修改申请当前状态为 {amendment['status']}，不能复核")
            self._conn.execute(
                "UPDATE amendments SET status = ?, reviewed_by = ?, review_comment = ?,"
                " reviewed_at = ? WHERE id = ?",
                (AmendmentStatus.REJECTED.value, actor_id, comment, _iso(self._now()),
                 amendment_id),
            )
            self._audit(actor_id, "REJECT_AMENDMENT", "amendment", amendment_id,
                        reason=comment, detail={"record_id": record["id"]})

    # ------------------------------------------------------------------
    # 批量导入（幂等：重复导入不累加）
    # ------------------------------------------------------------------
    def import_records(
        self, actor_id: str, rows: Iterable[dict[str, Any]]
    ) -> dict[str, Any]:
        """批量导入历史记录。

        每行必须携带 ``source_ref``（外部唯一键）。批次按内容哈希幂等：
        同一批数据重复导入直接返回首次导入的结果；批次内/跨批次的重复
        ``source_ref`` 会被跳过，不会重复计时。导入人必须是每行活动所属
        组织的授权审核员或管理员（导入行为本身即视同组织复核）。
        """
        rows = [dict(r) for r in rows]
        if not rows:
            raise ValidationError("导入内容为空")
        for i, row in enumerate(rows):
            for field in ("source_ref", "activity_id", "volunteer_id", "check_in", "check_out"):
                if not row.get(field):
                    raise ValidationError(f"第 {i + 1} 行缺少字段 {field}")
        canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM import_batches WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if existing is not None:
                return {**dict(existing), "already_imported": True}

            # 权限：逐行校验导入人对活动所属组织的审核权
            prepared = []
            for i, row in enumerate(rows):
                activity = self._get("activities", row["activity_id"], "活动")
                self._require_org_reviewer(actor_id, activity["org_id"])
                self._require_role(row["volunteer_id"], Role.VOLUNTEER)
                start = _to_utc(row["check_in"], f"第{i + 1}行 check_in")
                end = _to_utc(row["check_out"], f"第{i + 1}行 check_out")
                self._check_window(start, end)
                position_id = row.get("position_id")
                self._validate_position(row["activity_id"], position_id)
                prepared.append((row, start, end, position_id))

            batch_id = _new_id("imp")
            now_iso = _iso(self._now())
            # 先建批次占位（记录外键指向批次），再插记录，最后回填统计
            self._conn.execute(
                "INSERT INTO import_batches (id, content_hash, imported_by, created_at,"
                " row_count, inserted_count, skipped_count) VALUES (?, ?, ?, ?, ?, 0, 0)",
                (batch_id, content_hash, actor_id, now_iso, len(rows)),
            )
            inserted = skipped = 0
            for row, start, end, position_id in prepared:
                dup = self._conn.execute(
                    "SELECT 1 FROM attendance_records WHERE source_ref = ?",
                    (row["source_ref"],),
                ).fetchone()
                if dup is not None:
                    skipped += 1
                    continue
                rid = _new_id("rec")
                self._conn.execute(
                    "INSERT INTO attendance_records (id, activity_id, volunteer_id, position_id,"
                    " check_in_at, check_out_at, source, status, import_batch_id, source_ref,"
                    " reviewed_by, reviewed_at, created_by, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rid, row["activity_id"], row["volunteer_id"], position_id,
                     _iso(start), _iso(end), RecordSource.IMPORT.value,
                     RecordStatus.EFFECTIVE.value, batch_id, row["source_ref"],
                     actor_id, now_iso, actor_id, now_iso, now_iso),
                )
                inserted += 1
            self._conn.execute(
                "UPDATE import_batches SET inserted_count = ?, skipped_count = ? WHERE id = ?",
                (inserted, skipped, batch_id),
            )
            self._audit(actor_id, "IMPORT", "import_batch", batch_id,
                        detail={"row_count": len(rows), "inserted": inserted,
                                "skipped": skipped, "content_hash": content_hash})
            return {"id": batch_id, "content_hash": content_hash, "imported_by": actor_id,
                    "created_at": now_iso, "row_count": len(rows),
                    "inserted_count": inserted, "skipped_count": skipped,
                    "already_imported": False}

    # ------------------------------------------------------------------
    # 异议处理
    # ------------------------------------------------------------------
    def file_objection(self, actor_id: str, record_id: str, reason: str) -> str:
        """志愿者对本人记录提出异议；管理员可代为登记。"""
        reason = self._require_reason(reason, "提出异议")
        with self._lock, self._conn:
            record = self._get("attendance_records", record_id, "签到记录")
            if actor_id != record["volunteer_id"] and not self._is_admin(actor_id):
                raise PermissionDeniedError("只能对本人的签到记录提出异议")
            oid = _new_id("obj")
            self._conn.execute(
                "INSERT INTO objections (id, record_id, raised_by, reason, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (oid, record_id, actor_id, reason, ObjectionStatus.OPEN.value,
                 _iso(self._now())),
            )
            self._conn.execute(
                "UPDATE attendance_records SET disputed = 1, updated_at = ? WHERE id = ?",
                (_iso(self._now()), record_id),
            )
            self._audit(actor_id, "FILE_OBJECTION", "objection", oid, reason=reason,
                        detail={"record_id": record_id})
            return oid

    def resolve_objection(
        self, actor_id: str, objection_id: str, upheld: bool, note: str
    ) -> None:
        """处理异议。仅记录所属组织的授权审核员或管理员可操作。"""
        note = self._require_reason(note, "异议处理结论")
        with self._lock, self._conn:
            objection = self._get("objections", objection_id, "异议")
            record = self._get("attendance_records", objection["record_id"], "签到记录")
            self._require_org_reviewer(actor_id, self._record_org(record))
            if objection["status"] != ObjectionStatus.OPEN.value:
                raise StateError("该异议已处理，不能重复处理")
            status = (ObjectionStatus.RESOLVED_UPHELD if upheld
                      else ObjectionStatus.RESOLVED_REJECTED)
            self._conn.execute(
                "UPDATE objections SET status = ?, resolved_by = ?, resolution_note = ?,"
                " resolved_at = ? WHERE id = ?",
                (status.value, actor_id, note, _iso(self._now()), objection_id),
            )
            still_open = self._conn.execute(
                "SELECT 1 FROM objections WHERE record_id = ? AND status = ?",
                (record["id"], ObjectionStatus.OPEN.value),
            ).fetchone()
            if still_open is None:
                self._conn.execute(
                    "UPDATE attendance_records SET disputed = 0, updated_at = ? WHERE id = ?",
                    (_iso(self._now()), record["id"]),
                )
            self._audit(actor_id, "RESOLVE_OBJECTION", "objection", objection_id,
                        reason=note, detail={"record_id": record["id"],
                                             "upheld": upheld})

    # ------------------------------------------------------------------
    # 冻结管理（仅管理员；原始数据保留，冻结期间不计入时长）
    # ------------------------------------------------------------------
    def freeze_record(self, actor_id: str, record_id: str, reason: str) -> None:
        reason = self._require_reason(reason, "冻结记录")
        with self._lock, self._conn:
            self._require_role(actor_id, Role.ADMIN)
            record = self._get("attendance_records", record_id, "签到记录")
            if record["frozen"]:
                raise StateError("记录已处于冻结状态")
            self._conn.execute(
                "UPDATE attendance_records SET frozen = 1, updated_at = ? WHERE id = ?",
                (_iso(self._now()), record_id),
            )
            self._audit(actor_id, "FREEZE", "attendance_record", record_id, reason=reason)

    def unfreeze_record(self, actor_id: str, record_id: str, reason: str) -> None:
        reason = self._require_reason(reason, "解冻记录")
        with self._lock, self._conn:
            self._require_role(actor_id, Role.ADMIN)
            record = self._get("attendance_records", record_id, "签到记录")
            if not record["frozen"]:
                raise StateError("记录未处于冻结状态")
            self._conn.execute(
                "UPDATE attendance_records SET frozen = 0, updated_at = ? WHERE id = ?",
                (_iso(self._now()), record_id),
            )
            self._audit(actor_id, "UNFREEZE", "attendance_record", record_id, reason=reason)

    # ------------------------------------------------------------------
    # 时长计算
    # ------------------------------------------------------------------
    @staticmethod
    def _exclusion_reason(record: sqlite3.Row, activity: sqlite3.Row) -> str | None:
        """返回 None 表示计入时长，否则返回排除原因码。"""
        if record["status"] == RecordStatus.REJECTED.value:
            return EXCLUDE_RECORD_REJECTED
        if record["status"] == RecordStatus.PENDING.value:
            return EXCLUDE_PENDING_REVIEW
        if record["frozen"]:
            return EXCLUDE_FROZEN
        if activity["status"] == ActivityStatus.CANCELLED.value:
            return EXCLUDE_ACTIVITY_CANCELLED
        if record["check_out_at"] is None:
            return EXCLUDE_INCOMPLETE
        return None

    @staticmethod
    def _merge_intervals(items: list[tuple[datetime, datetime, str]]):
        """合并重叠时段，保证重复签到/重复导入不累加。返回 [(start, end, [record_ids])]。"""
        merged: list[list[Any]] = []
        for start, end, rid in sorted(items, key=lambda x: (x[0], x[1])):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
                merged[-1][2].append(rid)
            else:
                merged.append([start, end, [rid]])
        return [(s, e, ids) for s, e, ids in merged]

    def _summarize_records(
        self,
        records: list[sqlite3.Row],
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
        """把记录分成“计入”与“排除”两类，并对计入的按 (志愿者, 活动) 合并时段。"""
        activities = {
            row["id"]: row
            for row in self._conn.execute(
                "SELECT * FROM activities WHERE id IN (%s)"
                % ",".join("?" * len({r['activity_id'] for r in records})),
                tuple({r["activity_id"] for r in records}),
            ).fetchall()
        } if records else {}

        excluded: list[dict[str, Any]] = []
        counted: dict[tuple[str, str], list[tuple[datetime, datetime, str]]] = {}
        for record in records:
            activity = activities[record["activity_id"]]
            reason = self._exclusion_reason(record, activity)
            if reason is not None:
                excluded.append({
                    "record_id": record["id"],
                    "activity_id": record["activity_id"],
                    "activity_title": activity["title"],
                    "volunteer_id": record["volunteer_id"],
                    "reason": reason,
                    "record_status": record["status"],
                    "frozen": bool(record["frozen"]),
                    "check_in_at": record["check_in_at"],
                    "check_out_at": record["check_out_at"],
                })
                continue
            ci = _to_utc(record["check_in_at"], "check_in_at")
            co = _to_utc(record["check_out_at"], "check_out_at")
            # 与统计区间求交，按实际时段计算
            lo = max(ci, start) if start else ci
            hi = min(co, end) if end else co
            if hi <= lo:
                continue
            key = (record["volunteer_id"], record["activity_id"])
            counted.setdefault(key, []).append((lo, hi, record["id"]))

        entries: list[dict[str, Any]] = []
        total_seconds = 0.0
        for (volunteer_id, activity_id), items in sorted(counted.items()):
            activity = activities[activity_id]
            intervals = []
            hours = 0.0
            for lo, hi, record_ids in self._merge_intervals(items):
                secs = (hi - lo).total_seconds()
                total_seconds += secs
                hours += secs
                intervals.append({
                    "start": _iso(lo), "end": _iso(hi),
                    "hours": _hours(secs), "record_ids": sorted(record_ids),
                })
            entries.append({
                "volunteer_id": volunteer_id,
                "activity_id": activity_id,
                "activity_title": activity["title"],
                "activity_status": activity["status"],
                "org_id": activity["org_id"],
                "intervals": intervals,
                "hours": _hours(hours),
            })
        return entries, excluded, total_seconds

    def volunteer_summary(
        self,
        actor_id: str,
        volunteer_id: str,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[str, Any]:
        """个人时长汇总：返回总时长、计入明细（含计算依据）与排除明细（含原因）。"""
        lo = _to_utc(start, "start") if start is not None else None
        hi = _to_utc(end, "end") if end is not None else None
        if lo and hi and hi <= lo:
            raise ValidationError("统计区间 end 必须晚于 start")
        with self._lock:
            volunteer = self._require_role(volunteer_id, Role.VOLUNTEER)
            if actor_id != volunteer_id and not self._is_admin(actor_id):
                member_orgs = {
                    r["org_id"] for r in self._conn.execute(
                        "SELECT org_id FROM memberships WHERE user_id = ?", (volunteer_id,)
                    ).fetchall()
                }
                if not any(self._has_scope(actor_id, oid) for oid in member_orgs):
                    raise PermissionDeniedError("只能查看本人或授权组织成员的时长汇总")
            records = self._conn.execute(
                "SELECT * FROM attendance_records WHERE volunteer_id = ?", (volunteer_id,)
            ).fetchall()
            entries, excluded, total_seconds = self._summarize_records(records, lo, hi)
            return {
                "volunteer_id": volunteer_id,
                "volunteer_name": volunteer["name"],
                "period": {"start": _iso(lo) if lo else None, "end": _iso(hi) if hi else None},
                "total_hours": _hours(total_seconds),
                "entries": entries,
                "excluded": excluded,
                "generated_at": _iso(self._now()),
            }

    def group_summary(
        self,
        actor_id: str,
        org_id: str,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[str, Any]:
        """小组时长汇总：小组活动下全部有效记录的时长，含成员与活动两个维度。"""
        lo = _to_utc(start, "start") if start is not None else None
        hi = _to_utc(end, "end") if end is not None else None
        if lo and hi and hi <= lo:
            raise ValidationError("统计区间 end 必须晚于 start")
        with self._lock:
            org = self._get("organizations", org_id, "组织")
            is_member = self._conn.execute(
                "SELECT 1 FROM memberships WHERE user_id = ? AND org_id = ?",
                (actor_id, org_id),
            ).fetchone()
            if is_member is None:
                self._require_org_reviewer(actor_id, org_id)
            records = self._conn.execute(
                "SELECT r.* FROM attendance_records r"
                " JOIN activities a ON a.id = r.activity_id WHERE a.org_id = ?",
                (org_id,),
            ).fetchall()
            entries, excluded, total_seconds = self._summarize_records(records, lo, hi)

            names = {
                row["id"]: row["name"]
                for row in self._conn.execute("SELECT id, name FROM users").fetchall()
            }
            by_volunteer: dict[str, float] = {}
            by_activity: dict[str, float] = {}
            for entry in entries:
                by_volunteer[entry["volunteer_id"]] = (
                    by_volunteer.get(entry["volunteer_id"], 0.0) + entry["hours"]
                )
                by_activity[entry["activity_id"]] = (
                    by_activity.get(entry["activity_id"], 0.0) + entry["hours"]
                )
            # 花名册中没有记录的成员也列出，时长为 0
            for member in self._conn.execute(
                "SELECT user_id FROM memberships WHERE org_id = ?", (org_id,)
            ).fetchall():
                by_volunteer.setdefault(member["user_id"], 0.0)
            activity_titles = {
                row["id"]: (row["title"], row["status"])
                for row in self._conn.execute(
                    "SELECT id, title, status FROM activities WHERE org_id = ?", (org_id,)
                ).fetchall()
            }
            return {
                "org_id": org_id,
                "org_name": org["name"],
                "period": {"start": _iso(lo) if lo else None, "end": _iso(hi) if hi else None},
                "total_hours": _hours(total_seconds),
                "by_volunteer": [
                    {"volunteer_id": uid, "volunteer_name": names.get(uid, uid),
                     "hours": round(h, 2)}
                    for uid, h in sorted(by_volunteer.items())
                ],
                "by_activity": [
                    {"activity_id": aid,
                     "activity_title": activity_titles.get(aid, (aid, ""))[0],
                     "activity_status": activity_titles.get(aid, ("", ""))[1],
                     "hours": round(h, 2)}
                    for aid, h in sorted(by_activity.items())
                ],
                "entries": entries,
                "excluded": excluded,
                "generated_at": _iso(self._now()),
            }

    # ------------------------------------------------------------------
    # 明细、异议与审计查询
    # ------------------------------------------------------------------
    def get_record(self, actor_id: str, record_id: str) -> dict[str, Any]:
        """记录明细与完整审核链：记录本体、修改申请、异议、审计轨迹。"""
        with self._lock:
            record = self._get("attendance_records", record_id, "签到记录")
            org_id = self._record_org(record)
            if actor_id != record["volunteer_id"] and not self._is_admin(actor_id):
                self._require_org_reviewer(actor_id, org_id)
            activity = self._get("activities", record["activity_id"], "活动")
            position = None
            if record["position_id"]:
                position = dict(self._get("positions", record["position_id"], "岗位"))
            amendments = [
                dict(row) for row in self._conn.execute(
                    "SELECT * FROM amendments WHERE record_id = ? ORDER BY created_at, id",
                    (record_id,),
                ).fetchall()
            ]
            objections = [
                dict(row) for row in self._conn.execute(
                    "SELECT * FROM objections WHERE record_id = ? ORDER BY created_at, id",
                    (record_id,),
                ).fetchall()
            ]
            trail = self._audit_trail("attendance_record", record_id)
            # 修改申请的审计轨迹也并入审核链
            for amendment in amendments:
                trail.extend(self._audit_trail("amendment", amendment["id"]))
            for objection in objections:
                trail.extend(self._audit_trail("objection", objection["id"]))
            trail.sort(key=lambda e: (e["ts"], e["id"]))
            return {
                "record": dict(record),
                "activity": dict(activity),
                "position": position,
                "amendments": amendments,
                "objections": objections,
                "audit_trail": trail,
            }

    def _audit_trail(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM audit_log WHERE entity_type = ? AND entity_id = ? ORDER BY id",
            (entity_type, entity_id),
        ).fetchall()
        return [
            {**{k: row[k] for k in row.keys() if k != "detail"},
             "detail": json.loads(row["detail"])}
            for row in rows
        ]

    def list_objections(
        self, actor_id: str, org_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """异议列表。管理员看全部；审核员只看授权组织；志愿者只看本人提出的。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.*, a.org_id AS org_id FROM objections o"
                " JOIN attendance_records r ON r.id = o.record_id"
                " JOIN activities a ON a.id = r.activity_id ORDER BY o.created_at, o.id"
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                if org_id is not None and item["org_id"] != org_id:
                    continue
                if status is not None and item["status"] != status:
                    continue
                if self._is_admin(actor_id):
                    pass
                elif item["raised_by"] == actor_id:
                    pass
                elif self._has_scope(actor_id, item["org_id"]):
                    pass
                else:
                    continue
                result.append(item)
            if org_id is not None and not self._is_admin(actor_id):
                scoped = self._has_scope(actor_id, org_id)
                own = any(r["org_id"] == org_id and r["raised_by"] == actor_id for r in result)
                if not scoped and not own:
                    raise PermissionDeniedError(f"无权查看组织 {org_id} 的异议列表")
            return result

    def audit_query(
        self,
        actor_id: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """审计查询（仅管理员）。返回操作者、动作、原因与前后快照。"""
        with self._lock:
            self._require_role(actor_id, Role.ADMIN)
            clauses, params = [], []
            if entity_type is not None:
                clauses.append("entity_type = ?")
                params.append(entity_type)
            if entity_id is not None:
                clauses.append("entity_id = ?")
                params.append(entity_id)
            if actor is not None:
                clauses.append("actor_id = ?")
                params.append(actor)
            if action is not None:
                clauses.append("action = ?")
                params.append(action)
            if since is not None:
                clauses.append("ts >= ?")
                params.append(_iso(_to_utc(since, "since")))
            if until is not None:
                clauses.append("ts <= ?")
                params.append(_iso(_to_utc(until, "until")))
            sql = "SELECT * FROM audit_log"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
            return [
                {**{k: row[k] for k in row.keys() if k != "detail"},
                 "detail": json.loads(row["detail"])}
                for row in rows
            ]


# 兼容仓库中既有的入口命名
class Service(VolunteerService):
    """``VolunteerService`` 的兼容别名。"""

    def __init__(self, db_path: str | Path = ":memory:", **kwargs: Any):
        super().__init__(db_path, **kwargs)
