"""SQLite 持久化层。

所有业务状态（含冻结标记、复核链、异议、审计日志）都保存在 SQLite 中，
进程重启后重新打开同一数据文件即可完整恢复，无需额外的恢复流程。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   TEXT REFERENCES organizations(id),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('VOLUNTEER', 'REVIEWER', 'ADMIN')),
    created_at  TEXT NOT NULL
);

-- 志愿者与小组（组织）的隶属关系，用于小组花名册与权限判断
CREATE TABLE IF NOT EXISTS memberships (
    user_id     TEXT NOT NULL REFERENCES users(id),
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, org_id)
);

-- 审核员的授权组织范围：审核员只能处理被授权组织内的记录
CREATE TABLE IF NOT EXISTS reviewer_scopes (
    user_id     TEXT NOT NULL REFERENCES users(id),
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    granted_by  TEXT REFERENCES users(id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, org_id)
);

CREATE TABLE IF NOT EXISTS activities (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES organizations(id),
    title         TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end   TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('PLANNED', 'ONGOING', 'COMPLETED', 'CANCELLED')),
    created_by    TEXT REFERENCES users(id),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id          TEXT PRIMARY KEY,
    activity_id TEXT NOT NULL REFERENCES activities(id),
    name        TEXT NOT NULL,
    quota       INTEGER,
    created_at  TEXT NOT NULL,
    UNIQUE (activity_id, name)
);

CREATE TABLE IF NOT EXISTS attendance_records (
    id              TEXT PRIMARY KEY,
    activity_id     TEXT NOT NULL REFERENCES activities(id),
    volunteer_id    TEXT NOT NULL REFERENCES users(id),
    position_id     TEXT REFERENCES positions(id),
    check_in_at     TEXT NOT NULL,
    check_out_at    TEXT,               -- NULL 表示已签到未签退
    source          TEXT NOT NULL CHECK (source IN ('ONSITE', 'BACKFILL', 'IMPORT')),
    status          TEXT NOT NULL CHECK (status IN ('PENDING', 'EFFECTIVE', 'REJECTED')),
    frozen          INTEGER NOT NULL DEFAULT 0,   -- 管理员冻结标记，冻结期间不计入时长
    disputed        INTEGER NOT NULL DEFAULT 0,   -- 存在未结异议的标记（仅提示，不影响计时长）
    reason          TEXT,               -- 补录原因（BACKFILL 必填）
    review_comment  TEXT,               -- 复核意见
    reviewed_by     TEXT REFERENCES users(id),
    reviewed_at     TEXT,
    import_batch_id TEXT REFERENCES import_batches(id),
    source_ref      TEXT,               -- 导入行的外部唯一键，用于幂等去重
    created_by      TEXT REFERENCES users(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
-- 同一外部键只能入库一次：重复导入不会产生重复记录
CREATE UNIQUE INDEX IF NOT EXISTS uniq_attendance_source_ref
    ON attendance_records(source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_attendance_volunteer
    ON attendance_records(volunteer_id, activity_id);
CREATE INDEX IF NOT EXISTS idx_attendance_activity
    ON attendance_records(activity_id);

-- 导入批次：以内容哈希幂等，同一文件重复导入直接返回原批次结果
CREATE TABLE IF NOT EXISTS import_batches (
    id             TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL UNIQUE,
    imported_by    TEXT REFERENCES users(id),
    created_at     TEXT NOT NULL,
    row_count      INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL,
    skipped_count  INTEGER NOT NULL
);

-- 修改申请：原始记录不改动，复核通过后才应用新时段，全程留痕
CREATE TABLE IF NOT EXISTS amendments (
    id                TEXT PRIMARY KEY,
    record_id         TEXT NOT NULL REFERENCES attendance_records(id),
    proposed_check_in TEXT NOT NULL,
    proposed_check_out TEXT NOT NULL,
    reason            TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    requested_by      TEXT REFERENCES users(id),
    reviewed_by       TEXT REFERENCES users(id),
    review_comment    TEXT,
    created_at        TEXT NOT NULL,
    reviewed_at       TEXT
);

CREATE TABLE IF NOT EXISTS objections (
    id              TEXT PRIMARY KEY,
    record_id       TEXT NOT NULL REFERENCES attendance_records(id),
    raised_by       TEXT NOT NULL REFERENCES users(id),
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED_UPHELD', 'RESOLVED_REJECTED')),
    resolved_by     TEXT REFERENCES users(id),
    resolution_note TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

-- 追加式审计日志：只增不改不删，是“审核链”的物理载体
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor_id    TEXT,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    reason      TEXT,
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）数据库并保证表结构存在。"""
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
