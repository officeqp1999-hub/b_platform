"""Боты для сотрудников: MAX (основной) и Telegram (запасной)."""
from __future__ import annotations

import re
import time

from .base import CheckResult, ConnectorType, Ctx, Field, err, explain_exception, http, ok, warn

TELEGRAM_BLOCKED = ("Из России api.telegram.org часто блокируется — тогда укажите прокси в поле «Прокси» "
                    "или разместите сервер за пределами РФ.")


# ---------------------------------------------------------------------------
# MAX
# ---------------------------------------------------------------------------

async def check_max(cfg: dict, ctx: Ctx) -> CheckResult:
    token = cfg.get("token", "")
    if not token:
        return err("Не указан токен бота MAX.", "Откройте подключение и вставьте токен бота.")
    started = time.monotonic()
    try:
        resp = await http("GET", "https://platform-api.max.ru/me", headers={"Authorization": token}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "MAX")
        return err(msg, action)
    code = resp.status_code
    elapsed = int((time.monotonic() - started) * 1000)
    if code == 200:
        try:
            j = resp.json()
        except ValueError:
            j = {}
        title = j.get("name") or j.get("first_name") or "бот"
        uname = j.get("username")
        res = ok(f"Токен принят, бот: «{title}»" + (f" (@{uname})." if uname else "."), bot=title)
        res.elapsed_ms = elapsed
        return res
    if code in (401, 403):
        return err("MAX не принимает токен бота: он неверный или перевыпущен.",
                   "В кабинете бота MAX получите токен заново и вставьте его здесь целиком, без пробелов.")
    if code == 429:
        return warn("MAX просит подождать: слишком частые запросы.", "Повторите проверку через минуту.")
    if code >= 500:
        return err("MAX сейчас не отвечает (сбой на стороне сервиса).", "Повторите проверку через несколько минут.")
    return err(f"MAX ответил неожиданным кодом ({code}).", "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


MAX = ConnectorType(
    key="max",
    title="Бот MAX",
    group="Боты",
    icon="MX",
    single=True,
    description="Основной бот для сотрудников: через него они задают вопросы и получают ответы из всех систем. Бот может быть только один.",
    name_hint="Например: Бот MAX",
    fields=[
        Field("token", "Токен бота", "password", required=True, secret=True, ascii_only=True,
              help="Секретная строка, которую выдаёт платформа MAX при создании бота.",
              where=["Откройте business.max.ru (нужна организация, подтверждённая в MAX) — раздел «Чат-боты».",
                     "Создайте бота, задайте имя. После проверки в карточке бота откройте «Интеграция» → «Получить токен».",
                     "Скопируйте токен целиком. Если названия пунктов отличаются — смотрите документацию: dev.max.ru/docs."]),
    ],
    check=check_max,
)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _tg_validate(cfg: dict) -> dict:
    tok = cfg.get("token", "")
    if tok and not re.match(r"^\d{5,}:[A-Za-z0-9_-]{30,}$", tok):
        return {"token": "Токен Telegram выглядит так: 123456789:AAE-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx (цифры, двоеточие, буквы). "
                         "Скопируйте его из сообщения @BotFather целиком."}
    return {}


async def check_tg(cfg: dict, ctx: Ctx) -> CheckResult:
    token = cfg.get("token", "")
    proxy = cfg.get("proxy", "") or None
    if not token:
        return err("Не указан токен бота Telegram.", "Откройте подключение и вставьте токен от @BotFather.")
    started = time.monotonic()
    try:
        resp = await http("GET", f"https://api.telegram.org/bot{token}/getMe", proxy=proxy, timeout=15)
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "Telegram", blocked_hint=TELEGRAM_BLOCKED)
        return err(msg, action)
    code = resp.status_code
    elapsed = int((time.monotonic() - started) * 1000)
    if code == 200:
        try:
            r = resp.json().get("result", {})
        except ValueError:
            r = {}
        res = ok(f"Токен принят, бот: «{r.get('first_name', 'бот')}» (@{r.get('username', '?')})."
                 + (" Работает через прокси." if proxy else ""), bot=r.get("username", ""))
        res.elapsed_ms = elapsed
        return res
    if code in (401, 404):
        return err("Telegram не принимает токен бота: он неверный, бот удалён или токен перевыпущен.",
                   "Откройте @BotFather → /mybots → выберите бота → API Token — скопируйте токен заново и вставьте целиком.")
    if code == 409:
        return warn("Токен верный, но у бота включён другой способ получения сообщений (вебхук).", "Это нормально на этом этапе.")
    if code == 429:
        return warn("Telegram просит подождать: слишком частые запросы.", "Повторите проверку через минуту.")
    if code >= 500:
        return err("Telegram сейчас не отвечает.", "Повторите проверку через несколько минут.")
    return err(f"Telegram ответил неожиданным кодом ({code}).", "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


TELEGRAM = ConnectorType(
    key="telegram",
    title="Бот Telegram",
    group="Боты",
    icon="TG",
    single=True,
    description="Запасной бот на случай, если MAX недоступен. Бот может быть только один.",
    name_hint="Например: Бот Telegram (запасной)",
    validate=_tg_validate,
    fields=[
        Field("token", "Токен бота", "password", required=True, secret=True, ascii_only=True,
              placeholder="123456789:AAE-...",
              help="Токен, который присылает @BotFather при создании бота.",
              where=["В Telegram найдите @BotFather и напишите ему /newbot.",
                     "Придумайте имя и логин бота (логин заканчивается на bot).",
                     "BotFather пришлёт токен — скопируйте его целиком. Позже: /mybots → бот → API Token."]),
        Field("proxy", "Прокси (если Telegram закрыт)", "password", secret=True, advanced=True,
              placeholder="http://логин:пароль@адрес:порт",
              help="Необязательно. Нужен, если с этого сервера не открывается api.telegram.org."),
    ],
    check=check_tg,
)
