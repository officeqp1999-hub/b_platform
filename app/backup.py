"""Резервные копии: база + ключ шифрования + .env в один zip. Вызывается из резервная-копия.bat и из панели."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import config

PREFIX = "копия-"

README = """ВАЖНО. Этот архив содержит ключ шифрования (secret.key) и базу с зашифрованными токенами.
Только вместе они открывают доступ к вашим системам. Храните копии в защищённом месте,
не отправляйте по почте и не выкладывайте в общий доступ.

Как восстановить: остановите панель (остановить.bat), распакуйте platform.db и secret.key
в папку data (с заменой), запустите панель (запустить.bat).
"""


def _settings_value(key: str) -> str:
    try:
        con = sqlite3.connect(str(config.DB_PATH), timeout=5)
        try:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else ""
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def backup_dir() -> Path:
    custom = (_settings_value("backup_dir") or "").strip()
    return Path(custom) if custom else config.DEFAULT_BACKUP_DIR


def keep_count() -> int:
    try:
        return max(1, int(float(_settings_value("backup_keep") or 14)))
    except ValueError:
        return 14


def list_backups(directory: Path | None = None) -> list[Path]:
    d = directory or backup_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob(PREFIX + "*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)


def make_backup() -> Path:
    """Создаёт архив и удаляет самые старые сверх лимита. Возвращает путь к новому архиву."""
    target_dir = backup_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Не удалось создать папку для копий «{target_dir}»: {exc.strerror or exc}. "
            "Проверьте путь в Настройках (раздел «Журнал и резервные копии») и права на папку.") from exc
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = target_dir / f"{PREFIX}{stamp}.zip"

    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / "platform.db"
        if config.DB_PATH.exists():
            src = sqlite3.connect(str(config.DB_PATH), timeout=15)
            dst = sqlite3.connect(str(db_copy))
            try:
                src.backup(dst)   # согласованная копия, даже если панель сейчас работает
            finally:
                dst.close()
                src.close()
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                if db_copy.exists():
                    zf.write(db_copy, "platform.db")
                if config.KEY_PATH.exists():
                    zf.write(config.KEY_PATH, "secret.key")
                env = config.BASE_DIR / ".env"
                if env.exists():
                    zf.write(env, ".env")
                if config.SSL_DIR.is_dir():
                    for f in config.SSL_DIR.glob("*.pem"):
                        zf.write(f, f"ssl/{f.name}")
                zf.writestr("ПРОЧТИ-МЕНЯ.txt", README)
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise RuntimeError(
                f"Не удалось записать копию в «{target_dir}»: {exc.strerror or exc}. "
                "Проверьте, что на диске есть место и что у пользователя есть право записи в эту папку.") from exc

    # чистим старые
    for old in list_backups(target_dir)[keep_count():]:
        try:
            old.unlink()
        except OSError:
            pass
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def last_backup() -> tuple[Path | None, float, int]:
    """(путь, время изменения, сколько всего копий)."""
    items = list_backups()
    if not items:
        return None, 0.0, 0
    return items[0], items[0].stat().st_mtime, len(items)
