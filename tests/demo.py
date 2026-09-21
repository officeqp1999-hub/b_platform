"""Демо-режим: панель с поддельными внешними сервисами, чтобы можно было пощёлкать и всё проверить без настоящих ключей.

Запуск: демо.bat (или python tests/demo.py). Данные демо лежат в папке demo-data и при каждом запуске создаются заново.
Настоящие данные (папка data) не затрагиваются.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo-data"
PANEL_PORT, FAKE_HTTP, FAKE_HTTPS = 8099, 9210, 9211
FAKE = f"http://127.0.0.1:{FAKE_HTTP}"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def say(text: str = "") -> None:
    print(text, flush=True)


def port_busy(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def demo_env() -> dict:
    env = os.environ.copy()
    env.update({
        "PANEL_DATA_DIR": str(DEMO / "data"), "PANEL_LOG_DIR": str(DEMO / "logs"),
        "PANEL_PORT": str(PANEL_PORT), "PANEL_HOST": "127.0.0.1",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", "PYTHONUTF8": "1",
        "PLATFORMA_URL_MAP": ",".join(f"{h}={FAKE}" for h in (
            "api.telegram.org", "platform-api.max.ru", "api.anthropic.com", "common-api.wildberries.ru",
            "api-seller.ozon.ru", "api.partner.market.yandex.ru")),
        "PLATFORMA_GOOGLE_AUTH_URL": f"{FAKE}/o/oauth2/auth",
        "PLATFORMA_GOOGLE_TOKEN_URL": f"{FAKE}/token",
        "PLATFORMA_GOOGLE_USERINFO_URL": f"{FAKE}/userinfo",
        "PLATFORMA_GMAIL_PROFILE_URL": f"{FAKE}/gmail/v1/users/me/profile",
        "PLATFORMA_DRIVE_ABOUT_URL": f"{FAKE}/drive/v3/about",
    })
    return env


def wait_up(url: str, tries: int = 60) -> bool:
    import httpx
    for _ in range(tries):
        try:
            if httpx.get(url, timeout=1.5, trust_env=False, verify=False).status_code < 500:
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def seed() -> None:
    """Заполняет демо-базу: подключения (часть рабочих, часть с ошибками), сотрудников, настройки, записи журнала."""
    os.environ.update(demo_env())
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    import fake_services as fk
    from app import auth, config, db, diagnostics, store
    config.ensure_dirs()
    db.init_db()
    from app import crypto
    crypto.ensure_key()
    auth.ensure_default_admin()

    store.save_settings({"company_name": "Демо-компания", "public_url": "", "session_hours": "12", "log_retention_days": "90",
                         "auto_backup": "1", "backup_dir": "", "backup_keep": "14",
                         "google_client_id": "demo-client-id.apps.googleusercontent.com", "google_client_secret": "gsecret-abcdef123456"})
    one = {"username": "Админ", "password": "пароль123", "use_odata": "1", "verify_ssl": "1", "http_service_path": ""}
    conns = [
        ("onec", "1С:Комплексная автоматизация", {**one, "base_url": f"{FAKE}/ka/ok", "http_service_path": "hs/ok"}),
        ("onec", "1С (тестовая база — не опубликован OData)", {**one, "base_url": f"{FAKE}/ka/nopublish"}),
        ("bitrix24", "Битрикс24", {"webhook_url": f"{FAKE}/rest/1/{fk.BITRIX_CODE}/"}),
        ("wildberries", "Wildberries — ИП Иванов", {"token": fk.WB_GOOD}),
        ("wildberries", "Wildberries — ООО «Строй» (старый токен)", {"token": "wb-old-token-abcdefghijklmnop"}),
        ("ozon", "Ozon — ООО «Торг»", {"client_id": fk.OZON_ID, "api_key": fk.OZON_KEY}),
        ("yandex_market", "Яндекс Маркет", {"api_key": fk.YM_GOOD}),
        ("google", "Почта директора", {"email": "boss@example.com", "refresh_token": fk.REFRESH["good-code"]}),
        ("claude", "Ядро Claude", {"api_key": fk.CLAUDE_GOOD, "model": "claude-sonnet-5", "monthly_limit_usd": "50"}),
        ("telegram", "Telegram-бот", {"token": fk.TG_GOOD}),
    ]
    ids = []
    for type_key, name, values in conns:
        ids.append(store.create_connection(type_key, name, values))
    users = [("ivanov", "Иван Иванов", "head", "Руководитель-2026"), ("petrov", "Пётр Петров", "manager", "Менеджер-2026"),
             ("sidorova", "Анна Сидорова", "employee", "Сотрудник-2026")]
    for login, full, role, pw in users:
        store.create_user(login, full, role, auth.hash_password(pw))

    async def check_all() -> None:
        ctx = diagnostics.make_ctx()
        for cid in ids:
            await diagnostics.run_connection_check(store.get_connection(cid), ctx)
    asyncio.run(check_all())
    store.log_event("info", "system", "Демо-режим: тестовые данные созданы.")
    store.log_event("warn", "connections", "Пример предупреждения в журнале.")
    store.log_event("error", "connections", "Пример ошибки в журнале: не удалось связаться с сервисом.")


CHEATSHEET = """
============================================================
   ДЕМО-РЕЖИМ. Всё внешнее — поддельное, настоящие ключи не нужны
