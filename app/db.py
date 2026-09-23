"""SQLite: подключение и схема. Одна база в data/platform.db."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    login                TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name            TEXT NOT NULL DEFAULT '',
    role                 TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    is_active            INTEGER NOT NULL DEFAULT 1,
    max_user_id          TEXT NOT NULL DEFAULT '',
    telegram_id          TEXT NOT NULL DEFAULT '',
    note                 TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    last_login_at        TEXT NOT NULL DEFAULT '',
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    locked_until         REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf       TEXT NOT NULL,
    flash      TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ip         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS connections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    config          TEXT NOT NULL DEFAULT '{}',
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_status     TEXT NOT NULL DEFAULT '',
    last_message    TEXT NOT NULL DEFAULT '',
    last_action     TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,
    section  TEXT NOT NULL,
    message  TEXT NOT NULL,
    details  TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_level ON events(level);
CREATE INDEX IF NOT EXISTS idx_events_section ON events(section);

CREATE TABLE IF NOT EXISTS test_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    result      TEXT NOT NULL,
    by_user     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state         TEXT PRIMARY KEY,
    connection_id INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    redirect_uri  TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS health_probe (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,             -- 'user' | 'assistant'
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_user ON chat_messages(user_id, id);

CREATE TABLE IF NOT EXISTS ai_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    user_id       INTEGER NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON ai_usage(ts);
"""


def now_utc() -> str:
    """Время в UTC как строка 'ГГГГ-ММ-ДД ЧЧ:ММ:СС' — так храним всё в базе."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def session():
    """Короткое соединение на одну операцию: безопасно для потоков и не держит блокировок."""
    con = sqlite3.connect(str(config.DB_PATH), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 15000")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(config.DB_PATH), timeout=15)
    try:
        try:
            con.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            pass  # на сетевых дисках WAL недоступен — работаем в обычном режиме
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        con.commit()
    finally:
        con.close()
