"""Битрикс24 через входящий вебхук. Проверка — метод profile.json."""
from __future__ import annotations

import re
import time

from .base import CheckResult, ConnectorType, Ctx, Field, err, explain_exception, http, ok, warn


def normalize_webhook(url: str) -> str:
    url = (url or "").strip()
    # частая ошибка: вставили «пример URL для вызова» вместе с названием метода
    url = re.sub(r"/[A-Za-z0-9_.]+\.json(\?.*)?$", "/", url)
    if not url.endswith("/"):
        url += "/"
    return url


def _normalize(cfg: dict) -> dict:
    if cfg.get("webhook_url"):
        cfg["webhook_url"] = normalize_webhook(cfg["webhook_url"])
    return cfg


def _validate(cfg: dict) -> dict:
    url = cfg.get("webhook_url", "")
    if url and not re.match(r"^https?://[^\s/]+/rest/\d+/[A-Za-z0-9]+/?", url.strip()):
        return {"webhook_url": "Вебхук выглядит иначе, чем должен: https://ВАШ-ПОРТАЛ.bitrix24.ru/rest/1/КОД/ "
                               "Скопируйте «Ваш вебхук для вызова rest api» целиком."}
    return {}


async def check(cfg: dict, ctx: Ctx) -> CheckResult:
    hook = normalize_webhook(cfg.get("webhook_url", ""))
    if hook == "/":
        return err("Не указан вебхук Битрикс24.", "Впишите адрес входящего вебхука.")
    started = time.monotonic()
    try:
        resp = await http("GET", hook + "profile.json", timeout=20)
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "Битрикс24")
        return err(msg, action)
    elapsed = int((time.monotonic() - started) * 1000)
    code = resp.status_code
    try:
        data = resp.json()
    except ValueError:
        data = None
    code_name = (data or {}).get("error", "") if isinstance(data, dict) else ""
    desc = (data or {}).get("error_description", "") if isinstance(data, dict) else ""

    if code == 200 and isinstance(data, dict) and isinstance(data.get("result"), dict):
        r = data["result"]
        name = " ".join(x for x in (r.get("NAME"), r.get("LAST_NAME")) if x) or "без имени"
        admin = r.get("ADMIN") in (True, "Y", "true")
        res = ok(f"Портал отвечает, вебхук работает от имени: {name}" + (" (администратор)." if admin else "."),
                 user=name, admin=admin)
        if not admin:
            res = warn(res.message + " Это не администратор — часть данных может быть недоступна.",
                       "Если не хватает данных, создайте вебхук от имени администратора портала.", user=name)
        res.elapsed_ms = elapsed
        return res
    if code_name in ("INVALID_CREDENTIALS", "NO_AUTH_FOUND", "WRONG_AUTH_TYPE") or code in (401,):
        return err("Портал отвечает, но вебхук не принят: неверный код или вебхук удалён.",
                   "В Битрикс24: Разработчикам → Другое → Входящий вебхук — проверьте, что он ещё есть, и скопируйте адрес заново.")
    if code_name in ("insufficient_scope", "ACCESS_DENIED") or code == 403:
        return err("Вебхук работает, но у него нет нужных прав.",
                   "В настройках вебхука отметьте нужные права (CRM, Задачи, Пользователи, Диск, Календарь) и сохраните.")
    if code_name == "QUERY_LIMIT_EXCEEDED" or code == 503 and "LIMIT" in (desc or "").upper():
        return warn("Битрикс24 просит подождать: слишком много запросов.", "Это временно. Повторите проверку через минуту.")
    if code_name in ("ERROR_METHOD_NOT_FOUND",) or code == 404:
        return err("Такой адрес на портале не найден: возможно, неправильно указано имя портала или вебхук удалён.",
                   "Проверьте адрес портала (…bitrix24.ru) и скопируйте вебхук заново.")
    if code in (502, 503, 504):
        return err("Битрикс24 сейчас недоступен или перегружен.", "Повторите проверку позже. Проверьте status.bitrix24.ru.")
    if code == 200:
        return err("Адрес отвечает, но это не Битрикс24 или вебхук записан неверно.",
                   "Скопируйте вебхук заново: он должен выглядеть как https://ПОРТАЛ.bitrix24.ru/rest/1/КОД/")
    return err(f"Битрикс24 ответил неожиданным кодом ({code}).", "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


TYPE = ConnectorType(
    key="bitrix24",
    title="Битрикс24",
    group="Учёт и CRM",
    icon="B24",
    description="CRM и задачи. Подключение через входящий вебхук — от имени того сотрудника, который его создал.",
    name_hint="Например: Битрикс24 компании",
    howto=[
        "Вебхук — это ссылка с секретным кодом. Она даёт доступ к порталу, поэтому хранится в зашифрованном виде.",
        "Создавайте вебхук от имени отдельного сотрудника-администратора, чтобы он не зависел от увольнений.",
    ],
    fields=[
        Field("webhook_url", "Адрес входящего вебхука", "url", required=True, secret=True,
              placeholder="https://ВАШ-ПОРТАЛ.bitrix24.ru/rest/1/xxxxxxxxxxxxxxxx/",
              help="Ссылка вида https://портал.bitrix24.ru/rest/1/КОД/ — из раздела «Входящий вебхук».",
              where=["В Битрикс24 откройте: Разработчикам → Другое → Входящий вебхук (или «Приложения → Разработчикам»).",
                     "Нажмите «Добавить входящий вебхук». Отметьте права: Пользователи, CRM, Задачи, Диск, Календарь, Чат.",
                     "Сохраните и скопируйте строку «Ваш вебхук для вызова rest api» целиком.",
                     "Название метода на конце (например profile.json) добавлять не нужно — если вставите, мы уберём."]),
    ],
    check=check,
    validate=_validate,
    normalize=_normalize,
)
