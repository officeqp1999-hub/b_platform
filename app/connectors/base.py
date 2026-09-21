"""Общие классы для описания типов подключений и человеческие сообщения об ошибках.

Форма подключения строится автоматически из списка Field в описании типа (ConnectorType).
Функция check() каждого типа возвращает CheckResult с понятным русским текстом
и строкой «Что делать» — сырые коды вроде «401 Unauthorized» пользователю не показываются.
"""
from __future__ import annotations

import errno
import re
import socket
import ssl
import unicodedata
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx

OK, WARN, ERROR = "ok", "warn", "error"
STATUS_LABELS = {OK: "Работает", WARN: "Внимание", ERROR: "Ошибка"}


# ---------------------------------------------------------------------------
# Описание полей и типов
# ---------------------------------------------------------------------------

@dataclass
class Field:
    name: str                       # имя поля в конфиге
    label: str                      # подпись в форме
    type: str = "text"              # text | password | url | number | bool | select | textarea | readonly
    help: str = ""                  # короткая подсказка под полем
    where: list[str] = field(default_factory=list)  # шаги «откуда взять» (раскрывающийся блок)
    required: bool = False
    secret: bool = False            # шифруется, в интерфейсе — маска, пустое значение = «не менять»
    default: str = ""
    placeholder: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)  # для select: (значение, подпись)
    suggestions: list[str] = field(default_factory=list)          # подсказки для text (datalist)
    ascii_only: bool = False        # токены и ключи: только латиница/цифры/знаки, пробелы вычищаем
    hidden: bool = False            # управляется системой (например, refresh-token Google), в форме не показывается
    min_value: Optional[float] = None
    advanced: bool = False          # показывать в блоке «Дополнительно»


@dataclass
class Ctx:
    """Контекст проверки: глобальные настройки панели (например, OAuth-клиент Google)."""
    settings: dict = field(default_factory=dict)


@dataclass
class CheckResult:
    status: str
    message: str
    action: str = ""                # «Что делать»
    details: dict = field(default_factory=dict)   # только несекретное — попадает в отчёт
    elapsed_ms: int = 0


CheckFn = Callable[[dict, Ctx], Awaitable[CheckResult]]
ValidateFn = Callable[[dict], dict]


@dataclass
class ConnectorType:
    key: str
    title: str
    group: str
    description: str
    fields: list[Field]
    check: CheckFn
    single: bool = False            # можно создать только одно подключение этого типа
    validate: Optional[ValidateFn] = None   # межполевые проверки: dict[field] -> сообщение
    normalize: Optional[Callable[[dict], dict]] = None   # приведение значений к нужному виду (адреса и т. п.)
    oauth: str = ""                 # "google" — форма с кнопкой OAuth
    icon: str = ""                  # 1–2 буквы на плитке
    howto: list[str] = field(default_factory=list)  # общая инструкция по типу
    name_hint: str = ""             # подсказка для поля «Название»


def ok(message: str, **details) -> CheckResult:
    return CheckResult(OK, message, "", details)


def warn(message: str, action: str = "", **details) -> CheckResult:
    return CheckResult(WARN, message, action, details)


def err(message: str, action: str = "", **details) -> CheckResult:
    return CheckResult(ERROR, message, action, details)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _parse_url_map() -> dict[str, str]:
    """Только для автотестов: PLATFORMA_URL_MAP="api.telegram.org=http://127.0.0.1:9101,..." подменяет адреса сервисов."""
    import os
    result = {}
    for item in (os.environ.get("PLATFORMA_URL_MAP") or "").split(","):
        if "=" in item:
            host, _, target = item.strip().partition("=")
            result[host.strip()] = target.strip().rstrip("/")
    return result


_URL_MAP = _parse_url_map()


def _apply_url_map(url: str) -> str:
    if not _URL_MAP:
        return url
    m = re.match(r"^https?://([^/:]+)(:\d+)?(/.*)?$", url)
    if m and m.group(1) in _URL_MAP:
        return _URL_MAP[m.group(1)] + (m.group(3) or "")
    return url


