"""Свод по деньгам: общая точка входа для карточки «Финансы» на обзоре
и для инструмента, которым пользуется ядро Claude при ответах сотрудникам."""
from __future__ import annotations

from typing import Optional

from . import store
from .connectors import one_c_data


def primary_onec_connection() -> Optional[dict]:
    """Включённая база 1С. Пока баз обычно одна; если их несколько —
    берём первую созданную (по id), а не первую по имени: имя может быть каким угодно
    («тестовая», «резервная»...) и не должно решать, откуда брать деньги на обзор."""
    conns = [c for c in store.list_connections() if c["type"] == "onec" and c["enabled"]]
    return min(conns, key=lambda c: c["id"]) if conns else None


async def summary() -> Optional[one_c_data.DataResult]:
    conn = primary_onec_connection()
    if conn is None:
        return None
    cfg, _ = store.plain_config(conn)
    return await one_c_data.finance_summary(conn["id"], cfg)
