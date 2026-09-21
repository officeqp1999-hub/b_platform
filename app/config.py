"""Параметры запуска.

Значения читаются из файла .env (его создаёт установить.bat) и из переменных окружения.
Править .env вручную не нужно: всё остальное настраивается в браузере, в разделе «Настройки».
"""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "0.1.0"
STAGE = "этап 1 — каркас"
APP_NAME = "Центр управления"
APP_ID = "platforma"  # признак «это наша панель» в /health

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Простейший разбор .env: строки KEY=VALUE, # — комментарий. Уже заданные переменные не трогаем."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "да")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
PORT = _int("PANEL_PORT", 8080)

DATA_DIR = Path(os.environ.get("PANEL_DATA_DIR") or BASE_DIR / "data").resolve()
LOG_DIR = Path(os.environ.get("PANEL_LOG_DIR") or BASE_DIR / "logs").resolve()
DB_PATH = DATA_DIR / "platform.db"
KEY_PATH = DATA_DIR / "secret.key"
PID_PATH = DATA_DIR / "server.pid"
SSL_DIR = DATA_DIR / "ssl"
SSL_CERT = SSL_DIR / "cert.pem"
SSL_KEY = SSL_DIR / "key.pem"
DEFAULT_BACKUP_DIR = DATA_DIR / "backups"

# Панель стоит за IIS / nginx / Caddy и получает X-Forwarded-* — включается через .env (PANEL_BEHIND_PROXY=1)
BEHIND_PROXY = _bool("PANEL_BEHIND_PROXY", False)

# Имя задачи автозапуска в Планировщике Windows (см. автозапуск-включить.bat)
AUTOSTART_TASK = "Platforma-Centr-Upravleniya"

# Необязательные переопределения адресов внешних сервисов (нужны только для автотестов)
GOOGLE_AUTH_URL = os.environ.get("PLATFORMA_GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
GOOGLE_TOKEN_URL = os.environ.get("PLATFORMA_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
GOOGLE_USERINFO_URL = os.environ.get("PLATFORMA_GOOGLE_USERINFO_URL", "https://www.googleapis.com/oauth2/v2/userinfo")
GMAIL_PROFILE_URL = os.environ.get("PLATFORMA_GMAIL_PROFILE_URL", "https://gmail.googleapis.com/gmail/v1/users/me/profile")
DRIVE_ABOUT_URL = os.environ.get("PLATFORMA_DRIVE_ABOUT_URL", "https://www.googleapis.com/drive/v3/about")


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, DEFAULT_BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)
