"""Шифрование секретов (пароли, токены, ключи API).

Ключ Fernet лежит в data/secret.key и создаётся при первом запуске.
ВАЖНО: без этого файла сохранённые токены прочитать нельзя, поэтому если база уже содержит
секреты, а ключа нет — панель НЕ создаёт новый ключ молча, а останавливается с понятным сообщением.
"""
from __future__ import annotations

import os
import re
import sqlite3

from cryptography.fernet import Fernet, InvalidToken

from . import config

PREFIX = "enc:"
CANARY_TEXT = "platforma-canary-v1"

_fernet: Fernet | None = None


class KeyProblem(Exception):
    """Проблема с файлом ключа шифрования. Текст сообщения — уже по-русски и с действием."""


class DecryptError(Exception):
    pass


def _db_has_secrets() -> bool:
    """Есть ли в существующей базе что-то зашифрованное (тогда потеря ключа — это беда)."""
    if not config.DB_PATH.exists():
        return False
    try:
        con = sqlite3.connect(str(config.DB_PATH), timeout=5)
        try:
            row = con.execute("SELECT 1 FROM meta WHERE key='canary' LIMIT 1").fetchone()
            if row:
                return True
            row = con.execute("SELECT 1 FROM connections WHERE config LIKE ? LIMIT 1", (f"%{PREFIX}%",)).fetchone()
            return bool(row)
        finally:
            con.close()
    except sqlite3.Error:
        return False


def ensure_key() -> bool:
    """Создаёт ключ при первом запуске. Возвращает True, если ключ создан только что."""
    global _fernet
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if config.KEY_PATH.exists():
        try:
            key = config.KEY_PATH.read_bytes().strip()
            _fernet = Fernet(key)
        except Exception as exc:  # noqa: BLE001
            raise KeyProblem(
                "Файл ключа шифрования data\\secret.key повреждён или не тот (%s). "
                "Верните этот файл из резервной копии. Если копии нет — переименуйте data\\platform.db "
                "в platform.db.старая, запустите панель заново и введите все токены заново." % type(exc).__name__
            ) from exc
        return False
    if _db_has_secrets():
        raise KeyProblem(
            "Файл ключа шифрования data\\secret.key пропал, а в базе уже есть сохранённые токены. "
            "Без этого файла их не прочитать. Верните secret.key из резервной копии в папку data. "
            "Если копии нет — переименуйте data\\platform.db в platform.db.старая, запустите панель "
            "заново и введите все токены заново."
        )
    key = Fernet.generate_key()
    tmp = config.KEY_PATH.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.replace(tmp, config.KEY_PATH)
    try:
        os.chmod(config.KEY_PATH, 0o600)
    except OSError:
        pass
    _fernet = Fernet(key)
    return True


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        ensure_key()
    assert _fernet is not None
    return _fernet


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plain: str) -> str:
    return PREFIX + _f().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    if not is_encrypted(value):
        return value
    try:
        return _f().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise DecryptError("не удалось расшифровать значение этим ключом") from exc


def mask(plain: str) -> str:
    """Маска для интерфейса: ********t123. Для коротких секретов последние символы не показываем."""
    if not plain:
        return ""
    if len(plain) >= 12:
        return "********" + plain[-4:]
    return "********"


def make_canary() -> str:
    return encrypt(CANARY_TEXT)


def canary_ok(value: str | None) -> bool:
    if not value:
        return False
    try:
        return decrypt(value) == CANARY_TEXT
    except DecryptError:
        return False


def key_file_info() -> dict:
    info = {"exists": config.KEY_PATH.exists(), "size": 0, "valid": False}
    if info["exists"]:
        try:
            raw = config.KEY_PATH.read_bytes().strip()
            info["size"] = len(raw)
            Fernet(raw)
            info["valid"] = True
        except Exception:  # noqa: BLE001
            info["valid"] = False
    return info


# Регулярные выражения для вычищения секретов из текстов ошибок, журнала и отчёта
_PATTERNS = [
    re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"(?i)(/rest/\d+/)[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(api[-_ ]?key|apikey|token|password|passwd|secret|authorization|client[-_ ]?secret|refresh[-_ ]?token|access[-_ ]?token)(\"?\s*[:=]\s*\"?)([^\s\"',;&]{4,})"),
    re.compile(r"(?i)([?&](?:key|token|access_token|api_key|client_secret|refresh_token|code)=)[^&\s]+"),
]

_extra_secrets: list[str] = []


def set_known_secrets(values: list[str]) -> None:
    """Список реальных секретов, которые надо вычищать из любого текста (обновляется при сохранении)."""
    global _extra_secrets
    _extra_secrets = sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)


def redact(text: str) -> str:
    if not text:
        return text
    for secret in _extra_secrets:
        if secret in text:
            text = text.replace(secret, "***")
    text = _PATTERNS[0].sub("bot***", text)
    text = _PATTERNS[1].sub("sk-ant-***", text)
    text = _PATTERNS[2].sub("***jwt***", text)
    text = _PATTERNS[3].sub(r"\1***", text)
    text = _PATTERNS[4].sub(lambda m: m.group(1) + " ***", text)
    text = _PATTERNS[5].sub(lambda m: m.group(1) + m.group(2) + "***", text)
    text = _PATTERNS[6].sub(r"\1***", text)
    return text
