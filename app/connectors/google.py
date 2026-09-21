"""Google (Gmail + Google Диск): одно подключение на аккаунт, вход через OAuth прямо из панели."""
from __future__ import annotations

import time
from urllib.parse import urlencode

from .. import config
from .base import CheckResult, ConnectorType, Ctx, Field, err, explain_exception, http, ok, warn

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/userinfo.email",
]

CONSOLE_HINT = ("Настройка Google Cloud (один раз на всю компанию) описана в ИНСТРУКЦИИ, раздел «Google». "
                "Проверьте Client ID и Client Secret в разделе «Настройки».")


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return config.GOOGLE_AUTH_URL + "?" + urlencode(params)


def explain_token_error(resp) -> tuple[str, str]:
    """Ответ Google на обмен кода / обновление токена → (что случилось, что делать)."""
    try:
        data = resp.json()
    except ValueError:
        data = {}
    code = data.get("error", "") if isinstance(data, dict) else ""
    desc = (data.get("error_description", "") if isinstance(data, dict) else "") or ""
    if code == "invalid_grant":
        return ("Google больше не принимает доступ этого аккаунта: он отозван, истёк или пароль аккаунта менялся.",
                "Нажмите «Подключить аккаунт Google заново» и пройдите вход ещё раз. Если это повторяется каждую неделю — "
                "приложение в Google Cloud находится в режиме «Тестирование»: переведите его в «Опубликовано» (см. ИНСТРУКЦИЮ).")
    if code == "invalid_client":
        return ("Google не узнаёт приложение: Client ID или Client Secret неверные.",
                "Откройте «Настройки» и вставьте заново Client ID и Client Secret из Google Cloud (тип клиента «Веб-приложение»).")
    if code == "redirect_uri_mismatch":
        return ("Google не принял адрес возврата: он не совпадает с тем, что записан в Google Cloud.",
                "В Google Cloud → Учётные данные → ваш OAuth-клиент → «Разрешённые URI перенаправления» добавьте адрес, показанный на странице подключения.")
    if code == "access_denied":
        return ("Доступ не был разрешён: на экране Google нажата «Отмена» или аккаунта нет в списке тестовых пользователей.",
                "Повторите вход и разрешите все запрошенные доступы. Если аккаунт не добавлен в «Тестовые пользователи» — добавьте его в Google Cloud.")
    if code == "invalid_request":
        return ("Google отклонил запрос входа как некорректный.",
                "Повторите вход. Если повторяется — скачайте отчёт на экране «Тестирование» и передайте разработчику.")
    return ("Google вернул ошибку при получении доступа" + (f" ({code})" if code else "") + ".",
            "Повторите вход. Если повторяется — скачайте отчёт на экране «Тестирование» и передайте разработчику.")


def explain_api_error(resp, service: str) -> tuple[str, str]:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    e = data.get("error", {}) if isinstance(data, dict) else {}
    message = (e.get("message", "") if isinstance(e, dict) else str(e)) or ""
    reason = ""
    if isinstance(e, dict) and e.get("errors"):
        reason = e["errors"][0].get("reason", "")
    low = message.lower()
    if resp.status_code == 403 and ("has not been used" in low or "disabled" in low or reason in ("accessNotConfigured", "SERVICE_DISABLED")):
        return (f"В Google Cloud не включён {service} API для этого проекта.",
                f"Google Cloud → «API и сервисы» → «Библиотека» → найдите «{service} API» → «Включить». Подождите 2–3 минуты и повторите.")
    if resp.status_code in (401,):
        return (f"{service}: Google не принял доступ.", "Нажмите «Подключить аккаунт Google заново».")
    if resp.status_code == 403:
        return (f"{service}: доступа не хватает или он запрещён политикой аккаунта.",
                "Подключите аккаунт заново и разрешите все доступы. Для рабочего аккаунта Google Workspace администратор домена мог запретить доступ приложениям.")
    if resp.status_code == 429:
        return (f"{service}: Google просит подождать (слишком много запросов).", "Повторите проверку через несколько минут.")
    return (f"{service}: Google ответил ошибкой ({resp.status_code}).", "Повторите проверку. Если повторяется — скачайте отчёт и передайте разработчику.")