async def http(method: str, url: str, *, verify: bool = True, timeout: float = 20.0,
               proxy: str | None = None, headers: dict | None = None, **kwargs) -> httpx.Response:
    url = _apply_url_map(url)
    hdrs = {"User-Agent": "Platforma/1.0"}
    if headers:
        hdrs.update(headers)
    t = httpx.Timeout(timeout, connect=min(10.0, timeout))
    async with httpx.AsyncClient(verify=verify, timeout=t, proxy=proxy or None,
                                 follow_redirects=False) as client:
        return await client.request(method, url, headers=hdrs, **kwargs)


def _chain(exc: BaseException):
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


_REFUSED = {errno.ECONNREFUSED, 10061}
_TIMEDOUT = {errno.ETIMEDOUT, 10060}
_UNREACH = {errno.EHOSTUNREACH, errno.ENETUNREACH, 10065, 10051}


def explain_exception(exc: BaseException, service: str = "сервис", *, url: str = "",
                      can_skip_cert: bool = False, blocked_hint: str = "") -> tuple[str, str]:
    """Превращает исключение сети в (что случилось, что делать) по-русски."""
    chain = list(_chain(exc))

    # Кириллица и прочее в заголовках (токен с русскими буквами): ascii codec can't encode ...
    for e in chain:
        if isinstance(e, UnicodeEncodeError):
            bad = _bad_chars(str(getattr(e, "object", "")))
            return (
                "В ключе, токене или адресе есть символы, которых там быть не должно"
                + (f" ({bad})" if bad else "") + ".",
                "Откройте подключение и вставьте значение заново: копируйте только сам ключ, без подписи и кавычек, "
                "при английской раскладке.",
            )

    if any(isinstance(e, httpx.UnsupportedProtocol) for e in chain):
        return ("Адрес указан неверно: не хватает http:// или https:// в начале.",
                "Впишите адрес целиком, например https://адрес-сервера.")
    if any(isinstance(e, httpx.InvalidURL) for e in chain):
        return ("Адрес записан с ошибкой (лишние символы или пробелы).",
                "Проверьте адрес: он не должен содержать пробелов и русских букв, начинается с https://.")

    for e in chain:
        if isinstance(e, ssl.SSLCertVerificationError):
            code = getattr(e, "verify_code", None)
            text = (getattr(e, "verify_message", "") or str(e)).lower()
            skip = (" Если это ваш сервер по IP-адресу с самоподписанным сертификатом — снимите в подключении "
                    "галочку «Проверять сертификат».") if can_skip_cert else ""
            if code in (18, 19) or "self-signed" in text or "self signed" in text:
                return ("Сервер использует самоподписанный сертификат https, ему нельзя доверять автоматически.",
                        "Установите нормальный сертификат (например, Let's Encrypt)." + skip)
            if code == 10 or "expired" in text:
                return ("Срок действия сертификата https на сервере истёк.",
                        "Перевыпустите сертификат на сервере. Также проверьте дату и время на этом компьютере." + skip)
            if code in (62, 64) or "hostname" in text or "ip address mismatch" in text:
                return ("Сертификат https выдан на другое имя, чем указано в адресе (например, обращаетесь по IP, "
                        "а сертификат на домен).",
                        "Обращайтесь по тому имени, на которое выпущен сертификат." + skip)
            if code == 20 or "unable to get local issuer" in text:
                return ("Сертификат сервера выдан неизвестным центром сертификации — компьютер ему не доверяет.",
                        "Установите на сервер полную цепочку сертификатов или добавьте корневой сертификат в Windows." + skip)
            return ("Сертификат https сервера не прошёл проверку.",
                    "Проверьте сертификат на стороне сервиса и время на этом компьютере." + skip)
    for e in chain:
        if isinstance(e, ssl.SSLError):
            return ("Не удалось установить защищённое соединение (https).",
                    "Проверьте, что адрес начинается с правильной схемы: https:// для защищённого порта и http:// для обычного.")

    for e in chain:
        if isinstance(e, socket.gaierror):
            return (f"Не удаётся найти сервер по этому адресу ({service}): имя не распознаётся.",
                    "Проверьте, нет ли опечатки в адресе. Если адрес правильный — на сервере не работает DNS или нет интернета.")
    for e in chain:
        if isinstance(e, OSError):
            code = getattr(e, "winerror", None) or e.errno
            if code in _REFUSED:
                return (f"Сервер ({service}) отклонил соединение: по этому адресу и порту никто не отвечает.",
                        "Проверьте порт в адресе и что нужная программа на том сервере запущена. Проверьте файрвол.")
            if code in _TIMEDOUT:
                return (f"Сервер ({service}) не отвечает: соединение не удалось за отведённое время.",
                        "Проверьте адрес и порт, файрвол и связь между серверами." + (" " + blocked_hint if blocked_hint else ""))
            if code in _UNREACH:
                return (f"До сервера ({service}) нет маршрута: сеть недоступна.",
                        "Проверьте сетевое подключение этого компьютера и настройки шлюза/файрвола.")

    if any(isinstance(e, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout, httpx.WriteTimeout)) for e in chain):
        return (f"Сервер ({service}) не ответил вовремя.",
                "Проверьте адрес и порт, файрвол и загрузку самого сервера. Попробуйте ещё раз через минуту."
                + (" " + blocked_hint if blocked_hint else ""))
    if any(isinstance(e, httpx.ProxyError) for e in chain):
        return ("Не удалось подключиться через прокси-сервер.",
                "Проверьте адрес прокси и логин/пароль в нём: в подключении (поле «Прокси», если оно есть) или в настройках прокси самого сервера. "
                "Если прокси на сервере не нужен — отключите его в параметрах Windows («Прокси-сервер»).")
    if any(isinstance(e, httpx.RemoteProtocolError) for e in chain):
        return (f"Сервер ({service}) оборвал соединение или ответил не по правилам.",
                "Частая причина — https-адрес указан на порт, где работает обычный http (или наоборот). Проверьте схему и порт в адресе.")
    if any(isinstance(e, httpx.ConnectError) for e in chain):
        return (f"Не удалось соединиться с сервером ({service}).",
                "Проверьте адрес, порт, файрвол и интернет на сервере." + (" " + blocked_hint if blocked_hint else ""))
    if any(isinstance(e, httpx.TransportError) for e in chain):
        return (f"Сетевая ошибка при обращении к сервису ({service}).",
                "Повторите проверку. Если повторяется — скачайте отчёт на экране «Тестирование» и передайте разработчику.")
    return (f"Неожиданная ошибка при проверке ({type(exc).__name__}).",
            "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


# ---------------------------------------------------------------------------
# Очистка введённых значений
# ---------------------------------------------------------------------------

_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_ASCII_TOKEN_OK = re.compile(r"^[\x21-\x7e]+$")  # печатные ASCII без пробелов


def _bad_chars(text: str) -> str:
    seen: list[str] = []
    for ch in text:
        if ord(ch) > 126 and ch not in seen:
            seen.append(ch)
        if len(seen) >= 4:
            break
    return ", ".join(f"«{c}»" for c in seen)


def clean_value(f: Field, raw: str) -> tuple[str, list[str], str]:
    """Возвращает (очищенное значение, замечания, ошибка). Ошибка — человеческая, для показа под полем."""
    notes: list[str] = []
    value = unicodedata.normalize("NFC", raw or "").translate(_INVISIBLE)
    value = value.replace(" ", " ")
    original = value

    if f.type == "password" and not f.ascii_only:
        # пароли могут содержать любые символы, в том числе русские и пробелы — их не трогаем
        value = value.replace("\r", "").replace("\n", "")
        if value != value.strip() and value.strip():
            notes.append(f"В поле «{f.label}» есть пробел в начале или в конце. Мы его оставили — "
                         f"если он попал случайно, введите пароль заново без пробела.")
        return value, notes, ""

    if f.ascii_only or f.secret or f.type in ("url", "number"):
        value = re.sub(r"\s+", "", value) if (f.ascii_only or f.type in ("number",)) else value.strip()
    else:
        value = value.strip()
    if value != original.strip() and f.ascii_only and original.strip():
        notes.append(f"Из поля «{f.label}» убраны лишние пробелы и переводы строк — в ключах и токенах их не бывает.")

    if f.ascii_only:
        # снимаем случайные кавычки по краям
        value = value.strip("\"'`«»“”")
        if value and not _ASCII_TOKEN_OK.match(value):
            bad = _bad_chars(value)
            return value, notes, (
                f"В поле «{f.label}» есть символы, которых не бывает в ключах и токенах: {bad or 'посторонние знаки'}. "
                "Чаще всего это русская раскладка при копировании или скопирован лишний текст. "
                "Скопируйте значение заново целиком — только сам ключ, без подписи и кавычек."
            )
    return value, notes, ""
