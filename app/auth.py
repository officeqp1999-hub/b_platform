"""Вход, сессии, роли и права. Все проверки прав выполняются на сервере."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

from . import db, store

COOKIE_NAME = "platforma_session"
PBKDF2_ITERATIONS = 240_000
MAX_FAILED = 5
LOCK_SECONDS = 300

# Какая роль что может. Ключ права -> роли, которым оно разрешено.
PERMISSIONS: dict[str, set[str]] = {
    "overview.view":      {"admin", "head", "manager", "employee"},
    "chat.use":           {"admin", "head", "manager", "employee"},   # ядро Claude отвечает всем; что оно видит — решают их же права
    "finance.view":       {"admin", "head"},                # менеджер и сотрудник финансов не видят
    "connections.view":   {"admin", "head"},
    "connections.manage": {"admin"},
    "users.view":         {"admin", "head"},
    "users.manage":       {"admin"},
    "testing.run":        {"admin", "head"},
    "report.download":    {"admin", "head"},
    "logs.view":          {"admin", "head"},
    "settings.view":      {"admin"},
    "settings.manage":    {"admin"},
}

PERMISSION_TITLES = {
    "overview.view": "Обзор",
    "chat.use": "Чат с Claude",
    "finance.view": "Финансовые показатели",
    "connections.view": "Просмотр подключений",
    "connections.manage": "Создание и изменение подключений",
    "users.view": "Просмотр списка сотрудников",
    "users.manage": "Управление сотрудниками",
    "testing.run": "Запуск тестирования",
    "report.download": "Скачивание отчёта",
    "logs.view": "Журнал событий",
    "settings.view": "Настройки",
    "settings.manage": "Изменение настроек",
}

ROLE_DESCRIPTIONS = {
    "admin": "Полный доступ: подключения, сотрудники, настройки, журнал, финансы.",
    "head": "Видит обзор с финансами, подключения, сотрудников, журнал и тестирование, но ничего не меняет.",
    "manager": "Видит только обзор без финансов. Разделы управления и финансовые данные закрыты.",
    "employee": "Только личная страница и смена пароля. Основной инструмент сотрудника — бот.",
}


def can(user: Optional[dict], permission: str) -> bool:
    return bool(user) and user["role"] in PERMISSIONS.get(permission, set())


def roles_allowed(permission: str) -> list[str]:
    return [store.ROLES[r] for r in ("admin", "head", "manager", "employee") if r in PERMISSIONS.get(permission, set())]


# ---------------------------------------------------------------------------
# Пароли
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def validate_new_password(new: str, confirm: str, *, login: str = "", old_hash: str = "") -> Optional[str]:
    """Возвращает текст ошибки или None, если пароль подходит."""
    if new != confirm:
        return "Пароль и его повтор не совпадают. Введите одинаково в оба поля."
    if len(new) < 8:
        return "Пароль слишком короткий: нужно не меньше 8 символов."
    if new.strip() != new:
        return "Уберите пробелы в начале и в конце пароля."
    if new.lower() in ("admin", "password", "12345678", "qwertyui", "пароль12"):
        return "Этот пароль слишком простой. Придумайте другой: буквы и цифры, не меньше 8 символов."
    if login and new.lower() == login.lower():
        return "Пароль не должен совпадать с логином."
    if len(set(new)) < 4:
        return "Пароль слишком простой: слишком много одинаковых символов."
    if old_hash and verify_password(new, old_hash):
        return "Новый пароль должен отличаться от текущего."
    return None


def ensure_default_admin() -> bool:
    """При первом запуске создаёт admin/admin с требованием сменить пароль."""
    if store.list_users():
        return False
    store.create_user("admin", "Администратор", "admin", hash_password("admin"), must_change=True)
    store.log_event("warning", "auth", "Создан первый пользователь admin с паролем admin — при входе система потребует сменить пароль.")
    return True


def default_admin_pending() -> bool:
    """True, если пароль admin до сих пор admin/admin (для предупреждений на экранах)."""
    user = store.get_user_by_login("admin")
    return bool(user and user["is_active"] and verify_password("admin", user["password_hash"]))


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------

_ip_failures: dict[str, list[float]] = {}


def _ip_blocked(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _ip_failures.get(ip, []) if now - t < 600]
    _ip_failures[ip] = hits
    return len(hits) >= 30


def authenticate(login: str, password: str, ip: str) -> tuple[Optional[dict], str]:
    """Возвращает (пользователь, сообщение_об_ошибке)."""
    login = (login or "").strip()
    if _ip_blocked(ip):
        return None, "Слишком много неудачных попыток входа с этого компьютера. Подождите 10 минут и попробуйте снова."
    user = store.get_user_by_login(login)
    generic = "Неверный логин или пароль. Проверьте раскладку клавиатуры и Caps Lock."
    if not user:
        _ip_failures.setdefault(ip, []).append(time.time())
        store.log_event("warning", "auth", f"Неудачная попытка входа: логина «{login[:60]}» нет ({ip}).")
        return None, generic
    now = time.time()
    if user["locked_until"] and user["locked_until"] > now:
        wait = int((user["locked_until"] - now) // 60) + 1
        return None, f"Вход временно заблокирован после нескольких неверных паролей. Подождите {wait} мин. или попросите администратора сбросить пароль."
    if not user["is_active"]:
        store.log_event("warning", "auth", f"Вход отключённого пользователя {user['login']} ({ip}).", user=user["login"])
        return None, "Эта учётная запись отключена. Обратитесь к администратору."
    if not verify_password(password, user["password_hash"]):
        _ip_failures.setdefault(ip, []).append(time.time())
        fails = user["failed_attempts"] + 1
        locked = now + LOCK_SECONDS if fails >= MAX_FAILED else 0
        with db.session() as con:
            con.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?", (0 if locked else fails, locked, user["id"]))
        store.log_event("warning", "auth", f"Неверный пароль для {user['login']} ({ip})." + (" Вход заблокирован на 5 минут." if locked else ""),
                        user=user["login"])
        return None, generic
    with db.session() as con:
        con.execute("UPDATE users SET failed_attempts=0, locked_until=0, last_login_at=? WHERE id=?", (db.now_utc(), user["id"]))
    store.log_event("info", "auth", f"Вход: {user['login']} ({store.ROLES[user['role']]}) с {ip}.", user=user["login"])
    return store.get_user(user["id"]), ""


# ---------------------------------------------------------------------------
# Сессии
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_session(user_id: int, ip: str, hours: float) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = time.time()
    with db.session() as con:
        con.execute("INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at, ip) VALUES(?,?,?,?,?,?)",
                    (_hash_token(token), user_id, csrf, now, now + hours * 3600, ip))
    return token, csrf


def get_session(token: Optional[str], hours: float) -> Optional[dict]:
    """Возвращает {'user': ..., 'csrf': ..., 'token_hash': ...} или None. Продлевает сессию при активности."""
    if not token:
        return None
    try:
        th = _hash_token(token)
    except UnicodeEncodeError:
        return None
    now = time.time()
    with db.session() as con:
        row = con.execute("SELECT * FROM sessions WHERE token_hash=?", (th,)).fetchone()
        if not row or row["expires_at"] < now:
            if row:
                con.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
            return None
        if row["expires_at"] - now < hours * 3600 - 300:   # продлеваем не чаще раза в 5 минут
            con.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (now + hours * 3600, th))
    user = store.get_user(row["user_id"])
    if not user or not user["is_active"]:
        return None
    return {"user": user, "csrf": row["csrf"], "token_hash": th}


def destroy_session(token: Optional[str]) -> None:
    if token:
        with db.session() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def destroy_other_sessions(user_id: int, keep_token_hash: str) -> None:
    with db.session() as con:
        con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (user_id, keep_token_hash))


def add_flash(token_hash: str, kind: str, text: str) -> None:
    with db.session() as con:
        row = con.execute("SELECT flash FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
        if not row:
            return
        items = json.loads(row["flash"] or "[]")
        items.append({"kind": kind, "text": text})
        con.execute("UPDATE sessions SET flash=? WHERE token_hash=?", (json.dumps(items[-8:], ensure_ascii=False), token_hash))


def pop_flash(token_hash: str) -> list[dict]:
    with db.session() as con:
        row = con.execute("SELECT flash FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
        if not row or row["flash"] in ("", "[]"):
            return []
        con.execute("UPDATE sessions SET flash='[]' WHERE token_hash=?", (token_hash,))
    return json.loads(row["flash"])
