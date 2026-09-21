"""Маркетплейсы: Wildberries, Ozon, Яндекс Маркет — по одному подключению на кабинет."""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone

from .base import CheckResult, ConnectorType, Ctx, Field, err, explain_exception, http, ok, warn


def _too_many(service: str) -> CheckResult:
    return warn(f"{service} просит подождать: слишком частые запросы.",
                "Это временно и не считается поломкой. Повторите проверку через минуту.")


def _bad_gateway(service: str) -> CheckResult:
    return err(f"{service} сейчас не отвечает (сбой на стороне маркетплейса).",
               "Повторите проверку через 10–15 минут. Если не проходит больше часа — проверьте страницу статуса сервиса.")


# ---------------------------------------------------------------------------
# Wildberries
# ---------------------------------------------------------------------------

def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()))
    except Exception:  # noqa: BLE001
        return {}


async def check_wb(cfg: dict, ctx: Ctx) -> CheckResult:
    token = cfg.get("token", "")
    if not token:
        return err("Не указан токен Wildberries.", "Откройте подключение и вставьте токен из кабинета продавца.")
    started = time.monotonic()
    url = "https://common-api.wildberries.ru/api/v1/seller-info"
    try:
        resp = await http("GET", url, headers={"Authorization": token}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "Wildberries")
        return err(msg, action)
    code = resp.status_code
    elapsed = int((time.monotonic() - started) * 1000)
    if code == 200:
        try:
            info = resp.json()
        except ValueError:
            info = {}
        name = info.get("tradeMark") or info.get("name") or "кабинет"
        res = ok(f"Токен принят, кабинет: «{name}».", seller=name)
        exp = _jwt_payload(token).get("exp")
        if isinstance(exp, (int, float)):
            days = int((exp - datetime.now(timezone.utc).timestamp()) // 86400)
            res.details["days_left"] = days
            if days < 0:
                res = err("Срок действия токена Wildberries закончился.", "Выпустите новый токен в кабинете и вставьте его здесь.")
            elif days <= 14:
                res = warn(f"Токен работает, но истекает через {days} дн.",
                           "Заранее выпустите новый токен в кабинете Wildberries и замените его здесь, иначе выгрузка остановится.",
                           seller=name, days_left=days)
        res.elapsed_ms = elapsed
        return res
    if code == 401:
        return err("Wildberries не принимает токен: он неверный, отозван или срок его действия закончился.",
                   "В кабинете Wildberries выпустите новый токен и вставьте его здесь целиком. Токен действует ограниченный срок.")
    if code == 403:
        return err("Токен принят, но у него нет прав на этот раздел.",
                   "Создайте токен с нужными категориями (Контент, Аналитика, Цены, Маркетплейс, Статистика, Поставки) — сначала достаточно «только чтение».")
    if code == 429:
        return _too_many("Wildberries")
    if code >= 500:
        return _bad_gateway("Wildberries")
    return err(f"Wildberries ответил неожиданным кодом ({code}).", "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


WB = ConnectorType(
    key="wildberries",
    title="Wildberries",
    group="Маркетплейсы",
    icon="WB",
    description="Кабинет продавца Wildberries. Один кабинет — одно подключение.",
    name_hint="Например: WB — ИП Иванов",
    fields=[
        Field("token", "Токен продавца", "password", required=True, secret=True, ascii_only=True,
              help="Длинная строка из личного кабинета Wildberries (раздел доступа к API). Срок жизни ограничен.",
              where=["Зайдите в личный кабинет продавца на seller.wildberries.ru.",
                     "Профиль (справа вверху) → Интеграции → Доступ к API (название раздела у WB иногда меняется).",
                     "Нажмите «Создать токен». Для начала выберите тип «Базовый» и отметьте «Только чтение» для нужных категорий.",
                     "Скопируйте токен сразу — потом он не показывается. Вставьте сюда целиком, без пробелов."]),
    ],
    check=check_wb,
)


# ---------------------------------------------------------------------------
# Ozon
# ---------------------------------------------------------------------------

def _ozon_validate(cfg: dict) -> dict:
    cid = cfg.get("client_id", "")
    if cid and not cid.isdigit():
        return {"client_id": "Client-Id у Ozon — это число (например 123456). Проверьте, что вы не вставили туда API-ключ."}
    return {}


async def check_ozon(cfg: dict, ctx: Ctx) -> CheckResult:
    cid, key = cfg.get("client_id", ""), cfg.get("api_key", "")
    if not cid or not key:
        return err("Не заполнены Client-Id или API-ключ Ozon.", "Откройте подключение и заполните оба поля.")
    started = time.monotonic()
    try:
        resp = await http("POST", "https://api-seller.ozon.ru/v1/roles",
                          headers={"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"}, content=b"{}", timeout=20)
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "Ozon")
        return err(msg, action)
    code = resp.status_code
    elapsed = int((time.monotonic() - started) * 1000)
    if code == 200:
        try:
            roles = [r.get("name", "") for r in resp.json().get("roles", [])]
        except (ValueError, AttributeError):
            roles = []
        res = ok("Ключ принят, кабинет Ozon отвечает" + (f". Права ключа: {', '.join(roles)}." if roles else "."), roles=roles)
        res.elapsed_ms = elapsed
        return res
    if code in (401, 403):
        text = resp.text.lower()
        if "expired" in text or "истек" in text:
            return err("Срок действия API-ключа Ozon закончился.", "Создайте новый ключ в кабинете Ozon и вставьте его здесь.")
        return err("Ozon не принимает Client-Id и API-ключ: пара неверная, ключ удалён или у него нет прав.",
                   "Проверьте, что Client-Id и ключ взяты из одного кабинета (Настройки → API-ключи). При сомнениях создайте новый ключ.")
    if code == 429:
        return _too_many("Ozon")
    if code >= 500:
        return _bad_gateway("Ozon")
    return err(f"Ozon ответил неожиданным кодом ({code}).", "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


OZON = ConnectorType(
    key="ozon",
    title="Ozon",
    group="Маркетплейсы",
    icon="Oz",
    description="Кабинет продавца Ozon (Seller API). Один кабинет — одно подключение.",
    name_hint="Например: Ozon — ООО «Ромашка»",
    validate=_ozon_validate,
    fields=[
        Field("client_id", "Client-Id", required=True, placeholder="123456",
              help="Число, которое показано рядом с API-ключами в кабинете Ozon.",
              where=["Зайдите в личный кабинет seller.ozon.ru.",
                     "Настройки → API-ключи. Client ID указан вверху страницы."]),
        Field("api_key", "API-ключ", "password", required=True, secret=True, ascii_only=True,
              help="Длинная строка вида 1a2b3c4d-… из того же раздела.",
              where=["На той же странице «API-ключи» нажмите «Сгенерировать ключ».",
                     "Роль для начала — «Admin read only» (только чтение). Позже можно выдать больше.",
                     "Скопируйте ключ сразу и вставьте сюда."]),
    ],
    check=check_ozon,
)


# ---------------------------------------------------------------------------
# Яндекс Маркет
# ---------------------------------------------------------------------------

def _ym_validate(cfg: dict) -> dict:
    c = cfg.get("campaign_id", "")
    if c and not c.isdigit():
        return {"campaign_id": "Номер кампании — число. Оставьте поле пустым, если не знаете."}
    return {}


async def check_ym(cfg: dict, ctx: Ctx) -> CheckResult:
    key = cfg.get("api_key", "")
    if not key:
        return err("Не указан Api-Key Яндекс Маркета.", "Откройте подключение и вставьте ключ из кабинета.")
    started = time.monotonic()
    try:
        resp = await http("GET", "https://api.partner.market.yandex.ru/campaigns", headers={"Api-Key": key}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "Яндекс Маркет")
        return err(msg, action)
    code = resp.status_code
    elapsed = int((time.monotonic() - started) * 1000)
    if code == 200:
        try:
            camps = resp.json().get("campaigns", [])
        except (ValueError, AttributeError):
            camps = []
        want = cfg.get("campaign_id", "")
        if want and all(str(c.get("id")) != want for c in camps):
            return warn(f"Ключ работает, но кампании №{want} среди доступных нет (доступно кампаний: {len(camps)}).",
                        "Проверьте номер кампании или выдайте ключу доступ к нужному магазину.", campaigns=len(camps))
        names = ", ".join(str(c.get("domain") or c.get("id")) for c in camps[:3])
        res = ok(f"Ключ принят. Доступно кампаний (магазинов): {len(camps)}" + (f" — {names}." if names else "."), campaigns=len(camps))
        res.elapsed_ms = elapsed
        return res
    if code == 401:
        return err("Яндекс Маркет не принимает Api-Key: ключ неверный или отозван.",
                   "В кабинете Яндекс Маркета создайте новый ключ (Настройки → API и модули) и вставьте его здесь.")
    if code == 403:
        return err("Ключ принят, но у него нет прав для этого запроса.",
                   "В настройках ключа отметьте нужные доступы (кампании, заказы, товары, отчёты).")
    if code == 420 or code == 429:
        return _too_many("Яндекс Маркет")
    if code >= 500:
        return _bad_gateway("Яндекс Маркет")
    return err(f"Яндекс Маркет ответил неожиданным кодом ({code}).", "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


YM = ConnectorType(
    key="yandex_market",
    title="Яндекс Маркет",
    group="Маркетплейсы",
    icon="ЯМ",
    description="Кабинет Яндекс Маркета (Partner API). Один кабинет — одно подключение.",
    name_hint="Например: Яндекс Маркет — основной",
    validate=_ym_validate,
    fields=[
        Field("api_key", "Api-Key", "password", required=True, secret=True, ascii_only=True,
              help="Ключ доступа к API из кабинета Яндекс Маркета.",
              where=["Зайдите в partner.market.yandex.ru и выберите нужный кабинет.",
                     "Настройки → API и модули (в новых версиях — «Ключи API») → «Создать ключ».",
                     "Отметьте нужные доступы: сначала достаточно чтения. Скопируйте ключ и вставьте сюда."]),
        Field("campaign_id", "Номер кампании", advanced=True, placeholder="12345678",
              help="Необязательно. Если указать, проверка убедится, что ключ видит именно этот магазин."),
    ],
    check=check_ym,
)
