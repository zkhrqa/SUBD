"""Окна аналитики: просмотр, сортировка, независимые фильтры и закреплённые итоги."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from PyQt6.QtCore import QAbstractTableModel, QEvent, QModelIndex, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHeaderView,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableView, QVBoxLayout, QFileDialog,
)

from analysis_reports import REPORTS, FINANCING_GROUPS, build_report
from geography_filters import GEOGRAPHY_FIELDS, populate_geography


class ReportModel(QAbstractTableModel):
    def __init__(self, columns, parent=None, *, totals=False):
        super().__init__(parent)
        self.columns, self.rows, self.totals = columns, [], totals
        self.sort_column, self.sort_order = 0, Qt.SortOrder.AscendingOrder

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        field = self.columns[index.column()][0]
        row = self.rows[index.row()]
        value = row.get(field, "")
        if role == Qt.ItemDataRole.DisplayRole:
            if field == "execution":
                return "—" if value is None else f"{value:.2f} %".replace(".", ",")
            if value is None:
                return "—"
            return f"{value:,}".replace(",", " ") if isinstance(value, int) else str(value)
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.TextAlignmentRole and isinstance(value, (int, float)):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.FontRole and self.totals:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.BackgroundRole and self.totals:
            return QApplication.palette().alternateBase()
        if role == Qt.ItemDataRole.ToolTipRole:
            if field == "vuz":
                return row.get("full_name") or row.get("vuz")
            if field == "actual" and value is None:
                return "Вуз не определён; фактическое финансирование неизвестно."
            if field == "execution" and value is None:
                return "Процент не вычисляется: нулевой план или отсутствует факт."
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.columns[section][1]
        return None

    def flags(self, index):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable if index.isValid() else Qt.ItemFlag.NoItemFlags

    def replace(self, rows):
        self.beginResetModel()
        self.rows = [dict(row) for row in rows]
        self._sort_rows()
        self.endResetModel()

    def _sort_rows(self):
        if self.totals:
            return
        field = self.columns[self.sort_column][0]
        known = [r for r in self.rows if r.get(field) is not None]
        unknown = [r for r in self.rows if r.get(field) is None]
        def key(row):
            value = row.get(field, "")
            return value.casefold() if isinstance(value, str) else value
        self.rows = sorted(known, key=key, reverse=self.sort_order == Qt.SortOrder.DescendingOrder) + unknown

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if not 0 <= column < len(self.columns):
            return
        self.beginResetModel()
        self.sort_column, self.sort_order = column, order
        self._sort_rows()
        self.endResetModel()


class GeographyDialog(QDialog):
    def __init__(self, path: Path, selected: dict, parent=None):
        super().__init__(parent)
        self.path, self.geography = path, {}
        self.setWindowTitle("Фильтр аналитического отчёта")
        self.resize(650, 270)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for field, label in GEOGRAPHY_FIELDS:
            combo = QComboBox()
            combo.setMinimumContentsLength(24)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.geography[field] = combo
            combo.currentIndexChanged.connect(lambda _, name=field: self.changed(name))
            form.addRow(label, combo)
        populate_geography(self.geography, path, selected)
        layout.addLayout(form)
        hint = QLabel("Можно начать выбор с любого уровня. Условия применяются только к этому отчёту.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.reset_button = buttons.addButton("Сбросить", QDialogButtonBox.ButtonRole.ResetRole)
        self.reset_button.clicked.connect(lambda: populate_geography(self.geography, self.path, {}))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def changed(self, field):
        populate_geography(self.geography, self.path, after=field)

    def values(self):
        return {field: combo.currentData() for field, combo in self.geography.items()
                if combo.currentData() is not None}


class AnalysisDialog(QDialog):
    def __init__(self, path: Path, kind: str, parent=None, before_refresh=None,
                 shared_filters=None, open_shared_filter=None):
        super().__init__(parent)
        self.path, self.kind, self.before_refresh = path, kind, before_refresh
        self.shared_filters = shared_filters
        self.open_shared_filter = open_shared_filter
        self.geography = {}
        self.grouping = "subject"
        self.result = None
        self.config = REPORTS[kind]
        self.setWindowTitle(self.config["title"])
        self.resize(1100, 700)
        self.setMinimumSize(740, 430)
        layout = QVBoxLayout(self)
        title = QLabel(self.config["title"])
        title.setStyleSheet("font-size: 17px; font-weight: bold;")
        title.setWordWrap(True)
        layout.addWidget(title)
        hint = QLabel(self.config["hint"])
        hint.setWordWrap(True)
        layout.addWidget(hint)
        if kind == "financing":
            grouping_row = QHBoxLayout()
            grouping_row.addWidget(QLabel("Группировка:"))
            self.grouping_combo = QComboBox()
            for key, caption in FINANCING_GROUPS:
                self.grouping_combo.addItem(caption, key)
            self.grouping_combo.setCurrentIndex(self.grouping_combo.findData(self.grouping))
            self.grouping_combo.currentIndexChanged.connect(self.change_grouping)
            grouping_row.addWidget(self.grouping_combo)
            grouping_row.addStretch()
            layout.addLayout(grouping_row)
        self.table = QTableView()
        self.model = ReportModel(self.config["columns"], self)
        self.table.setModel(self.model)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.horizontalHeader().setMinimumSectionSize(75)
        layout.addWidget(self.table, 1)

        self.footer = QTableView()
        self.footer_model = ReportModel(self.config["columns"], self, totals=True)
        self.footer.setModel(self.footer_model)
        self.footer.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.footer.setSelectionMode(QTableView.SelectionMode.NoSelection)
        self.footer.horizontalHeader().hide()
        self.footer.verticalHeader().hide()
        self.footer.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.footer.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.footer.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.footer.setFixedHeight(34)
        layout.addWidget(self.footer, 0, Qt.AlignmentFlag.AlignLeft)
        self.sync_timer = QTimer(self)
        self.sync_timer.setSingleShot(True)
        self.sync_timer.timeout.connect(self.sync_footer)
        self.table.viewport().installEventFilter(self)
        self.table.horizontalHeader().sectionResized.connect(self.sync_footer)
        self.table.horizontalScrollBar().valueChanged.connect(self.sync_footer)

        self.filter_summary = QLabel()
        self.filter_summary.setWordWrap(True)
        layout.addWidget(self.filter_summary)
        controls = QHBoxLayout()
        self.filter_button, self.refresh_button = QPushButton("Общий фильтр"), QPushButton("Обновить")
        self.export_button = QPushButton("Сохранить в Excel")
        self.close_button = QPushButton("Закрыть")
        self.filter_button.clicked.connect(self.open_filter)
        self.refresh_button.clicked.connect(lambda: self.reload())
        self.export_button.clicked.connect(self.save_excel)
        self.close_button.clicked.connect(self.close)
        controls.addWidget(self.filter_button)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.export_button)
        controls.addStretch()
        controls.addWidget(self.close_button)
        layout.addLayout(controls)
        self.reload()

    def change_grouping(self, _=None):
        selected = self.grouping_combo.currentData()
        old = self.grouping
        self.grouping = selected
        if not self.reload():
            self.grouping = old
            blocked = self.grouping_combo.blockSignals(True)
            self.grouping_combo.setCurrentIndex(self.grouping_combo.findData(old))
            self.grouping_combo.blockSignals(blocked)

    def save_excel(self):
        """Экспорт только результатов текущего отчёта, без изменений БД."""
        if self.result is None:
            return
        suggested = "Отчёт_" + self.kind + ".xlsx"
        filename, _ = QFileDialog.getSaveFileName(self, "Сохранить аналитический отчёт",
                                                   suggested, "Excel (*.xlsx)")
        if not filename:
            return
        if not filename.lower().endswith(".xlsx"):
            filename += ".xlsx"
        try:
            from report_export import write_excel_report
            write_excel_report(filename, self.config["title"], self.config["columns"],
                               self.model.rows, self.result.totals,
                               geography=self.geography,
                               rubric=self.shared_filters.rubric if self.shared_filters else None,
                               grouping=dict(FINANCING_GROUPS)[self.grouping] if self.kind == "financing" else None,
                               source_count=self.result.source_count)
        except (OSError, ValueError, PermissionError) as exc:
            QMessageBox.warning(self, "Ошибка экспорта", f"Не удалось сохранить файл: {exc}")
            return
        QMessageBox.information(self, "Экспорт", "Отчёт сохранён в Excel.")

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.sync_timer.start(0)
        return super().eventFilter(watched, event)

    def sync_footer(self, *_):
        if not self.footer_model.rowCount():
            return
        self.footer.setFixedWidth(self.table.viewport().width() + 2 * self.footer.frameWidth())
        for column in range(self.model.columnCount()):
            self.footer.setColumnWidth(column, self.table.columnWidth(column))
        self.footer.horizontalHeader().setOffset(self.table.horizontalHeader().offset())
        self.footer.viewport().update()

    def reload(self, geography=None):
        if self.before_refresh is not None and not self.before_refresh():
            return False
        selected = (dict(self.shared_filters.geography) if self.shared_filters is not None
                    else self.geography if geography is None else geography)
        rubric = self.shared_filters.rubric if self.shared_filters is not None else None
        try:
            result = build_report(self.kind, selected, path=self.path,
                                  group_by=self.grouping, rubric=rubric)
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Отчёт не обновлён", str(exc))
            return False
        self.geography, self.result = dict(selected), result
        self.model.replace(result.rows)
        self.footer_model.replace([result.totals] if result.totals is not None else [])
        self.footer.setVisible(result.totals is not None)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.resizeRowsToContents()
        for column in range(self.footer_model.columnCount()):
            if result.totals is not None:
                # Итоги также участвуют в подборе ширины числовых колонок.
                text = self.footer_model.data(self.footer_model.index(0, column))
                width = self.footer.fontMetrics().horizontalAdvance(text) + 32
                if column != 1:
                    self.table.setColumnWidth(column, max(width, self.table.columnWidth(column)))
        self.footer.setRowHeight(0, 32)
        units = {"vuz": "Вузов", "grnti": "Рубрик", "character": "Характеров", "financing": "Групп"}[self.kind]
        description = (self.shared_filters.describe() if self.shared_filters is not None
                       else " · ".join(f"{dict(GEOGRAPHY_FIELDS)[field]}: {value}"
                                       for field, value in self.geography.items() if value is not None))
        status = f"Общий фильтр: {description}" if description and description != "Без фильтра" else "Без фильтра"
        self.filter_summary.setText(f"{status} · {units}: {len(result.rows)} · НИР в выборке: {result.source_count}")
        self.sync_timer.start(0)
        return True

    def open_filter(self):
        if self.open_shared_filter is not None:
            self.open_shared_filter()
        else:
            # Запасной вариант для самостоятельного открытия отчёта вне главного окна.
            dialog = GeographyDialog(self.path, self.geography, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.reload(dialog.values())
            dialog.deleteLater()
