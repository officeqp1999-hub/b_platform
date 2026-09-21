"""Описание полей раздела «Настройки» (форма строится так же, как форма подключения)."""
from __future__ import annotations

import re

from .connectors.base import Field

SECTIONS: list[tuple[str, str, list[Field]]] = [
    ("Панель", "Основные параметры этого приложения.", [
        Field("company_name", "Название компании", default="Моя компания",
              help="Показывается в заголовке панели."),
        Field("public_url", "Внешний адрес панели", "url", placeholder="https://panel.example.ru",
              help="Адрес, по которому панель открывается снаружи (домен, https). Нужен для проверки доступа снаружи и для входа через Google. "
                   "Если панель работает только внутри сети — оставьте пустым."),
        Field("session_hours", "Сколько часов держать вход", "number", default="12", min_value=1,
              help="Через сколько часов бездействия пользователя попросят войти заново."),
    ]),
    ("Журнал и резервные копии", "Старые записи и копии чистятся автоматически.", [
        Field("log_retention_days", "Хранить журнал, дней", "number", default="90", min_value=7,
              help="Записи журнала старше этого срока удаляются сами."),
        Field("auto_backup", "Делать резервную копию автоматически раз в сутки", "bool", default="1",
              help="Панель сама сохраняет копию базы и ключа шифрования, пока работает. Рекомендуется оставить включённым."),
        Field("backup_dir", "Папка для резервных копий", placeholder="D:\\Backups\\Platforma",
              help="Пустое поле — папка data\\backups рядом с программой. Лучше указать другой диск или сетевую папку: "
                   "копия на том же диске не спасёт при его поломке."),
        Field("backup_keep", "Сколько копий хранить", "number", default="14", min_value=1,
              help="Более старые копии удаляются автоматически при создании новой."),
    ]),
    ("Google (вход через аккаунт)", "Один раз для всей компании. Нужно для подключений Gmail и Google Диска.", [
        Field("google_client_id", "Client ID", ascii_only=True,
              help="Из Google Cloud → API и сервисы → Учётные данные → Идентификатор клиента OAuth (тип «Веб-приложение»).",
              where=["Откройте console.cloud.google.com, создайте проект (или выберите существующий).",
                     "«API и сервисы» → «Библиотека»: включите Gmail API и Google Drive API.",
                     "«Экран согласия OAuth»: тип «Внешний» (или «Внутренний» для Google Workspace), добавьте нужные аккаунты как тестовых пользователей, "
                     "затем нажмите «Опубликовать приложение» — иначе доступ будет слетать каждые 7 дней.",
                     "«Учётные данные» → «Создать учётные данные» → «Идентификатор клиента OAuth» → тип «Веб-приложение».",
                     "В «Разрешённые URI перенаправления» добавьте адрес, который показан на странице подключения Google.",
                     "Скопируйте Client ID и Client Secret сюда."]),
        Field("google_client_secret", "Client Secret", "password", secret=True, ascii_only=True,
              help="Секрет того же OAuth-клиента. Хранится в зашифрованном виде."),
    ]),
]

ALL_FIELDS: list[Field] = [f for _, _, fields in SECTIONS for f in fields]
SECRET_KEYS = {f.name for f in ALL_FIELDS if f.secret}
DEFAULTS = {f.name: f.default for f in ALL_FIELDS}


def validate(values: dict) -> dict:
    errors = {}
    url = (values.get("public_url") or "").strip()
    if url:
        if not re.match(r"^https?://[^\s/]+", url):
            errors["public_url"] = "Адрес должен начинаться с http:// или https:// и не содержать пробелов, например https://panel.example.ru"
    for key, lo, hi, label in (("session_hours", 1, 168, "часов входа"),):
        try:
            v = float(values.get(key) or 0)
            if not lo <= v <= hi:
                errors[key] = f"Допустимо от {lo} до {hi}."
        except ValueError:
            errors[key] = "Нужно число."
    return errors
