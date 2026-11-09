from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QActionGroup, QFontMetrics
from PyQt6.QtSql import QSqlDatabase, QSqlQuery, QSqlTableModel
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QFileDialog,
    QFrame,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from database import DB_PATH, create_database, rebuild_financing, run_validation
from nir_operations import delete_nir, prepare_database, read_rows, save_actual_financing, rubric_sql
from nir_forms import ChangeLogDialog, NirDialog
from geography_filters import populate_geography
from shared_filters import SharedFilters, UnifiedFilterDialog
from analysis_forms import AnalysisDialog
from analysis_reports import REPORTS
from report_export import write_all_nir_excel
from export_filename import dated_excel_filename


TABLE_CONFIG = {
    "nir": {
        "title": "НИР",
        "headers": {
            "id": "ID",
            "codvuz": "Код вуза",
            "rnw": "Рег. номер",
            "f1": "Характер НИР",
            "z2": "Вуз (кратко)",
            "f6": "Руководитель (ФИО)",
            "f10": "Код(ы) ГРНТИ",
            "f2": "Наименование НИР",
            "f7": "Должность руководителя",
            "f18": "Плановое финансирование, руб.",
        },
        "editable": set(),
        "hint": "Для редактирования НИР используйте кнопки снизу. Рег. номер уникален внутри вуза; ГРНТИ — один или два кода через ';'.",
    },
    "financing": {
        "title": "Финансирование вузов",
        "headers": {
            "codvuz": "Код вуза",
            "z2": "Вуз (кратко)",
            "z3": "План, руб.",
            "z18": "Факт, руб.",
            "numworks": "Количество НИР",
        },
        "editable": {"z18"},
        "hint": "План и количество НИР рассчитываются автоматически. НИР без связи с вузом показаны строкой «Вуз не определён». Факт редактируется только для определённых вузов.",
    },
    "vuz": {
        "title": "Справочник вузов",
        "headers": {
            "codvuz": "Код вуза",
            "z1": "Наименование",
            "z1full": "Полное юридическое наименование",
            "z2": "Сокращенное наименование",
            "region": "Федеральный округ",
            "city": "Город",
            "status": "Статус",
            "obl": "Код субъекта",
            "oblname": "Субъект РФ",
            "gr_ved": "Группа ведомств",
            "prof": "Профиль",
        },
        "editable": set(),
        "hint": "VUZ — справочник: только просмотр. Изменение из интерфейса ЛР №1 запрещено.",
    },
    "grnti": {
        "title": "Рубрики ГРНТИ",
        "headers": {"codrub": "Код рубрики", "rubrika": "Наименование рубрики"},
        "editable": set(),
        "hint": "Справочник рубрик используется для автоматической проверки и анализа кодов ГРНТИ.",
    },
}


class ControlledTableModel(QSqlTableModel):
    """QSqlTableModel, в котором редактируются только разрешенные поля."""

    def __init__(self, editable_fields: set[str], parent=None, db=QSqlDatabase()):
        super().__init__(parent, db)
        self.editable_fields = editable_fields
        self.key_sort = False
        self.key_order = Qt.SortOrder.AscendingOrder
        self.save_pending = self.submitAll
        self.unassigned_financing = False

    def is_unassigned_row(self, index):
        if not self.unassigned_financing or not index.isValid():
            return False
        column = self.record().indexOf("codvuz")
        value = super().data(self.index(index.row(), column), Qt.ItemDataRole.EditRole)
        return value in (None, "")

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if self.is_unassigned_row(index):
            if role == Qt.ItemDataRole.DisplayRole and self.record().fieldName(index.column()) in {"codvuz", "z18"}:
                return "—"
            if role == Qt.ItemDataRole.ToolTipRole:
                return "НИР без кода вуза или с кодом, отсутствующим в справочнике. Фактическое финансирование не определено."
        return super().data(index, role)

    def select(self):
        result = super().select()
        # Загружаем все строки, чтобы выделение и фильтры работали со всей таблицей.
        while result and self.canFetchMore():
            self.fetchMore()
        return result

    def orderByClause(self):
        if self.key_sort:
            direction = "DESC" if self.key_order == Qt.SortOrder.DescendingOrder else "ASC"
            return f"ORDER BY codvuz {direction}, rnw COLLATE BINARY {direction}, id {direction}"
        return super().orderByClause()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if self.isDirty() and not self.save_pending():
            QMessageBox.warning(None, "Сортировка не выполнена", self.lastError().text())
            return
        self.key_sort = False
        super().sort(column, order)

    def flags(self, index):
        flags = super().flags(index)
        if not index.isValid():
            return flags
        field = self.record().fieldName(index.column())
        if field not in self.editable_fields or self.is_unassigned_row(index):
            return flags & ~Qt.ItemFlag.ItemIsEditable
        return flags | Qt.ItemFlag.ItemIsEditable

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.EditRole and index.isValid():
            if self.is_unassigned_row(index):
                return False
            field = self.record().fieldName(index.column())
            text = str(value).strip() if value is not None else ""
            if field == "rnw":
                value = "".join(text.split())
            elif field == "f10":
                value = "; ".join(p.strip() for p in text.replace(",", ";").split(";") if p.strip())
            elif field in {"f18", "z18"}:
                if not re.fullmatch(r"[0-9]+", text) or int(text) > 9223372036854775807:
                    QMessageBox.warning(None, "Некорректное значение", "Сумма должна быть целым неотрицательным числом рублей.")
                    return False
                value = int(text)
            elif field == "f1":
                value = text.upper()
                if value not in {"Ф", "П", "Р"}:
                    QMessageBox.warning(None, "Некорректное значение", "Характер НИР: только Ф, П или Р.")
                    return False
            else:
                value = text
        return super().setData(index, value, role)


