"""SQLite 连接与 schema 初始化。"""
from __future__ import annotations

import os
import sqlite3

DB_PATH = os.getenv("GATE_DB_PATH", "/data/gate.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    error_threshold_pct REAL NOT NULL,
    window_seconds       INTEGER NOT NULL,
    auto_rollback        INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id  INTEGER NOT NULL REFERENCES services(id),
    version     TEXT NOT NULL,
    percentage  INTEGER NOT NULL,
    status      TEXT NOT NULL
                CHECK (status IN ('canary','paused','stable','superseded','rolled_back')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (service_id, version)
);

CREATE TABLE IF NOT EXISTS telemetry (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL REFERENCES services(id),
    release_id INTEGER NOT NULL REFERENCES releases(id),
    user_id    TEXT NOT NULL,
    success    INTEGER NOT NULL,
    ts         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telemetry_release_ts
    ON telemetry (release_id, ts);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER REFERENCES services(id),
    release_id INTEGER REFERENCES releases(id),
    action     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    ts         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_service_ts
    ON audit_log (service_id, id);
"""


def get_conn() -> sqlite3.Connection:
    """每请求一个连接：启用 WAL、外键、Row 工厂。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
