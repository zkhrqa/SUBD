"""Выгрузка отчётов и распоряжений в Excel."""
from contextlib import suppress
from datetime import date
from io import BytesIO
from itertools import count
from os.path import isdir
from pathlib import Path
from textwrap import wrap
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from PyQt6.QtCore import QCoreApplication, QIODevice, QSaveFile, QStandardPaths
from PyQt6.QtWidgets import QFileDialog, QMessageBox
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


EXPORT_FOLDERS = {
    "vuz": "НИР по вузам",
    "grnti": "НИР по рубрикам ГРНТИ",
    "character": "НИР по характеру",
    "funding": "Распределение траншей",
}
TEST_COPY_PROPERTY = "subd_test_copy"
EXPORT_NAME = "Экспорт"
TEST_EXPORT_NAME = "Тестовый экспорт"
ORDERS_FOLDER = "Распоряжения"


def workbook_data(sheet_name, title, details, columns, rows, totals, *, identifier=None):
    workbook = Workbook()
    workbook.properties.identifier = identifier
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.sheet_view.showGridLines = False
    sheet["A2"] = title
    sheet["A2"].font = Font(name="Arial", size=15, bold=True, color="26384A")
    sheet.row_dimensions[2].height = 24
    body_font = Font(name="Arial", size=10, color="26384A")
    for index, line in enumerate(details, start=4):
        sheet.cell(index, 1, line).font = body_font

    header_row = len(details) + 5
    last_col = get_column_letter(len(columns))
    table = [columns, *rows]
    if totals:
        table.append(totals)
    widths = []
    for values in zip(*table):
        longest = max(len(line) for value in values for line in str(value).split("\n"))
        widths.append(min(58, max(13, longest + 5)))
    title_width = len(title) * 3 // 2 + 4
    widths[1] = min(58, widths[1] + max(0, title_width - sum(widths)))
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width

    header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="26384A")
    total_font = Font(name="Arial", size=10, bold=True, color="26384A")
    total_fill = PatternFill("solid", fgColor="EAF0F5")
    total_row = header_row + len(table) - 1 if totals else None
    for row_number, values in enumerate(table, start=header_row):
        lines = 1
        for column, (value, width) in enumerate(zip(values, widths), start=1):
            cell = sheet.cell(row_number, column)
            is_number = type(value) is int
            # Excel хранит только 15 значащих цифр числового значения.
            cell.value = str(value) if is_number and abs(value) >= 10**15 else value
            if isinstance(cell.value, str):
                cell.data_type = "s"
                if cell.value.isdecimal():
                    cell.number_format = "@"
            elif is_number:
                cell.number_format = "#,##0"
            if row_number == header_row:
                cell.font, cell.fill = header_font, header_fill
                horizontal = "center"
            else:
                cell.font = body_font
                horizontal = "right" if is_number else "left"
                if row_number == total_row:
                    cell.font, cell.fill = total_font, total_fill
            cell.alignment = Alignment(horizontal=horizontal, vertical="center", wrap_text=True)
            if isinstance(cell.value, str):
                lines = max(lines, sum(max(1, len(wrap(line, width - 3))) for line in cell.value.split("\n")))
        sheet.row_dimensions[row_number].height = 14 * lines + 5

    if rows:
        sheet.auto_filter.ref = f"A{header_row}:{last_col}{header_row + len(rows)}"
        sheet.freeze_panes = f"A{header_row + 1}"
    sheet.print_title_rows = f"{header_row}:{header_row}"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    with BytesIO() as output:
        workbook.save(output)
        return output.getvalue()