async def refresh_access_token(client_id: str, client_secret: str, refresh_token: str):
    return await http("POST", config.GOOGLE_TOKEN_URL, timeout=20, data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token"})


async def check(cfg: dict, ctx: Ctx) -> CheckResult:
    refresh = cfg.get("refresh_token", "")
    email = cfg.get("email", "")
    if not refresh:
        return warn("Аккаунт Google ещё не подключён.", "Откройте подключение и нажмите «Подключить аккаунт Google».")
    cid = ctx.settings.get("google_client_id", "")
    csec = ctx.settings.get("google_client_secret", "")
    if not cid or not csec:
        return err("Не заполнены Client ID и Client Secret для входа через Google.",
                   "Откройте «Настройки» → блок «Google» и вставьте их из Google Cloud. " + CONSOLE_HINT)
    started = time.monotonic()
    try:
        resp = await refresh_access_token(cid, csec, refresh)
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "Google")
        return err(msg, action)
    if resp.status_code != 200:
        msg, action = explain_token_error(resp)
        return err(msg, action)
    access = resp.json().get("access_token", "")
    headers = {"Authorization": f"Bearer {access}"}
    parts, problems = [], []
    details = {"email": email}
    try:
        g = await http("GET", config.GMAIL_PROFILE_URL, headers=headers, timeout=20)
        if g.status_code == 200:
            j = g.json()
            parts.append(f"Gmail: {j.get('emailAddress', email)}, писем {j.get('messagesTotal', '?')}")
            details["gmail"] = "ok"
        else:
            problems.append(explain_api_error(g, "Gmail"))
        d = await http("GET", config.DRIVE_ABOUT_URL, headers=headers, params={"fields": "user,storageQuota"}, timeout=20)
        if d.status_code == 200:
            q = d.json().get("storageQuota", {})
            used = int(q.get("usage", 0) or 0) / (1024 ** 3)
            parts.append(f"Диск: занято {used:.1f} ГБ")
            details["drive"] = "ok"
        else:
            problems.append(explain_api_error(d, "Google Диск"))
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "Google")
        return err(msg, action)
    elapsed = int((time.monotonic() - started) * 1000)
    if problems:
        text = " ".join(p[0] for p in problems)
        action = " ".join(p[1] for p in problems)
        res = err(text, action, **details)
        if parts:
            res.status = "warn"
            res.message = "Частично работает (" + "; ".join(parts) + "). " + text
        res.elapsed_ms = elapsed
        return res
    res = ok("Аккаунт подключён. " + "; ".join(parts) + ".", **details)
    res.elapsed_ms = elapsed
    return res


TYPE = ConnectorType(
    key="google",
    title="Google (Gmail и Диск)",
    group="Почта и файлы",
    icon="G",
    description="Один Google-аккаунт: почта Gmail и Google Диск сразу. Вход выполняется кнопкой ниже — пароль от Google панель не видит и не хранит.",
    name_hint="Например: Почта director@company.ru",
    oauth="google",
    howto=[
        "Для каждого аккаунта Google — отдельное подключение. Сколько аккаунтов, столько подключений.",
        "Один раз на всю компанию в разделе «Настройки» вписываются Client ID и Client Secret из Google Cloud (пошагово — в ИНСТРУКЦИИ).",
        "Google принимает адрес возврата только с https и доменом (или http://localhost). Если панель открыта по IP — "
        "выполните вход, открыв панель на самом сервере по адресу http://localhost:ПОРТ.",
    ],
    fields=[
        Field("email", "Аккаунт Google", "readonly", help="Заполняется автоматически после входа."),
        Field("refresh_token", "Токен доступа", "password", secret=True, hidden=True),
    ],
    check=check,
)
