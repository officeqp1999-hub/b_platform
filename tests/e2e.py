"""Сквозная проверка платформы: поднимает поддельные внешние сервисы и саму панель, проходит сценарии как браузер.

Запуск:  python tests/e2e.py [--keep]     (--keep — не удалять данные и не останавливать панель, для скриншота)
Формы отправляются так же, как это делает браузер: application/x-www-form-urlencoded, кириллица в %XX (UTF-8).
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import fake_services as fk  # noqa: E402

KEEP = "--keep" in sys.argv
TMP = Path(os.environ.get("E2E_DIR") or tempfile.mkdtemp(prefix="platforma-e2e-"))
DATA, LOGS = TMP / "data", TMP / "logs"
PANEL_PORT = int(os.environ.get("E2E_PANEL_PORT", "8110"))
FAKE_HTTP, FAKE_HTTPS = 9210, 9211
BASE = f"http://127.0.0.1:{PANEL_PORT}"
FAKE = f"http://127.0.0.1:{FAKE_HTTP}"

RESULTS: list[tuple[bool, str, str]] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(cond), label, detail))
    print(("  ✓ " if cond else "  ✗ ") + label + ("" if cond else f"\n      → {detail[:600]}"), flush=True)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n== {title}", flush=True)


def text_of(resp: httpx.Response) -> str:
    return html.unescape(resp.text)


def flashes(page: str) -> str:
    """Склеенный текст всех сообщений на странице."""
    return " ".join(re.sub(r"<[^>]+>", "", m) for m in re.findall(r'<div class="flash [a-z]+"[^>]*>(.*?)</div>', page, re.S))


class Browser:
    def __init__(self) -> None:
        self.c = httpx.Client(base_url=BASE, follow_redirects=False, trust_env=False, timeout=120)
        self._csrf = ""

    def get(self, path: str, **kw) -> httpx.Response:
        return self.c.get(path, **kw)

    def token(self) -> str:
        if not self._csrf:
            r = self.c.get("/password")
            m = re.search(r'name="csrf" value="([^"]+)"', r.text)
            self._csrf = m.group(1) if m else ""
        return self._csrf

    def post(self, path: str, data: dict | None = None, *, follow: bool = True, csrf: bool = True) -> httpx.Response:
        payload = dict(data or {})
        if csrf and "csrf" not in payload:
            payload["csrf"] = self.token()
        body = urlencode(payload, quote_via=quote_plus)  # UTF-8 в %XX, пробелы как +, ровно как у браузера
        r = self.c.post(path, content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if follow and r.status_code in (301, 302, 303, 307, 308):
            return self.c.get(r.headers["location"])
        return r

    def follow(self, path: str) -> httpx.Response:
        """GET и, если пришёл редирект, переход по нему (как браузер)."""
        r = self.c.get(path)
        return self.c.get(r.headers["location"]) if r.status_code in (301, 302, 303, 307, 308) else r

    def login(self, login: str, password: str) -> httpx.Response:
        return self.post("/login", {"login": login, "password": password}, follow=False, csrf=False)


def db_rows(sql: str, params=()):
    con = sqlite3.connect(str(DATA / "platform.db"))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def db_exec(sql: str, params=()):
    con = sqlite3.connect(str(DATA / "platform.db"))
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def conn_id(name: str) -> int:
    rows = db_rows("SELECT id FROM connections WHERE name=?", (name,))
    return rows[0]["id"] if rows else -1


def wait_http(url: str, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            if httpx.get(url, timeout=1.5, trust_env=False, verify=False).status_code < 500:
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------

def service_env() -> dict:
    """Переменные, которые перенаправляют адреса внешних сервисов на поддельные (только для тестов)."""
    return {
        "PLATFORMA_URL_MAP": ",".join(f"{h}={FAKE}" for h in (
            "api.telegram.org", "platform-api.max.ru", "api.anthropic.com", "common-api.wildberries.ru",
            "api-seller.ozon.ru", "api.partner.market.yandex.ru")),
        "PLATFORMA_GOOGLE_AUTH_URL": f"{FAKE}/o/oauth2/auth",
        "PLATFORMA_GOOGLE_TOKEN_URL": f"{FAKE}/token",
        "PLATFORMA_GOOGLE_USERINFO_URL": f"{FAKE}/userinfo",
        "PLATFORMA_GMAIL_PROFILE_URL": f"{FAKE}/gmail/v1/users/me/profile",
        "PLATFORMA_DRIVE_ABOUT_URL": f"{FAKE}/drive/v3/about",
    }


def start_services() -> list[subprocess.Popen]:
    procs = []
    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    procs.append(subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_services.py"), str(FAKE_HTTP), str(FAKE_HTTPS), str(TMP / "certs")],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT))
    assert wait_http(f"{FAKE}/health"), "поддельные сервисы не запустились"

    denv = env.copy()
    denv.update(service_env())
    denv.update({"PANEL_DATA_DIR": str(DATA), "PANEL_LOG_DIR": str(LOGS), "PANEL_PORT": str(PANEL_PORT), "PANEL_HOST": "127.0.0.1"})
    procs.append(subprocess.Popen([sys.executable, str(ROOT / "run.py"), "serve"], env=denv, cwd=str(ROOT),
                                  stdout=open(TMP / "panel.out", "w"), stderr=subprocess.STDOUT))
    assert wait_http(f"{BASE}/health"), "панель не запустилась: " + (TMP / "panel.out").read_text()
    return procs


def setup_app_env() -> None:
    """Чтобы в самом тесте пользоваться модулями панели (шифрование, проверки) с теми же данными."""
    os.environ.update({"PANEL_DATA_DIR": str(DATA), "PANEL_LOG_DIR": str(LOGS), "NO_PROXY": "127.0.0.1,localhost", **service_env()})


# ---------------------------------------------------------------------------
# Сценарии
# ---------------------------------------------------------------------------

def scenario_login(admin: Browser) -> None:
    section("1. Вход и обязательная смена пароля")
    r = httpx.get(f"{BASE}/", follow_redirects=False, trust_env=False)
    check(r.status_code == 303 and "/login" in r.headers["location"], "без входа «/» отправляет на страницу входа")
    r = httpx.get(f"{BASE}/connections", follow_redirects=False, trust_env=False)
    check(r.status_code == 303, "без входа «/connections» тоже закрыт")
    r = httpx.get(f"{BASE}/login", trust_env=False)
    check("admin" in r.text and "Первый запуск" in r.text, "на странице входа при первом запуске подсказка admin/admin")
    check("Content-Security-Policy" in r.headers and r.headers.get("X-Frame-Options") == "DENY", "заголовки безопасности выставлены")

    r = admin.login("admin", "неверный")
    check(r.status_code == 401 and "Неверный логин или пароль" in text_of(r), "неверный пароль — понятное сообщение")
    r = admin.login("admin", "admin")
    check(r.status_code == 303 and r.headers["location"] == "/password", "admin/admin пускает, но сразу требует сменить пароль")
    r = admin.get("/")
    check(r.status_code == 303 and r.headers["location"] == "/password", "пока пароль не сменён, остальные разделы закрыты")

    r = admin.post("/password", {"old": "admin", "new": "admin", "confirm": "admin"})
    check(r.status_code == 400 and "слишком" in text_of(r), "пароль «admin» как новый — отказ с объяснением", text_of(r)[:300])
    r = admin.post("/password", {"old": "admin", "new": "Abc12345", "confirm": "Abc12346"})
    check("не совпадают" in text_of(r), "разные пароли — отказ")
    r = admin.post("/password", {"old": "admin", "new": "КрепкийПароль-2026", "confirm": "КрепкийПароль-2026"})
    check("Пароль изменён" in flashes(text_of(r)), "русский пароль принят и сохранён (кириллица в пароле)", text_of(r)[:300])
    r = admin.get("/")
    check(r.status_code == 200 and "Обзор" in r.text, "после смены пароля панель открывается")
    check('id="finance"' in r.text, "администратор видит блок «Финансы»")

    r = admin.post("/password", {"old": "КрепкийПароль-2026", "new": "x", "confirm": "x"}, csrf=False)
    check(r.status_code == 400 and "устарела" in r.text, "POST без токена защиты (CSRF) отклоняется")

    other = Browser()
    r = other.login("admin", "КрепкийПароль-2026")
    check(r.status_code == 303 and r.headers["location"] == "/", "вход с новым русским паролем работает")


def scenario_1c(admin: Browser) -> None:
    section("2. Подключение 1С: кириллица, пустой секрет, ошибки")
    name = "1С КА — основная + тест & Ко"
    r = admin.post("/connections/new/onec", {
        "name": name, "f_base_url": f"{FAKE}/ka/ok", "f_username": "Админ", "f_password": "пароль123",
        "f_use_odata": "1", "f_verify_ssl": "1", "f_http_service_path": "", "action": "save_check"})
    page = text_of(r)
    check(r.status_code == 200 and "сохранено" in flashes(page), "подключение 1С создаётся", flashes(page)[:300])
    check("OData работает" in flashes(page) and "37 объектов" in flashes(page), "проверка связи с 1С успешна (русский логин/пароль в Basic-авторизации)", flashes(page)[:300])
    rows = db_rows("SELECT * FROM connections WHERE type='onec'")
    check(rows and rows[0]["name"] == name, "русское название с «+», «&» и «—» сохранено без искажений", str(rows[:1]))
    cfg = json.loads(rows[0]["config"])
    check(cfg["password"].startswith("enc:") and "пароль123" not in rows[0]["config"], "пароль в базе лежит только зашифрованным")
    check(cfg["username"] == "Админ", "русский логин пользователя 1С сохранён верно")
    cid = rows[0]["id"]

    setup_app_env()
    from app import crypto
    old_cipher = cfg["password"]
    check(crypto.decrypt(old_cipher) == "пароль123", "шифртекст расшифровывается ключом из data/secret.key")

    r = admin.get(f"/connections/{cid}")
    p = text_of(r)
    check("пароль123" not in p, "в форме нет открытого пароля")
    check("Пустое поле = не менять" in p and "Сохранено:" in p, "у секрета подсказка «пустое поле = не менять» и маска")

    # пустое поле секрета: старое значение должно остаться
    r = admin.post(f"/connections/{cid}", {
        "name": name, "f_base_url": f"{FAKE}/ka/ok", "f_username": "Админ", "f_password": "",
        "f_use_odata": "1", "f_verify_ssl": "1", "f_http_service_path": "", "action": "save_check"})
    fl = flashes(text_of(r))
    check("OData работает" in fl, "после сохранения с ПУСТЫМ полем пароля связь по-прежнему работает (старый секрет сохранился)", fl[:300])
    cfg2 = json.loads(db_rows("SELECT config FROM connections WHERE id=?", (cid,))[0]["config"])
    check(crypto.decrypt(cfg2["password"]) == "пароль123", "в базе тот же пароль, не затёртый пустотой")

    # новый неверный пароль → понятная ошибка; затем вернуть верный
    r = admin.post(f"/connections/{cid}", {
        "name": name, "f_base_url": f"{FAKE}/ka/ok", "f_username": "Админ", "f_password": "неверный-пароль",
        "f_use_odata": "1", "f_verify_ssl": "1", "action": "save_check"})
    fl = flashes(text_of(r))
    check("логин или пароль не подходят" in fl and "401" not in fl, "неверный пароль 1С: «логин или пароль не подходят», без «401»", fl[:300])
    admin.post(f"/connections/{cid}", {"name": name, "f_base_url": f"{FAKE}/ka/ok", "f_username": "Админ", "f_password": "пароль123",
                                        "f_use_odata": "1", "f_verify_ssl": "1"})

    # ошибки разных видов
    variants = [
        ("1С-нет-публикации", f"{FAKE}/ka/nopublish", "интерфейс OData не опубликован", "Публиковать стандартный интерфейс OData", True),
        ("1С-нет-прав", f"{FAKE}/ka/noaccess", "не хватает прав", "", True),
        ("1С-сбой-500", f"{FAKE}/ka/err500", "внутреннюю ошибку", "", True),
        ("1С-шлюз", f"{FAKE}/ka/gateway", "1С за ним недоступна", "", True),
        ("1С-не-1С", f"{FAKE}/ka/html", "не интерфейс OData", "", True),
        ("1С-порт-закрыт", "http://127.0.0.1:9/ka", "отклонил соединение", "", True),
        ("1С-нет-имени", "http://no-such-host-platforma.invalid/ka", "Не удаётся найти сервер", "", True),
        ("1С-сертификат", f"https://127.0.0.1:{FAKE_HTTPS}/ka/ok", "самоподписанный сертификат", "Проверять сертификат", True),
        ("1С-https-на-http", f"https://127.0.0.1:{FAKE_HTTP}/ka/ok", "", "", True),
    ]
    for cname, url, must, action_must, _ in variants:
        r = admin.post("/connections/new/onec", {"name": cname, "f_base_url": url, "f_username": "Админ", "f_password": "пароль123",
                                                  "f_use_odata": "1", "f_verify_ssl": "1", "action": "save_check"})
        fl = flashes(text_of(r))
        good = (must in fl) if must else ("Traceback" not in fl and "ssl." not in fl.lower() and "Exception" not in fl and len(fl) > 30)
        check(good and (action_must in fl), f"{cname}: {fl[:210]}", fl)
        if must:
            check(not re.search(r"\b(401|403|404|500|502)\b", fl.replace("500", "").replace("HTTP 500", "")) or cname in ("1С-сбой-500",),
                  f"{cname}: в тексте нет голых кодов ошибок")

    r = admin.post("/connections/new/onec", {"name": "1С-пустой-состав", "f_base_url": f"{FAKE}/ka/empty", "f_username": "Админ", "f_password": "пароль123",
                                              "f_use_odata": "1", "f_verify_ssl": "1", "action": "save_check"})
    fl = flashes(text_of(r))
    check("ни один объект" in fl and "УстановитьСоставСтандартногоИнтерфейсаOData" in fl, "OData включён, но состав пуст — «внимание», а не «работает»", fl[:300])
    check(db_rows("SELECT last_status FROM connections WHERE name='1С-пустой-состав'")[0]["last_status"] == "warn", "…и статус в базе жёлтый")

    # снимаем «проверять сертификат» — должно заработать
    cid_ssl = conn_id("1С-сертификат")
    r = admin.post(f"/connections/{cid_ssl}", {"name": "1С-сертификат", "f_base_url": f"https://127.0.0.1:{FAKE_HTTPS}/ka/ok",
                                                "f_username": "Админ", "f_password": "", "f_use_odata": "1", "action": "save_check"})
    fl = flashes(text_of(r))
    check("OData работает" in fl and "проверка сертификата отключена" in fl, "с выключенной галочкой «проверять сертификат» самоподписанный https работает", fl[:300])

    # HTTP-сервис расширения: нет — это «внимание», а не «ошибка»
    r = admin.post("/connections/new/onec", {"name": "1С-с-расширением", "f_base_url": f"{FAKE}/ka/ok", "f_username": "Админ",
                                              "f_password": "пароль123", "f_use_odata": "1", "f_verify_ssl": "1",
                                              "f_http_service_path": "hs/platforma", "action": "save_check"})
    check("HTTP-сервис расширения пока недоступен" in flashes(text_of(r)) or "расширение" in flashes(text_of(r)),
          "нет расширения 1С — предупреждение, а не поломка", flashes(text_of(r))[:300])

    # валидация формы
    r = admin.post("/connections/new/onec", {"name": "1С-без-схемы", "f_base_url": "127.0.0.1:9210/ka", "f_username": "a", "f_password": "b", "f_use_odata": "1"})
    check(r.status_code == 400 and "должен начинаться с http" in text_of(r), "адрес без http:// — ошибка у поля, ничего не сохраняется")
    r = admin.post("/connections/new/onec", {"name": "", "f_base_url": f"{FAKE}/ka", "f_username": "a", "f_password": "b", "f_use_odata": "1"})
    check(r.status_code == 400 and "Впишите название" in text_of(r), "пустое название — ошибка у поля")
    r = admin.post("/connections/new/onec", {"name": name, "f_base_url": f"{FAKE}/ka", "f_username": "a", "f_password": "b", "f_use_odata": "1"})
    check(r.status_code == 409 and "уже есть" in text_of(r), "повтор названия — отказ")


def _save_onec(admin: Browser, cid: int, name: str, http_service_path: str) -> httpx.Response:
    return admin.post(f"/connections/{cid}", {"name": name, "f_base_url": f"{FAKE}/ka/ok", "f_username": "Админ",
                                               "f_password": "", "f_use_odata": "1", "f_verify_ssl": "1",
                                               "f_http_service_path": http_service_path, "action": "save_check"})


def scenario_1c_finance(admin: Browser) -> None:
    section("2b. Свод по деньгам из 1С: HTTP-сервис расширения, кэш, лимит запросов")
    name = "1С КА — основная + тест & Ко"
    cid = conn_id(name)

    b = admin.get("/api/finance/summary").json()
    check(b["status"] == "warn" and "путь к нему" in b["message"], "без пути к HTTP-сервису — «внимание», без обращения к 1С", b)
    check(b["revenue_month"] is None and b["cash_total"] is None and b["stock_value"] is None and b["cached"] is False,
          "…и все три числа пустые, кэш ни при чём", b)

    # прописываем путь к расширению, которое умеет считать свод (см. ТЗ-1С-HTTP-сервисы.md)
    _save_onec(admin, cid, name, "hs/ok")
    b1 = admin.get("/api/finance/summary").json()
    check(b1["status"] == "ok" and b1["cached"] is False, "первый запрос свода получен из 1С, не из кэша", b1)
    check(b1["revenue_month"] == 4520000.5 and b1["cash_total"] == 812340.0 and abs(b1["stock_value"] - 9876543.21) < 1e-6,
          "числа переданы из 1С без искажений", b1)

    b2 = admin.get("/api/finance/summary").json()
    check(b2["cached"] is True and b2["revenue_month"] == b1["revenue_month"],
          "повторный запрос — из кэша (открытие обзора несколькими сотрудниками не бьёт в 1С каждый раз)", b2)

    # сама страница обзора цифры не запрашивает (чтобы медленная 1С её не тормозила) — она рендерится
    # мгновенно с местом под них, а числа (уже проверенные выше через /api/finance/summary) подгружает app.js
    page = admin.get("/").text
    check('id="finance"' in page and "data-finance-live" in page,
          "обзор рендерится сразу, с местом под цифры из 1С (их подгружает app.js)")

    # смена настроек подключения должна сразу сбрасывать кэш — иначе сисадмин почти минуту видел бы старые цифры
    _save_onec(admin, cid, name, "hs/empty")
    b3 = admin.get("/api/finance/summary").json()
    check(b3["status"] == "warn" and b3["cached"] is False and "пуст" in b3["message"],
          "после смены настроек кэш сброшен: видно новое состояние сразу, а не через 45 секунд", b3)

    for path, expect_status, must in [
        ("hs/noaccess", "error", "не хватает прав"),
        ("hs/err500", "error", "ошибку при расчёте"),
        ("hs/missing", "warn", "не считает свод"),
        ("hs/badjson", "error", "не в формате JSON"),
        ("hs/notdict", "error", "неожиданными данными"),
    ]:
        _save_onec(admin, cid, name, path)
        b = admin.get("/api/finance/summary").json()
        check(b["status"] == expect_status and must in b["message"], f"{path}: {b['message'][:200]}", b)

    _save_onec(admin, cid, name, "hs/ok")  # рабочее состояние — для дальнейших сценариев


def scenario_1c_finance_cache_internals(admin: Browser) -> None:
    section("2c. Кэш и защита 1С от параллельных запросов (прямая проверка модуля)")
    setup_app_env()
    os.environ["PLATFORMA_ONEC_CACHE_TTL"] = "1"
    os.environ["PLATFORMA_ONEC_MAX_CONCURRENT"] = "2"
    from app.connectors import one_c_data

    cfg = {"base_url": f"{FAKE}/ka/ok", "username": "Админ", "password": "пароль123",
           "http_service_path": "hs/ok", "verify_ssl": "1"}

    async def ttl_check():
        r1 = await one_c_data.finance_summary(900001, cfg)
        r2 = await one_c_data.finance_summary(900001, cfg)
        await asyncio.sleep(1.2)
        r3 = await one_c_data.finance_summary(900001, cfg)
        return r1, r2, r3

    r1, r2, r3 = asyncio.run(ttl_check())
    check(r1.cached is False and r2.cached is True, "второй вызов подряд для той же базы — из кэша, не новый запрос к 1С")
    check(r3.cached is False and r3.data == r1.data, "кэш живёт не дольше отведённого срока, потом сам обновляется")

    # правка настроек, начатая, пока уже шёл запрос к 1С со старыми настройками: результат
    # этого запроса не должен лечь в кэш поверх правки (иначе почти минуту видны старые данные)
    async def race_check():
        slow = {**cfg, "http_service_path": "hs/slow"}
        task = asyncio.ensure_future(one_c_data.finance_summary(900010, slow))
        await asyncio.sleep(0.2)  # запрос уже в пути (fake-сервис отвечает через 1,5 с)
        one_c_data.invalidate(900010)  # сисадмин в этот момент сохранил другие настройки
        await task
        return await one_c_data.finance_summary(900010, {**cfg, "http_service_path": "hs/empty"})

    after_race = asyncio.run(race_check())
    check(after_race.status == "warn" and after_race.cached is False,
          "запрос, начатый до правки настроек, не затирает в кэше уже новые (после invalidate) данные")

    # дедупликация: несколько запросов ЗА ОДИН И ТОТ ЖЕ свод одной базы, пока первый ещё не
    # ответил, не долбят в 1С каждый по-своему, а ждут единственный уже идущий запрос
    before = httpx.get(f"{FAKE}/debug/onec-summary-calls", trust_env=False).json()["n"]
    slow_cfg = {**cfg, "http_service_path": "hs/slow"}

    async def dedup_check():
        return await asyncio.gather(*[one_c_data.finance_summary(900002, slow_cfg) for _ in range(5)])

    results = asyncio.run(dedup_check())
    after = httpx.get(f"{FAKE}/debug/onec-summary-calls", trust_env=False).json()["n"]
    check(all(r.status == "ok" for r in results), "все 5 одновременных запросов за один и тот же свод получили результат")
    check(after - before == 1, f"…но в 1С ушёл только один запрос, остальные дождались его ответа (было +{after - before})")

    # а вот сам лимит одновременных запросов (MAX_CONCURRENT_REQUESTS=2) проверяем в обход кэша —
    # напрямую через _fetch_summary, иначе описанная выше дедупликация схлопнёт всё в один запрос
    # и лимит окажется недостижим и, соответственно, непроверяем
    async def limit_check():
        return await asyncio.gather(*[one_c_data._fetch_summary(900003, slow_cfg) for _ in range(4)])

    t0 = time.monotonic()
    raw_results = asyncio.run(limit_check())
    took = time.monotonic() - t0
    check(all(r.status == "ok" for r in raw_results), "все 4 прямых (без кэша) запроса к «медленной» сводке дошли и посчитались")
    check(1.4 * 2 <= took < 1.4 * 4,
          f"лимит (2) реально ограничивает: 4 запроса по 1,5 с идут в 2 захода, а не разом и не по одному ({took:.1f} с)",
          f"{took:.1f}")


def scenario_bitrix_mask(admin: Browser) -> None:
    section("3. Битрикс24 и маска секрета")
    hook = f"{FAKE}/rest/1/{fk.BITRIX_CODE}/"
    r = admin.post("/connections/new/bitrix24", {"name": "Битрикс24 — портал", "f_webhook_url": f" {hook}profile.json \n", "action": "save_check"})
    fl = flashes(text_of(r))
    check("вебхук работает от имени: Иван Петров" in fl, "вебхук принят, лишний profile.json и пробелы убраны", fl[:300])
    cid = conn_id("Битрикс24 — портал")
    page = text_of(admin.get(f"/connections/{cid}"))
    check("********" + hook[-4:] in page, f"секрет показан маской вида ********{hook[-4:]}")
    check(fk.BITRIX_CODE not in page, "код вебхука в HTML не попал")
    r = admin.post("/connections/new/bitrix24", {"name": "Битрикс-битый", "f_webhook_url": f"{FAKE}/rest/1/wrongcode1234/", "action": "save_check"})
    fl = flashes(text_of(r))
    check("вебхук не принят" in fl, "неверный вебхук — «вебхук не принят», не «401»", fl[:300])
    r = admin.post("/connections/new/bitrix24", {"name": "Битрикс-мусор", "f_webhook_url": "https://example.ru/abc"})
    check(r.status_code == 400 and "выглядит иначе" in text_of(r), "вебхук неправильного вида — подсказка, как должен выглядеть")


def scenario_tokens(admin: Browser) -> None:
    section("4. Русские буквы и пробелы в токене")
    lookalike = "123456789:ААЕ_test_token_abcdefghijklmnopqrstuvwxyz"  # первые буквы — кириллица
    r = admin.post("/connections/new/telegram", {"name": "Telegram-бот", "f_token": lookalike})
    p = text_of(r)
    check(r.status_code == 400 and "есть символы, которых не бывает в ключах и токенах" in p, "русские буквы в токене: понятная ошибка у поля")
    check("ascii" not in p.lower() and "codec" not in p.lower() and "encode" not in p.lower(), "в сообщении нет слов про ascii/codec/encode")
    check("«А»" in p or "«Е»" in p, "сообщение показывает, какие именно символы лишние")
    check(db_rows("SELECT COUNT(*) c FROM connections WHERE type='telegram'")[0]["c"] == 0, "с ошибочным токеном подключение не создано")

    spaced = f"  {fk.TG_GOOD[:20]}\r\n {fk.TG_GOOD[20:]}   "
    r = admin.post("/connections/new/telegram", {"name": "Telegram-бот", "f_token": spaced, "action": "save_check"})
    fl = flashes(text_of(r))
    check("убраны лишние пробелы" in fl, "пробелы и перенос строки внутри токена вычищены с уведомлением", fl[:300])
    check("Токен принят" in fl, "после очистки токен работает", fl[:300])

    # WB: сохраняем хороший, потом подкладываем в базу русский токен «мимо формы» — проверка не должна упасть с ascii-ошибкой
    r = admin.post("/connections/new/wildberries", {"name": "WB — ИП Иванов", "f_token": fk.WB_GOOD, "action": "save_check"})
    check("кабинет: «Ромашка»" in flashes(text_of(r)), "WB: токен принят, показано имя кабинета", flashes(text_of(r))[:300])
    cid = conn_id("WB — ИП Иванов")
    setup_app_env()
    from app import crypto
    cfg = json.loads(db_rows("SELECT config FROM connections WHERE id=?", (cid,))[0]["config"])
    cfg["token"] = crypto.encrypt("токенСРусскимиБуквами")
    db_exec("UPDATE connections SET config=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), cid))
    r = admin.post(f"/connections/{cid}/check")
    fl = flashes(text_of(r))
    check("символы" in fl and "ascii" not in fl.lower() and "codec" not in fl.lower(),
          "русский токен, попавший в базу в обход формы: при проверке человеческое сообщение, без «ascii»", fl[:400])
    check("Что делать" in fl, "и рядом написано, что делать")
    # возвращаем хороший токен
    admin.post(f"/connections/{cid}", {"name": "WB — ИП Иванов", "f_token": fk.WB_GOOD})

    # WB: неверный токен и токен на грани срока
    r = admin.post(f"/connections/{cid}", {"name": "WB — ИП Иванов", "f_token": "wb-bad-token-abcdef", "action": "save_check"})
    check("не принимает токен" in flashes(text_of(r)), "WB: неверный токен — «не принимает токен»", flashes(text_of(r))[:300])
    r = admin.post(f"/connections/{cid}", {"name": "WB — ИП Иванов", "f_token": fk.WB_EXPIRING, "action": "save_check"})
    check("истекает через" in flashes(text_of(r)), "WB: токен, который скоро истечёт, даёт предупреждение", flashes(text_of(r))[:300])
    admin.post(f"/connections/{cid}", {"name": "WB — ИП Иванов", "f_token": fk.WB_GOOD})

    r = admin.post("/connections/new/ozon", {"name": "Ozon — ООО", "f_client_id": "abc", "f_api_key": fk.OZON_KEY})
    check(r.status_code == 400 and "это число" in text_of(r), "Ozon: Client-Id не число — подсказка")
    r = admin.post("/connections/new/ozon", {"name": "Ozon — ООО", "f_client_id": fk.OZON_ID, "f_api_key": fk.OZON_KEY, "action": "save_check"})
    check("Ключ принят" in flashes(text_of(r)), "Ozon: пара Client-Id + ключ принята", flashes(text_of(r))[:300])
    r = admin.post("/connections/new/yandex_market", {"name": "Яндекс Маркет — основной", "f_api_key": "bad-key-abcdefghij", "action": "save_check"})
    check("не принимает Api-Key" in flashes(text_of(r)), "Яндекс Маркет: неверный ключ — понятное сообщение", flashes(text_of(r))[:300])
    ym = conn_id("Яндекс Маркет — основной")
    r = admin.post(f"/connections/{ym}", {"name": "Яндекс Маркет — основной", "f_api_key": fk.YM_GOOD, "action": "save_check"})
    check("Ключ принят" in flashes(text_of(r)), "Яндекс Маркет: верный ключ принят", flashes(text_of(r))[:300])


def scenario_single(admin: Browser) -> None:
    section("5. Ядро Claude и боты — только по одному")
    r = admin.post("/connections/new/claude", {"name": "Ядро Claude", "f_api_key": f"  {fk.CLAUDE_GOOD}  ", "f_model": "claude-sonnet-5",
                                                "f_monthly_limit_usd": "50,5", "action": "save_check"})
    fl = flashes(text_of(r))
    check("Ключ принят" in fl, "Ядро Claude создано, ключ принят", fl[:300])
    cfg = json.loads(db_rows("SELECT config FROM connections WHERE type='claude'")[0]["config"])
    check(cfg["monthly_limit_usd"] == "50.5", "лимит «50,5» с запятой сохранён как число 50.5")

    r = admin.post("/connections/new/claude", {"name": "Второе ядро", "f_api_key": fk.CLAUDE_GOOD, "f_model": "claude-sonnet-5", "f_monthly_limit_usd": "10"})
    check(r.status_code == 409 and "можно создать только одно" in text_of(r), "второе ядро Claude отбито сервером (409)", text_of(r)[:300])
    check(db_rows("SELECT COUNT(*) c FROM connections WHERE type='claude'")[0]["c"] == 1, "в базе по-прежнему одно ядро Claude")
    r = admin.get("/connections/new/claude")
    check(r.status_code == 303 and "/connections/" in r.headers["location"], "страница создания второго ядра перенаправляет на существующее")
    page = text_of(admin.get("/connections/new"))
    check("Уже создано" in page, "в списке типов ядро Claude помечено «Уже создано»")
    try:
        db_exec("INSERT INTO connections(type,name,config,enabled,created_at,updated_at) VALUES('claude','обход','{}',1,'x','x')")
        check(False, "прямой SQL-вставки второго ядра база не должна допускать")
    except sqlite3.IntegrityError:
        check(True, "даже прямая вставка в базу второго ядра отбивается индексом уникальности")

    # неверный ключ и неверная модель
    cid = conn_id("Ядро Claude")
    r = admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": "sk-ant-wrong-key-123456789", "f_model": "claude-sonnet-5",
                                            "f_monthly_limit_usd": "50", "action": "save_check"})
    check("Ключ API неверный" in flashes(text_of(r)), "неверный ключ Claude: «Ключ API неверный»", flashes(text_of(r))[:300])
    r = admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": fk.CLAUDE_GOOD, "f_model": "claude-nonexistent",
                                            "f_monthly_limit_usd": "50", "action": "save_check"})
    check("нет" in flashes(text_of(r)) and "claude-sonnet-5" in flashes(text_of(r)), "неверная модель: сообщение и список доступных", flashes(text_of(r))[:300])
    r = admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": "", "f_model": "claude-sonnet-5", "f_monthly_limit_usd": "0", "action": "save"})
    check(r.status_code == 400 and "больше нуля" in text_of(r), "лимит 0 отклоняется", text_of(r)[-400:])
    admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": "", "f_model": "claude-sonnet-5", "f_monthly_limit_usd": "50"})

    # MAX: два запроса одновременно — создаться должен ровно один
    tok = admin.token()

    def make(n: int) -> int:
        b = Browser()
        b.c.cookies.update(admin.c.cookies)
        b._csrf = tok
        return b.post("/connections/new/max", {"name": f"MAX-{n}", "f_token": fk.MAX_GOOD}, follow=False).status_code

    with ThreadPoolExecutor(2) as ex:
        codes = sorted(ex.map(make, [1, 2]))
    check(codes == [303, 409], f"два одновременных запроса на создание бота MAX: создан один, второй отбит (коды {codes})")
    cid = db_rows("SELECT id FROM connections WHERE type='max'")[0]["id"]
    r = admin.post(f"/connections/{cid}/check")
    check("Токен принят" in flashes(text_of(r)), "бот MAX: токен принят", flashes(text_of(r))[:300])
    r = admin.post("/connections/new/telegram", {"name": "Ещё Telegram", "f_token": fk.TG_GOOD})
    check(r.status_code == 409, "второй бот Telegram отбит")


def scenario_google(admin: Browser) -> None:
    section("6. Google OAuth")
    r = admin.post("/connections/new/google", {"name": "Почта директора"})
    check("сохранено" in flashes(text_of(r)), "подключение Google создаётся (только название)")
    gid = conn_id("Почта директора")
    r = admin.post(f"/connections/{gid}/google/start", follow=False)
    check(r.status_code == 303 and "/settings" in r.headers["location"], "без Client ID/Secret вход в Google не начинается — отправляет в «Настройки»")
    check("Client ID" in text_of(admin.get(r.headers["location"])), "…с пояснением (в «Настройках» есть поле Client ID)")

    r = admin.post("/settings", {"f_company_name": "ООО «Ромашка» — Москва", "f_public_url": "", "f_session_hours": "12", "f_log_retention_days": "90",
                                 "f_backup_dir": "", "f_backup_keep": "14", "f_google_client_id": "1234-abc.apps.googleusercontent.com",
                                 "f_google_client_secret": "gsecret-abcdef123456"})
    check("Настройки сохранены" in flashes(text_of(r)), "Client ID/Secret Google сохранены в настройках")
    r = admin.post(f"/connections/{gid}/google/start", follow=False)
    loc = r.headers.get("location", "")
    q = parse_qs(urlparse(loc).query)
    check(r.status_code == 303 and loc.startswith(f"{FAKE}/o/oauth2/auth"), "кнопка ведёт редиректом на страницу Google")
    check(q.get("access_type") == ["offline"] and q.get("prompt") == ["consent"], "access_type=offline и prompt=consent выставлены")
    scopes = q.get("scope", [""])[0]
    check(all(s in scopes for s in ("gmail.modify", "auth/drive", "userinfo.email")), "запрошены scope gmail.modify, drive, userinfo.email")
    check(q.get("redirect_uri") == [f"{BASE}/oauth/google/callback"] and q.get("response_type") == ["code"], "адрес возврата /oauth/google/callback")
    state = q["state"][0]

    r = admin.follow(f"/oauth/google/callback?code=good-code&state={state}")
    fl = flashes(text_of(r))
    check("boss@example.com подключён" in fl, "callback: код обменян, аккаунт подключён", fl[:300])
    check("Gmail" in fl and "Диск" in fl, "сразу выполнена проверка Gmail и Диска", fl[:300])
    row = db_rows("SELECT config FROM connections WHERE id=?", (gid,))[0]["config"]
    cfg = json.loads(row)
    check(cfg["refresh_token"].startswith("enc:") and "refresh-token-first" not in row, "refresh-токен сохранён в зашифрованном виде")
    check(cfg["email"] == "boss@example.com", "email аккаунта сохранён")

    # повторное использование state
    r = admin.follow(f"/oauth/google/callback?code=good-code&state={state}")
    check("устарела" in flashes(text_of(r)), "повторно использованный state отклоняется")
    r = admin.follow("/oauth/google/callback?code=x&state=подделка")
    check("устарела" in flashes(text_of(r)), "чужой state отклоняется")

    # второй аккаунт
    admin.post("/connections/new/google", {"name": "Второй ящик"})
    g2 = conn_id("Второй ящик")
    st2 = parse_qs(urlparse(admin.post(f"/connections/{g2}/google/start", follow=False).headers["location"]).query)["state"][0]
    r = admin.follow(f"/oauth/google/callback?code=good-code-2&state={st2}")
    check("second@example.com подключён" in flashes(text_of(r)), "второй Google-аккаунт подключается отдельным подключением")
    # тот же аккаунт ещё раз
    admin.post("/connections/new/google", {"name": "Дубль ящика"})
    g3 = conn_id("Дубль ящика")
    st3 = parse_qs(urlparse(admin.post(f"/connections/{g3}/google/start", follow=False).headers["location"]).query)["state"][0]
    r = admin.follow(f"/oauth/google/callback?code=dup-code&state={st3}")
    check("уже подключён" in flashes(text_of(r)), "тот же аккаунт второй раз не подключается", flashes(text_of(r))[:300])
    check(conn_id("Дубль ящика") == -1, "пустое дублирующее подключение убрано")
    # отказ и битый код, нет refresh-токена
    admin.post("/connections/new/google", {"name": "Отказ"})
    g4 = conn_id("Отказ")
    st4 = parse_qs(urlparse(admin.post(f"/connections/{g4}/google/start", follow=False).headers["location"]).query)["state"][0]
    r = admin.follow(f"/oauth/google/callback?error=access_denied&state={st4}")
    check("отменён" in flashes(text_of(r)) and "Что делать" in flashes(text_of(r)), "пользователь нажал «Отмена» у Google — понятное сообщение")
    st5 = parse_qs(urlparse(admin.post(f"/connections/{g4}/google/start", follow=False).headers["location"]).query)["state"][0]
    r = admin.follow(f"/oauth/google/callback?code=nocode-refresh&state={st5}")
    check("постоянный доступ" in flashes(text_of(r)), "Google не выдал refresh-токен — понятное сообщение")
    st6 = parse_qs(urlparse(admin.post(f"/connections/{g4}/google/start", follow=False).headers["location"]).query)["state"][0]
    r = admin.follow(f"/oauth/google/callback?code=garbage&state={st6}")
    check("больше не принимает" in flashes(text_of(r)) or "отклонил" in flashes(text_of(r)) or "ошибку" in flashes(text_of(r)), "неверный код от Google — человеческая ошибка", flashes(text_of(r))[:300])
    admin.post(f"/connections/{g4}/delete")

    # отозванный доступ
    setup_app_env()
    from app import crypto
    cfg["refresh_token"] = crypto.encrypt("1//revoked-token-abcdefghijklmn")
    db_exec("UPDATE connections SET config=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), gid))
    fl = flashes(text_of(admin.post(f"/connections/{gid}/check")))
    check("отозван" in fl and "Подключить аккаунт Google заново" in fl, "отозванный доступ Google: «отозван… подключить заново»", fl[:300])
    cfg["refresh_token"] = crypto.encrypt(fk.REFRESH["good-code"])
    db_exec("UPDATE connections SET config=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), gid))
    fl = flashes(text_of(admin.post(f"/connections/{gid}/check")))
    check("Аккаунт подключён" in fl, "восстановленный доступ снова работает", fl[:300])


def scenario_roles(admin: Browser) -> dict[str, Browser]:
    section("7. Роли и проверки прав на сервере")
    users = {"petrova": ("Петрова Анна", "manager"), "head1": ("Сидоров Пётр", "head"), "emp1": ("Козлов Иван", "employee")}
    for login, (full, role) in users.items():
        r = admin.post("/users/new", {"login": login, "full_name": full, "role": role, "password": "Passw0rd-x1", "is_active": "1"})
        check("добавлен" in flashes(text_of(r)), f"создан сотрудник {login} ({role})", flashes(text_of(r))[:200])
    r = admin.post("/users/new", {"login": "petrova", "full_name": "Дубль", "role": "manager", "password": "Passw0rd-x1", "is_active": "1"})
    check(r.status_code == 400 and "занят" in text_of(r), "повторный логин отклонён")
    r = admin.post("/users/new", {"login": "weak", "full_name": "Слабый", "role": "manager", "password": "123", "is_active": "1"})
    check(r.status_code == 400 and "слишком короткий" in text_of(r), "слабый пароль отклонён")

    br = {}
    for login in users:
        b = Browser()
        r = b.login(login, "Passw0rd-x1")
        check(r.status_code == 303 and r.headers["location"] == "/", f"{login} входит в панель")
        br[login] = b

    m = br["petrova"]
    for path in ("/settings", "/users", "/users/new", "/connections", "/connections/new", "/connections/new/claude", "/testing", "/logs", "/testing/report.txt"):
        r = m.get(path)
        check(r.status_code == 403 and "Недостаточно прав" in text_of(r), f"менеджер: {path} → 403 «Недостаточно прав»")
    r = m.get("/settings")
    check("Что делать" in text_of(r) and "Администратор" in text_of(r), "отказ объясняет, у кого есть доступ")
    r = m.get("/api/finance/summary")
    check(r.status_code == 403 and r.json().get("error"), "менеджер: финансовый API → 403 (JSON)")
    r = m.get("/")
    check(r.status_code == 200 and "Финансы" not in r.text and "Ваш доступ" in r.text, "менеджер: на обзоре нет блока «Финансы»")
    check("Настройки" not in r.text.split("</aside>")[0] and "Подключения" not in r.text.split("</aside>")[0], "менеджер: в меню нет закрытых разделов")
    r = m.post("/users/new", {"login": "hack", "full_name": "Хакер", "role": "admin", "password": "Passw0rd-x1"}, follow=False)
    check(r.status_code == 403, "менеджер не может создать сотрудника прямым POST (с верным CSRF)")
    r = m.post("/connections/new/telegram", {"name": "x", "f_token": fk.TG_GOOD}, follow=False)
    check(r.status_code == 403, "менеджер не может создать подключение прямым POST")
    r = m.post("/settings", {"f_company_name": "взлом"}, follow=False)
    check(r.status_code == 403, "менеджер не может сохранить настройки прямым POST")
    check(db_rows("SELECT COUNT(*) c FROM users WHERE login='hack'")[0]["c"] == 0, "и ничего в базе не появилось")

    e = br["emp1"]
    check(e.get("/").status_code == 200 and e.get("/testing").status_code == 403 and e.get("/api/finance/summary").status_code == 403,
          "сотрудник: обзор открывается, остальное закрыто")

    h = br["head1"]
    r = h.get("/api/finance/summary")
    check(r.status_code == 200, "руководитель видит финансовый API")
    check('id="finance"' in h.get("/").text, "руководитель видит блок «Финансы» на обзоре")
    check(h.get("/connections").status_code == 200 and h.get("/logs").status_code == 200 and h.get("/testing").status_code == 200, "руководитель открывает подключения, журнал и тестирование")
    check(h.post("/settings", {"f_company_name": "x"}, follow=False).status_code == 403, "руководитель не может менять настройки")
    check(h.get("/settings").status_code == 403, "руководителю раздел «Настройки» закрыт")
    cid = conn_id("Ядро Claude")
    page = h.get(f"/connections/{cid}")
    check(page.status_code == 200 and "Сохранить" not in page.text, "руководитель видит подключение только для чтения")
    check(h.post(f"/connections/{cid}/delete", follow=False).status_code == 403, "руководитель не может удалить подключение")
    check(admin.get("/api/finance/summary").status_code == 200, "администратор видит финансовый API")

    # нельзя убрать последнего администратора и самого себя
    admin_id = db_rows("SELECT id FROM users WHERE login='admin'")[0]["id"]
    r = admin.post(f"/users/{admin_id}", {"login": "admin", "full_name": "Администратор", "role": "manager", "is_active": "1"})
    check(r.status_code == 400 and "себя" in text_of(r), "администратор не может понизить сам себя")
    r = admin.post(f"/users/{admin_id}/delete")
    check("самого себя" in flashes(text_of(r)), "администратор не может удалить сам себя")
    return br


def scenario_claude_chat(admin: Browser, br: dict[str, Browser]) -> None:
    section("7b. Чат с ядром Claude: диалог, инструменты по правам, лимит расходов")
    cid = conn_id("Ядро Claude")
    head, manager, emp = br["head1"], br["petrova"], br["emp1"]

    for b, who in ((admin, "администратор"), (head, "руководитель"), (manager, "менеджер"), (emp, "сотрудник")):
        check(b.get("/chat").status_code == 200, f"{who}: страница чата открывается")

    page = text_of(admin.post("/chat/ask", {"question": "Расскажи короткий факт про облака"}))
    check("Обычный ответ на вопрос: Расскажи короткий факт про облака" in page,
          "обычный вопрос без инструментов — ответ показан в истории", page[:300])
    check("Расходы на Claude в этом месяце" in page, "администратор видит расходы на Claude за месяц", page[:400])
    check("Расходы на Claude" not in text_of(manager.get("/chat")), "менеджеру расходы не показываются (это финансовые данные)")

    # вопрос про деньги: у админа есть finance.view — Claude реально вызывает инструмент,
    # а не просто предполагает; данные приходят из инструмента, а не выдумываются
    page = text_of(admin.post("/chat/ask", {"question": "Сколько у нас выручки за месяц?"}))
    check(fk.FINANCE_TOOL_ANSWER in page, "вопрос про деньги (есть право): инструмент вызван, ответ по данным 1С", page[:300])

    # тот же вопрос от менеджера: права нет — инструмент серверу вообще не предлагается Claude,
    # а не просто «Claude сам решил не отвечать»
    page = text_of(manager.post("/chat/ask", {"question": "Сколько у нас выручки за месяц?"}))
    check(fk.NO_ACCESS_ANSWER in page and fk.FINANCE_TOOL_ANSWER not in page,
          "менеджер: тот же вопрос — инструмент недоступен по правам, реальных цифр в ответе нет", page[:300])

    # ошибки API — понятные сообщения нужной степени тревожности, без голых кодов
    cases = [
        ("фейк:429 — покажи выручку", "warn", "перегружен"),
        ("фейк:500 — покажи выручку", "warn", "недоступен"),
        ("фейк:refusal — покажи выручку", "warn", "отказался"),
        ("фейк:badjson — покажи выручку", "error", "ожидаемом формате"),
    ]
    for question, kind, must in cases:
        raw = text_of(admin.post("/chat/ask", {"question": question}))
        fl = flashes(raw)
        check(fl != "", f"«{question[:12]}…»: есть понятное сообщение об ошибке", fl[:300])
        check(f'class="flash {kind}"' in raw, f"«{question[:12]}…»: степень тревожности верная ({kind})", raw[:300])
        check(must in fl, f"«{question[:12]}…»: сообщение по делу — «{must}»", fl[:300])
        check(not re.search(r"\b(401|403|404|429|500|502)\b", fl), f"«{question[:12]}…»: без голых кодов ошибок", fl)

    # обрезанный ответ (max_tokens) — не ошибка, короткий ответ с пояснением
    page = text_of(admin.post("/chat/ask", {"question": "фейк:maxtokens — длинный вопрос"}))
    check("Незаконченный отв" in page and "обрезан" in page, "обрезанный ответ показан как есть, с пояснением", page[:400])

    # ядро Claude отключено — понятная ошибка вместо попытки достучаться до API
    admin.post(f"/connections/{cid}/toggle")
    r = admin.post("/chat/ask", {"question": "Привет"})
    check("Ядро Claude ещё не подключено" in flashes(text_of(r)), "отключённое ядро Claude — понятная ошибка, без запроса к API")
    admin.post(f"/connections/{cid}/toggle")

    # модель не из нашего прайс-листа — расход всё равно считается (по «дорогому» тарифу),
    # а не бесплатно: иначе месячный лимит незаметно перестаёт работать для такой модели
    admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": "", "f_model": "claude-opus-4-8", "f_monthly_limit_usd": "50"})
    admin.post("/chat/ask", {"question": "Ещё один факт про облака"})
    row = db_rows("SELECT * FROM ai_usage ORDER BY id DESC LIMIT 1")[0]
    check(row["model"] == "claude-opus-4-8" and row["cost_usd"] > 0,
          "расход для модели не из прайс-листа посчитан не по нулю", str(row))
    admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": "", "f_model": "claude-sonnet-5", "f_monthly_limit_usd": "50"})

    # лимит проверяется и МЕЖДУ обращениями внутри одного вопроса (цепочка вызовов инструмента),
    # а не только один раз в начале — иначе один вопрос может пробить лимит сразу на несколько запросов
    spent_now = db_rows("SELECT COALESCE(SUM(cost_usd), 0) s FROM ai_usage")[0]["s"]
    round_cost = 123 / 1_000_000 * 2.00 + 45 / 1_000_000 * 10.00  # claude-sonnet-5: $2/$10 за млн токенов
    admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": "", "f_model": "claude-sonnet-5",
                                        "f_monthly_limit_usd": f"{spent_now + round_cost:.6f}"})
    calls_before = httpx.get(f"{FAKE}/debug/claude-messages-calls", trust_env=False).json()["n"]
    fl = flashes(text_of(admin.post("/chat/ask", {"question": "Сколько у нас выручки за месяц?"})))
    check("нескольких обращений" in fl and "лимит" in fl.lower(),
          "лимит проверяется и между обращениями внутри одного вопроса, не только в начале", fl[:300])
    calls_after = httpx.get(f"{FAKE}/debug/claude-messages-calls", trust_env=False).json()["n"]
    check(calls_after - calls_before == 1,
          f"…первое обращение (вызов инструмента) прошло, второе — уже нет (было +{calls_after - calls_before})")
    admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": "", "f_model": "claude-sonnet-5", "f_monthly_limit_usd": "50"})

    # месячный лимит расходов: панель останавливает запросы САМА, не дожидаясь ответа от Anthropic
    calls_before = httpx.get(f"{FAKE}/debug/claude-messages-calls", trust_env=False).json()["n"]
    spent = db_rows("SELECT COALESCE(SUM(cost_usd), 0) s FROM ai_usage")[0]["s"]
    admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": "", "f_model": "claude-sonnet-5",
                                        "f_monthly_limit_usd": f"{spent:.6f}"})
    r = admin.post("/chat/ask", {"question": "Ещё один вопрос про облака"})
    fl = flashes(text_of(r))
    check("лимит" in fl.lower() and "исчерпан" in fl, "месячный лимит расходов исчерпан — вопрос отклонён", fl[:300])
    calls_after = httpx.get(f"{FAKE}/debug/claude-messages-calls", trust_env=False).json()["n"]
    check(calls_after == calls_before, "…и к Claude при этом даже не обращались (лимит проверяется до вызова API)")
    admin.post(f"/connections/{cid}", {"name": "Ядро Claude", "f_api_key": "", "f_model": "claude-sonnet-5", "f_monthly_limit_usd": "50"})

    # история и её очистка — только своя, по сотруднику
    page = text_of(admin.get("/chat"))
    check('class="chat-msg user"' in page and "Расскажи короткий факт" in page, "история вопросов сохраняется и показывается")
    check(text_of(manager.get("/chat")).count("Сколько у нас выручки") <= 1 and "Расскажи короткий факт" not in text_of(manager.get("/chat")),
          "история — только своя, а не общая на всех сотрудников")
    admin.post("/chat/clear")
    page = text_of(admin.get("/chat"))
    check("Пока пусто" in page and "Расскажи короткий факт" not in page, "очистка истории работает")


def scenario_testing(admin: Browser) -> None:
    section("8. Экран «Тестирование»")
    setup_app_env()
    from app import connectors, diagnostics, store

    # параллельность и устойчивость к сбоям: 3 «медленных» подключения по 3 секунды
    for i in range(3):
        admin.post("/connections/new/onec", {"name": f"Медленная 1С {i}", "f_base_url": f"{FAKE}/ka/slow", "f_username": "Админ",
                                              "f_password": "пароль123", "f_use_odata": "1", "f_verify_ssl": "1"})
    db_exec("INSERT INTO connections(type,name,config,enabled,created_at,updated_at) VALUES('zzz','Неизвестный тип','{}',1,'2026-01-01 00:00:00','2026-01-01 00:00:00')")
    db_exec("INSERT INTO connections(type,name,config,enabled,created_at,updated_at) VALUES('onec','Битый конфиг','not-json',1,'2026-01-01 00:00:00','2026-01-01 00:00:00')")
    db_exec("INSERT INTO connections(type,name,config,enabled,created_at,updated_at) VALUES('onec','Отключённая 1С','{}',0,'2026-01-01 00:00:00','2026-01-01 00:00:00')")
    n_active = len([c for c in store.list_connections() if c["enabled"]])
    t0 = time.monotonic()
    items = asyncio.run(diagnostics.connections_group())
    took = time.monotonic() - t0
    check(len(items) == len(store.list_connections()), f"проверены все {len(items)} подключений разом")
    check(took < 7.5, f"проверка идёт параллельно: 3 подключения по 3 с + остальные ({n_active} шт.) заняли {took:.1f} с, а не 9+ с", f"{took:.1f}")
    by_name = {i["title"]: i for i in items}
    check(by_name["Неизвестный тип"]["status"] == "error", "неизвестный тип подключения — красная строка, остальные проверены")
    check(by_name["Отключённая 1С"]["status"] == "off", "отключённое подключение не проверяется")
    check(by_name["Ядро Claude"]["status"] == "ok", "исправные подключения остались зелёными")

    # ошибка внутри проверки одного коннектора не должна ронять остальные
    ct = connectors.get_type("telegram")
    orig = ct.check

    async def boom(cfg, ctx):
        raise RuntimeError("внутренний сбой")

    ct.check = boom
    try:
        items = asyncio.run(diagnostics.connections_group())
    finally:
        ct.check = orig
    by_name = {i["title"]: i for i in items}
    check(by_name["Telegram-бот"]["status"] == "error" and "не смогла выполниться" in by_name["Telegram-бот"]["message"], "падение проверки одного подключения превращается в красную строку")
    check(by_name["Ядро Claude"]["status"] == "ok" and by_name["Битрикс24 — портал"]["status"] == "ok", "остальные подключения при этом проверены нормально")

    # сама кнопка
    r = admin.post("/testing/run", follow=False)
    check(r.status_code == 303, "POST /testing/run принят")
    page = text_of(admin.get("/testing"))
    for word in ("Сервер", "Сеть", "Подключения", "Проверить всё", "Скачать отчёт", "Что делать"):
        check(word in page, f"на экране есть «{word}»")
    for word in ("Версия Python", "Место на диске", "Запись в базу данных", "Ключ шифрования", "Секреты читаются ключом", "Время сервера", "Резервные копии"):
        check(word in page, f"группа «Сервер»: {word}")
    for word in ("Claude (api.anthropic.com)", "Wildberries", "Ozon", "Google", "Панель по внешнему адресу", "Защищённое соединение"):
        check(word in page, f"группа «Сеть»: {word}")
    check("Работает" in page and "Ошибка" in page and "Внимание" in page, "на экране есть все три состояния")
    last = store.last_test_run()
    check(last is not None and all(g["items"] for g in last["groups"]), "результат сохранён в базе, во всех трёх группах есть строки")
    bad = [i for g in last["groups"] for i in g["items"] if i["status"] in ("warn", "error") and not i.get("action")]
    check(not bad, "у каждой жёлтой и красной строки есть «Что делать»", str([b["title"] for b in bad]))
    # резервных копий ещё нет → внимание
    backup_item = next(i for i in last["groups"][0]["items"] if i["id"] == "backup")
    check(backup_item["status"] == "warn", "без резервных копий — «внимание» с подсказкой запустить резервная-копия.bat")
    pw_item = next(i for i in last["groups"][0]["items"] if i["id"] == "adminpw")
    check(pw_item["status"] == "ok", "пароль admin сменён — проверка зелёная")


def scenario_public_url(admin: Browser) -> None:
    section("9. Внешний адрес панели")
    base_settings = {"f_company_name": "ООО «Ромашка» — Москва", "f_session_hours": "12", "f_log_retention_days": "90",
                     "f_backup_dir": "", "f_backup_keep": "14", "f_google_client_id": "1234-abc.apps.googleusercontent.com", "f_google_client_secret": ""}
    setup_app_env()
    from app import diagnostics
    r = admin.post("/settings", {**base_settings, "f_public_url": "panel example"})
    check(r.status_code == 400 and "должен начинаться с http" in text_of(r), "кривой внешний адрес отклоняется")
    r = admin.post("/settings", {**base_settings, "f_public_url": BASE + "/"})
    check("сохранены" in flashes(text_of(r)), "внешний адрес сохранён (хвостовой / убран)")
    items = asyncio.run(diagnostics._external_items(BASE, False))
    check(items[0]["status"] == "ok", f"панель открывается по внешнему адресу: {items[0]['message']}")
    check(items[1]["status"] == "warn" and "http" in items[1]["message"], "http вместо https — «внимание» с пояснением")
    items = asyncio.run(diagnostics._external_items("http://127.0.0.1:9", False))
    check(items[0]["status"] == "error" and "не открывается" in items[0]["message"] and items[0]["action"], f"недоступный адрес — ошибка с действиями: {items[0]['message'][:120]}")
    items = asyncio.run(diagnostics._external_items(f"https://127.0.0.1:{FAKE_HTTPS}", False))
    check(items[0]["status"] == "error", f"https с самоподписанным сертификатом — ошибка: {items[0]['message'][:100]}")
    items = asyncio.run(diagnostics._external_items(FAKE, False))
    check(items[0]["status"] == "error" and "не наша панель" in items[0]["message"], "по адресу отвечает другая программа — ошибка")
    r = admin.post("/settings", {**base_settings, "f_public_url": ""})
    check("сохранены" in flashes(text_of(r)), "внешний адрес очищен обратно")


def scenario_backup_report_logs(admin: Browser, manager: Browser) -> None:
    section("10. Резервная копия, отчёт, журнал")
    r = admin.post("/settings/backup")
    check("Резервная копия создана" in flashes(text_of(r)), "резервная копия создаётся из панели", flashes(text_of(r))[:200])
    zips = sorted((DATA / "backups").glob("копия-*.zip"))
    check(len(zips) == 1, "файл копии лежит в data/backups")
    if zips:
        names = zipfile.ZipFile(zips[0]).namelist()
        check("platform.db" in names and "secret.key" in names, "в копии есть база и ключ шифрования", str(names))
    admin.post("/testing/run")
    last_items = {i["id"]: i for g in json.loads(db_rows("SELECT result FROM test_runs ORDER BY id DESC LIMIT 1")[0]["result"])["groups"] for i in g["items"]}
    check(last_items["backup"]["status"] == "ok", "после копии проверка «Резервные копии» зелёная")

    r = admin.get("/testing/report.txt")
    body = r.content.decode("utf-8-sig")
    check(r.status_code == 200 and "attachment" in r.headers.get("content-disposition", ""), "отчёт скачивается файлом")
    check("отч" in r.headers.get("content-disposition", "").lower() or "%D0%BE" in r.headers.get("content-disposition", ""), "у файла русское имя (filename*)")
    secrets_list = [fk.TG_GOOD, fk.WB_GOOD, fk.CLAUDE_GOOD, fk.OZON_KEY, fk.YM_GOOD, fk.BITRIX_CODE, fk.MAX_GOOD, "пароль123", "gsecret-abcdef123456",
                    "refresh-token-first", "КрепкийПароль-2026", "Passw0rd-x1", (DATA / "secret.key").read_text().strip(), "wb-bad-token", "токенСРусскимиБуквами"]
    leaks = [s for s in secrets_list if s in body]
    check(not leaks, "в отчёте нет ни одного пароля, токена или ключа", str(leaks))
    masks = re.findall(r"\*{8}\w{4}", body)
    check(not masks, "в отчёте нет даже масок с последними символами секретов", str(masks))
    check("ОТЧЁТ ДЛЯ РАЗРАБОТЧИКА" in body and "Что делать:" in body and "ПОДКЛЮЧЕНИЯ" in body, "в отчёте есть заголовок, результаты с «Что делать» и список подключений")
    check("<скрыто>" in body or "***" in body, "секретные поля в отчёте помечены как скрытые")
    check("\r\n" in r.content.decode("utf-8-sig"), "отчёт с переводами строк Windows (открывается в Блокноте)")
    check(manager.get("/testing/report.txt").status_code == 403, "менеджеру отчёт не отдаётся")

    # журнал: фильтры
    page = text_of(admin.get("/logs?level=error"))
    check("<tbody>" in page and "Информация" not in page.split("<tbody>")[1], "фильтр по важности «Ошибка» оставляет только ошибки")
    page = text_of(admin.get("/logs?section=auth"))
    check("Вход:" in page, "фильтр по разделу «Вход и доступ» показывает входы")
    page = text_of(admin.get("/logs?q=" + quote_plus("Ядро Claude")))
    check("Ядро Claude" in page, "поиск по тексту работает с кириллицей")
    page = text_of(admin.get("/logs?section=auth&level=warning"))
    check("Отказано в доступе" in page, "попытки менеджера попасть в закрытые разделы записаны в журнал")
    tok_events = db_rows("SELECT message FROM events")
    check(not any(fk.TG_GOOD in e["message"] or fk.CLAUDE_GOOD in e["message"] for e in tok_events), "в журнале нет токенов")

    # автоочистка
    db_exec("INSERT INTO events(ts,level,section,message) VALUES('2020-01-01 00:00:00','info','system','очень старая запись')")
    db_exec("INSERT INTO events(ts,level,section,message) VALUES('2020-01-02 00:00:00','error','system','очень старая ошибка')")
    r = admin.post("/settings/cleanup")
    check("удалены" in flashes(text_of(r)), "очистка журнала выполняется")
    check(db_rows("SELECT COUNT(*) c FROM events WHERE ts < '2021-01-01'")[0]["c"] == 0, "старые записи удалены автоочисткой")
    check(db_rows("SELECT COUNT(*) c FROM events")[0]["c"] > 5, "свежие записи остались")


def scenario_forms_encoding(admin: Browser) -> None:
    section("11. Кодировка форм (как отправляет браузер)")
    raw_name = "Тест %D0%B8 %2B + %26 – Ёж"
    # тело формы вручную: %XX от UTF-8, «+» как пробел, %2B как плюс, %26 как амперсанд
    body = ("csrf=" + admin.token() + "&login=enc.test&full_name=%D0%A2%D0%B5%D1%81%D1%82+%D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2+%2B+%D0%9F%D0%B5%D1%82%D1%80%D0%BE%D0%B2+%26+%D0%9A%D0%BE"
            "&role=employee&password=Passw0rd-x1&is_active=1")
    r = admin.c.post("/users/new", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    row = db_rows("SELECT full_name FROM users WHERE login='enc.test'")
    check(bool(row) and row[0]["full_name"] == "Тест Иванов + Петров & Ко", f"«%D0%A2…» из формы превращается в «Тест Иванов + Петров & Ко»", str(row))
    page = text_of(admin.get("/users"))
    check("Тест Иванов + Петров & Ко" in page, "на странице сотрудников имя показано без искажений")
    r = admin.c.post("/users/new", content=body.replace("enc.test", "enc.test2").replace("%D0%A2%D0%B5%D1%81%D1%82", "%D0%A2%D0%B5%D1%81%D1%82;charset"),
                     headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
    check(r.status_code in (303, 200), "заголовок с charset=utf-8 тоже разбирается")
    r = admin.get("/logs")
    check("charset=utf-8" in r.headers.get("content-type", "").lower(), "страницы отдаются с charset=utf-8")
    company = db_rows("SELECT value FROM settings WHERE key='company_name'")[0]["value"]
    check(company == "ООО «Ромашка» — Москва", "название компании с кавычками-«ёлочками» и длинным тире сохранено", company)


def scenario_session(admin: Browser) -> None:
    section("12. Выход")
    b = Browser()
    b.login("admin", "КрепкийПароль-2026")
    r = b.post("/logout", follow=False)
    check(r.status_code == 303, "выход выполняется")
    r = b.get("/")
    check(r.status_code == 303 and "/login" in r.headers["location"], "после выхода сессия недействительна")
    for _ in range(5):
        Browser().login("admin", "не-тот")
    r = Browser().login("admin", "КрепкийПароль-2026")
    check(r.status_code == 401 and "заблокирован" in text_of(r), "после 5 неверных паролей вход блокируется на несколько минут")
    db_exec("UPDATE users SET failed_attempts=0, locked_until=0")


def main() -> int:
    procs: list[subprocess.Popen] = []
    try:
        procs = start_services()
        admin = Browser()
        scenario_login(admin)
        scenario_1c(admin)
        scenario_1c_finance(admin)
        scenario_1c_finance_cache_internals(admin)
        scenario_bitrix_mask(admin)
        scenario_tokens(admin)
        scenario_single(admin)
        scenario_google(admin)
        br = scenario_roles(admin)
        scenario_claude_chat(admin, br)
        scenario_testing(admin)
        scenario_public_url(admin)
        scenario_backup_report_logs(admin, br["petrova"])
        scenario_forms_encoding(admin)
        scenario_session(admin)
    finally:
        if not KEEP:
            for p in procs:
                p.terminate()
            for p in procs:
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
            shutil.rmtree(TMP, ignore_errors=True)
        else:
            print(f"\n[--keep] данные: {TMP}; панель {BASE}; процессы: {[p.pid for p in procs]}")
    passed = sum(1 for ok_, _, _ in RESULTS if ok_)
    failed = [(l, d) for ok_, l, d in RESULTS if not ok_]
    print(f"\nИтого: {passed} из {len(RESULTS)} проверок пройдено, провалено: {len(failed)}")
    for label, _ in failed:
        print("  ✗", label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
