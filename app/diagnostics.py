"""Экран «Тестирование»: проверки сервера, сети и подключений + текстовый отчёт для разработчика.

Три состояния каждой проверки: работает (ok) / внимание (warn) / ошибка (error).
У жёлтых и красных обязательно есть строка «Что делать».
Ошибка в одной проверке не роняет остальные: каждая обёрнута в _safe().
"""
from __future__ import annotations

import asyncio
import platform
import shutil
import socket
import ssl
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from . import backup, config, connectors, crypto, db, store
from .connectors.base import Ctx, explain_exception, http

START_TIME = time.time()
GROUP_TITLES = {"server": "Сервер", "network": "Сеть", "connections": "Подключения"}


def item(id_: str, title: str, status: str, message: str, action: str = "", **extra) -> dict:
    d = {"id": id_, "title": title, "status": status, "message": message, "action": action}
    d.update(extra)
    return d


async def _safe(id_: str, title: str, coro) -> dict | list[dict]:
    """Запускает одну проверку; любое исключение превращает в красную строку вместо падения всего теста."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        store.log_event("error", "testing", f"Сама проверка «{title}» упала: {type(exc).__name__}: {exc}",
                        details=traceback.format_exc())
        return item(id_, title, "error", f"Проверка не смогла выполниться ({type(exc).__name__}).",
                    "Скачайте отчёт кнопкой ниже и передайте разработчику — это ошибка в самой панели, а не в ваших настройках.")


def _local(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).astimezone()


def _fmt_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} Б"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------------------
# Сервер
# ---------------------------------------------------------------------------

def _check_python() -> dict:
    v = sys.version_info
    text = f"Python {v.major}.{v.minor}.{v.micro}, {platform.system()} {platform.release()}"
    if (v.major, v.minor) < (3, 11):
        return item("py", "Версия Python", "error", f"{text}. Нужен Python 3.11 или новее.",
                    "Установите Python 3.11 с python.org (галочка «Add python.exe to PATH»), удалите папку venv и запустите установить.bat заново.")
    return item("py", "Версия Python", "ok", text + ".")


def _check_disk() -> dict:
    try:
        usage = shutil.disk_usage(config.DATA_DIR)
    except OSError as exc:
        return item("disk", "Место на диске", "error", f"Не удалось узнать свободное место ({exc.strerror or exc}).",
                    "Проверьте, что папка data существует и доступна.")
    free_gb = usage.free / 1024 ** 3
    db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    text = f"Свободно {free_gb:.1f} ГБ из {usage.total / 1024 ** 3:.0f} ГБ. База данных: {_fmt_size(db_size)}."
    if free_gb < 1:
        return item("disk", "Место на диске", "error", text + " Места почти нет.",
                    "Освободите место на диске сервера (удалите ненужные файлы, очистите корзину). Когда место закончится, панель перестанет сохранять данные.")
    if free_gb < 5:
        return item("disk", "Место на диске", "warn", text + " Места становится мало.",
                    "Освободите место на диске: желательно, чтобы оставалось не меньше 5 ГБ.")
    return item("disk", "Место на диске", "ok", text)


def _check_db_write() -> dict:
    t0 = time.monotonic()
    try:
        stamp = db.now_utc()
        with db.session() as con:
            con.execute("INSERT OR REPLACE INTO health_probe(id, ts) VALUES(1, ?)", (stamp,))
        with db.session() as con:
            row = con.execute("SELECT ts FROM health_probe WHERE id=1").fetchone()
        if not row or row["ts"] != stamp:
            return item("db", "Запись в базу данных", "error", "Запись прошла, но прочиталась другая — база работает некорректно.",
                        "Сделайте резервную копию и передайте отчёт разработчику.")
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" in text:
            return item("db", "Запись в базу данных", "error", "База данных занята другой программой и не даёт записать.",
                        "Закройте программы, которые могут открывать файл data\\platform.db (антивирус, копирование, просмотр базы), и повторите.")
        if "readonly" in text or "read-only" in text or "unable to open" in text:
            return item("db", "Запись в базу данных", "error", "Нет права записывать в папку с базой данных.",
                        "Дайте пользователю, от имени которого запущена панель, право «Изменение» на папку data (Свойства папки → Безопасность).")
        if "full" in text or "disk i/o" in text:
            return item("db", "Запись в базу данных", "error", "Не удаётся записать в базу: диск переполнен или неисправен.",
                        "Освободите место на диске и проверьте диск на ошибки.")
        return item("db", "Запись в базу данных", "error", f"База данных вернула ошибку: {exc}.", "Передайте отчёт разработчику.")
    ms = int((time.monotonic() - t0) * 1000)
    return item("db", "Запись в базу данных", "ok", f"Запись и чтение работают ({ms} мс).")


def _check_key_file() -> dict:
    info = crypto.key_file_info()
    if not info["exists"]:
        return item("key", "Ключ шифрования", "error", "Файл ключа data\\secret.key не найден.",
                    "Верните файл из резервной копии в папку data и перезапустите панель. Без него сохранённые токены не прочитать.")
    if not info["valid"]:
        return item("key", "Ключ шифрования", "error", "Файл ключа есть, но повреждён.",
                    "Верните secret.key из резервной копии. Если копии нет — переименуйте data\\platform.db, запустите панель заново и введите токены заново.")
    return item("key", "Ключ шифрования", "ok", "Файл data\\secret.key на месте и корректен.")


def _check_secrets_readable() -> dict:
    total, bad = 0, 0
    problems: list[str] = []
    canary = store.get_meta("canary")
    if not crypto.canary_ok(canary):
        return item("secrets", "Секреты читаются ключом", "error",
                    "Ключ шифрования не подходит к базе: контрольная запись не расшифровывается. Похоже, файл secret.key подменён.",
                    "Верните оригинальный secret.key из резервной копии. Если его нет — переименуйте data\\platform.db, запустите панель заново и введите токены заново.")
    for conn in store.list_connections():
        ctype = connectors.get_type(conn["type"])
        secret_names = {f.name for f in ctype.fields if f.secret} if ctype else set()
        for name, value in conn["config"].items():
            if name in secret_names and crypto.is_encrypted(value):
                total += 1
                try:
                    crypto.decrypt(value)
                except crypto.DecryptError:
                    bad += 1
                    problems.append(conn["name"])
    with db.session() as con:
        for r in con.execute("SELECT key, value FROM settings").fetchall():
            if crypto.is_encrypted(r["value"]):
                total += 1
                try:
                    crypto.decrypt(r["value"])
                except crypto.DecryptError:
                    bad += 1
                    problems.append("настройки: " + r["key"])
    if bad:
        return item("secrets", "Секреты читаются ключом", "error",
                    f"Не читаются {bad} из {total} сохранённых секретов ({', '.join(sorted(set(problems)))}).",
                    "Верните оригинальный secret.key из резервной копии либо заново введите эти токены в подключениях.")
    return item("secrets", "Секреты читаются ключом", "ok", f"Все сохранённые секреты расшифровываются ({total} шт.).")


def _check_backups() -> dict:
    path, mtime, count = backup.last_backup()
    directory = backup.backup_dir()
    if path is None:
        return item("backup", "Резервные копии", "warn", f"Резервных копий пока нет (папка: {directory}).",
                    "Запустите резервная-копия.bat. Потом настройте его запуск по расписанию (см. ИНСТРУКЦИЮ, раздел «Резервные копии»).")
    age_days = (time.time() - mtime) / 86400
    when = _local(mtime).strftime("%d.%m.%Y %H:%M")
    text = f"Последняя копия: {when} ({age_days:.0f} дн. назад), всего копий: {count}."
    if age_days > 7:
        return item("backup", "Резервные копии", "warn", text + " Копия давно не делалась.",
                    "Запустите резервная-копия.bat и настройте регулярный запуск по расписанию.")
    return item("backup", "Резервные копии", "ok", text)


def _check_admin_password() -> dict:
    from . import auth
    if auth.default_admin_pending():
        return item("adminpw", "Пароль администратора", "error", "Пароль администратора не менялся: до сих пор admin / admin.",
                    "Войдите под admin и смените пароль (сверху справа: «Сменить пароль»). Пока пароль стандартный, панель уязвима.")
    return item("adminpw", "Пароль администратора", "ok", "Стандартный пароль admin/admin не используется.")


def _check_autostart() -> dict | None:
    if sys.platform != "win32":
        return None
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", config.AUTOSTART_TASK], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return item("autostart", "Автозапуск", "warn", "Не удалось проверить автозапуск.", "Запустите автозапуск-включить.bat от имени администратора.")
    if r.returncode == 0:
        return item("autostart", "Автозапуск", "ok", "Панель запускается сама при старте Windows.")
    return item("autostart", "Автозапуск", "warn", "Автозапуск не включён: после перезагрузки сервера панель сама не запустится.",
                "Нажмите правой кнопкой на автозапуск-включить.bat → «Запуск от имени администратора».")


def _check_uptime() -> dict:
    hours = (time.time() - START_TIME) / 3600
    return item("uptime", "Панель", "ok", f"Версия {config.VERSION}, работает {hours:.1f} ч.")


def _server_time_item(skew: float | None, source: str) -> dict:
    local = _local()
    tz = local.strftime("%z")
    tz_txt = f"UTC{tz[:3]}:{tz[3:]}" if tz else ""
    base = f"Сейчас на сервере {local.strftime('%d.%m.%Y %H:%M:%S')} ({tz_txt}, {time.tzname[0]})."
    if skew is None:
        return item("time", "Время сервера", "warn", base + " Сверить с точным временем не удалось (нет доступа к интернету).",
                    "Проверьте вручную: дата, время и часовой пояс должны совпадать с реальными. Неверное время ломает вход через Google и проверку сертификатов.")
    if abs(skew) > 120:
        direction = "спешат" if skew > 0 else "отстают"
        return item("time", "Время сервера", "error" if abs(skew) > 600 else "warn",
                    base + f" Часы {direction} примерно на {abs(skew) / 60:.0f} мин. (по данным {source}).",
                    "Настройте синхронизацию времени: Параметры Windows → Время и язык → Дата и время → «Синхронизировать сейчас». "
                    "Проверьте часовой пояс.")
    return item("time", "Время сервера", "ok", base + f" Расхождение с точным временем: {abs(skew):.0f} сек.")


async def server_group() -> list[dict]:
    loop = asyncio.get_running_loop()
    tasks = [
        _safe("py", "Версия Python", loop.run_in_executor(None, _check_python)),
        _safe("disk", "Место на диске", loop.run_in_executor(None, _check_disk)),
        _safe("db", "Запись в базу данных", loop.run_in_executor(None, _check_db_write)),
        _safe("key", "Ключ шифрования", loop.run_in_executor(None, _check_key_file)),
        _safe("secrets", "Секреты читаются ключом", loop.run_in_executor(None, _check_secrets_readable)),
        _safe("backup", "Резервные копии", loop.run_in_executor(None, _check_backups)),
        _safe("adminpw", "Пароль администратора", loop.run_in_executor(None, _check_admin_password)),
        _safe("autostart", "Автозапуск", loop.run_in_executor(None, _check_autostart)),
        _safe("uptime", "Панель", loop.run_in_executor(None, _check_uptime)),
    ]
    return [r for r in await asyncio.gather(*tasks) if r]


# ---------------------------------------------------------------------------
# Сеть
# ---------------------------------------------------------------------------

# (id, название, адреса для проверки, какие типы подключений от неё зависят, подсказка на случай блокировки)
NETWORK_TARGETS = [
    ("claude", "Claude (api.anthropic.com)", ["https://api.anthropic.com/"], ["claude"],
     "Из России этот адрес обычно закрыт: укажите прокси в подключении «Ядро Claude» или разместите сервер за рубежом."),
    ("wb", "Wildberries", ["https://common-api.wildberries.ru/ping"], ["wildberries"], ""),
    ("ozon", "Ozon", ["https://api-seller.ozon.ru/"], ["ozon"], ""),
    ("ym", "Яндекс Маркет", ["https://api.partner.market.yandex.ru/"], ["yandex_market"], ""),
    ("google", "Google (вход, Gmail, Диск)",
     ["https://oauth2.googleapis.com/token", "https://gmail.googleapis.com/", "https://www.googleapis.com/drive/v3/about"], ["google"], ""),
    ("telegram", "Telegram (api.telegram.org)", ["https://api.telegram.org/"], ["telegram"],
     "В России Telegram часто блокируется: укажите прокси в подключении «Бот Telegram»."),
    ("max", "MAX (platform-api.max.ru)", ["https://platform-api.max.ru/"], ["max"], ""),
]


async def _probe(url: str, proxy: str | None = None) -> dict:
    t0 = time.monotonic()
    try:
        resp = await http("GET", url, timeout=8, proxy=proxy)
        return {"ok": True, "code": resp.status_code, "ms": int((time.monotonic() - t0) * 1000),
                "date": resp.headers.get("date", "")}
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, urlparse(url).hostname or url)
        return {"ok": False, "msg": msg, "action": action, "ms": int((time.monotonic() - t0) * 1000)}


async def _network_item(target, conns_by_type: dict) -> tuple[dict, list[str]]:
    id_, title, urls, types, hint = target
    used = [t for t in types if t in conns_by_type]
    results = await asyncio.gather(*[_probe(u) for u in urls])
    dates = [r["date"] for r in results if r.get("ok") and r.get("date")]
    good = [r for r in results if r["ok"]]
    if len(good) == len(results):
        avg = sum(r["ms"] for r in results) // len(results)
        return item(f"net-{id_}", title, "ok", f"Сервер достаётся ({avg} мс)."), dates

    # напрямую не вышло — если в подключении настроен прокси, пробуем через него
    for t in used:
        cfg, _ = store.plain_config(conns_by_type[t])
        proxy = cfg.get("proxy") or None
        if proxy:
            pr = await asyncio.gather(*[_probe(u, proxy) for u in urls])
            if all(r["ok"] for r in pr):
                return item(f"net-{id_}", title, "ok", "Напрямую не открывается, но через прокси из подключения всё работает."), dates
    first_bad = next(r for r in results if not r["ok"])
    partial = f" Достаётся {len(good)} из {len(results)} адресов." if good else ""
    severity = "error" if used else "warn"
    tail = "" if used else " Пока подключение к этому сервису не создано, поэтому это не срочно — но до создания доступ нужно открыть."
    action = first_bad["action"] + ((" " + hint) if hint else "") + tail
    return item(f"net-{id_}", title, severity, first_bad["msg"] + partial, action.strip()), dates


def _cert_days_left(host: str, port: int) -> int | None:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as s:
                cert = s.getpeercert()
        not_after = ssl.cert_time_to_seconds(cert["notAfter"])
        return int((not_after - time.time()) // 86400)
    except Exception:  # noqa: BLE001
        return None


async def _external_items(public_url: str, own_https: bool) -> list[dict]:
    out: list[dict] = []
    if not public_url:
        out.append(item("ext", "Панель по внешнему адресу", "warn", "Внешний адрес панели не указан — проверять нечего.",
                        "Впишите адрес в «Настройки» → «Внешний адрес панели». Если панелью пользуются только внутри сети — можно не заполнять."))
        if own_https:
            out.append(item("https", "Защищённое соединение (https)", "ok", "Панель сама работает по https (сертификат из папки data\\ssl)."))
        else:
            out.append(item("https", "Защищённое соединение (https)", "warn",
                            "Панель работает по обычному http: пароли и токены передаются по сети открытым текстом.",
                            "Если панель открывается только внутри защищённой сети — можно оставить. Иначе включите https "
                            "(см. ИНСТРУКЦИЮ, раздел «Доступ снаружи»)."))
        return out

    url = public_url.rstrip("/") + "/health"
    parsed = urlparse(public_url)
    ok_body = False
    t0 = time.monotonic()
    ext_item: dict
    try:
        resp = await http("GET", url, timeout=10)
        ms = int((time.monotonic() - t0) * 1000)
        try:
            ok_body = resp.status_code == 200 and resp.json().get("app") == config.APP_ID
        except ValueError:
            ok_body = False
        if ok_body:
            ext_item = item("ext", "Панель по внешнему адресу", "ok", f"По адресу {public_url} панель открывается ({ms} мс).")
        else:
            ext_item = item("ext", "Панель по внешнему адресу", "error",
                            f"По адресу {public_url} что-то отвечает, но это не наша панель (код {resp.status_code}).",
                            "Проверьте, что адрес и порт ведут именно на этот сервер и эту панель (не на другой сайт, IIS по умолчанию и т. п.).")
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, parsed.hostname or public_url)
        extra = (" Если снаружи адрес открывается, а отсюда нет — некоторые роутеры не пускают «сам к себе» по внешнему адресу: "
                 "проверьте с другого компьютера или с телефона через мобильную сеть.")
        ext_item = item("ext", "Панель по внешнему адресу", "error", f"Панель по адресу {public_url} не открывается: {msg}",
                        action + " Также проверьте: домен указывает на этот сервер, порт открыт в файрволе Windows и на роутере." + extra)
    out.append(ext_item)

    if parsed.scheme == "https":
        port = parsed.port or 443
        days = await asyncio.get_running_loop().run_in_executor(None, _cert_days_left, parsed.hostname or "", port)
        if ext_item["status"] == "error":
            out.append(item("https", "Защищённое соединение (https)", "error", "https проверить не удалось — панель по внешнему адресу не открылась.",
                            ext_item["action"]))
        elif days is None:
            out.append(item("https", "Защищённое соединение (https)", "warn", "https работает, но срок действия сертификата определить не удалось.",
                            "Проверьте сертификат вручную в браузере (значок замка)."))
        elif days < 0:
            out.append(item("https", "Защищённое соединение (https)", "error", "Сертификат https просрочен.", "Перевыпустите сертификат и перезапустите панель."))
        elif days < 14:
            out.append(item("https", "Защищённое соединение (https)", "warn", f"https работает, но сертификат истекает через {days} дн.",
                            "Перевыпустите сертификат заранее (Let's Encrypt делает это автоматически при правильной настройке)."))
        else:
            out.append(item("https", "Защищённое соединение (https)", "ok", f"https работает, сертификат действует ещё {days} дн."))
    else:
        out.append(item("https", "Защищённое соединение (https)", "warn",
                        "Внешний адрес начинается с http:// — пароли и токены идут открытым текстом.",
                        "Настройте https и впишите в «Настройки» адрес, начинающийся с https:// (см. ИНСТРУКЦИЮ, раздел «Доступ снаружи»)."))
    return out


async def network_group(public_url: str, own_https: bool) -> tuple[list[dict], float | None, str]:
    conns_by_type = {c["type"]: c for c in store.list_connections() if c["enabled"]}
    tasks = [_safe(f"net-{t[0]}", t[1], _network_item(t, conns_by_type)) for t in NETWORK_TARGETS]
    ext = _safe("ext", "Панель по внешнему адресу", _external_items(public_url, own_https))
    results = await asyncio.gather(ext, *tasks)
    items = list(results[0]) if isinstance(results[0], list) else [results[0]]
    dates: list[str] = []
    for r in results[1:]:
        if isinstance(r, tuple):
            items.append(r[0])
            dates.extend(r[1])
        else:
            items.append(r)
    # сверка времени по заголовку Date ответов внешних серверов
    skew, source = None, ""
    for d in dates:
        try:
            ref = parsedate_to_datetime(d).astimezone(timezone.utc).timestamp()
            skew = time.time() - ref
            source = "заголовку Date внешнего сервера"
            break
        except (TypeError, ValueError):
            continue
    return items, skew, source


# ---------------------------------------------------------------------------
# Подключения
# ---------------------------------------------------------------------------

async def run_connection_check(conn: dict, ctx: Ctx, *, save: bool = True) -> dict:
    ctype = connectors.get_type(conn["type"])
    title = conn["name"]
    if ctype is None:
        return item(f"conn-{conn['id']}", title, "error", "Неизвестный тип подключения.", "Удалите это подключение и создайте заново.",
                    conn_id=conn["id"], type_title="?")
    cfg, failed = store.plain_config(conn)
    t0 = time.monotonic()
    if failed:
        res_item = item(f"conn-{conn['id']}", title, "error",
                        "Не удалось расшифровать сохранённые секреты этого подключения: ключ шифрования не подходит.",
                        "Верните оригинальный data\\secret.key из резервной копии или заново введите токены в этом подключении.",
                        conn_id=conn["id"], type_title=ctype.title)
    else:
        try:
            res = await asyncio.wait_for(ctype.check(cfg, ctx), timeout=60)
            res_item = item(f"conn-{conn['id']}", title, res.status, res.message, res.action,
                            conn_id=conn["id"], type_title=ctype.title, ms=res.elapsed_ms or int((time.monotonic() - t0) * 1000),
                            details=res.details)
        except asyncio.TimeoutError:
            res_item = item(f"conn-{conn['id']}", title, "error", "Проверка не уложилась в 60 секунд: сервис не отвечает.",
                            "Проверьте адрес, файрвол и что сама система запущена. Повторите позже.",
                            conn_id=conn["id"], type_title=ctype.title)
        except Exception as exc:  # noqa: BLE001
            store.log_event("error", "connections", f"Внутренняя ошибка проверки «{title}»: {type(exc).__name__}: {exc}",
                            details=traceback.format_exc())
            res_item = item(f"conn-{conn['id']}", title, "error", f"Проверка не смогла выполниться ({type(exc).__name__}).",
                            "Скачайте отчёт на экране «Тестирование» и передайте разработчику.",
                            conn_id=conn["id"], type_title=ctype.title)
    res_item["message"] = crypto.redact(res_item["message"])
    res_item["action"] = crypto.redact(res_item["action"])
    if save:
        store.save_check_result(conn["id"], res_item["status"], res_item["message"], res_item["action"])
    return res_item


def make_ctx() -> Ctx:
    return Ctx(settings=store.get_settings())


async def connections_group() -> list[dict]:
    conns = store.list_connections()
    if not conns:
        return [item("conn-none", "Подключения", "warn", "Ни одного подключения ещё не создано.",
                     "Откройте «Подключения» → «Добавить» и начните с ядра Claude и 1С.")]
    ctx = make_ctx()
    active = [c for c in conns if c["enabled"]]
    disabled = [c for c in conns if not c["enabled"]]
    # параллельно; return_exceptions — ещё одна страховка: сбой одной проверки не роняет остальные
    results = await asyncio.gather(*[run_connection_check(c, ctx) for c in active], return_exceptions=True)
    items: list[dict] = []
    for c, r in zip(active, results):
        if isinstance(r, BaseException):
            items.append(item(f"conn-{c['id']}", c["name"], "error", f"Проверка не смогла выполниться ({type(r).__name__}).",
                              "Скачайте отчёт и передайте разработчику.", conn_id=c["id"]))
        else:
            items.append(r)
    for c in disabled:
        ctype = connectors.get_type(c["type"])
        items.append(item(f"conn-{c['id']}", c["name"], "off", "Подключение отключено — не проверяется.",
                          "", conn_id=c["id"], type_title=ctype.title if ctype else ""))
    return items


# ---------------------------------------------------------------------------
# Полный прогон
# ---------------------------------------------------------------------------

async def run_all(public_url_override: str | None = None, own_https: bool = False) -> dict:
    started = time.time()
    settings = store.get_settings()
    public_url = (public_url_override if public_url_override is not None else settings.get("public_url", "")).strip()
    own = own_https or (config.SSL_CERT.exists() and config.SSL_KEY.exists())

    server_task = _safe_group("server", server_group())
    net_task = _safe_group("network", network_group(public_url, own))
    conn_task = _safe_group("connections", connections_group())
    server_items, net_res, conn_items = await asyncio.gather(server_task, net_task, conn_task)

    if isinstance(net_res, tuple):
        net_items, skew, source = net_res
    else:
        net_items, skew, source = net_res, None, ""
    time_item = _server_time_item(skew, source)
    # время вставляем в группу «Сервер» после диска
    idx = next((i for i, it in enumerate(server_items) if it["id"] == "db"), len(server_items))
    server_items.insert(idx + 1, time_item)

    groups = [
        {"key": "server", "title": GROUP_TITLES["server"], "items": server_items},
        {"key": "network", "title": GROUP_TITLES["network"], "items": net_items},
        {"key": "connections", "title": GROUP_TITLES["connections"], "items": conn_items},
    ]
    summary = {"ok": 0, "warn": 0, "error": 0}
    for g in groups:
        for it in g["items"]:
            if it["status"] in summary:
                summary[it["status"]] += 1
    finished = time.time()
    return {
        "started_at": datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": datetime.fromtimestamp(finished, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": int((finished - started) * 1000),
        "groups": groups,
        "summary": summary,
        "version": config.VERSION,
    }


async def _safe_group(key: str, coro):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        store.log_event("error", "testing", f"Группа проверок «{GROUP_TITLES[key]}» упала: {type(exc).__name__}: {exc}",
                        details=traceback.format_exc())
        return [item(f"{key}-fail", GROUP_TITLES[key], "error", f"Группа проверок не смогла выполниться ({type(exc).__name__}).",
                     "Скачайте отчёт и передайте разработчику.")]


# ---------------------------------------------------------------------------
# Текстовый отчёт для разработчика (без паролей и токенов)
# ---------------------------------------------------------------------------

STATUS_TEXT = {"ok": "РАБОТАЕТ", "warn": "ВНИМАНИЕ", "error": "ОШИБКА", "off": "ОТКЛЮЧЕНО"}


def build_report(result: dict | None) -> str:
    lines: list[str] = []
    add = lines.append
    now = _local()
    add("ОТЧЁТ ДЛЯ РАЗРАБОТЧИКА — Центр управления")
    add("=" * 60)
    add(f"Сформирован:   {now.strftime('%d.%m.%Y %H:%M:%S %z')}")
    add(f"Версия:        {config.VERSION} ({config.STAGE})")
    add(f"Python:        {platform.python_version()} ({platform.machine()})")
    add(f"Система:       {platform.system()} {platform.release()} {platform.version()}")
    add(f"Компьютер:     {socket.gethostname()}")
    add(f"Папка данных:  {config.DATA_DIR}")
    add("Секретные данные (пароли, токены, ключи) в отчёт НЕ включаются.")
    add("")

    s = store.get_settings()
    add("НАСТРОЙКИ ПАНЕЛИ")
    add("-" * 60)
    add(f"Внешний адрес:      {s.get('public_url') or '(не указан)'}")
    add(f"Вход держится, ч:   {s.get('session_hours')}")
    add(f"Журнал, дней:       {s.get('log_retention_days')}")
    default_dir = "(по умолчанию data\\backups)"
    add(f"Папка копий:        {s.get('backup_dir') or default_dir}")
    add(f"Google, Client ID:   {'заполнен' if s.get('google_client_id') else 'не заполнен'}")
    add(f"Google, Client Secret — {'заполнен' if s.get('google_client_secret') else 'не заполнен'}")
    add(f"HTTPS из data\\ssl:  {'да' if config.SSL_CERT.exists() and config.SSL_KEY.exists() else 'нет'}")
    add("")

    add("РЕЗУЛЬТАТЫ ТЕСТИРОВАНИЯ")
    add("-" * 60)
    if not result:
        add("Тестирование ещё не запускалось.")
    else:
        when = result.get("finished_at", "")
        try:
            when = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M:%S")
        except ValueError:
            pass
        sm = result["summary"]
        add(f"Проверка от {when}, длилась {result['duration_ms'] / 1000:.1f} с")
        add(f"Итого: работает — {sm['ok']}, внимание — {sm['warn']}, ошибок — {sm['error']}")
        for g in result["groups"]:
            add("")
            add(f"[{g['title']}]")
            for it in g["items"]:
                add(f"  {STATUS_TEXT.get(it['status'], it['status']):<9} {it['title']}")
                add(f"            {it['message']}")
                if it.get("action") and it["status"] in ("warn", "error"):
                    add(f"            Что делать: {it['action']}")
    add("")

    add("ПОДКЛЮЧЕНИЯ (секретные поля скрыты)")
    add("-" * 60)
    conns = store.list_connections()
    if not conns:
        add("Подключений нет.")
    for c in conns:
        ctype = connectors.get_type(c["type"])
        add(f"#{c['id']} {c['name']} — {ctype.title if ctype else c['type']} — {'включено' if c['enabled'] else 'отключено'}")
        add(f"    последняя проверка: {c['last_status'] or '—'} {c['last_checked_at']} {c['last_message']}")
        for k, v in store.redacted_config_for_report(c).items():
            add(f"    {k}: {v}")
    add("")

    add("ПОСЛЕДНИЕ ПРЕДУПРЕЖДЕНИЯ И ОШИБКИ ИЗ ЖУРНАЛА")
    add("-" * 60)
    events = [e for e in store.query_events(page=1, per_page=400)[0] if e["level"] in ("warning", "error")][:60]
    if not events:
        add("Нет записей.")
    for e in events:
        add(f"{e['ts']} UTC [{e['level']}] [{store.SECTIONS.get(e['section'], e['section'])}] {e['message']}")
    add("")
    text = crypto.redact("\r\n".join(lines))
    return text
