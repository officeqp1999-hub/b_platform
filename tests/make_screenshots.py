"""Делает скриншоты панели на поддельных сервисах (для проверки внешнего вида).

Запуск:  python tests/make_screenshots.py ПАПКА_ДЛЯ_PNG
Нужен playwright с установленным chromium (только для этого скрипта, самой панели он не нужен).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "screens")
OUT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("E2E_DIR", tempfile.mkdtemp(prefix="platforma-shots-"))
os.environ.setdefault("E2E_PANEL_PORT", "8120")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e  # noqa: E402
import fake_services as fk  # noqa: E402

PASSWORD = "КрепкийПароль-2026"


def seed(b: "e2e.Browser") -> None:
    F = e2e.FAKE
    b.login("admin", "admin")
    b.post("/password", {"old": "admin", "new": PASSWORD, "confirm": PASSWORD})
    b.post("/settings", {"f_company_name": "КУПИБАС ГРУПП", "f_public_url": "", "f_session_hours": "12", "f_log_retention_days": "90",
                         "f_auto_backup": "1", "f_backup_dir": "", "f_backup_keep": "14", "f_google_client_id": "", "f_google_client_secret": ""})
    one = {"f_username": "Админ", "f_password": "пароль123", "f_use_odata": "1", "f_verify_ssl": "1", "action": "save_check"}
    b.post("/connections/new/onec", {"name": "1С:Комплексная автоматизация", "f_base_url": f"{F}/ka/ok", **one})
    b.post("/connections/new/onec", {"name": "1С:Комплексная автоматизация (тестовая база)", "f_base_url": f"{F}/ka/nopublish", **one})
    b.post("/connections/new/bitrix24", {"name": "Битрикс24", "f_webhook_url": f"{F}/rest/1/{fk.BITRIX_CODE}/", "action": "save_check"})
    b.post("/connections/new/wildberries", {"name": "Wildberries — ИП Иванов", "f_token": fk.WB_GOOD, "action": "save_check"})
    b.post("/connections/new/wildberries", {"name": "Wildberries — ООО «Строй»", "f_token": "wb-old-token-abcdefghijklmnop", "action": "save_check"})
    b.post("/connections/new/ozon", {"name": "Ozon — ООО «Торг»", "f_client_id": fk.OZON_ID, "f_api_key": fk.OZON_KEY, "action": "save_check"})
    b.post("/connections/new/yandex_market", {"name": "Яндекс Маркет", "f_api_key": fk.YM_GOOD, "action": "save_check"})
    b.post("/connections/new/claude", {"name": "Ядро Claude", "f_api_key": fk.CLAUDE_GOOD, "f_model": "claude-sonnet-5", "f_monthly_limit_usd": "50", "action": "save_check"})
    b.post("/connections/new/telegram", {"name": "Telegram-бот", "f_token": fk.TG_GOOD, "action": "save_check"})
    b.post("/users/new", {"login": "petrov", "full_name": "Пётр Петров", "role": "manager", "password": "Менеджер-2026", "confirm": "Менеджер-2026"})


def main() -> None:
    from playwright.sync_api import sync_playwright
    procs = e2e.start_services()
    try:
        seed(e2e.Browser())
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            for scheme in ("light", "dark"):
                ctx = br.new_context(viewport={"width": 1360, "height": 900}, color_scheme=scheme, locale="ru-RU")
                page = ctx.new_page()
                page.goto(e2e.BASE + "/login")
                if scheme == "light":
                    page.screenshot(path=str(OUT / "вход.png"))
                page.fill("input[name=login]", "admin")
                page.fill("input[name=password]", PASSWORD)
                page.click("button[type=submit]")
                page.wait_for_url(e2e.BASE + "/")
                page.goto(e2e.BASE + "/testing")
                page.click("text=Проверить всё")
                page.wait_for_selector("text=Скачать отчёт", timeout=90000)
                page.wait_for_timeout(800)
                page.screenshot(path=str(OUT / f"тестирование-{scheme}.png"), full_page=True)
                if scheme == "light":
                    for path, name in (("/", "обзор"), ("/connections", "подключения"), ("/connections/new", "новое-подключение"),
                                       ("/connections/new/google", "форма-google"), ("/logs", "журнал"), ("/settings", "настройки"), ("/users", "сотрудники")):
                        page.goto(e2e.BASE + path)
                        page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
                ctx.close()
            br.close()
    finally:
        for p in procs:
            p.terminate()
        print("Скриншоты:", OUT)


if __name__ == "__main__":
    main()
