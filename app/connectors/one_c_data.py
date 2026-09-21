"""1С: прямые бизнес-запросы (не проверка связи, а сами цифры).

Отличие от one_c.py: там — «жива ли 1С и опубликован ли OData» для экрана
«Тестирование», здесь — конкретные сводные вопросы (сейчас: карточка
«Финансы» на обзоре) через HTTP-сервис расширения, с кэшем и лимитом
одновременных запросов к одной базе — см. CLAUDE.md, раздел «Решение по 1С».

Через голый OData сводные цифры (выручка, деньги на счетах, остатки) не
получить: это справочный интерфейс без агрегатов. Считает их расширение
внутри 1С и отдаёт уже готовым числом — контракт описан в
ТЗ-1С-HTTP-сервисы.md.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from .base import explain_exception, http
from .one_c import basic_header, normalize_base_url

# Кэш и лимит — из «Решения по 1С» в CLAUDE.md. Переменные окружения — только
# для автотестов (нужно быстро состариться кэш и проверить лимит), в бою не выставляются.
CACHE_TTL_SECONDS = float(os.environ.get("PLATFORMA_ONEC_CACHE_TTL", "45"))
MAX_CONCURRENT_REQUESTS = max(1, int(os.environ.get("PLATFORMA_ONEC_MAX_CONCURRENT", "4")))


@dataclass
class DataResult:
    status: str                            # ok | warn | error
    message: str
    action: str = ""                       # «Что делать», как в CheckResult
    data: dict = field(default_factory=dict)
    elapsed_ms: int = 0
    cached: bool = False


def _ok(message: str, data: dict) -> DataResult:
    return DataResult("ok", message, "", data)


def _warn(message: str, action: str, data: dict | None = None) -> DataResult:
    return DataResult("warn", message, action, data or {})


def _err(message: str, action: str = "") -> DataResult:
    return DataResult("error", message, action, {})


# Кэш и служебные структуры — в памяти процесса панели, между базами и видами
# отчёта не пересекаются (ключ — id подключения + название отчёта).
_cache: dict[tuple[int, str], tuple[float, DataResult]] = {}
_cache_locks: dict[tuple[int, str], asyncio.Lock] = {}
_semaphores: dict[int, asyncio.Semaphore] = {}

# Счётчик правок подключения. invalidate() увеличивает его, чтобы результат уже
# начатого запроса (со СТАРЫМИ логином/паролем/путём) не лёг в кэш поверх правки,
# если ответ от 1С придёт уже после того, как сисадмин сохранил новые настройки.
_generation: dict[int, int] = {}


def _cache_lock(key: tuple[int, str]) -> asyncio.Lock:
    lock = _cache_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _cache_locks[key] = lock
    return lock


def _semaphore(conn_id: int) -> asyncio.Semaphore:
    sem = _semaphores.get(conn_id)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        _semaphores[conn_id] = sem
    return sem


def invalidate(conn_id: int) -> None:
    """Сбросить кэш подключения — вызывается при изменении, отключении или удалении подключения,
    чтобы после правки настроек не показывались старые цифры до истечения TTL."""
    _generation[conn_id] = _generation.get(conn_id, 0) + 1
    for key in [k for k in _cache if k[0] == conn_id]:
        _cache.pop(key, None)


async def finance_summary(conn_id: int, cfg: dict) -> DataResult:
    """Свод для карточки «Финансы»: выручка за месяц, деньги на счетах, остатки на складах.

    Кэшируется на CACHE_TTL_SECONDS (30–60 с по решению из CLAUDE.md), чтобы
    открытие обзора несколькими сотрудниками подряд не било в 1С каждый раз.
    Пока кто-то один обновляет кэш — остальные ждут его результат, а не
    выполняют тот же запрос параллельно.
    """
    key = (conn_id, "summary")
    fresh = _fresh_cached(key)
    if fresh is not None:
        return fresh
    async with _cache_lock(key):
        fresh = _fresh_cached(key)  # пока ждали лок, кто-то мог уже обновить кэш
        if fresh is not None:
            return fresh
        gen = _generation.get(conn_id, 0)
        result = await _fetch_summary(conn_id, cfg)
        # пока ждали ответ от 1С, настройки могли поменяться (invalidate() увеличил gen) —
        # тогда результат посчитан по уже неактуальным логину/паролю/пути, в кэш его не кладём
        if _generation.get(conn_id, 0) == gen:
            _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, result)
        return result


def _fresh_cached(key: tuple[int, str]) -> DataResult | None:
    entry = _cache.get(key)
    if entry is None or entry[0] <= time.monotonic():
        return None
    result = entry[1]
    # копия, а не тот же dict: кто-то мог бы случайно поправить .data у возвращённого
    # результата и незаметно испортить кэш для всех остальных читателей
    return DataResult(result.status, result.message, result.action, dict(result.data), result.elapsed_ms, cached=True)


async def _fetch_summary(conn_id: int, cfg: dict) -> DataResult:
    base = normalize_base_url(cfg.get("base_url", ""))
    path = (cfg.get("http_service_path") or "").strip().strip("/")
    if not base:
        return _err("Не указан адрес базы 1С.", "Откройте подключение «1С» и впишите адрес базы.")
    if not path:
        return _warn(
            "Свод по деньгам считает расширение 1С через HTTP-сервис — путь к нему ещё не указан в подключении.",
            "Когда программист 1С установит расширение с HTTP-сервисами (см. ТЗ-1С-HTTP-сервисы.md), впишите путь "
            "к нему в подключении «1С», поле «Путь к HTTP-сервису расширения».")

    user = cfg.get("username", "")
    password = cfg.get("password", "")
    verify = bool(cfg.get("verify_ssl", True))
    url = f"{base}/{path}/summary"
    started = time.monotonic()
    try:
        async with _semaphore(conn_id):
            resp = await http("GET", url, verify=verify, timeout=30,
                              headers={**basic_header(user, password), "Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "1С", url=base, can_skip_cert=verify)
        return _err(msg, action)
    result = _parse_summary(resp)
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def _parse_summary(resp) -> DataResult:
    code = resp.status_code
    if code in (401, 403):
        return _err("1С отвечает, но не пускает к своду по деньгам — не хватает прав у пользователя.",
                    "Дайте пользователю 1С, указанному в подключении, права на HTTP-сервис расширения.")
    if code == 404:
        return _warn("Расширение 1С ещё не считает свод по деньгам (метод «/summary» не найден).",
                     "Это нормально, пока программист 1С не доустановил расширение (см. ТЗ-1С-HTTP-сервисы.md). "
                     "Само подключение к 1С при этом может работать.")
    if code >= 500:
        return _err("1С вернула ошибку при расчёте свода по деньгам.",
                    "Посмотрите журнал регистрации 1С за это время. Если не ясно — скачайте отчёт на экране "
                    "«Тестирование» и передайте программисту 1С.")
    if code != 200:
        return _err(f"1С ответила неожиданным кодом ({code}) на запрос свода по деньгам.",
                    "Скачайте отчёт на экране «Тестирование» и передайте программисту 1С.")
    try:
        payload = resp.json()
    except ValueError:
        return _err("1С ответила не в формате JSON на запрос свода по деньгам.",
                    "Проверьте расширение: метод «/summary» должен возвращать JSON (Content-Type application/json).")
    if not isinstance(payload, dict):
        return _err("1С ответила неожиданными данными на запрос свода по деньгам.",
                    "Скачайте отчёт на экране «Тестирование» и передайте программисту 1С.")
    data = {
        "revenue_month": _num(payload.get("revenue_month")),
        "cash_total": _num(payload.get("cash_total")),
        "stock_value": _num(payload.get("stock_value")),
    }
    if all(v is None for v in data.values()):
        return _warn("1С ответила, но свод по деньгам пуст — ни одно значение не посчитано.",
                     "Проверьте расширение 1С: метод «/summary» должен возвращать хотя бы одно из чисел "
                     "(revenue_month, cash_total, stock_value).", data=data)
    return _ok("Свод по деньгам получен из 1С.", data)


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