============================================================
Панель:  http://localhost:8099
Вход:    admin / admin  (панель сразу попросит придумать новый пароль)
Другие роли для проверки прав (пароли уже сменены):
   ivanov / Руководитель-2026     petrov / Менеджер-2026     sidorova / Сотрудник-2026

Что можно вписывать при создании подключений (правильные значения):
   1С          адрес  http://127.0.0.1:9210/ka/ok      логин  Админ     пароль  пароль123
               варианты сбоев — вместо «ok» впишите:  nopublish (OData не включён)  slow (тормозит 3 с)
               err500 (ошибка 1С)  gateway (шлюз)  html (не 1С)  noaccess (нет прав)
               самоподписанный https:  https://127.0.0.1:9211/ka/ok  (проверьте галочку «проверять сертификат»)
               путь к HTTP-сервису расширения (свод по деньгам на обзоре): hs/ok — считает; hs/empty — пусто;
               hs/noaccess — нет прав; hs/err500 — ошибка 1С; hs/slow — тормозит 1,5 с; оставить пустым — расширение не настроено
   Битрикс24   http://127.0.0.1:9210/rest/1/abc123def456/
   Telegram    123456789:AAE_test_token_abcdefghijklmnopqrstuvwxyz
   MAX         max-good-token-1234567890
   Claude      sk-ant-good-key-1234567890
   Wildberries wb-good-token-abcdefghijklmnop
   Ozon        Client-Id 123456   API-ключ ozon-good-key-abcdefghijklmnop
   Яндекс      ym-good-key-abcdefghijklmnop
   Google      Настройки уже заполнены. Нажмите «Войти через Google» — откроется эмулятор со списком вариантов.
Любые другие значения — это «неверный ключ», можно смотреть, как панель объясняет ошибки.

Остановить: закройте это окно или нажмите Ctrl+C. Данные демо создаются заново при каждом запуске.
"""


def main() -> int:
    for port in (PANEL_PORT, FAKE_HTTP, FAKE_HTTPS):
        if port_busy(port):
            say(f"ОШИБКА: порт {port} занят. Возможно, демо уже запущено в другом окне или панель работает на этом порту.")
            say("ЧТО ДЕЛАТЬ: закройте другое окно демо (или остановите программу, которая занимает порт) и запустите демо.bat ещё раз.")
            return 1
    if DEMO.exists():
        shutil.rmtree(DEMO, ignore_errors=True)
    (DEMO / "certs").mkdir(parents=True, exist_ok=True)
    say("Готовлю демо-данные…")
    seed_proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--seed"], env=demo_env(), cwd=str(ROOT))
    if seed_proc.returncode != 0:
        say("ОШИБКА: не удалось подготовить демо-данные. ЧТО ДЕЛАТЬ: покажите текст выше разработчику.")
        return 1
    procs: list[subprocess.Popen] = []
    try:
        say("Запускаю поддельные внешние сервисы и панель…")
        procs.append(subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_services.py"), str(FAKE_HTTP), str(FAKE_HTTPS), str(DEMO / "certs")],
                                      env=demo_env(), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT))
        if not wait_up(f"{FAKE}/health"):
            say("ОШИБКА: поддельные сервисы не запустились.")
            return 1
        procs.append(subprocess.Popen([sys.executable, str(ROOT / "run.py"), "serve"], env=demo_env(), cwd=str(ROOT),
                                      stdout=open(DEMO / "panel.out", "w", encoding="utf-8"), stderr=subprocess.STDOUT))
        if not wait_up(f"http://127.0.0.1:{PANEL_PORT}/health"):
            say("ОШИБКА: панель не запустилась. Вот что она написала:")
            say((DEMO / "panel.out").read_text(encoding="utf-8", errors="replace")[-1500:])
            return 1
        say(CHEATSHEET)
        (DEMO / "шпаргалка.txt").write_text(CHEATSHEET, encoding="utf-8")
        try:
            webbrowser.open(f"http://localhost:{PANEL_PORT}")
        except Exception:  # noqa: BLE001
            pass
        while all(p.poll() is None for p in procs):
            time.sleep(1)
        say("Один из процессов остановился — демо завершено.")
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass


if __name__ == "__main__":
    if "--seed" in sys.argv:
        seed()
        sys.exit(0)
    sys.exit(main())
