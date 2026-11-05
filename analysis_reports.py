"""Аналитика по единому снимку SQLite. Все соединения открываются только на чтение."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from database import DB_PATH
from nir_operations import rubric_sql

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
    "financing": {
        "title": "Сводный анализ финансирования",
        "columns": (("number", "№ п/п"), ("group", "Территория / вуз"),
                    ("universities", "Количество\nвузов"),
                    ("count", "Количество\nНИР"),
                    ("planned", "Плановое\nфинансирование, руб."),
                    ("actual", "Фактическое\nфинансирование, руб."),
                    ("execution", "Исполнение\nплана, %")),
        "hint": "План и НИР суммируются по работам, факт учитывается один раз на вуз. "
                "Процент = факт / план × 100; при нулевом плане стоит прочерк. "
                "Для работ без вуза факт неизвестен и не входит в расчёт процента.",
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
FINANCING_GROUPS = (
    ("region", "По федеральным округам"),
    ("subject", "По субъектам РФ"),
    ("city", "По городам"),
    ("vuz", "По вузам"),
)


def percent(actual: int | None, planned: int) -> float | None:
    """Процент исполнения. Ноль плана или неизвестный факт — не 0 %."""
    return round(100 * actual / planned, 2) if actual is not None and planned > 0 else None


def _financing_report(conn: sqlite3.Connection, source: str, parameters: list,
                      source_count: int, group_by: str) -> ReportResult:
    # Сначала сворачиваем НИР по вузам, только потом присоединяем фактическое
    # финансирование. Иначе факт одного вуза умножится на число его НИР.
    per_vuz = conn.execute(f"""
        WITH work AS (
            SELECT v.codvuz, v.z2, v.z1, v.region, v.oblname, v.city,
                   COUNT(*) AS work_count, COALESCE(SUM(n.f18), 0) AS planned
            {source}
            GROUP BY v.codvuz
        )
        SELECT w.*, CASE WHEN w.codvuz IS NULL THEN NULL ELSE COALESCE(f.z18,0) END AS actual
        FROM work w LEFT JOIN financing f ON f.codvuz=w.codvuz
    """, parameters).fetchall()

    grouped = {}
    for university in per_vuz:
        code = university["codvuz"]
        if code is None:
            key, label = ("unknown",), "Вуз не определён"
        elif group_by == "region":
            region = university["region"] or "Округ не указан"
            key, label = ("region", region), region
        elif group_by == "subject":
            subject = university["oblname"] or "Субъект не указан"
            region = university["region"] or "Округ не указан"
            key, label = ("subject", region, subject), f"{subject} ({region})"
        elif group_by == "city":
            city = university["city"] or "Город не указан"
            subject = university["oblname"] or "Субъект не указан"
            region = university["region"] or "Округ не указан"
            key, label = ("city", region, subject, city), f"{city} — {subject}"
        else:
            key = ("vuz", code)
            label = f"{university['z2'] or university['z1'] or 'Без названия'} (код {code})"
        row = grouped.setdefault(key, {
            "group": label, "universities": 0, "count": 0,
            "planned": 0, "actual": 0, "_known_plan": 0, "_has_actual": False,
        })
        row["universities"] += int(code is not None)
        row["count"] += university["work_count"]
        row["planned"] += university["planned"]
        if code is not None:
            row["actual"] += university["actual"]
            row["_known_plan"] += university["planned"]
            row["_has_actual"] = True

    rows = []
    for number, row in enumerate(sorted(grouped.values(), key=lambda r: r["group"].casefold()), 1):
        known_actual = row["actual"] if row.pop("_has_actual") else None
        known_plan = row.pop("_known_plan")
        row["number"] = number
        row["actual"] = known_actual
        row["execution"] = percent(known_actual, known_plan)
        rows.append(row)
    total_plan_known = sum(r["planned"] for r in per_vuz if r["codvuz"] is not None)
    actual_known = sum(r["actual"] for r in per_vuz if r["codvuz"] is not None)
    totals = {
        "number": "Итого", "group": "Все выбранные территории",
        "universities": sum(r["universities"] for r in rows),
        "count": sum(r["count"] for r in rows),
        "planned": sum(r["planned"] for r in rows),
        "actual": actual_known if total_plan_known or any(r["codvuz"] is not None for r in per_vuz) else None,
        "execution": percent(actual_known, total_plan_known),
    }
    return ReportResult(rows, totals, source_count)



@dataclass
class ReportResult:
    rows: list[dict]
    totals: dict | None
    source_count: int


def build_report(kind: str, geography: dict | None = None, *, path: Path = DB_PATH,
                 group_by: str = "subject", rubric: str | None = None) -> ReportResult:
    if kind not in REPORTS:
        raise ValueError("Неизвестный аналитический отчёт.")
    if kind == "financing" and group_by not in dict(FINANCING_GROUPS):
        raise ValueError("Неизвестная группировка финансирования.")
    geography = geography or {}
    if rubric is not None and (not isinstance(rubric, str) or not re.fullmatch(r"\d{2}", rubric)):
        raise ValueError("Некорректная рубрика ГРНТИ.")
    if set(geography) - set(GEOGRAPHY_KEYS):
        raise ValueError("Неизвестный географический фильтр.")
    conditions, parameters = [], []
    for field in GEOGRAPHY_KEYS:
        if geography.get(field) is not None:
            conditions.append(f"v.{field} = ?")
            parameters.append(geography[field])
    if rubric is not None:
        # Одинаковая рубрика действует на ВСЕ аналитические отчёты.
        conditions.append(rubric_sql("n.f10"))
        parameters.extend((rubric, rubric))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    source = "FROM nir n LEFT JOIN vuz v ON v.codvuz=n.codvuz" + where

    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        source_count = conn.execute(f"SELECT COUNT(*) {source}", parameters).fetchone()[0]
        if kind == "financing":
            return _financing_report(conn, source, parameters, source_count, group_by)
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