class TablePage(QWidget):
    nir_changed = pyqtSignal(int)
    finance_report_requested = pyqtSignal()
    finance_check_requested = pyqtSignal()

    def __init__(self, db: QSqlDatabase, table_name: str, filters: SharedFilters, parent=None):
        super().__init__(parent)
        self.table_name = table_name
        self.shared_filters = filters
        self.open_unified_filter = None
        self.path = Path(db.databaseName())
        self.before_nir_change = None
        self.reload_after_nir_change = None
        self.after_table_save = None
        self.last_save_error = ""
        cfg = TABLE_CONFIG[table_name]

        self.model = ControlledTableModel(cfg["editable"], self, db)
        self.model.unassigned_financing = table_name == "financing"
        self.model.setTable("financing_display" if table_name == "financing" else table_name)
        self.model.setEditStrategy(QSqlTableModel.EditStrategy.OnManualSubmit)
        self.model.save_pending = self.save
        self.model.select()

        self.field_names: list[str] = []
        rec = self.model.record()
        for col in range(rec.count()):
            field = rec.fieldName(col)
            self.field_names.append(field)
            self.model.setHeaderData(col, Qt.Orientation.Horizontal, cfg["headers"].get(field, field))

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        # Убираем пустой угловой блок в левом верхнем углу таблицы
        # (corner button над вертикальным заголовком).
        self.view.setCornerButtonEnabled(False)
        self.view.horizontalHeader().setStretchLastSection(table_name in {"nir", "vuz"})
        self.view.resizeColumnsToContents()
        if table_name == "nir":
            self.view.setColumnHidden(self.field_names.index("id"), True)
            header = self.view.horizontalHeader()
            for position, field in enumerate(("f18", "f7", "f2"), start=len(self.field_names) - 3):
                header.moveSection(header.visualIndex(self.field_names.index(field)), position)
            self.view.doubleClicked.connect(lambda _: self.edit_nir())
        elif table_name == "vuz":
            header = self.view.horizontalHeader()
            order = ("codvuz", "z2", "city", "oblname", "region", "status", "obl", "gr_ved", "prof", "z1", "z1full")
            for position, field in enumerate(order):
                header.moveSection(header.visualIndex(self.field_names.index(field)), position)
        if table_name in {"nir", "vuz", "grnti"}:
            header = self.view.horizontalHeader()
            self.view.setWordWrap(True)
            self.view.setTextElideMode(Qt.TextElideMode.ElideNone)
            self.row_resize_timer = QTimer(self)
            self.row_resize_timer.setSingleShot(True)
            self.row_resize_timer.setInterval(60)
            self.row_resize_timer.timeout.connect(self.view.resizeRowsToContents)
            header.sectionResized.connect(lambda *_: self.row_resize_timer.start())
            self.model.modelReset.connect(self.row_resize_timer.start)
            self.model.rowsInserted.connect(lambda *_: self.row_resize_timer.start())
        self.apply_column_widths()

        # Храним поисковые значения отдельно для каждой вкладки; сами элементы
        # ввода отображаются в единой строке поиска главного окна.
        self.search_field = QComboBox(self)
        for field in self.field_names:
            if field != "id":
                self.search_field.addItem(cfg["headers"].get(field, field), field)
        self.search_edit = QLineEdit(self)
        # Состояние поиска хранится отдельно для каждой таблицы, но вводить
        # запрос пользователь должен только через общую верхнюю строку.
        # Эти дочерние виджеты не входят в layout; без hide() Qt показывает
        # их поверх верхнего левого угла таблицы при отображении страницы.
        self.search_field.hide()
        self.search_edit.hide()
        self.search_edit.textChanged.connect(self.filter_control_changed)
        self.search_field.currentIndexChanged.connect(self.filter_control_changed)
        layout = QVBoxLayout(self)
        # Убираем служебные подсказки над таблицами: они занимали отдельную
        # полосу и накладывались на элементы интерфейса при масштабировании.
        # Один общий фильтр находится в верхней панели главного окна.
        layout.addWidget(self.view, 1)
        if table_name == "financing":
            self.total_label = QLabel()
            self.total_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.total_label.setStyleSheet("font-weight: bold; padding: 6px;")
            self.total_label.setWordWrap(True)
            layout.addWidget(self.total_label)
            self.model.modelReset.connect(self.update_financing_total)
            self.model.rowsInserted.connect(self.update_financing_total)
            self.model.rowsRemoved.connect(self.update_financing_total)
            self.model.dataChanged.connect(self.update_financing_total)
            self.update_financing_total()
        buttons = QHBoxLayout()
        if table_name == "nir":
            self.add_button = QPushButton("Добавить")
            self.edit_button = QPushButton("Изменить")
            self.delete_button = QPushButton("Удалить")
            self.add_button.clicked.connect(self.add_nir)
            self.edit_button.clicked.connect(self.edit_nir)
            self.delete_button.clicked.connect(self.remove_nir)
            for button in (self.add_button, self.edit_button, self.delete_button):
                buttons.addWidget(button)
        if table_name == "financing":
            self.analysis_button = QPushButton("Сводный анализ финансирования")
            self.analysis_button.clicked.connect(lambda _=False: self.finance_report_requested.emit())
            buttons.addWidget(self.analysis_button)
            self.finance_check_button = QPushButton("Проверить суммы")
            self.finance_check_button.clicked.connect(lambda _=False: self.finance_check_requested.emit())
            buttons.addWidget(self.finance_check_button)
            self.finance_save_button = QPushButton("Сохранить факт")
            self.finance_save_button.clicked.connect(self.save)
            buttons.addWidget(self.finance_save_button)
        # Сортировка НИР находится в верхней панели, а не среди кнопок редактирования.
        self.filter_summary = QLabel()
        buttons.addWidget(self.filter_summary, 1)
        layout.addLayout(buttons)
        self.model.modelReset.connect(self.update_filter_summary)
        self.model.rowsInserted.connect(self.update_filter_summary)
        self.model.rowsRemoved.connect(self.update_filter_summary)
        self.update_filter_summary()
        if table_name == "nir":
            self.view.selectionModel().selectionChanged.connect(self.update_buttons)
            self.view.selectionModel().currentChanged.connect(self.update_buttons)
            self.model.modelReset.connect(self.update_buttons)
            self.update_buttons()
            self.apply_sort()

    def open_filter(self):
        if self.open_unified_filter is not None:
            self.open_unified_filter()

    def filter_control_changed(self, *_):
        self.apply_filter()

    def clear_search(self):
        blocked = self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(blocked)
        self.filter_control_changed()

    def update_filter_summary(self, *_):
        searched = bool(self.search_edit.text().strip())
        filtered = (any(v is not None for v in self.shared_filters.geography.values()) or
                    self.shared_filters.rubric is not None)
        status = ("Фильтр и поиск применены" if filtered and searched else "Поиск применён" if searched
                  else "Фильтр применён" if filtered else "Без фильтра")
        self.filter_summary.setText(f"{status} · Строк: {self.model.rowCount()}")
        details = []
        if searched:
            details.append(f"Поиск — {self.search_field.currentText()}: {self.search_edit.text().strip()}")
        details.append(self.shared_filters.describe())
        self.filter_summary.setToolTip("\n".join(details))

    def update_financing_total(self, *_):
        # Все итоги ниже относятся к *видимым* вузам/НИР. Факт берём один
        # раз на вуз; для строки без вуза факт не выдумываем.
        n_universities = total_works = planned = actual = known_plan = unknown_works = 0
        for i in range(self.model.rowCount()):
            record = self.model.record(i)
            n = int(record.value("numworks") or 0)
            plan = int(record.value("z3") or 0)
            total_works += n
            planned += plan
            if record.value("codvuz") in (None, ""):
                unknown_works += n
            else:
                n_universities += 1
                known_plan += plan
                actual += int(record.value("z18") or 0)
        percentage = (f"{actual * 100 / known_plan:.2f} %".replace(".", ",")
                      if known_plan > 0 else "—")
        fmt = lambda value: f"{value:,}".replace(",", " ")
        suffix = " · Только отфильтрованные строки" if self.model.filter() else ""
        note = (f" · НИР без вуза: {unknown_works} (их план не включён в процент)"
                if unknown_works else "")
        self.total_label.setText(
            f"Вузов: {n_universities} · НИР: {total_works} · "
            f"План: {fmt(planned)} ₽ · Факт: {fmt(actual)} ₽ · "
            f"Исполнение: {percentage}{note}{suffix}")

    def apply_column_widths(self):
        if self.table_name == "nir":
            # Короткие поля занимают место по данным и заголовку, а длинные
            # должности/ФИО переносятся, оставляя место названию работы.
            headers = {
                "codvuz": "Код\nвуза", "rnw": "Рег.\nномер", "f1": "Характер\nНИР",
                "z2": "Вуз\n(кратко)", "f6": "Руководитель\n(ФИО)",
                "f10": "Код(ы) ГРНТИ", "f18": "Плановое\nфинансирование, руб.",
                "f7": "Должность\nруководителя",
            }
            limits = {"z2": 180, "f6": 210, "f7": 190}
            self.fit_column_widths(headers, limits)
            # Последний столбец расширяется на оставшееся пространство окна.
            self.view.setColumnWidth(self.field_names.index("f2"), 440)
            self.row_resize_timer.start()
        elif self.table_name == "vuz":
            headers = {
                "codvuz": "Код\nвуза", "z2": "Вуз\n(кратко)", "city": "Город",
                "oblname": "Субъект РФ", "region": "Федеральный\nокруг", "status": "Статус",
                "obl": "Код\nсубъекта", "gr_ved": "Группа\nведомств", "prof": "Профиль",
                "z1": "Наименование", "z1full": "Полное юридическое\nнаименование",
            }
            limits = {"z2": 150, "city": 170, "oblname": 190, "region": 150,
                      "z1": 280, "z1full": 380}
            self.fit_column_widths(headers, limits)
            self.row_resize_timer.start()
        elif self.table_name == "financing":
            self.fit_column_widths({
                "codvuz": "Код\nвуза", "z2": "Вуз (кратко)",
                "z3": "План, руб.", "z18": "Факт, руб.", "numworks": "Количество\nНИР",
            }, {})
        elif self.table_name == "grnti":
            self.fit_column_widths({"codrub": "Код\nрубрики", "rubrika": "Наименование рубрики"},
                                   {"rubrika": 760})
            self.row_resize_timer.start()

    def fit_column_widths(self, headers: dict[str, str], limits: dict[str, int]):
        metrics = QFontMetrics(self.view.font())
        header_metrics = QFontMetrics(self.view.horizontalHeader().font())
        records = [self.model.record(row) for row in range(self.model.rowCount())]
        for field, label in headers.items():
            column = self.field_names.index(field)
            self.model.setHeaderData(column, Qt.Orientation.Horizontal, label)
            content_width = max((metrics.horizontalAdvance(str(record.value(field) or "")) + 16
                                 for record in records), default=0)
            header_width = max(header_metrics.horizontalAdvance(line) for line in label.splitlines()) + 24
            width = max(header_width, min(content_width, limits.get(field, content_width)))
            self.view.setColumnWidth(column, width)

    @staticmethod
    def sql_text(text: str) -> str:
        return "'" + str(text).replace("'", "''") + "'"

    def apply_filter(self, *_):
        # Не теряем несохранённый факт финансирования при смене фильтра.
        if self.model.isDirty() and not self.save():
            QMessageBox.warning(self, "Фильтр не применён", self.last_save_error)
            return False
        field = self.search_field.currentData()
        text = self.search_edit.text().strip()
        conditions = []
        if field and text:
            # SQLite LIKE не учитывает регистр кириллицы. Сравниваем casefold
            # в Python, а в модель передаём только числовые ключи найденных строк.
            key = {"nir": "id", "financing": "codvuz", "vuz": "codvuz", "grnti": "codrub"}[self.table_name]
            table = "financing_display" if self.table_name == "financing" else self.table_name
            matches = [r[key] for r in read_rows(f"SELECT {key}, {field} FROM {table}", path=self.path)
                       if text.casefold() in str(r[field] if r[field] is not None else "").casefold()]
            keys = [str(int(value)) for value in matches if value is not None]
            match_conditions = [f"{key} IN ({','.join(keys)})"] if keys else []
            if None in matches:
                match_conditions.append(f"{key} IS NULL")
            conditions.append("(" + " OR ".join(match_conditions) + ")" if match_conditions else "0")
        geo = [f"{name} = {self.sql_text(value)}"
               for name, value in self.shared_filters.geography.items() if value is not None]
        if geo and self.table_name != "grnti":
            conditions.append(f"codvuz IN (SELECT codvuz FROM vuz WHERE {' AND '.join(geo)})")
        rubric = self.shared_filters.rubric
        if rubric is not None:
            code = self.sql_text(rubric)
            # Один и тот же способ распознавания рубрик во всех отчётах и таблицах.
            rubric_sql_condition = rubric_sql("f10", code)
            if self.table_name == "nir":
                conditions.append(rubric_sql_condition)
            elif self.table_name in ("financing", "vuz"):
                conditions.append(f"codvuz IN (SELECT codvuz FROM nir WHERE {rubric_sql_condition})")
            else:
                conditions.append(f"codrub = {int(rubric)}")
        if self.table_name == "grnti" and geo:
            # Справочник рубрик не содержит географии: показываем только
            # рубрики, реально встречающиеся у НИР выбранной территории.
            where = " AND ".join(f"v.{name} = {self.sql_text(value)}"
                                  for name, value in self.shared_filters.geography.items() if value is not None)
            texts = read_rows("SELECT n.f10 FROM nir n JOIN vuz v ON v.codvuz=n.codvuz "
                              f"WHERE {where}", path=self.path)
            prefixes = set()
            for row in texts:
                prefixes.update(re.findall(r"(?<!\d)(\d{2})\.\d{2}\.\d{2}(?!\d)", row["f10"] or ""))
            conditions.append("codrub IN (" + ",".join(str(int(x)) for x in sorted(prefixes)) + ")"
                              if prefixes else "0")
        self.model.setFilter(" AND ".join(conditions))
        selected = self.model.select()
        self.update_filter_summary()
        return selected

    def clear_filter(self):
        self.shared_filters.reset()

    def apply_sort(self, order=Qt.SortOrder.AscendingOrder):
        selected = self.selected_record()
        self.model.key_sort = True
        self.model.key_order = order
        header = self.view.horizontalHeader()
        header.setSectionsClickable(True)
        blocked = header.blockSignals(True)
        header.setSortIndicator(self.field_names.index("codvuz"), order)
        header.blockSignals(blocked)
        header.setSortIndicatorShown(True)
        self.model.select()
        if selected is not None:
            self.select_record(selected["id"])

    def update_buttons(self, *_):
        selected = self.view.currentIndex().isValid()
        self.edit_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)

    def selected_record(self) -> dict | None:
        index = self.view.currentIndex()
        if not index.isValid():
            return None
        record = self.model.record(index.row())
        row_id = record.value("id")
        rows = read_rows("SELECT * FROM nir WHERE id=?", (row_id,), path=self.path)
        return rows[0] if rows else None

    def ready_for_nir_change(self) -> bool:
        return self.before_nir_change is None or self.before_nir_change()

    def add_nir(self):
        if not self.ready_for_nir_change():
            return
        dialog = NirDialog(self.path, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.after_nir_save(dialog.saved_id)

    def edit_nir(self):
        row = self.selected_record()
        if row is None or not self.ready_for_nir_change():
            return
        dialog = NirDialog(self.path, row, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.after_nir_save(dialog.saved_id)

    def remove_nir(self):
        row = self.selected_record()
        if row is None:
            return
        answer = QMessageBox.question(self, "Удалить НИР?",
            f"Вуз: {row['codvuz']}\nРег. номер: {row['rnw']}\n{row['f2']}\n\nУдалить эту запись?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes or not self.ready_for_nir_change():
            return
        try:
            delete_nir(row["id"], path=self.path, expected=row)
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "НИР не удалена", str(exc))
            return
        self.reload_nir_data()
        self.nir_changed.emit(row["id"])

    def reload_nir_data(self):
        if self.reload_after_nir_change is not None:
            self.reload_after_nir_change()
        else:
            self.refresh()

    def select_record(self, row_id: int) -> bool:
        while self.model.canFetchMore():
            self.model.fetchMore()
        for row in range(self.model.rowCount()):
            if self.model.record(row).value("id") == row_id:
                index = self.model.index(row, self.field_names.index("codvuz"))
                self.view.setCurrentIndex(index)
                self.view.selectRow(row)
                self.view.scrollTo(index, QTableView.ScrollHint.PositionAtCenter)
                self.view.setFocus()
                return True
        return False

    def after_nir_save(self, row_id: int):
        self.reload_nir_data()
        if not self.select_record(row_id):
            self.clear_search()
            self.clear_filter()
            self.select_record(row_id)
            QMessageBox.information(self, "Фильтр сброшен", "Запись сохранена. Фильтр сброшен, чтобы показать её в таблице.")
        self.nir_changed.emit(row_id)

    def save(self) -> bool:
        self.last_save_error = ""
        if self.table_name == "financing" and self.model.isDirty():
            column = self.field_names.index("z18")
            changes = [
                (self.model.record(row).value("codvuz"), self.model.record(row).value("z18"))
                for row in range(self.model.rowCount()) if self.model.isDirty(self.model.index(row, column))
            ]
            try:
                save_actual_financing(changes, path=self.path)
            except (ValueError, sqlite3.Error) as exc:
                self.last_save_error = str(exc)
                return False
            self.model.revertAll()
            if self.after_table_save is not None:
                self.after_table_save()
            return True
        return self.model.submitAll()

    def revert(self):
        self.model.revertAll()
        self.model.select()

    def refresh(self):
        self.apply_filter()
        self.view.resizeColumnsToContents()
        self.apply_column_widths()


class MainWindow(QMainWindow):
    def __init__(self, path: Path = DB_PATH):
        super().__init__()
        self.setWindowTitle("СУБД — вариант 9: тематический план НИР")
        self.resize(1450, 820)

        self.path = path
        self.analysis_windows = {}
        self.shared_filters = SharedFilters(path, self)
        self.db = self.connect_db(path)
        self.tabs = QTabWidget()
        self.pages: dict[str, TablePage] = {}
        for name in ("nir", "financing", "vuz", "grnti"):
            page = TablePage(self.db, name, self.shared_filters)
            self.pages[name] = page
            self.tabs.addTab(page, TABLE_CONFIG[name]["title"])
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.setUsesScrollButtons(True)
        # Верхняя панель: общий фильтр/сброс сгруппированы, сортировка НИР
        # вынесена из нижних кнопок, поиск — строка с выбором столбца.
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(7, 7, 7, 7)
        central_layout.setSpacing(6)
        toolbar = QHBoxLayout()
        self.filter_button = QPushButton("⚲  Фильтр")
        self.filter_button.clicked.connect(self.open_shared_filter)
        self.reset_filter_button = QPushButton("Сбросить фильтр")
        self.reset_filter_button.clicked.connect(self.shared_filters.reset)
        self.sort_label = QLabel("Сортировка НИР:")
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("По вузу ↑", "university_asc")
        self.sort_combo.addItem("По вузу ↓", "university_desc")
        self.sort_combo.addItem("По рег. номеру ↑", "number_asc")
        self.sort_combo.addItem("По названию ↑", "title_asc")
        self.sort_combo.setToolTip("Для других вариантов сортировки можно нажать заголовок столбца таблицы.")
        self.sort_combo.currentIndexChanged.connect(self.sort_current_nir)
        self.export_all_button = QPushButton("Выгрузить все НИР в Excel")
        self.export_all_button.clicked.connect(self.export_all_nir)
        toolbar.addWidget(self.filter_button)
        toolbar.addWidget(self.reset_filter_button)
        toolbar.addSpacing(14)
        toolbar.addWidget(self.sort_label)
        toolbar.addWidget(self.sort_combo)
        toolbar.addStretch(1)
        toolbar.addWidget(self.export_all_button)
        central_layout.addLayout(toolbar)

        search_row = QHBoxLayout()
        self.search_field_label = QLabel("🔍 Поиск:")

        # Поле выбора столбца и ввод текста визуально образуют одну строку.
        # Рамка рисуется только вокруг общего контейнера, а не вокруг двух
        # отдельных контролов — как у единого поисковика.
        self.search_box = QFrame()
        self.search_box.setObjectName("searchBox")
        self.search_box.setStyleSheet("""
            QFrame#searchBox {
                border: 1px solid palette(mid);
                border-radius: 6px;
                background: palette(base);
            }
            QComboBox#searchFieldSelector {
                border: none;
                background: transparent;
                padding: 3px 21px 3px 6px;
            }
            QComboBox#searchFieldSelector::drop-down {
                border: none;
                width: 18px;
            }
            QLineEdit#searchInput {
                border: none;
                border-radius: 0;
                background: transparent;
                padding: 4px 5px;
            }
        """)
        search_box_layout = QHBoxLayout(self.search_box)
        search_box_layout.setContentsMargins(5, 1, 5, 1)
        search_box_layout.setSpacing(4)

        self.search_field_selector = QComboBox()
        self.search_field_selector.setObjectName("searchFieldSelector")
        self.search_field_selector.setToolTip("Выберите столбец таблицы для поиска")
        self.search_field_selector.currentIndexChanged.connect(self.change_search_field)
        self.search_field_selector.currentTextChanged.connect(self.resize_search_field_selector)
        self.search_field_selector.setFixedWidth(110)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("searchInput")
        self.search_input.setPlaceholderText("Искать в текущей таблице…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.change_search_text)

        search_separator = QFrame()
        search_separator.setFrameShape(QFrame.Shape.VLine)
        search_separator.setStyleSheet("color: palette(mid);")
        search_box_layout.addWidget(self.search_field_selector)
        search_box_layout.addWidget(search_separator)
        search_box_layout.addWidget(self.search_input, 1)

        self.shared_summary = QLabel()
        self.shared_summary.setWordWrap(True)
        self.shared_summary.setMaximumWidth(360)
        self.shared_summary.setToolTip("Этот фильтр действует во всех вкладках и аналитических отчётах")
        search_row.addWidget(self.search_field_label)
        search_row.addWidget(self.search_box, 2)
        search_row.addWidget(self.shared_summary, 1)
        central_layout.addLayout(search_row)
        central_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.shared_filters.changed.connect(self.shared_filter_changed)
        self.shared_filter_changed()
        for page in self.pages.values():
            page.open_unified_filter = self.open_shared_filter
            page.search_edit.textChanged.connect(
                lambda _text, current_page=page: self.sync_search_if_current(current_page))
            page.search_field.currentIndexChanged.connect(
                lambda _index, current_page=page: self.sync_search_if_current(current_page))
        self.pages["nir"].before_nir_change = self.save_financing_if_dirty
        self.pages["nir"].reload_after_nir_change = self.reload_database_views
        self.pages["financing"].after_table_save = self.reload_database_views
        self.pages["nir"].nir_changed.connect(self.on_nir_changed)
        self.pages["financing"].finance_report_requested.connect(lambda: self.open_analysis("financing"))
        self.pages["financing"].finance_check_requested.connect(self.check_financing_totals)

        self.build_menu()
        self.statusBar().showMessage("Готово")

        # Автоматическая проверка — по замечанию преподавателя.
        self.auto_check(show_dialog=False)

    @staticmethod
    def connect_db(path: Path) -> QSqlDatabase:
        db = QSqlDatabase.addDatabase("QSQLITE", f"nir-{uuid4().hex}")
        # Ждем до 30 секунд, если SQLite кратковременно занят другой операцией.
        db.setConnectOptions("QSQLITE_BUSY_TIMEOUT=30000")
        db.setDatabaseName(str(path))
        if not db.open():
            raise RuntimeError(f"Не удалось открыть БД: {path}\n{db.lastError().text()}")

        MainWindow.configure_db(db)
        return db

    @staticmethod
    def configure_db(db):
        # WAL снимает конфликт «Qt читает / sqlite3 пишет», который и давал
        # ошибку 'database is locked' в первой версии проекта.
        q = QSqlQuery(db)
        for pragma in (
            "PRAGMA busy_timeout = 30000",
            "PRAGMA foreign_keys = ON",
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
        ):
            if not q.exec(pragma):
                raise RuntimeError(f"Не удалось выполнить {pragma}: {q.lastError().text()}")
            q.finish()

    def reload_database_views(self):
        # QSqlTableModel держит несколько активных SELECT на одном соединении.
        # Переподключение освобождает их снимок после записи через sqlite3.
        self.db.close()
        if not self.db.open():
            raise RuntimeError(self.db.lastError().text())
        self.configure_db(self.db)
        for page in self.pages.values():
            page.refresh()
        for dialog in self.analysis_windows.values():
            if dialog.isVisible():
                dialog.reload()

    def build_menu(self):
        # На macOS меню всегда остаётся в хеддере самого окна.
        bar = self.menuBar()
        bar.setNativeMenuBar(False)
        bar.setStyleSheet("QMenuBar { padding: 5px; } QMenuBar::item { padding: 7px 12px; }")

        menu_data = bar.addMenu("Данные")
        self.data_action_group = QActionGroup(self)
        self.data_actions = {}
        for name, page in self.pages.items():
            action = menu_data.addAction(TABLE_CONFIG[name]["title"])
            action.setCheckable(True)
            self.data_action_group.addAction(action)
            action.triggered.connect(lambda checked=False, widget=page: self.tabs.setCurrentWidget(widget))
            self.data_actions[name] = action

        menu_data.addSeparator()
        menu_data.addAction("Выгрузить все НИР в Excel…").triggered.connect(self.export_all_nir)
        menu_analysis = bar.addMenu("Анализ")
        self.analysis_actions = {}
        for kind, config in REPORTS.items():
            action = menu_analysis.addAction(config["title"])
            action.triggered.connect(lambda checked=False, name=kind: self.open_analysis(name))
            self.analysis_actions[kind] = action

        menu_options = bar.addMenu("Опции")
        a_save = menu_options.addAction("Сохранить изменения")
        a_save.setShortcut("Ctrl+S")
        a_save.triggered.connect(self.save_all)
        a_revert = menu_options.addAction("Отменить несохраненные изменения")
        a_revert.triggered.connect(self.revert_all)
        menu_options.addSeparator()
        a_recalc = menu_options.addAction("Пересчитать финансирование")
        a_recalc.triggered.connect(self.recalculate_financing)
        a_check = menu_options.addAction("Автоматическая проверка данных")
        a_check.triggered.connect(lambda: self.auto_check(show_dialog=True))
        menu_options.addSeparator()
        a_edit = menu_options.addAction("Редактировать НИР")
        a_edit.triggered.connect(self.edit_nir)
        a_log = menu_options.addAction("Журнал изменений")
        a_log.triggered.connect(self.show_change_log)

        menu_help = bar.addMenu("Помощь")
        menu_help.addAction("Справка").triggered.connect(self.show_help)
        menu_help.addAction("О программе").triggered.connect(self.show_about)
        bar.addAction("Выход").triggered.connect(self.close)
        self.tabs.currentChanged.connect(self.update_current_table)
        self.update_current_table()

    def open_shared_filter(self):
        dialog = UnifiedFilterDialog(self.shared_filters, self)
        dialog.exec()
        dialog.deleteLater()

    def sync_search_if_current(self, page):
        if self.tabs.currentWidget() is page:
            self.sync_search_controls()

    def sync_search_controls(self):
        # Поиск текущей вкладки показываем в одной строке; при переключении
        # вкладок отдельные запросы сохраняются, как и раньше.
        page = self.tabs.currentWidget()
        for widget in (self.search_field_selector, self.search_input):
            widget.blockSignals(True)
        try:
            self.search_field_selector.clear()
            for index in range(page.search_field.count()):
                self.search_field_selector.addItem(
                    page.search_field.itemText(index), page.search_field.itemData(index))
            self.search_field_selector.setCurrentIndex(
                self.search_field_selector.findData(page.search_field.currentData()))
            self.search_input.setText(page.search_edit.text())
            self.search_input.setPlaceholderText(f"Искать в таблице «{TABLE_CONFIG[page.table_name]['title']}»…")
        finally:
            for widget in (self.search_field_selector, self.search_input):
                widget.blockSignals(False)
        self.resize_search_field_selector()

    def resize_search_field_selector(self, *_):
        # Короткий "Код вуза" не должен занимать столько же места, сколько
        # длинные наименования столбцов. Для длинных значений есть ограничение.
        current_text = self.search_field_selector.currentText()
        text_width = self.search_field_selector.fontMetrics().horizontalAdvance(current_text)
        self.search_field_selector.setFixedWidth(max(95, min(235, text_width + 43)))
        self.search_field_selector.setToolTip(current_text or "Выберите столбец для поиска")

    def change_search_field(self, _index):
        page = self.tabs.currentWidget()
        index = page.search_field.findData(self.search_field_selector.currentData())
        if index >= 0:
            page.search_field.setCurrentIndex(index)

    def change_search_text(self, text):
        self.tabs.currentWidget().search_edit.setText(text)

    def sort_current_nir(self, _index):
        if self.tabs.currentWidget() is not self.pages["nir"]:
            return
        page = self.pages["nir"]
        option = self.sort_combo.currentData()
        if option in ("university_asc", "university_desc"):
            order = (Qt.SortOrder.DescendingOrder if option == "university_desc"
                     else Qt.SortOrder.AscendingOrder)
            page.apply_sort(order)
        elif option == "number_asc":
            page.view.sortByColumn(page.field_names.index("rnw"), Qt.SortOrder.AscendingOrder)
        elif option == "title_asc":
            page.view.sortByColumn(page.field_names.index("f2"), Qt.SortOrder.AscendingOrder)

    def shared_filter_changed(self):
        self.shared_summary.setText(self.shared_filters.describe())
        for page in self.pages.values():
            page.apply_filter()
        for dialog in self.analysis_windows.values():
            dialog.reload()

    def export_all_nir(self):
        if not self.save_financing_if_dirty():
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Выгрузить все НИР", dated_excel_filename("Все_НИР.xlsx"), "Книга Excel (*.xlsx)")
        if not filename:
            return
        filename = dated_excel_filename(filename)
        if not self.confirm_dated_overwrite(filename):
            return
        try:
            count = write_all_nir_excel(filename, db_path=self.path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Ошибка экспорта", str(exc))
            return
        QMessageBox.information(self, "Экспорт завершён",
                                f"В Excel сохранено {count} НИР. Активные фильтры не применялись.\n{filename}")

    def confirm_dated_overwrite(self, filename):
        if not Path(filename).exists():
            return True
        return QMessageBox.question(
            self, "Перезаписать Excel-файл?",
            f"Файл уже существует:\n{filename}\n\nПерезаписать его?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def update_current_table(self, *_):
        for name, page in self.pages.items():
            current = self.tabs.currentWidget() is page
            self.data_actions[name].setChecked(current)
        is_nir = self.tabs.currentWidget() is self.pages["nir"]
        self.sort_label.setVisible(is_nir)
        self.sort_combo.setVisible(is_nir)
        self.sync_search_controls()

    def save_all(self):
        failed = []
        for name, page in self.pages.items():
            if TABLE_CONFIG[name]["editable"] and not page.save():
                failed.append(name)
        if failed:
            QMessageBox.critical(self, "Ошибка сохранения", "Не удалось сохранить: " + ", ".join(failed))
            return False
        if not self.recalculate_financing(show_message=False):
            return False
        self.auto_check(show_dialog=False)
        self.statusBar().showMessage("Изменения сохранены, финансирование пересчитано, проверка выполнена.", 7000)
        return True

    def save_financing_if_dirty(self):
        page = self.pages["financing"]
        if page.model.isDirty() and not page.save():
            QMessageBox.warning(self, "Ошибка сохранения финансирования", page.last_save_error)
            return False
        return True

    def on_nir_changed(self, _row_id):
        self.auto_check(show_dialog=False)

    def edit_nir(self):
        self.tabs.setCurrentWidget(self.pages["nir"])
        if not self.pages["nir"].view.currentIndex().isValid():
            QMessageBox.information(self, "Редактировать НИР", "Выберите НИР в таблице и нажмите «Изменить».")
            return
        self.pages["nir"].edit_nir()

    def show_change_log(self):
        if not self.save_financing_if_dirty():
            return
        ChangeLogDialog(self.path, self, on_change=self.reload_database_views).exec()

    def revert_all(self):
        for page in self.pages.values():
            page.revert()
        self.statusBar().showMessage("Несохраненные изменения отменены.", 5000)

    def recalculate_financing(self, show_message=True):
        # WAL + busy_timeout позволяют безопасно пересчитывать таблицу через
        # отдельное sqlite3-соединение при открытом интерфейсе Qt.
        try:
            if not self.save_financing_if_dirty():
                return False
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                rebuild_financing(conn)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            QMessageBox.critical(
                self,
                "Ошибка базы данных",
                "Не удалось пересчитать финансирование.\n\n"
                f"{exc}\n\n"
                "Закройте вторую копию программы и программы просмотра SQLite, "
                "если они открыты, затем повторите.",
            )
            return False
        self.reload_database_views()
        if show_message:
            QMessageBox.information(self, "Пересчет", "Таблица финансирования пересчитана по текущим данным НИР.")
        return True

    def auto_check(self, show_dialog=True):
        try:
            # В интерфейсе проверка только читает БД. Журнал уже формируется
            # при первичном создании базы; лишняя запись здесь не нужна.
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                issues = run_validation(conn, write_log=False)
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            if show_dialog:
                QMessageBox.critical(self, "Ошибка проверки", str(exc))
            self.statusBar().showMessage(f"Проверка не выполнена: {exc}", 7000)
            return
        errors = sum(1 for i in issues if i[0] == "ERROR")
        warnings = sum(1 for i in issues if i[0] == "WARNING")
        self.statusBar().showMessage(f"Автопроверка: ошибок — {errors}, предупреждений — {warnings}")
        if not show_dialog:
            return
        if not issues:
            QMessageBox.information(self, "Проверка данных", "Ошибок и предупреждений не найдено.")
            return
        lines = [f"{sev}: {ref} — {msg}" for sev, _table, ref, msg in issues[:30]]
        if len(issues) > 30:
            lines.append(f"... еще {len(issues) - 30} сообщений")
        QMessageBox.warning(
            self,
            "Проверка данных",
            f"Найдено ошибок: {errors}; предупреждений: {warnings}.\n\n" + "\n".join(lines),
        )

    def check_financing_totals(self):
        if not self.save_financing_if_dirty():
            return
        try:
            from financial_checks import inspect_financing
            result = inspect_financing(self.path)
        except sqlite3.Error as exc:
            QMessageBox.warning(self, "Проверка финансирования", str(exc))
            return
        message = (f"Всего НИР: {result['works']}\n"
                   f"План по всем НИР: {result['planned']:,} руб.\n"
                   f"Факт по вузам: {result['actual']:,} руб.\n"
                   f"НИР без корректной связи с вузом: {result['unknown_works']}\n\n")
        if result["issues"]:
            QMessageBox.warning(self, "Найдены расхождения", message + "\n".join(result["issues"][:30]))
        else:
            QMessageBox.information(self, "Проверка финансирования", message + "План и количество НИР по каждому вузу совпадают с исходной таблицей.")

    def open_analysis(self, kind):
        if not self.save_financing_if_dirty():
            return
        dialog = self.analysis_windows.get(kind)
        if dialog is None:
            dialog = AnalysisDialog(self.path, kind, self,
                                    before_refresh=self.save_financing_if_dirty,
                                    shared_filters=self.shared_filters,
                                    open_shared_filter=self.open_shared_filter)
            self.analysis_windows[kind] = dialog
        else:
            dialog.reload()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def show_help(self):
        QMessageBox.information(
            self,
            "Справка",
            "ЛР №1, вариант 9.\n\n"
            "• Таблицы переключаются вкладками сверху или через меню «Данные» внутри окна.\n"
            "• VUZ и ГРНТИ — только просмотр.\n"
            "• RNW сохраняется без пробелов.\n"
            "• НИР добавляются и изменяются через формы; удаление требует подтверждения.\n"
            "• Рег. номер уникален внутри вуза; 07 и 7 — разные значения.\n"
            "• ГРНТИ вводится по маске, второй код необязателен.\n"
            "• Сортировка НИР находится наверху, рядом с фильтром.\n"
            "• Кнопка «Фильтр» задаёт единый географический и тематический фильтр для всех таблиц и отчётов.\n"
            "• Если сохранённая НИР скрыта фильтром, фильтр сбрасывается.\n"
            "• Поиск — строка над таблицами: сначала выберите поле, затем введите текст.\n"
            "• Рег. номер вводится вручную; формат имени руководителя свободный, должность обязательна.\n"
            "• Журнал изменений и пересчёт доступны в меню «Опции».\n"
            "• Меню «Анализ» открывает четыре отчёта с общим фильтром и экспортом в Excel.\n"
            "• Сводка финансирования группируется по округам, субъектам, городам или вузам.\n"
            "• В финансировании показаны сумма плана, факта, количество НИР и процент исполнения.\n"
            "• В отчётах по вузам и характеру строка «Итого» закреплена внизу.\n"
            "• Денежные суммы хранятся целыми рублями.\n"
            "• После сохранения выполняются пересчет финансирования и автоматическая проверка.",
        )

    def show_about(self):
        QMessageBox.information(
            self,
            "О программе",
            "Учебная оболочка информационной системы\n"
            "«Управление научными исследованиями по тематическому плану»\n"
            "Вариант №9, НИУ «МЭИ», 2026.",
        )

    def closeEvent(self, event):
        if not any(page.model.isDirty() for page in self.pages.values()):
            self.db.close()
            for dialog in self.analysis_windows.values():
                dialog.close()
            event.accept()
            return
        reply = QMessageBox.question(
            self,
            "Выход",
            "Сохранить изменения перед выходом?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            event.ignore()
            return
        if reply == QMessageBox.StandardButton.Yes:
            if not self.save_all():
                event.ignore()
                return
        self.db.close()
        for dialog in self.analysis_windows.values():
            dialog.close()
        event.accept()


def main():
    create_database(force=False)
    prepare_database()
    app = QApplication(sys.argv)
    app.setApplicationName("СУБД ЛР1 — вариант 9")
    try:
        window = MainWindow()
    except Exception as exc:
        QMessageBox.critical(None, "Ошибка запуска", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
