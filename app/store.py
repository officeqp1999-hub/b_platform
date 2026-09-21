"""Хранилище: подключения, пользователи, настройки, журнал, результаты тестов.

Все секреты пишутся в базу только зашифрованными (crypto.encrypt) и читаются через plain_config().
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import connectors, crypto, db, settings_schema
from .connectors.base import ConnectorType, Field

_create_lock = threading.Lock()

ROLES = {
    "admin": "Администратор",
    "head": "Руководитель",
    "manager": "Менеджер",
    "employee": "Сотрудник",
}

LEVELS = {"info": "Информация", "warning": "Предупреждение", "error": "Ошибка"}
SECTIONS = {
    "auth": "Вход и доступ",
    "connections": "Подключения",
    "users": "Сотрудники",
    "testing": "Тестирование",
    "settings": "Настройки",
    "system": "Система",
    "backup": "Резервные копии",
}


class StoreError(Exception):
    """Ошибка бизнес-логики; текст уже по-русски и годится для показа пользователю."""


# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------

def init_store() -> None:
    db.init_db()
    with db.session() as con:
        for key in connectors.single_types():
            # Страховка на уровне базы: даже при гонке запросов второе такое подключение не создастся
            con.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_single_{key} ON connections(type) WHERE type = '{key}'")
        row = con.execute("SELECT value FROM meta WHERE key='canary'").fetchone()
        if row is None:
            con.execute("INSERT INTO meta(key, value) VALUES('canary', ?)", (crypto.make_canary(),))
    refresh_redaction()


def get_meta(key: str) -> str:
    with db.session() as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def refresh_redaction() -> None:
    """Обновляет список реальных секретов, которые вычищаются из журнала и отчёта."""
    secrets: list[str] = []
    try:
        for conn in list_connections():
            cfg, _ = plain_config(conn)
            ctype = connectors.get_type(conn["type"])
            if not ctype:
                continue
            for f in ctype.fields:
                if f.secret and cfg.get(f.name):
                    secrets.append(str(cfg[f.name]))
        s = get_settings()
        for k in settings_schema.SECRET_KEYS:
            if s.get(k):
                secrets.append(str(s[k]))
    except Exception:  # noqa: BLE001
        pass
    crypto.set_known_secrets(secrets)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def get_settings() -> dict:
    """Все настройки с расшифрованными секретами и значениями по умолчанию."""
    values = dict(settings_schema.DEFAULTS)
    with db.session() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    for r in rows:
        v = r["value"]
        if crypto.is_encrypted(v):
            try:
                v = crypto.decrypt(v)
            except crypto.DecryptError:
                v = ""
        values[r["key"]] = v
    return values


def save_settings(values: dict) -> None:
    with db.session() as con:
        for f in settings_schema.ALL_FIELDS:
            if f.name not in values:
                continue
            raw = values[f.name]
            if f.type == "bool":
                v = "1" if raw in (True, "1", "True", "true") else ""
            else:
                v = "" if raw is None else str(raw)
            if f.secret and v:
                v = crypto.encrypt(v)
            con.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f.name, v))
    refresh_redaction()


def setting_int(name: str, default: int) -> int:
    try:
        return int(float(get_settings().get(name) or default))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Подключения
# ---------------------------------------------------------------------------

def _row_to_conn(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["config"] = json.loads(d["config"] or "{}")
    except ValueError:
        d["config"] = {}
    d["enabled"] = bool(d["enabled"])
    return d


def list_connections() -> list[dict]:
    with db.session() as con:
        rows = con.execute("SELECT * FROM connections ORDER BY type, name COLLATE NOCASE").fetchall()
    return [_row_to_conn(r) for r in rows]


def get_connection(conn_id: int) -> Optional[dict]:
    with db.session() as con:
        row = con.execute("SELECT * FROM connections WHERE id=?", (conn_id,)).fetchone()
    return _row_to_conn(row) if row else None


def plain_config(conn: dict) -> tuple[dict, list[str]]:
    """Расшифрованные значения + список полей, которые не удалось расшифровать."""
    ctype = connectors.get_type(conn["type"])
    cfg: dict[str, Any] = {}
    failed: list[str] = []
    fields = {f.name: f for f in ctype.fields} if ctype else {}
    for name, value in conn["config"].items():
        if crypto.is_encrypted(value):
            try:
                cfg[name] = crypto.decrypt(value)
            except crypto.DecryptError:
                cfg[name] = ""
                failed.append(name)
        else:
            cfg[name] = value
    # значения по умолчанию для отсутствующих полей
    for name, f in fields.items():
        if name not in cfg:
            cfg[name] = (f.default not in ("", "0")) if f.type == "bool" else f.default
    return cfg, failed


def _encode_config(ctype: ConnectorType, values: dict) -> str:
    out: dict[str, Any] = {}
    secret_names = {f.name for f in ctype.fields if f.secret}
    for name, value in values.items():
        if name in secret_names and value:
            out[name] = crypto.encrypt(str(value))
        else:
            out[name] = value
    return json.dumps(out, ensure_ascii=False)


def _name_taken(con, name: str, exclude_id: int | None = None) -> bool:
    row = con.execute("SELECT id FROM connections WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return bool(row) and row["id"] != exclude_id


def create_connection(type_key: str, name: str, values: dict) -> int:
    ctype = connectors.get_type(type_key)
    if ctype is None:
        raise StoreError("Такого типа подключения не существует.")
    name = (name or "").strip()
    if not name:
        raise StoreError("Впишите название подключения.")
    with _create_lock:
        with db.session() as con:
            if ctype.single:
                row = con.execute("SELECT name FROM connections WHERE type=?", (type_key,)).fetchone()
                if row:
                    raise StoreError(
                        f"Подключение «{ctype.title}» уже есть («{row['name']}») — такое можно создать только одно. "
                        f"Откройте существующее и измените его.")
            if _name_taken(con, name):
                raise StoreError(f"Подключение с названием «{name}» уже есть. Придумайте другое название.")
            now = db.now_utc()
            try:
                cur = con.execute(
                    "INSERT INTO connections(type, name, config, enabled, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                    (type_key, name, _encode_config(ctype, values), 1, now, now))
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"Подключение «{ctype.title}» уже есть — такое можно создать только одно.") from exc
            new_id = cur.lastrowid
    refresh_redaction()
    return int(new_id)


def update_connection(conn_id: int, name: str, values: dict) -> None:
    conn = get_connection(conn_id)
    if not conn:
        raise StoreError("Подключение не найдено — возможно, его уже удалили.")
    ctype = connectors.get_type(conn["type"])
    assert ctype is not None
    name = (name or "").strip()
    if not name:
        raise StoreError("Впишите название подключения.")
    with db.session() as con:
        if _name_taken(con, name, conn_id):
            raise StoreError(f"Подключение с названием «{name}» уже есть. Придумайте другое название.")
        con.execute("UPDATE connections SET name=?, config=?, updated_at=? WHERE id=?",
                    (name, _encode_config(ctype, values), db.now_utc(), conn_id))
    refresh_redaction()


def set_connection_enabled(conn_id: int, enabled: bool) -> None:
    with db.session() as con:
        con.execute("UPDATE connections SET enabled=?, updated_at=? WHERE id=?", (1 if enabled else 0, db.now_utc(), conn_id))


def delete_connection(conn_id: int) -> None:
    with db.session() as con:
        con.execute("DELETE FROM connections WHERE id=?", (conn_id,))
    refresh_redaction()


def save_check_result(conn_id: int, status: str, message: str, action: str) -> None:
    with db.session() as con:
        con.execute("UPDATE connections SET last_status=?, last_message=?, last_action=?, last_checked_at=? WHERE id=?",
                    (status, crypto.redact(message), crypto.redact(action), db.now_utc(), conn_id))


def masked_view(ctype: ConnectorType, conn_cfg_plain: dict) -> dict:
    """Что показываем в форме: обычные значения как есть, секреты — маской ********t123."""
    view: dict[str, dict] = {}
    for f in ctype.fields:
        v = conn_cfg_plain.get(f.name, "")
        if f.secret:
            view[f.name] = {"value": "", "mask": crypto.mask(str(v)) if v else "", "has": bool(v)}
        else:
            view[f.name] = {"value": v, "mask": "", "has": bool(v)}
    return view


def redacted_config_for_report(conn: dict) -> dict:
    """Конфиг для отчёта разработчику: секреты полностью скрыты (даже маска не показывается)."""
    ctype = connectors.get_type(conn["type"])
    secret_names = {f.name for f in ctype.fields if f.secret} if ctype else set()
    out = {}
    for k, v in conn["config"].items():
        out[k] = "<скрыто>" if (k in secret_names and v) else v
    return out


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------

def _user(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    d["is_active"] = bool(d["is_active"])
    d["must_change_password"] = bool(d["must_change_password"])
    return d


def list_users() -> list[dict]:
    with db.session() as con:
        rows = con.execute("SELECT * FROM users ORDER BY is_active DESC, login COLLATE NOCASE").fetchall()
    return [_user(r) for r in rows]


def get_user(user_id: int) -> Optional[dict]:
    with db.session() as con:
        return _user(con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_user_by_login(login: str) -> Optional[dict]:
    with db.session() as con:
        return _user(con.execute("SELECT * FROM users WHERE login=? COLLATE NOCASE", ((login or "").strip(),)).fetchone())


def count_active_admins(exclude_id: int | None = None) -> int:
    with db.session() as con:
        row = con.execute("SELECT COUNT(*) c FROM users WHERE role='admin' AND is_active=1 AND id != ?",
                          (exclude_id or -1,)).fetchone()
    return int(row["c"])


def create_user(login: str, full_name: str, role: str, password_hash: str, *, must_change: bool = False,
                max_user_id: str = "", telegram_id: str = "", note: str = "", is_active: bool = True) -> int:
    login = (login or "").strip()
    if not login:
        raise StoreError("Впишите логин.")
    if role not in ROLES:
        raise StoreError("Выберите роль из списка.")
    with db.session() as con:
        try:
            cur = con.execute(
                "INSERT INTO users(login, full_name, role, password_hash, must_change_password, is_active, max_user_id, "
                "telegram_id, note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (login, full_name.strip(), role, password_hash, 1 if must_change else 0, 1 if is_active else 0,
                 max_user_id.strip(), telegram_id.strip(), note.strip(), db.now_utc()))
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"Логин «{login}» уже занят. Выберите другой.") from exc
        return int(cur.lastrowid)


def update_user(user_id: int, *, login: str, full_name: str, role: str, is_active: bool, max_user_id: str,
                telegram_id: str, note: str, password_hash: str | None = None, must_change: bool | None = None) -> None:
    user = get_user(user_id)
    if not user:
        raise StoreError("Сотрудник не найден.")
    if role not in ROLES:
        raise StoreError("Выберите роль из списка.")
    login = login.strip()
    if not login:
        raise StoreError("Впишите логин.")
    loses_admin = user["role"] == "admin" and user["is_active"] and (role != "admin" or not is_active)
    if loses_admin and count_active_admins(exclude_id=user_id) == 0:
        raise StoreError("Нельзя лишить прав или отключить последнего администратора — в панель тогда никто не сможет войти.")
    with db.session() as con:
        try:
            con.execute(
                "UPDATE users SET login=?, full_name=?, role=?, is_active=?, max_user_id=?, telegram_id=?, note=? WHERE id=?",
                (login, full_name.strip(), role, 1 if is_active else 0, max_user_id.strip(), telegram_id.strip(), note.strip(), user_id))
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"Логин «{login}» уже занят. Выберите другой.") from exc
        if password_hash is not None:
            con.execute("UPDATE users SET password_hash=?, failed_attempts=0, locked_until=0 WHERE id=?", (password_hash, user_id))
        if must_change is not None:
            con.execute("UPDATE users SET must_change_password=? WHERE id=?", (1 if must_change else 0, user_id))
        if not is_active:
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def delete_user(user_id: int) -> None:
    user = get_user(user_id)
    if not user:
        return
    if user["role"] == "admin" and user["is_active"] and count_active_admins(exclude_id=user_id) == 0:
        raise StoreError("Нельзя удалить последнего администратора.")
    with db.session() as con:
        con.execute("DELETE FROM users WHERE id=?", (user_id,))


def set_password(user_id: int, password_hash: str, must_change: bool = False) -> None:
    with db.session() as con:
        con.execute("UPDATE users SET password_hash=?, must_change_password=?, failed_attempts=0, locked_until=0 WHERE id=?",
                    (password_hash, 1 if must_change else 0, user_id))


# ---------------------------------------------------------------------------
# Журнал
# ---------------------------------------------------------------------------

def log_event(level: str, section: str, message: str, *, user: str = "", details: str = "") -> None:
    """Пишет событие в журнал. Никогда не роняет вызывающий код. Секреты вычищаются."""
    try:
        with db.session() as con:
            con.execute("INSERT INTO events(ts, level, section, message, details, username) VALUES(?,?,?,?,?,?)",
                        (db.now_utc(), level, section, crypto.redact(message)[:2000], crypto.redact(details)[:8000], user))
    except Exception:  # noqa: BLE001
        pass


def query_events(*, level: str = "", section: str = "", q: str = "", page: int = 1, per_page: int = 50) -> tuple[list[dict], int]:
    where, params = [], []
    if level in LEVELS:
        where.append("level=?")
        params.append(level)
    if section in SECTIONS:
        where.append("section=?")
        params.append(section)
    if q:
        where.append("(message LIKE ? OR details LIKE ? OR username LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.session() as con:
        total = con.execute(f"SELECT COUNT(*) c FROM events {clause}", params).fetchone()["c"]
        rows = con.execute(f"SELECT * FROM events {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                           params + [per_page, max(0, (page - 1) * per_page)]).fetchall()
    return [dict(r) for r in rows], int(total)


def recent_events(limit: int = 8) -> list[dict]:
    with db.session() as con:
        rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def cleanup_events(days: int | None = None) -> int:
    """Автоочистка: удаляет записи старше N дней. Возвращает, сколько удалено."""
    if days is None:
        days = setting_int("log_retention_days", 90)
    border = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db.session() as con:
        cur = con.execute("DELETE FROM events WHERE ts < ?", (border,))
        removed = cur.rowcount
        # предохранитель от разрастания базы
        con.execute("DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id DESC LIMIT -1 OFFSET 200000)")
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now(timezone.utc).timestamp(),))
        con.execute("DELETE FROM oauth_states WHERE created_at < ?", (datetime.now(timezone.utc).timestamp() - 3600,))
        con.execute("DELETE FROM test_runs WHERE id NOT IN (SELECT id FROM test_runs ORDER BY id DESC LIMIT 30)")
    return int(removed)


def event_stats() -> dict:
    day = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with db.session() as con:
        rows = con.execute("SELECT level, COUNT(*) c FROM events WHERE ts >= ? GROUP BY level", (day,)).fetchall()
        total = con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    stats = {r["level"]: r["c"] for r in rows}
    stats["total"] = total
    return stats


# ---------------------------------------------------------------------------
# Результаты тестов
# ---------------------------------------------------------------------------

def save_test_run(result: dict, by_user: str) -> int:
    with db.session() as con:
        cur = con.execute("INSERT INTO test_runs(started_at, finished_at, duration_ms, result, by_user) VALUES(?,?,?,?,?)",
                          (result["started_at"], result["finished_at"], result["duration_ms"],
                           json.dumps(result, ensure_ascii=False), by_user))
        return int(cur.lastrowid)


def last_test_run() -> Optional[dict]:
    with db.session() as con:
        row = con.execute("SELECT * FROM test_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    data = json.loads(row["result"])
    data["by_user"] = row["by_user"]
    return data


# ---------------------------------------------------------------------------
# OAuth-состояния
# ---------------------------------------------------------------------------

def save_oauth_state(state: str, connection_id: int, user_id: int, redirect_uri: str) -> None:
    with db.session() as con:
        con.execute("INSERT INTO oauth_states(state, connection_id, user_id, redirect_uri, created_at) VALUES(?,?,?,?,?)",
                    (state, connection_id, user_id, redirect_uri, datetime.now(timezone.utc).timestamp()))


def pop_oauth_state(state: str) -> Optional[dict]:
    with db.session() as con:
        row = con.execute("SELECT * FROM oauth_states WHERE state=?", (state,)).fetchone()
        if row:
            con.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if not row:
        return None
    if datetime.now(timezone.utc).timestamp() - row["created_at"] > 900:
        return None
    return dict(row)
