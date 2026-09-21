"""1С:Предприятие (Комплексная автоматизация 2.5). Проверка — запрос к стандартному интерфейсу OData."""
from __future__ import annotations

import base64
import re
import time

from .base import (CheckResult, ConnectorType, Ctx, Field, err, explain_exception, http, ok, warn)


def normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    # если вставили адрес тонкого клиента или уже с /odata/... — отрезаем лишнее
    url = re.sub(r"/odata/.*$", "", url, flags=re.I)
    url = re.sub(r"/(ru_RU|en_US|ru|en)(/.*)?$", "", url, flags=re.I)
    return url.rstrip("/")


def basic_header(user: str, password: str) -> dict:
    raw = f"{user}:{password}".encode("utf-8")   # 1С ожидает UTF-8: русские логины и пароли допустимы
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _normalize(cfg: dict) -> dict:
    if cfg.get("base_url"):
        cfg["base_url"] = normalize_base_url(cfg["base_url"])
    return cfg


def _validate(cfg: dict) -> dict:
    errors = {}
    if not cfg.get("use_odata") and not (cfg.get("http_service_path") or "").strip():
        errors["use_odata"] = ("Включите OData или укажите путь к HTTP-сервису — иначе проверять и читать из 1С будет нечем.")
    url = cfg.get("base_url", "")
    if url and not re.match(r"^https?://[^\s/]+", url):
        errors["base_url"] = "Адрес должен начинаться с http:// или https://, например https://192.168.1.10/ka"
    return errors


