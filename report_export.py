"""Экспорт аналитических отчётов в XLSX без изменения исходной базы."""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


def _excel_safe(value):
    # Excel воспринимает строки вида '=...', '+...' как выражения.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_excel_report(path: str | Path, title: str, columns: tuple, rows: list[dict],
                       totals: dict | None, *, geography: dict | None = None,
                       grouping: str | None = None, source_count: int = 0,
                       rubric: str | None = None) -> None:
    """Экспортирует именно отображаемую сортировку, фильтр и закреплённые итоги."""
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Отчёт"
    last_col = get_column_letter(len(columns))
    sheet.merge_cells(f"A1:{last_col}1")
    sheet["A1"] = title
    sheet["A1"].font = Font(size=15, bold=True)
    sheet["A2"] = f"Количество уникальных НИР в выборке: {source_count}"
    filters = ", ".join(f"{k}: {v}" for k, v in (geography or {}).items() if v is not None)
    if rubric is not None:
        filters += (", " if filters else "") + f"ГРНТИ: {rubric}"
    sheet["A3"] = "Фильтр: " + (filters or "без фильтра")
    if grouping:
        sheet["A4"] = "Группировка: " + grouping
    header_row = 6
    for j, (_, heading) in enumerate(columns, 1):
        cell = sheet.cell(header_row, j, heading.replace("\n", " "))
        cell.fill = PatternFill("solid", fgColor="DCE6F1")
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    sheet.row_dimensions[header_row].height = 32

    for i, row in enumerate([*rows, *([totals] if totals is not None else [])], header_row + 1):
        is_total = totals is not None and i == header_row + len(rows) + 1
        for j, (field, _) in enumerate(columns, 1):
            value = row.get(field)
            cell = sheet.cell(i, j, _excel_safe(value) if field != "execution" else
                              (value / 100 if value is not None else None))
            if field == "execution":
                cell.number_format = "0.00%"
            elif field in {"count", "planned", "actual", "universities", "number"} and isinstance(value, int):
                cell.number_format = "#,##0"
            elif field == "code":
                cell.number_format = "@"
            cell.alignment = Alignment(vertical="center", wrap_text=(field in {"group", "rubric", "vuz"}))
            if is_total:
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="E8F0E5")
    for j, (field, _) in enumerate(columns, 1):
        letter = get_column_letter(j)
        if field in {"group", "rubric", "vuz", "character"}:
            width = 44
        elif field in {"planned", "actual"}:
            width = 24
        else:
            width = 19
        sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = f"A{header_row+1}"
    if rows:
        sheet.auto_filter.ref = f"A{header_row}:{last_col}{header_row+len(rows)}"
    wb.save(path)


def write_all_nir_excel(destination: str | Path, *, db_path: str | Path) -> int:
    """Выгрузить абсолютно все НИР, независимо от активного фильтра или поиска."""
    import sqlite3

    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        records = connection.execute("""
            SELECT n.id,n.codvuz,n.rnw,n.f1,n.z2,n.f6,n.f10,n.f2,n.f7,n.f18,
                   v.region,v.oblname,v.city
            FROM nir n LEFT JOIN vuz v ON v.codvuz=n.codvuz
            ORDER BY n.codvuz IS NULL,n.codvuz,n.rnw COLLATE BINARY,n.id
        """).fetchall()
    finally:
        connection.close()

    columns = (
        ("id", "ID"), ("codvuz", "Код вуза"), ("rnw", "Регистрационный номер"),
        ("f1", "Характер НИР"), ("z2", "Вуз (кратко)"),
        ("f6", "Руководитель (ФИО)"), ("f10", "Коды ГРНТИ"),
        ("f2", "Наименование НИР"), ("f7", "Должность руководителя"),
        ("f18", "Плановое финансирование, руб."),
        ("region", "Федеральный округ"), ("oblname", "Субъект РФ"),
        ("city", "Город"),
    )
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Все НИР"
    end_col = get_column_letter(len(columns))
    sheet.merge_cells(f"A1:{end_col}1")
    sheet["A1"] = "Полная выгрузка научно-исследовательских работ"
    sheet["A1"].font = Font(size=15, bold=True)
    sheet["A2"] = f"Всего НИР: {len(records)}. Фильтры интерфейса не применяются."
    sheet["A3"] = f"Плановое финансирование: {sum(int(r['f18'] or 0) for r in records):,} руб.".replace(",", " ")
    header_row = 5
    for col, (_, title) in enumerate(columns, 1):
        cell = sheet.cell(header_row, col, title)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="244A73")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[header_row].height = 34

    for row_number, record in enumerate(records, header_row + 1):
        for column, (field, _) in enumerate(columns, 1):
            value = record[field]
            cell = sheet.cell(row_number, column, _excel_safe(value))
            cell.alignment = Alignment(vertical="top", wrap_text=field in ("f2", "f6", "f7"))
            if field in ("rnw", "f10"):
                # 07 и 7, а также 20.10.10 должны оставаться текстовыми кодами.
                cell.number_format = "@"
            if field == "f18":
                cell.number_format = "#,##0"
            if row_number % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="EEF3F9")
    widths = {
        "id": 10, "codvuz": 15, "rnw": 21, "f1": 16, "z2": 23,
        "f6": 29, "f10": 22, "f2": 65, "f7": 33, "f18": 26,
        "region": 24, "oblname": 27, "city": 23,
    }
    for column, (field, _) in enumerate(columns, 1):
        sheet.column_dimensions[get_column_letter(column)].width = widths[field]
    sheet.freeze_panes = "D6"
    sheet.auto_filter.ref = f"A{header_row}:{end_col}{header_row + len(records)}"
    sheet.sheet_view.showGridLines = False
    wb.save(destination)
    return len(records)
