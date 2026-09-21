"""SQLite 持久化层。

设计要点：
- 所有表只追加或就地更新受控字段；不存在任何 DELETE 接口，原始数据不可删除。
- entries.idx_import 唯一索引保证重复导入不累加（幂等 upsert 由业务层处理）。
- 冻结标记、复核链(reviews)、异议(disputes)、审计(audit_log)全部落盘，
  进程重启后从同一数据库文件完整恢复。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,
    org_id          TEXT,
    authorized_orgs TEXT NOT NULL DEFAULT '[]'   -- JSON array，审核员可处理的组织
);

CREATE TABLE IF NOT EXISTS groups (
    id     TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    org_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    org_id   TEXT NOT NULL,
    group_id TEXT NOT NULL REFERENCES groups(id),
    start_ts REAL NOT NULL,
    end_ts   REAL NOT NULL,
    status   TEXT NOT NULL DEFAULT 'planned',
    CHECK (end_ts > start_ts)
);

CREATE TABLE IF NOT EXISTS entries (
    id              TEXT PRIMARY KEY,
    activity_id     TEXT NOT NULL REFERENCES activities(id),
    volunteer_id    TEXT NOT NULL REFERENCES users(id),
    kind            TEXT NOT NULL,
    check_in        REAL NOT NULL,
    check_out       REAL,                      -- 签退前为空（NULL）
    raw_hours       REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    reason          TEXT,
    import_key      TEXT,
    frozen          INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL,
    created_ts      REAL NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    CHECK (check_out > check_in),
    CHECK (raw_hours >= 0)
);

-- 同一活动下同一志愿者的同一导入批次只允许一条：重复导入不累加
CREATE UNIQUE INDEX IF NOT EXISTS idx_import
    ON entries(activity_id, volunteer_id, import_key)
    WHERE import_key IS NOT NULL;

-- 同一活动同一志愿者只保留一条现场签到时段（重复签到不累加）
CREATE UNIQUE INDEX IF NOT EXISTS idx_checkin_once
    ON entries(activity_id, volunteer_id)
    WHERE kind = 'checkin';

CREATE TABLE IF NOT EXISTS positions (
    id           TEXT PRIMARY KEY,
    activity_id  TEXT NOT NULL REFERENCES activities(id),
    volunteer_id TEXT NOT NULL REFERENCES users(id),
    post         TEXT NOT NULL,
    assigned_by  TEXT NOT NULL,
    assigned_ts  REAL NOT NULL,
    UNIQUE(activity_id, volunteer_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id        TEXT PRIMARY KEY,
    entry_id  TEXT NOT NULL REFERENCES entries(id),
    seq       INTEGER NOT NULL,
    action    TEXT NOT NULL,
    actor_id  TEXT NOT NULL,
    ts        REAL NOT NULL,
    reason    TEXT,
    check_in  REAL,
    check_out REAL,
    UNIQUE(entry_id, seq)
);

CREATE TABLE IF NOT EXISTS disputes (
    id              TEXT PRIMARY KEY,
    volunteer_id    TEXT NOT NULL REFERENCES users(id),
    entry_id        TEXT REFERENCES entries(id),
    activity_id     TEXT REFERENCES activities(id),
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    created_ts      REAL NOT NULL,
    resolved_by     TEXT,
    resolved_ts     REAL,
    resolution_note TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}'
);
"""


class Database:
    """SQLite 连接持有器，同时承担行→字典的简单读取。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # -- 审计 -------------------------------------------------------------

    def write_audit(self, ts: float, actor_id: str, action: str,
                    entity_type: str, entity_id: str, detail: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO audit_log(ts, actor_id, action, entity_type, entity_id, detail)"
            " VALUES (?,?,?,?,?,?)",
            (ts, actor_id, action, entity_type, entity_id, json.dumps(detail, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def query_audit(self, *, entity_type: str | None = None,
                    entity_id: str | None = None,
                    actor_id: str | None = None,
                    action: str | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM audit_log WHERE 1=1", []
        if entity_type is not None:
            sql += " AND entity_type=?"; params.append(entity_type)
        if entity_id is not None:
            sql += " AND entity_id=?"; params.append(entity_id)
        if actor_id is not None:
            sql += " AND actor_id=?"; params.append(actor_id)
        if action is not None:
            sql += " AND action=?"; params.append(action)
        sql += " ORDER BY id"
        return list(self.conn.execute(sql, params))
