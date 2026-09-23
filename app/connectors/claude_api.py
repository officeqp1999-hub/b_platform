"""Ядро на Claude API: ключ, модель и месячный лимит расходов."""
from __future__ import annotations

import time

from .base import CheckResult, ConnectorType, Ctx, Field, err, explain_exception, http, ok, warn

DEFAULT_BASE = "https://api.anthropic.com"
REGION_HINT = ("Из России api.anthropic.com обычно закрыт. Укажите прокси в поле «Прокси» "
               "или разместите сервер за пределами РФ.")


def classify_error(code: int, body: dict) -> str:
    """Общая классификация ошибок Claude API — используется и здесь (проверка связи),
    и в app/claude_core.py (чат), чтобы коды/типы ошибок Anthropic не жили в двух местах
    и не расходились. Возвращает: auth | region | permission | rate_limit | credit | overloaded | unexpected."""
    err_obj = (body.get("error") or {}) if isinstance(body, dict) else {}
    etype = err_obj.get("type", "")
    emsg = (err_obj.get("message", "") or "").lower()
    if code == 401 or etype == "authentication_error":
        return "auth"
    if code == 403 or etype == "permission_error":
        if "not allowed" in emsg or "region" in emsg or "country" in emsg or not emsg:
            return "region"
        return "permission"
    if code == 429 or etype == "rate_limit_error":
        return "rate_limit"
    if "credit balance" in emsg:
        return "credit"
    if code in (500, 502, 503, 529):
        return "overloaded"
    return "unexpected"


def _normalize(cfg: dict) -> dict:
    if cfg.get("base_url"):
        cfg["base_url"] = cfg["base_url"].rstrip("/")
    return cfg


def _validate(cfg: dict) -> dict:
    errors = {}
    lim = cfg.get("monthly_limit_usd", "")
    if lim != "":
        try:
            if float(str(lim).replace(",", ".")) <= 0:
                errors["monthly_limit_usd"] = "Лимит должен быть больше нуля."
        except ValueError:
            errors["monthly_limit_usd"] = "Впишите лимит числом, например 50."
    key = cfg.get("api_key", "")
    if key and not key.startswith("sk-"):
        errors["api_key"] = "Ключ Claude начинается с «sk-ant-». Похоже, вставлено что-то другое — скопируйте ключ из консоли Anthropic."
    return errors


async def check(cfg: dict, ctx: Ctx) -> CheckResult:
    key = cfg.get("api_key", "")
    if not key:
        return err("Не указан ключ Claude API.", "Откройте подключение и вставьте ключ из console.anthropic.com.")
    base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
    proxy = cfg.get("proxy", "") or None
    model = cfg.get("model", "")
    started = time.monotonic()
    try:
        resp = await http("GET", f"{base}/v1/models", params={"limit": 1000}, proxy=proxy, timeout=20,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "Claude API", blocked_hint=REGION_HINT)
        return err(msg, action)
    elapsed = int((time.monotonic() - started) * 1000)
    code = resp.status_code
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if code == 200:
        ids = [m.get("id", "") for m in body.get("data", [])] if isinstance(body, dict) else []
        lim = cfg.get("monthly_limit_usd", "")
        limtxt = f" Месячный лимит расходов: ${lim}." if lim else ""
        if ids and model and model not in ids:
            shown = ", ".join(ids[:6])
            return err(f"Ключ Claude работает, но модели «{model}» у него нет.",
                       f"Впишите в поле «Модель» одну из доступных: {shown}.", models=len(ids))
        res = ok(f"Ключ принят, модель «{model or 'по умолчанию'}» доступна.{limtxt}" + (" Работает через прокси." if proxy else ""),
                 models=len(ids))
        res.elapsed_ms = elapsed
        return res
    kind = classify_error(code, body)
    if kind == "auth":
        return err("Ключ API неверный или удалён.",
                   "В console.anthropic.com → API Keys создайте новый ключ и вставьте его здесь целиком (начинается с sk-ant-).")
    if kind == "region":
        return err("Claude не пускает запросы с этого сервера (ограничение по стране/адресу).", REGION_HINT)
    if kind == "permission":
        return err("У ключа нет прав на этот запрос.", "Создайте ключ в console.anthropic.com в нужной рабочей области (Workspace).")
    if kind == "rate_limit":
        return warn("Claude просит подождать: превышен лимит запросов.", "Это временно. Повторите через минуту; если постоянно — поднимите лимиты в консоли Anthropic.")
    if kind == "credit":
        return err("На счёте Anthropic закончились деньги.", "Пополните баланс в console.anthropic.com → Plans & Billing.")
    if kind == "overloaded":
        return warn("Claude сейчас перегружен или недоступен (сбой на стороне Anthropic).", "Повторите проверку через несколько минут. Статус: status.anthropic.com.")
    if code == 404:
        return err("По этому адресу нет Claude API.", "Уберите поле «Адрес API» (дополнительно) или впишите https://api.anthropic.com.")
    return err(f"Claude API ответил неожиданным кодом ({code}).", "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


TYPE = ConnectorType(
    key="claude",
    title="Ядро Claude",
    group="Ядро ИИ",
    icon="Cl",
    single=True,
    description="«Мозг» платформы: Claude отвечает на вопросы сотрудников по данным из всех систем. Подключение может быть только одно.",
    name_hint="Например: Ядро Claude",
    validate=_validate,
    normalize=_normalize,
    howto=["Оплата идёт отдельно, по факту использования. Месячный лимит расходов вы задаёте здесь и (обязательно) в консоли Anthropic."],
    fields=[
        Field("api_key", "Ключ API", "password", required=True, secret=True, ascii_only=True,
              placeholder="sk-ant-...",
              help="Секретный ключ из консоли Anthropic. Начинается с sk-ant-.",
              where=["Откройте console.anthropic.com и войдите (или зарегистрируйтесь).",
                     "Пополните баланс: Plans & Billing.",
                     "API Keys → Create Key. Назовите ключ «Платформа». Скопируйте ключ сразу — потом он не показывается."]),
        Field("model", "Модель", required=True, default="claude-sonnet-5",
              suggestions=["claude-sonnet-5", "claude-sonnet-4-5", "claude-haiku-4-5"],
              help="Название модели. Проверка убедится, что она доступна вашему ключу; если нет — покажет доступные."),
        Field("monthly_limit_usd", "Месячный лимит расходов, $", "number", required=True, default="50",
              help="Панель остановит запросы к Claude, когда расходы за месяц дойдут до этой суммы (учёт — на третьем этапе)."),
        Field("proxy", "Прокси (если Claude закрыт)", "password", secret=True, advanced=True,
              placeholder="http://логин:пароль@адрес:порт",
              help="Необязательно. Нужен, если с этого сервера не открывается api.anthropic.com."),
        Field("base_url", "Адрес API", "url", advanced=True, placeholder="https://api.anthropic.com",
              help="Менять не нужно. Заполняется только если вы работаете через собственный шлюз."),
    ],
    check=check,
)
