"""Датированные названия экспортов в Excel."""
from __future__ import annotations

from datetime import date
from pathlib import Path


def dated_excel_filename(filename: str | Path, *, on_date: date | None = None) -> str:
    """Добавляет дату экспорта к имени, даже если пользователь ввёл имя вручную.

    Дата в формате ГГГГ-ММ-ДД гарантирует сортировку файлов по имени.
    Повторный вызов с той же датой не дублирует суффикс.
    """
    target = Path(filename)
    stamp = (on_date or date.today()).isoformat()
    stem = target.stem if target.suffix.lower() == ".xlsx" else target.name
    if not stem.endswith("_" + stamp):
        stem += "_" + stamp
    return str(target.with_name(stem + ".xlsx"))
