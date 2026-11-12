from __future__ import annotations

from contextlib import closing
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
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from database import DB_PATH, FUNDING_LOCK_MESSAGE, create_database, funding_started, rebuild_financing, run_validation
from nir_operations import delete_nir, prepare_database, read_rows
from nir_forms import ChangeLogDialog, NirDialog
from geography_filters import GEOGRAPHY_FIELDS, populate_geography
from analysis_forms import AnalysisDialog
from analysis_reports import REPORTS, rubric_condition
from funding_forms import FundingDialog


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
    },
    "grnti": {
        "title": "Рубрики ГРНТИ",
        "headers": {"codrub": "Код рубрики", "rubrika": "Наименование рубрики"},
    },
}


class ControlledTableModel(QSqlTableModel):
    """Таблицы для просмотра. Изменения НИР проходят через формы."""

    def __init__(self, parent=None, db=QSqlDatabase()):
        super().__init__(parent, db)
        self.key_sort = False
        self.key_order = Qt.SortOrder.AscendingOrder
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
        self.key_sort = False
        super().sort(column, order)

    def flags(self, index):
        return super().flags(index) & ~Qt.ItemFlag.ItemIsEditable

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.EditRole:
            return False
        return super().setData(index, value, role)


class TablePage(QWidget):
    nir_changed = pyqtSignal(int)

    def __init__(self, db: QSqlDatabase, table_name: str, parent=None):
        super().__init__(parent)
        self.table_name = table_name
        self.path = Path(db.databaseName())
        self.nir_locked = False
        self.reload_after_nir_change = None
        cfg = TABLE_CONFIG[table_name]

        self.model = ControlledTableModel(self, db)
        self.model.unassigned_financing = table_name == "financing"
        self.model.setTable("financing_display" if table_name == "financing" else table_name)
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
        self.view.horizontalHeader().setStretchLastSection(table_name in {"nir", "vuz"})
        self.view.resizeColumnsToContents()
        if table_name == "nir":
            self.view.setColumnHidden(self.field_names.index("id"), True)
            header = self.view.horizontalHeader()
            for position, field in enumerate(("f18", "f7", "f2"), start=len(self.field_names) - 3):
                header.moveSection(header.visualIndex(self.field_names.index(field)), position)
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

        self.editing_filter = False
        self.editing_search = False
        self.search_dialog = QDialog(self)
        self.search_dialog.setWindowTitle(f"Поиск — {cfg['title']}")
        self.search_dialog.resize(540, 180)
        search_dialog_layout = QVBoxLayout(self.search_dialog)
        search_group = QGroupBox("Найти текст в выбранном поле")
        search_layout = QFormLayout(search_group)
        self.search_field = QComboBox()
        for field in self.field_names:
            if field != "id":
                self.search_field.addItem(cfg["headers"].get(field, field), field)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Введите слово или часть текста")
        self.search_edit.textChanged.connect(self.filter_control_changed)
        self.search_field.currentIndexChanged.connect(self.filter_control_changed)
        search_layout.addRow("Поле", self.search_field)
        search_layout.addRow("Содержит", self.search_edit)
        search_dialog_layout.addWidget(search_group)
        search_dialog_layout.addWidget(QLabel("Поиск выполняется среди строк, подходящих под выбранные фильтры."))
        search_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        search_buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Найти")
        search_buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.search_reset_button = search_buttons.addButton("Очистить поиск", QDialogButtonBox.ButtonRole.ResetRole)
        self.search_reset_button.clicked.connect(self.clear_search)
        search_buttons.accepted.connect(self.accept_search)
        search_buttons.rejected.connect(self.search_dialog.reject)
        search_dialog_layout.addWidget(search_buttons)
        self.filter_dialog = QDialog(self)
        self.filter_dialog.setWindowTitle(f"Фильтр — {cfg['title']}")
        self.filter_dialog.resize(650, 380 if table_name == "nir" else 290)
        filter_layout = QVBoxLayout(self.filter_dialog)

        layout = QVBoxLayout(self)
        self.geography: dict[str, QComboBox] = {}
        if table_name in {"nir", "financing", "vuz"}:
            geography_group = QGroupBox("География")
            geography_layout = QFormLayout(geography_group)
            for field, label in GEOGRAPHY_FIELDS:
                combo = QComboBox()
                combo.addItem("Все", None)
                combo.setMinimumContentsLength(12)
                combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                self.geography[field] = combo
                combo.currentIndexChanged.connect(lambda _, name=field: self.geography_changed(name))
                geography_layout.addRow(label, combo)
            self.rebuild_geography()
            filter_layout.addWidget(geography_group)
        if table_name == "nir":
            rubric_group = QGroupBox("ГРНТИ")
            rubric_layout = QFormLayout(rubric_group)
            self.rubric = QComboBox()
            self.rubric.addItem("Все рубрики", None)
            for row in read_rows("SELECT codrub,rubrika FROM grnti ORDER BY codrub", path=self.path):
                code = f"{row['codrub']:02d}"
                self.rubric.addItem(f"{code} — {row['rubrika']}", code)
            self.rubric.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.rubric.setMinimumContentsLength(35)
            self.rubric.currentIndexChanged.connect(self.filter_control_changed)
            rubric_layout.addRow("Рубрика", self.rubric)
            rubric_layout.addRow(QLabel("Рубрика учитывается в любом из двух кодов НИР."))
            filter_layout.addWidget(rubric_group)
        filter_layout.addWidget(QLabel("Условия объединяются. Таблица изменится после нажатия «Применить»."))
        filter_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        filter_buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        filter_buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.filter_reset_button = filter_buttons.addButton("Сбросить", QDialogButtonBox.ButtonRole.ResetRole)
        self.filter_reset_button.clicked.connect(self.clear_filter)
        filter_buttons.accepted.connect(self.accept_filter)
        filter_buttons.rejected.connect(self.filter_dialog.reject)
        filter_layout.addWidget(filter_buttons)
        layout.addWidget(self.view, 1)
        if table_name == "financing":
            self.total_label = QLabel()
            self.total_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.total_label.setStyleSheet("font-weight: bold; padding: 6px;")
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
        self.filter_button = QPushButton("Фильтр")
        self.filter_button.clicked.connect(self.open_filter)
        buttons.addWidget(self.filter_button)
        self.search_button = QPushButton("Поиск")
        self.search_button.clicked.connect(self.open_search)
        buttons.addWidget(self.search_button)
        if table_name == "nir":
            self.key_sort_button = QPushButton("Сортировка по ключу")
            self.key_sort_button.setToolTip("По возрастанию: сначала код вуза, затем рег. номер внутри вуза.")
            self.key_sort_button.clicked.connect(self.apply_sort)
            buttons.addWidget(self.key_sort_button)
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

    def filter_controls(self):
        return [*self.geography.values(), *([self.rubric] if hasattr(self, "rubric") else [])]

    def rebuild_geography(self, values=None, after=None):
        """Каждый список ограничен выбранными значениями верхних уровней."""
        populate_geography(self.geography, self.path, values, after)

    def geography_changed(self, field):
        self.rebuild_geography(after=field)
        self.filter_control_changed()

    def open_filter(self):
        geography = {f: c.currentData() for f, c in self.geography.items()}
        rubric = self.rubric.currentData() if hasattr(self, "rubric") else None
        self.editing_filter = True
        try:
            if self.filter_dialog.exec() != QDialog.DialogCode.Accepted:
                self.rebuild_geography(geography)
                if hasattr(self, "rubric"):
                    blocked = self.rubric.blockSignals(True)
                    self.rubric.setCurrentIndex(max(0, self.rubric.findData(rubric)))
                    self.rubric.blockSignals(blocked)
        finally:
            self.editing_filter = False

    def open_search(self):
        field, text = self.search_field.currentData(), self.search_edit.text()
        self.editing_search = True
        try:
            if self.search_dialog.exec() != QDialog.DialogCode.Accepted:
                for widget in (self.search_field, self.search_edit):
                    widget.blockSignals(True)
                self.search_field.setCurrentIndex(self.search_field.findData(field))
                self.search_edit.setText(text)
                for widget in (self.search_field, self.search_edit):
                    widget.blockSignals(False)
        finally:
            self.editing_search = False

    def filter_control_changed(self, *_):
        if not self.editing_filter and not self.editing_search:
            self.apply_filter()

    def accept_filter(self):
        if self.apply_filter():
            self.filter_dialog.accept()

    def accept_search(self):
        if self.apply_filter():
            self.search_dialog.accept()

    def clear_search(self):
        blocked = self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(blocked)
        self.filter_control_changed()

    def update_filter_summary(self, *_):
        searched = bool(self.search_edit.text().strip())
        filtered = any(c.currentData() is not None for c in self.filter_controls())
        status = ("Фильтр и поиск применены" if filtered and searched else "Поиск применён" if searched
                  else "Фильтр применён" if filtered else "Без фильтра")
        self.filter_summary.setText(f"{status} · Строк: {self.model.rowCount()}")
        details = []
        if searched:
            details.append(f"Поиск — {self.search_field.currentText()}: {self.search_edit.text().strip()}")
        for combo in self.geography.values():
            if combo.currentData() is not None:
                details.append(combo.currentText())
        if hasattr(self, "rubric") and self.rubric.currentData() is not None:
            details.append(f"{self.rubric.currentText()} — в любом из двух кодов")
        self.filter_summary.setToolTip("\n".join(details) if details else "Показаны все записи.")

    def update_financing_total(self, *_):
        total = sum(int(self.model.record(row).value("numworks") or 0)
                    for row in range(self.model.rowCount()))
        suffix = " (по отфильтрованным строкам)" if self.model.filter() else ""
        self.total_label.setText(f"Итого НИР: {total}{suffix}")

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
        geo = [f"{name} = {self.sql_text(combo.currentData())}"
               for name, combo in getattr(self, "geography", {}).items() if combo.currentData() is not None]
        if geo:
            where = " AND ".join(geo)
            conditions.append(f"codvuz IN (SELECT codvuz FROM vuz WHERE {where})")
        if hasattr(self, "rubric") and self.rubric.currentData() is not None:
            conditions.append(rubric_condition("f10", self.sql_text(self.rubric.currentData())))
        self.model.setFilter(" AND ".join(conditions))
        selected = self.model.select()
        self.update_filter_summary()
        return selected

    def clear_filter(self):
        self.rebuild_geography({})
        if hasattr(self, "rubric"):
            blocked = self.rubric.blockSignals(True)
            self.rubric.setCurrentIndex(0)
            self.rubric.blockSignals(blocked)
        self.filter_control_changed()

    def apply_sort(self, *_):
        selected = self.selected_record()
        self.model.key_sort = True
        self.model.key_order = Qt.SortOrder.AscendingOrder
        header = self.view.horizontalHeader()
        header.setSectionsClickable(True)
        blocked = header.blockSignals(True)
        header.setSortIndicator(self.field_names.index("codvuz"), Qt.SortOrder.AscendingOrder)
        header.blockSignals(blocked)
        header.setSortIndicatorShown(True)
        self.model.select()
        if selected is not None:
            self.select_record(selected["id"])

    def update_buttons(self, *_):
        selected = self.view.currentIndex().isValid()
        self.add_button.setEnabled(not self.nir_locked)
        self.edit_button.setEnabled(selected and not self.nir_locked)
        self.delete_button.setEnabled(selected and not self.nir_locked)

    def selected_record(self) -> dict | None:
        index = self.view.currentIndex()
        if not index.isValid():
            return None
        record = self.model.record(index.row())
        row_id = record.value("id")
        rows = read_rows("SELECT * FROM nir WHERE id=?", (row_id,), path=self.path)
        return rows[0] if rows else None

    def ready_for_nir_change(self) -> bool:
        if self.nir_locked:
            QMessageBox.warning(self, "Изменения НИР заблокированы", FUNDING_LOCK_MESSAGE)
            return False
        return True

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
        self.db = self.connect_db(path)
        self.table_stack = QStackedWidget()
        self.pages: dict[str, TablePage] = {}
        for name in ("nir", "financing", "vuz", "grnti"):
            page = TablePage(self.db, name)
            self.pages[name] = page
            self.table_stack.addWidget(page)
        self.setCentralWidget(self.table_stack)
        self.pages["nir"].reload_after_nir_change = self.reload_database_views
        self.pages["nir"].nir_changed.connect(self.on_nir_changed)

        self.build_menu()
        self.update_funding_lock()
        self.statusBar().showMessage("Готово")

        # Автоматическая проверка — по замечанию преподавателя.
        self.auto_check()

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
        self.update_funding_lock()

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
            action.triggered.connect(lambda checked=False, widget=page: self.table_stack.setCurrentWidget(widget))
            self.data_actions[name] = action

        menu_analysis = bar.addMenu("Анализ")
        self.analysis_actions = {}
        for kind, config in REPORTS.items():
            action = menu_analysis.addAction(config["title"])
            action.triggered.connect(lambda checked=False, name=kind: self.open_analysis(name))
            self.analysis_actions[kind] = action

        menu_funding = bar.addMenu("Финансирование")
        menu_funding.addAction("Распределение траншей").triggered.connect(self.open_funding)

        menu_options = bar.addMenu("Опции")
        self.recalculate_action = menu_options.addAction("Пересчитать финансирование")
        self.recalculate_action.triggered.connect(lambda: self.recalculate_financing())
        a_log = menu_options.addAction("Журнал изменений")
        a_log.triggered.connect(self.show_change_log)

        bar.addAction("Выход").triggered.connect(self.close)
        self.table_stack.currentChanged.connect(self.update_current_table)
        self.update_current_table()

    def update_current_table(self, *_):
        for name, page in self.pages.items():
            current = self.table_stack.currentWidget() is page
            self.data_actions[name].setChecked(current)

    def update_funding_lock(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            locked = funding_started(conn)
        page = self.pages["nir"]
        page.nir_locked = locked
        page.update_buttons()
        self.recalculate_action.setEnabled(not locked)
        if locked:
            page.add_button.setToolTip(FUNDING_LOCK_MESSAGE)
            page.edit_button.setToolTip(FUNDING_LOCK_MESSAGE)
            page.delete_button.setToolTip(FUNDING_LOCK_MESSAGE)

    def open_funding(self):
        try:
            dialog = FundingDialog(self.path, self)
        except sqlite3.Error as exc:
            QMessageBox.warning(self, "Финансирование не открыто", str(exc))
            return
        dialog.exec()
        if dialog.has_approved:
            self.reload_database_views()
        else:
            self.update_funding_lock()
        dialog.deleteLater()

    def on_nir_changed(self, _row_id):
        self.auto_check()

    def show_change_log(self):
        ChangeLogDialog(self.path, self).exec()

    def recalculate_financing(self):
        try:
            with closing(sqlite3.connect(self.path, timeout=30)) as conn, conn:
                rebuild_financing(conn)
        except (sqlite3.Error, ValueError) as exc:
            self.update_funding_lock()
            QMessageBox.warning(self, "Не удалось пересчитать финансирование", str(exc))
            return False
        self.reload_database_views()
        QMessageBox.information(self, "Пересчёт", "Таблица финансирования пересчитана по текущим данным НИР.")
        return True

    def auto_check(self):
        try:
            # В интерфейсе проверка только читает БД. Журнал уже формируется
            # при первичном создании базы; лишняя запись здесь не нужна.
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                issues = run_validation(conn, write_log=False)
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            self.statusBar().showMessage(f"Проверка не выполнена: {exc}", 7000)
            return
        errors = sum(1 for i in issues if i[0] == "ERROR")
        warnings = sum(1 for i in issues if i[0] == "WARNING")
        self.statusBar().showMessage(f"Автопроверка: ошибок — {errors}, предупреждений — {warnings}")

    def open_analysis(self, kind):
        nir = self.pages["nir"]
        geography = {field: combo.currentData() for field, combo in nir.geography.items()
                     if combo.currentData() is not None}
        rubric = nir.rubric.currentData()
        conditions = [f"{label} — {nir.geography[field].currentText()}"
                      for field, label in GEOGRAPHY_FIELDS if field in geography]
        if rubric is not None:
            conditions.append(f"ГРНТИ — {nir.rubric.currentText()}")
        description = "Фильтр НИР — " + " · ".join(conditions) if conditions else "Без фильтра"
        dialog = AnalysisDialog(self.path, kind, self, geography=geography,
                                rubric=rubric, filter_description=description)
        if dialog.result is not None:
            dialog.exec()
        dialog.deleteLater()

    def closeEvent(self, event):
        self.db.close()
        super().closeEvent(event)


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