def export_locations():
    def subd_directory(location, fallback):
        path = QStandardPaths.writableLocation(location)
        return (Path(path) if path else Path.home() / fallback) / "SUBD"

    desktop = subd_directory(QStandardPaths.StandardLocation.DesktopLocation, "Desktop")
    documents = subd_directory(QStandardPaths.StandardLocation.DocumentsLocation, "Documents")
    app = QCoreApplication.instance()
    is_test = app and app.property(TEST_COPY_PROPERTY)
    folder_name = TEST_EXPORT_NAME if is_test else EXPORT_NAME
    other_mode = EXPORT_NAME if is_test else TEST_EXPORT_NAME
    root = documents / folder_name
    desktop_root = desktop / folder_name
    if isdir(desktop_root) or (not isdir(root) and isdir(desktop / other_mode)):
        root = desktop_root
    return root, other_mode, (desktop, documents)


def choose_export_directory(parent, category):
    root, other_mode, locations = export_locations()
    default = root / EXPORT_FOLDERS[category]
    try:
        for folder in EXPORT_FOLDERS.values():
            (root / folder).mkdir(parents=True, exist_ok=True)
    except OSError:
        # Диалог остаётся доступным, даже если папку по умолчанию нельзя создать.
        default = next((path for path in (default, root, root.parent, root.parent.parent) if isdir(path)), Path.home())

    selected = QFileDialog.getExistingDirectory(parent, "Выберите папку для сохранения", str(default))
    if not selected:
        return None
    directory = Path(selected).resolve()
    if any(directory.is_relative_to((location / other_mode).resolve()) for location in locations):
        QMessageBox.warning(parent, "Выберите другую папку",
                            f"Папка «{other_mode}» предназначена для другого режима запуска.")
        return None
    if any(directory.is_relative_to((location / root.name / ORDERS_FOLDER).resolve()) for location in locations):
        QMessageBox.warning(parent, "Выберите другую папку",
                            "В папку «Распоряжения» программа сохраняет документы при утверждении.")
        return None
    return directory


def workbook_stem(document_date, name):
    return f"{document_date:%d.%m.%Y} — {name}"


def write_workbook(directory, stem, data):
    for number in count(1):
        suffix = f" ({number})" if number > 1 else ""
        path = directory / f"{stem}{suffix}.xlsx"
        try:
            path.touch(exist_ok=False)
        except FileExistsError:
            continue
        break

    target = QSaveFile(str(path))
    if (not target.open(QIODevice.OpenModeFlag.WriteOnly)
            or target.write(data) != len(data) or not target.commit()):
        error = target.errorString()
        target.cancelWriting()
        with suppress(OSError):
            path.unlink(missing_ok=True)
        raise OSError(f"{path}\n{error}")
    return path


def find_order_file(name: str, document_date: date, identifier: str) -> Path | None:
    directory = export_locations()[0] / ORDERS_FOLDER
    stem = workbook_stem(document_date, name)
    for path in directory.glob(f"{stem}*.xlsx"):
        try:
            with ZipFile(path) as archive:
                properties = ElementTree.fromstring(archive.read("docProps/core.xml"))
        except (OSError, BadZipFile, KeyError, ElementTree.ParseError):
            continue
        if properties.findtext("{http://purl.org/dc/elements/1.1/}identifier") == identifier:
            return path
    return None


def save_order_file(name: str, *, document_date: date, identifier: str,
                    title, details, columns, rows, totals) -> Path:
    existing = find_order_file(name, document_date, identifier)
    if existing is not None:
        return existing
    directory = export_locations()[0] / ORDERS_FOLDER
    directory.mkdir(parents=True, exist_ok=True)
    data = workbook_data(ORDERS_FOLDER, title, details, columns, rows, totals, identifier=identifier)
    return write_workbook(directory, workbook_stem(document_date, name), data)


def save_workbook(parent, category: str, name: str, *, document_date: date,
                  title, details, columns, rows, totals=()):
    directory = None
    try:
        directory = choose_export_directory(parent, category)
        if directory is None:
            return
        data = workbook_data(EXPORT_FOLDERS[category], title, details, columns, rows, totals)
        path = write_workbook(directory, workbook_stem(document_date, name), data)
    except OSError as exc:
        QMessageBox.warning(parent, "Документ не сохранён", f"{directory}\n{exc}" if directory else str(exc))
        return

    QMessageBox.information(parent, "Документ сохранён", str(path))
