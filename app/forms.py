"""Разбор и проверка форм, построенных из списка Field (подключения и настройки).

Правила:
  * секрет: пустое поле при редактировании = «не менять» (старое значение остаётся);
    удалить необязательный секрет можно галочкой «Удалить сохранённое значение»;
  * пробелы и переводы строк в токенах вычищаются автоматически, о чём пользователю сообщается;
  * русские буквы в токене — понятная ошибка у поля, а не «ascii codec can't encode».
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .connectors.base import Field, clean_value

FIELD_PREFIX = "f_"
CLEAR_PREFIX = "clear_"


@dataclass
class FormResult:
    values: dict = field(default_factory=dict)   # итоговые значения (секреты — открытым текстом, шифрует store)
    errors: dict = field(default_factory=dict)   # имя поля -> сообщение
    notes: list = field(default_factory=list)    # замечания вроде «убрали пробелы»
    secret_notes: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _visible(fields: Iterable[Field]) -> list[Field]:
    return [f for f in fields if not f.hidden and f.type != "readonly"]


def process_form(fields: list[Field], form, existing: dict, *,
                 validate: Optional[Callable[[dict], dict]] = None,
                 normalize: Optional[Callable[[dict], dict]] = None) -> FormResult:
    """form — объект с методом .get(имя) (Starlette FormData подходит)."""
    res = FormResult()
    values = dict(existing)  # скрытые и неизменённые поля переносим как есть

    for f in _visible(fields):
        raw = form.get(FIELD_PREFIX + f.name)
        raw = "" if raw is None else str(raw)

        if f.type == "bool":
            values[f.name] = bool(form.get(FIELD_PREFIX + f.name))
            continue

        if f.secret:
            has_old = bool(existing.get(f.name))
            wants_clear = bool(form.get(CLEAR_PREFIX + f.name)) and not f.required
            if raw == "":
                if wants_clear:
                    values[f.name] = ""
                elif not has_old and f.required:
                    res.errors[f.name] = f"Поле «{f.label}» обязательно: впишите значение."
                else:
                    values[f.name] = existing.get(f.name, "")   # пусто = не менять
                continue
            value, notes, error = clean_value(f, raw)
            res.notes.extend(notes)
            if error:
                res.errors[f.name] = error + " Старое сохранённое значение не изменено."
                values[f.name] = existing.get(f.name, "")
                continue
            values[f.name] = value
            continue

        value, notes, error = clean_value(f, raw)
        res.notes.extend(notes)
        if error:
            res.errors[f.name] = error
            values[f.name] = value
            continue

        if f.type == "number" and value != "":
            try:
                num = float(value.replace(",", "."))
            except ValueError:
                res.errors[f.name] = f"В поле «{f.label}» нужно число, например 50."
                values[f.name] = value
                continue
            if f.min_value is not None and num < f.min_value:
                res.errors[f.name] = f"Значение в поле «{f.label}» должно быть не меньше {f.min_value:g}."
                values[f.name] = value
                continue
            value = f"{num:g}"

        if f.type == "select" and value and value not in [o[0] for o in f.options]:
            res.errors[f.name] = f"Выберите вариант из списка в поле «{f.label}»."

        if f.type == "url" and value and not value.lower().startswith(("http://", "https://")):
            res.errors[f.name] = (f"Адрес в поле «{f.label}» должен начинаться с http:// или https://. "
                                  f"Вы ввели: {value[:40]}")

        if f.required and value == "":
            res.errors[f.name] = f"Поле «{f.label}» обязательно: впишите значение."
        values[f.name] = value

    if normalize is not None and not res.errors:
        labels = {f.name: f.label for f in fields}
        fixed = normalize(dict(values))
        for name, new_value in fixed.items():
            if new_value != values.get(name) and values.get(name):
                res.notes.append(f"В поле «{labels.get(name, name)}» убрано лишнее из адреса — он приведён к нужному виду.")
        values = fixed
    res.values = values
    if validate is not None and not res.errors:
        for name, message in validate(values).items():
            res.errors.setdefault(name, message)
    return res
