"""Аналитика по единому снимку SQLite. Все соединения открываются только на чтение."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from database import DB_PATH

REPORTS = {
    "vuz": {
        "title": "Распределение НИР по вузам",
        "columns": (("number", "№ п/п"), ("vuz", "Вуз"), ("count", "Количество\nНИР"),
                    ("planned", "Сумма планового\nфинансирования, руб."),
                    ("actual", "Фактическое\nфинансирование, руб.")),
        "hint": "Количество и план рассчитаны по НИР. Факт взят из финансирования вузов.",
    },
    "grnti": {
        "title": "Распределение НИР по рубрикам ГРНТИ",
        "columns": (("code", "Код рубрики"), ("rubric", "Название рубрики"),
                    ("count", "Количество НИР")),
        "hint": "Работа учитывается один раз в каждой своей рубрике. Суммирование строк даёт повторный учёт НИР с разными рубриками.",
    },
    "character": {
        "title": "Распределение НИР по характеру",
        "columns": (("number", "№ п/п"), ("character", "Характер НИР"),
                    ("count", "Количество\nНИР"),
                    ("planned", "Сумма планового\nфинансирования, руб.")),
        "hint": "По одной строке на каждый характер НИР. Итоги соответствуют выбранной географии.",
    },
}
CHARACTERS = (("Ф", "Фундаментальные исследования"),
              ("П", "Прикладные исследования"), ("Р", "Разработки"))
GEOGRAPHY_KEYS = ("region", "oblname", "city", "codvuz")


@dataclass
class ReportResult:
    rows: list[dict]
    totals: dict | None
    source_count: int


def rubric_condition(field: str, code: str) -> str:
    # В старых записях коды могли разделяться запятой, пробелом или табуляцией.
    value = f"trim(replace(replace({field}, char(9), ' '), ',', ';'))"
    second = f"""CASE WHEN instr({value}, ';') > 0
        THEN substr(trim(substr({value}, instr({value}, ';')+1)),1,2)
        WHEN instr({value}, ' ') > 0
        THEN substr(trim(substr({value}, instr({value}, ' ')+1)),1,2) END"""
    return f"(substr({value},1,2) = {code} OR ({second}) = {code})"


def build_report(kind: str, geography: dict | None = None, *, rubric: str | None = None,
                 path: Path = DB_PATH) -> ReportResult:
    if kind not in REPORTS:
        raise ValueError("Неизвестный аналитический отчёт.")
    geography = geography or {}
    if set(geography) - set(GEOGRAPHY_KEYS):
        raise ValueError("Неизвестный географический фильтр.")
    conditions, parameters = [], []
    for field in GEOGRAPHY_KEYS:
        if geography.get(field) is not None:
            conditions.append(f"v.{field} = ?")
            parameters.append(geography[field])
    if rubric is not None:
        conditions.append(rubric_condition("n.f10", "?"))
        parameters.extend((rubric, rubric))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    source = "FROM nir n LEFT JOIN vuz v ON v.codvuz=n.codvuz" + where

    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        source_count = conn.execute(f"SELECT COUNT(*) {source}", parameters).fetchone()[0]
        if kind == "vuz":
            # Присоединяем факт после группировки, чтобы не умножить его на число НИР.
            rows = [dict(r) for r in conn.execute(f"""
                WITH grouped AS (
                    SELECT v.codvuz, COALESCE(NULLIF(v.z2,''), NULLIF(v.z1,''), 'Вуз не определён') AS vuz,
                           v.z1 AS full_name, COUNT(*) AS count, SUM(n.f18) AS planned
                    {source} GROUP BY v.codvuz
                )
                SELECT g.*, CASE WHEN g.codvuz IS NULL THEN NULL ELSE COALESCE(f.z18,0) END AS actual
                FROM grouped g LEFT JOIN financing f ON f.codvuz=g.codvuz
                ORDER BY g.codvuz IS NULL, g.codvuz
            """, parameters)]
            for number, row in enumerate(rows, 1):
                row["number"] = number
            totals = {"number": "Итого", "count": sum(r["count"] for r in rows),
                      "planned": sum(r["planned"] for r in rows),
                      "actual": sum(r["actual"] or 0 for r in rows)}
        elif kind == "character":
            grouped = {r["code"]: r for r in conn.execute(
                f"SELECT n.f1 AS code, COUNT(*) AS count, SUM(n.f18) AS planned {source} GROUP BY n.f1",
                parameters)}
            rows = [{"number": number, "character": label,
                     "count": grouped[code]["count"] if code in grouped else 0,
                     "planned": grouped[code]["planned"] if code in grouped else 0}
                    for number, (code, label) in enumerate(CHARACTERS, 1)]
            totals = {"number": "Итого", "count": sum(r["count"] for r in rows),
                      "planned": sum(r["planned"] for r in rows)}
        else:
            names = {f"{int(r[0]):02d}": r[1] for r in conn.execute("SELECT codrub,rubrika FROM grnti")}
            counts = Counter()
            for record in conn.execute(f"SELECT n.f10 {source}", parameters):
                # set исключает удвоение НИР с двумя кодами одной рубрики.
                codes = set(re.findall(r"(?<!\d)(\d{2})\.\d{2}\.\d{2}(?!\d)", record[0] or ""))
                counts.update(codes)
            rows = [{"code": code, "rubric": names.get(code, "Рубрика отсутствует в справочнике"),
                     "count": counts[code]} for code in sorted(counts)]
            totals = None
        return ReportResult(rows, totals, source_count)
