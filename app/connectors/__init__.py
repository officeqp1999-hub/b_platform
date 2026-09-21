"""Реестр типов подключений. Чтобы добавить новый тип — создайте файл с ConnectorType и допишите его сюда."""
from __future__ import annotations

from . import bitrix24, bots, claude_api, google, marketplaces, one_c
from .base import ConnectorType

_ALL = [
    one_c.TYPE,
    bitrix24.TYPE,
    marketplaces.WB,
    marketplaces.OZON,
    marketplaces.YM,
    google.TYPE,
    bots.MAX,
    bots.TELEGRAM,
    claude_api.TYPE,
]

TYPES: dict[str, ConnectorType] = {t.key: t for t in _ALL}
GROUP_ORDER = ["Учёт и CRM", "Маркетплейсы", "Почта и файлы", "Боты", "Ядро ИИ"]


def get_type(key: str) -> ConnectorType | None:
    return TYPES.get(key)


def single_types() -> list[str]:
    return [t.key for t in _ALL if t.single]


def grouped() -> list[tuple[str, list[ConnectorType]]]:
    result = []
    for g in GROUP_ORDER:
        items = [t for t in _ALL if t.group == g]
        if items:
            result.append((g, items))
    return result
