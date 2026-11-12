"""Окна аналитики: просмотр, сортировка, экспорт и закреплённые итоги."""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from PyQt6.QtCore import QAbstractTableModel, QEvent, QModelIndex, Qt, QTimer
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication, QDialog, QHeaderView,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableView, QVBoxLayout,
)

from analysis_reports import REPORTS, build_report
from report_export import save_workbook


TEXT_COLUMN_LIMITS = {"vuz": 220, "rubric": 360, "character": 250}


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
            if value is None:
                return "—"
            return f"{value:,}".replace(",", " ") if isinstance(value, int) else str(value)
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.TextAlignmentRole and isinstance(value, int):
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


class AnalysisDialog(QDialog):
    def __init__(self, path: Path, kind: str, parent=None, *, geography=None,
                 rubric=None, filter_description="Без фильтра"):
        super().__init__(parent)
        self.path, self.kind = path, kind
        self.geography, self.rubric = dict(geography or {}), rubric
        self.filter_description = filter_description
        self.result = None
        self.config = REPORTS[kind]
        self.setWindowTitle(self.config["title"])
        self.setMinimumSize(360, 220)
        layout = QVBoxLayout(self)
        title = QLabel(self.config["title"])
        title.setStyleSheet("font-size: 17px; font-weight: bold;")
        title.setWordWrap(True)
        layout.addWidget(title)
        hint = QLabel(self.config["hint"])
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableView()
        self.model = ReportModel(self.config["columns"], self)
        self.table.setModel(self.model)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.horizontalHeader().setMinimumSectionSize(36)
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
        layout.addWidget(self.footer)
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
        self.export_button = QPushButton("Сохранить отчёт")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_report)
        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.close)
        controls.addWidget(self.export_button)
        controls.addStretch()
        controls.addWidget(self.close_button)
        layout.addLayout(controls)
        self.ensurePolished()
        if self.reload():
            self.fit_window()

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.sync_timer.start(0)
        return super().eventFilter(watched, event)

    def sync_footer(self, *_):
        if not self.footer_model.rowCount():
            return
        for column in range(self.model.columnCount()):
            self.footer.setColumnWidth(column, self.table.columnWidth(column))
        self.footer.horizontalHeader().setOffset(self.table.horizontalHeader().offset())
        self.footer.viewport().update()

    def fit_columns(self):
        self.table.resizeColumnsToContents()
        for column, (field, _) in enumerate(self.model.columns):
            width = self.table.columnWidth(column)
            if field in TEXT_COLUMN_LIMITS:
                width = min(width, TEXT_COLUMN_LIMITS[field])
            if self.footer_model.rowCount():
                index = self.footer_model.index(0, column)
                text = self.footer_model.data(index)
                metrics = QFontMetrics(self.footer_model.data(index, Qt.ItemDataRole.FontRole))
                width = max(width, metrics.horizontalAdvance(text) + 16)
            self.table.setColumnWidth(column, width)
        self.table.resizeRowsToContents()

    def fit_window(self):
        layout = self.layout()
        margins = layout.contentsMargins()
        frame = 2 * self.table.frameWidth()
        table_width = self.table.horizontalHeader().length() + frame
        table_width += self.table.verticalScrollBar().sizeHint().width()
        table_height = self.table.horizontalHeader().sizeHint().height() + self.table.verticalHeader().length()
        width = max(410, table_width + margins.left() + margins.right())
        if self.kind == "character":
            scrollbar = self.table.horizontalScrollBar().sizeHint().height()
            self.table.setMinimumHeight(table_height + frame + scrollbar)
            height = max(layout.totalMinimumSize().height(), layout.minimumHeightForWidth(width))
            self.setMinimumHeight(height)
        else:
            height = max(265, min(600, table_height + 165))
        self.resize(width, height)

    def reload(self):
        try:
            result = build_report(self.kind, self.geography, rubric=self.rubric, path=self.path)
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Отчёт не построен", str(exc))
            return False
        self.result = result
        self.export_button.setEnabled(True)
        self.model.replace(result.rows)
        self.footer_model.replace([result.totals] if result.totals is not None else [])
        self.footer.setVisible(result.totals is not None)
        self.fit_columns()
        if self.footer_model.rowCount():
            self.footer.setRowHeight(0, 32)
        units = {"vuz": "Вузов", "grnti": "Рубрик", "character": "Характеров"}[self.kind]
        self.filter_summary.setText(
            f"{self.filter_description} · {units}: {len(result.rows)} · НИР в выборке: {result.source_count}"
        )
        self.sync_timer.start(0)
        return True

    def export_report(self):
        columns = self.config["columns"]
        rows = [tuple(row.get(field) for field, _ in columns) for row in self.model.rows]
        totals = tuple(self.result.totals.get(field) for field, _ in columns) if self.result.totals else ()
        title = self.config["title"]
        report_date = date.today()
        details = ("Вид документа: аналитический отчёт",
                   f"Дата формирования: {report_date:%d.%m.%Y}",
                   *self.filter_summary.text().split(" · "))
        save_workbook(self, self.kind, f"Отчёт — {title}", document_date=report_date,
                      title=title, details=details, columns=[label for _, label in columns],
                      rows=rows, totals=totals)
