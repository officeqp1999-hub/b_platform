"""Ядро на Claude: диалог с сотрудниками, вызов инструментов, учёт расходов по месячному лимиту.

Один вопрос — один цикл «спросить → (если нужно) выполнить инструмент → спросить ещё раз»,
не длиннее MAX_TOOL_ROUNDS попыток. Инструменты сотруднику доступны по тем же правам, что и
остальной панели (auth.PERMISSIONS) — Claude физически не получает инструмент, на который
у спросившего нет прав, а не полагается на то, что сам откажется его звать.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from . import auth, finance, store
from .connectors import claude_api
from .connectors.base import explain_exception, http

API_VERSION = "2023-06-01"
DEFAULT_BASE = "https://api.anthropic.com"
MAX_TOOL_ROUNDS = 4          # предохранитель от зацикливания вызовов инструментов
HISTORY_MESSAGES = 10        # сколько последних сообщений диалога передаётся как контекст
MAX_TOKENS = 1500

# $ за 1 млн токенов (вход, выход). Обновлять при появлении новых моделей.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# Цена модели не из списка выше (например, вписана вручную и ещё не добавлена нами) — берём по
# тарифу Opus, самому дорогому из известных. Если считать неизвестную модель бесплатной, месячный
# лимит расходов перестаёт работать незаметно для сисадмина — лучше немного переоценить, чем никак.
UNKNOWN_MODEL_RATES = (5.00, 25.00)

SYSTEM_PROMPT = (
    "Ты — ассистент компании внутри «Центра управления» (панель вместо n8n для торговой "
    "и строительной компании). Отвечаешь сотрудникам по-русски, коротко и по делу, без "
    "канцелярита. Для точных данных компании (деньги, остатки и т. п.) используй инструменты, "
    "а не придумывай цифры. Если подходящего инструмента нет — прямо скажи, что не можешь "
    "получить эти данные (например, из-за роли спросившего), а не выдумывай ответ."
)


@dataclass
class AskResult:
    status: str            # ok | warn | error
    answer: str = ""
    action: str = ""


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict
    permission: Optional[str]                                    # None = доступен всем
    handler: Callable[[dict, dict], Awaitable[dict]]              # (аргументы, пользователь) -> результат


async def _tool_finance_summary(_args: dict, _user: dict) -> dict:
    result = await finance.summary()
    if result is None:
        return {"есть_данные": False, "сообщение": "1С ещё не подключена."}
    out = {"есть_данные": result.status != "error", "статус": result.status, "сообщение": result.message}
    out.update(result.data)
    return out


TOOLS: list[ToolDef] = [
    ToolDef(
        name="finance_summary",
        description=("Свод по деньгам компании из 1С: выручка за текущий месяц (revenue_month), деньги на счетах "
                     "и в кассах (cash_total), стоимость остатков на складах (stock_value), в рублях. Используй, "
                     "когда спрашивают про выручку, обороты, деньги на счетах, остатки на складах."),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        permission="finance.view",
        handler=_tool_finance_summary,
    ),
]


def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = PRICING.get(model, UNKNOWN_MODEL_RATES)
    return input_tokens / 1_000_000 * inp + output_tokens / 1_000_000 * out


def _friendly_api_error(code: int, body: dict) -> AskResult:
    kind = claude_api.classify_error(code, body)
    if kind == "auth":
        return AskResult("error", action="Ключ Claude API стал недействителен. Откройте подключение «Ядро Claude» и обновите ключ.")
    if kind == "region":
        return AskResult("error", action="Claude не пускает запросы с этого сервера (ограничение по стране/адресу). " + claude_api.REGION_HINT)
    if kind == "permission":
        return AskResult("error", action="У ключа нет прав на этот запрос. Проверьте подключение «Ядро Claude» на экране «Тестирование».")
    if kind == "rate_limit":
        return AskResult("warn", action="Claude сейчас перегружен запросами. Повторите вопрос через минуту.")
    if kind == "credit":
        return AskResult("error", action="На счету Anthropic закончились деньги. Пополните баланс в console.anthropic.com → Plans & Billing.")
    if kind == "overloaded":
        return AskResult("warn", action="Claude сейчас недоступен (сбой на стороне Anthropic). Повторите вопрос через несколько минут.")
    return AskResult("error", action=f"Claude API ответил неожиданным кодом ({code}). Скачайте отчёт на экране «Тестирование» и передайте разработчику.")


def _extract_text(content: list[dict]) -> str:
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()


def monthly_limit_usd(cfg: dict) -> float:
    """0 — лимит не задан (не ограничиваем)."""
    raw = cfg.get("monthly_limit_usd", "")
    try:
        return float(str(raw).replace(",", ".")) if raw not in ("", None) else 0.0
    except ValueError:
        return 0.0


async def ask(user: dict, question: str) -> AskResult:
    question = (question or "").strip()
    if not question:
        return AskResult("warn", action="Напишите вопрос.")

    conn = next((c for c in store.list_connections() if c["type"] == "claude" and c["enabled"]), None)
    if conn is None:
        return AskResult("error", action="Ядро Claude ещё не подключено. Раздел «Подключения» → «Ядро Claude».")
    cfg, _ = store.plain_config(conn)
    api_key = cfg.get("api_key", "")
    if not api_key:
        return AskResult("error", action="В подключении «Ядро Claude» не указан ключ API.")

    limit = monthly_limit_usd(cfg)
    spent = store.month_ai_spend_usd()
    if limit and spent >= limit:
        return AskResult("warn", action=f"Месячный лимит расходов на Claude (${limit:g}) исчерпан (потрачено ${spent:.2f} "
                                        f"с начала месяца). Увеличьте лимит в подключении «Ядро Claude» или дождитесь "
                                        f"следующего месяца.")

    model = cfg.get("model") or "claude-sonnet-5"
    base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
    proxy = cfg.get("proxy") or None

    tools_for_user = [t for t in TOOLS if t.permission is None or auth.can(user, t.permission)]
    api_tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools_for_user]
    tools_by_name = {t.name: t for t in tools_for_user}

    history = store.recent_chat_messages(user["id"], HISTORY_MESSAGES)
    messages: list[dict] = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": question})

    total_in = total_out = 0
    final_text = ""
    error_result: Optional[AskResult] = None

    for _round in range(MAX_TOOL_ROUNDS):
        if _round > 0 and limit and spent + _cost_usd(model, total_in, total_out) >= limit:
            # один вопрос уже потребовал нескольких обращений подряд (цепочка вызовов инструментов) —
            # проверяем лимит перед КАЖДЫМ новым обращением, а не только один раз в начале вопроса,
            # иначе один вопрос может пробить месячный лимит на несколько запросов сразу
            error_result = AskResult("warn", action=f"Вопрос потребовал нескольких обращений к Claude подряд и упёрся "
                                                     f"в месячный лимит расходов (${limit:g}). Ответ не досчитан — "
                                                     f"задайте вопрос проще или отдельными вопросами.")
            break
        body: dict = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM_PROMPT,
                      "messages": messages, "output_config": {"effort": "low"}}
        if api_tools:
            body["tools"] = api_tools
        try:
            resp = await http("POST", f"{base}/v1/messages", json=body, proxy=proxy, timeout=60,
                              headers={"x-api-key": api_key, "anthropic-version": API_VERSION,
                                      "content-type": "application/json"})
        except Exception as exc:  # noqa: BLE001
            msg, action = explain_exception(exc, "Claude API")
            error_result = AskResult("error", action=f"{msg} {action}".strip())
            break

        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            error_result = AskResult("error", action=f"Claude API ответил не в ожидаемом формате (код {resp.status_code}). "
                                                      "Скачайте отчёт на экране «Тестирование» и передайте разработчику.")
            break
        usage = data.get("usage") or {}
        total_in += int(usage.get("input_tokens") or 0)
        total_out += int(usage.get("output_tokens") or 0)

        if resp.status_code != 200:
            error_result = _friendly_api_error(resp.status_code, data)
            break

        stop_reason = data.get("stop_reason")
        content = data.get("content") or []
        if stop_reason == "refusal":
            error_result = AskResult("warn", action="Claude отказался отвечать на этот вопрос. Переформулируйте его.")
            break
        if stop_reason != "tool_use":
            final_text = _extract_text(content)
            if stop_reason == "max_tokens" and final_text:
                final_text += "\n\n(Ответ обрезан — задайте вопрос точнее, чтобы получить более короткий ответ.)"
            elif not final_text:
                error_result = AskResult("warn", action="Claude не дал текстового ответа. Попробуйте переформулировать вопрос.")
            break

        # stop_reason == tool_use: выполняем инструменты и продолжаем цикл с их результатами
        messages.append({"role": "assistant", "content": content})
        tool_results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            tool = tools_by_name.get(block.get("name", ""))
            if tool is None:
                tool_results.append({"type": "tool_result", "tool_use_id": block.get("id"),
                                     "content": "Инструмент недоступен.", "is_error": True})
                continue
            try:
                result = await tool.handler(block.get("input") or {}, user)
                tool_results.append({"type": "tool_result", "tool_use_id": block.get("id"),
                                     "content": json.dumps(result, ensure_ascii=False)})
            except Exception as exc:  # noqa: BLE001
                tool_results.append({"type": "tool_result", "tool_use_id": block.get("id"),
                                     "content": f"Ошибка при выполнении инструмента: {exc}", "is_error": True})
        messages.append({"role": "user", "content": tool_results})
    else:
        error_result = AskResult("warn", action="Claude слишком долго вызывал инструменты и не дал ответа. "
                                                "Попробуйте задать вопрос иначе.")

    if total_in or total_out:
        store.log_ai_usage(user["id"], model, total_in, total_out, _cost_usd(model, total_in, total_out))

    if error_result is not None:
        return error_result
    store.save_chat_message(user["id"], "user", question)
    store.save_chat_message(user["id"], "assistant", final_text)
    return AskResult("ok", answer=final_text)


def month_spend_usd() -> float:
    return store.month_ai_spend_usd()