async def check(cfg: dict, ctx: Ctx) -> CheckResult:
    base = normalize_base_url(cfg.get("base_url", ""))
    user = cfg.get("username", "")
    password = cfg.get("password", "")
    verify = bool(cfg.get("verify_ssl", True))
    if not base:
        return err("Не указан адрес базы 1С.", "Откройте подключение и впишите адрес, например https://192.168.1.10/ka")
    started = time.monotonic()
    cert_note = "" if verify else " (проверка сертификата отключена)"
    can_skip = verify
    result: CheckResult | None = None

    if cfg.get("use_odata", True):
        url = f"{base}/odata/standard.odata/?$format=json"
        try:
            resp = await http("GET", url, verify=verify, timeout=40, headers={**basic_header(user, password), "Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001
            msg, action = explain_exception(exc, "1С", url=base, can_skip_cert=can_skip)
            return err(msg, action)
        result = _odata_result(resp, base, cert_note)
        if result.status == "error":
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

    path = (cfg.get("http_service_path") or "").strip().strip("/")
    if path:
        svc = await _check_http_service(base, path, user, password, verify, can_skip)
        if result is None:
            result = svc
        elif svc.status != "ok":
            # OData работает, а HTTP-сервис ещё нет — это «внимание», а не «ошибка»
            result = warn(result.message + " Но HTTP-сервис расширения пока недоступен: " + svc.message,
                          svc.action, **{**result.details, **svc.details})
        else:
            result.message += " HTTP-сервис расширения отвечает."
            result.details.update(svc.details)
    assert result is not None
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def _odata_result(resp, base: str, cert_note: str) -> CheckResult:
    code = resp.status_code
    if code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("location", "")
        return err("Адрес перенаправляет на другой адрес" + (f" ({loc.split('?')[0]})" if loc else "") + ".",
                   "Впишите в подключение тот адрес, на который идёт перенаправление (частая причина: нужен https:// вместо http://).")
    if code == 200:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and "value" in data:
            n = len(data["value"]) if isinstance(data["value"], list) else 0
            if n == 0:
                return warn("1С отвечает, OData включён, но ни один объект в него не выложен — данных из 1С пока получить нельзя.",
                            "Программисту 1С: задать состав стандартного интерфейса OData (метод УстановитьСоставСтандартногоИнтерфейсаOData) "
                            "или установить расширение платформы — это делается на втором этапе. Подключение можно оставить как есть.",
                            odata_objects=0)
            return ok(f"1С отвечает, OData работает: доступно {n} объектов (справочников, документов и т. д.){cert_note}.",
                      odata_objects=n, verify_ssl=cert_note == "")
        if isinstance(data, dict) and "odata.error" in data or isinstance(data, dict) and "error" in data:
            return err("1С ответила ошибкой на запрос OData.", "Скачайте отчёт на экране «Тестирование» и передайте программисту 1С.")
        return err("Адрес отвечает, но это не интерфейс OData 1С (пришёл другой ответ).",
                   "Проверьте адрес базы: он должен вести именно на публикацию 1С, например https://192.168.1.10/ka")
    if code == 401:
        return err("1С отвечает, но логин или пароль не подходят.",
                   "Проверьте пользователя и пароль (регистр важен). Если пользователь только что создан — убедитесь, "
                   "что он есть в списке пользователей базы и ему разрешён вход через веб-клиент.")
    if code == 403:
        return err("1С отвечает и узнала пользователя, но не разрешает ему работать через OData: не хватает прав.",
                   "В 1С у пользователя должен быть профиль с правом «Использование OData» (роль «Полные права» тоже подходит).")
    if code == 404:
        return err("1С отвечает, но интерфейс OData не опубликован — включите галочку в настройках публикации.",
                   "Конфигуратор → Администрирование → Публикация на веб-сервере → поставьте «Публиковать стандартный интерфейс OData» "
                   "→ Опубликовать. Затем перезапустите веб-сервер (IIS/Apache). Если галочка уже стоит — проверьте, что имя публикации в адресе базы написано верно.")
    if code == 400:
        return err("1С не поняла запрос к OData.", "Скачайте отчёт и передайте программисту 1С.")
    if code in (502, 503, 504):
        return err("Веб-сервер отвечает, но 1С за ним недоступна.",
                   "Проверьте, что сервер 1С:Предприятия запущен, и что веб-сервер (IIS/Apache) видит его. Затем повторите.")
    if code >= 500:
        return err("1С вернула внутреннюю ошибку при обращении к OData.",
                   "Посмотрите журнал регистрации 1С за это время. Если не ясно — скачайте отчёт и передайте программисту 1С.")
    return err(f"1С ответила неожиданным кодом ({code}).", "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


async def _check_http_service(base, path, user, password, verify, can_skip) -> CheckResult:
    url = f"{base}/{path}"
    try:
        resp = await http("GET", url, verify=verify, timeout=20, headers=basic_header(user, password))
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "HTTP-сервис 1С", can_skip_cert=can_skip)
        return err(msg, action)
    code = resp.status_code
    if code in (401, 403):
        return err("HTTP-сервис отвечает, но пользователю нет доступа.", "Дайте пользователю права на HTTP-сервис в расширении.")
    if code == 404:
        return warn("расширение с HTTP-сервисами не найдено по указанному пути.",
                    "Это нормально, пока программист 1С не установил расширение (второй этап). Потом проверьте путь, например hs/platforma.")
    if code >= 500:
        return err("HTTP-сервис вернул внутреннюю ошибку.", "Передайте программисту 1С.")
    return ok("HTTP-сервис отвечает.", http_service="ok")


TYPE = ConnectorType(
    key="onec",
    title="1С:Предприятие",
    group="Учёт и CRM",
    icon="1С",
    description="Основная учётная система (1С:Комплексная автоматизация). Из неё платформа будет брать остатки, продажи, деньги и документы.",
    name_hint="Например: 1С КА основная",
    howto=[
        "Нужен доступ к 1С по сети: адрес базы, который вы вводите в браузере для входа, но без «/ru_RU/» на конце.",
        "Для чтения данных должен быть опубликован стандартный интерфейс OData (Конфигуратор → Администрирование → Публикация на веб-сервере).",
        "Лучше создать в 1С отдельного пользователя «Платформа» с правами только на чтение.",
    ],
    fields=[
        Field("base_url", "Адрес базы 1С", "url", required=True, placeholder="https://192.168.1.10/ka",
              help="Адрес, по которому вы открываете 1С в браузере (без «/ru_RU/» на конце).",
              where=["Откройте 1С в браузере как обычно.",
                     "Скопируйте адрес из адресной строки до имени публикации: например https://192.168.1.10/ka",
                     "Если справа стоит /ru_RU/ или что-то после имени — уберите."]),
        Field("username", "Пользователь 1С", required=True,
              help="Имя пользователя, как при входе в 1С. Русские буквы допустимы."),
        Field("password", "Пароль", "password", secret=True,
              help="Пароль этого пользователя. Оставьте пустым, если в 1С у пользователя нет пароля."),
        Field("use_odata", "Читать данные через стандартный OData", "bool", default="1",
              help="Оставьте галочку. Интерфейс OData должен быть опубликован в настройках публикации 1С."),
        Field("http_service_path", "Путь к HTTP-сервису расширения", advanced=True, placeholder="hs/platforma",
              help="Понадобится на втором этапе, когда программист 1С установит расширение. Пока можно оставить пустым."),
        Field("verify_ssl", "Проверять сертификат https", "bool", default="1", advanced=True,
              help="Снимите галочку, пока 1С открывается по IP-адресу с самоподписанным сертификатом. "
                   "Когда получите нормальный сертификат на домен — включите обратно."),
    ],
    check=check,
    validate=_validate,
    normalize=_normalize,
)
