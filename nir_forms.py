"""Диалог НИР и просмотр журнала изменений."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from PyQt6.QtGui import QRegularExpressionValidator, QColor, QBrush
from PyQt6.QtCore import Qt, QRegularExpression
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QHBoxLayout, QPushButton, QAbstractItemView,
)

from nir_operations import normalize_grnti_input, read_rows, save_nir, undo_nir_change


class NirDialog(QDialog):
    def __init__(self, path: Path, row: dict | None = None, parent=None):
        super().__init__(parent)
        self.path, self.original = path, row
        self.saved_id = None
        self.setWindowTitle("Изменить НИР" if row else "Добавить НИР")
        self.resize(680, 510)
        self.vuz = QComboBox()
        self.vuz.addItem("Выберите вуз…", None)
        for v in read_rows("SELECT codvuz,z2,z1 FROM vuz ORDER BY codvuz", path=path):
            self.vuz.addItem(f"{v['codvuz']} — {v['z2'] or v['z1']}", v["codvuz"])
        self.rnw = QLineEdit()
        self.rnw.setPlaceholderText("Введите номер без пробелов")
        self.rnw.setToolTip("Введите регистрационный номер в формате вуза. Буквы и ведущие нули сохраняются.")
        self.rnw.textEdited.connect(self.strip_number_spaces)
        self.character = QComboBox()
        self.character.addItem("Выберите характер…", None)
        for code, name in (("Ф", "Фундаментальная"), ("П", "Прикладная"), ("Р", "Разработка")):
            self.character.addItem(f"{code} — {name}", code)
        self.title = QTextEdit()
        self.title.setAcceptRichText(False)
        self.title.setMaximumHeight(100)
        self.leader = QLineEdit()
        self.leader.setPlaceholderText("Например, Иванов И.И.; другой формат имени допустим")
        self.position = QLineEdit()
        self.grnti_first, self.grnti_second = QLineEdit(), QLineEdit()
        for edit in (self.grnti_first, self.grnti_second):
            edit.setInputMask("00.00.00;_")
        self.money = QLineEdit("0")
        self.money.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9]{1,19}"), self))
        form = QFormLayout()
        for label, widget in (
            ("Вуз", self.vuz), ("Рег. номер", self.rnw), ("Характер НИР", self.character),
            ("Наименование НИР", self.title), ("Руководитель", self.leader),
            ("Должность руководителя (обязательно)", self.position), ("Первый код ГРНТИ", self.grnti_first),
            ("Второй код ГРНТИ (необязательно)", self.grnti_second),
            ("Плановое финансирование, руб.", self.money),
        ):
            form.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.hide()
        layout.addWidget(self.note)
        layout.addWidget(buttons)
        if row:
            index = self.vuz.findData(row["codvuz"])
            if index < 0:
                self.vuz.addItem(f"{row['codvuz']} — отсутствует в справочнике", row["codvuz"])
                index = self.vuz.count() - 1
            self.vuz.setCurrentIndex(index)
            self.rnw.setText(row["rnw"] or "")
            self.character.setCurrentIndex(self.character.findData(row["f1"]))
            self.title.setPlainText(row["f2"] or "")
            self.leader.setText(row["f6"] or "")
            self.position.setText(row["f7"] or "")
            normalized = normalize_grnti_input(row["f10"] or "")
            codes = normalized.split(";")
            self.grnti_first.setText(codes[0].strip())
            if len(codes) > 1:
                self.grnti_second.setText(codes[1].strip())
            self.money.setText(str(row["f18"]))
            if normalized != (row["f10"] or ""):
                self.note.setText("Исходная запись ГРНТИ: " + (row["f10"] or "") +
                                  ". При сохранении формат будет приведён к XX.XX.XX; YY.YY.YY.")
                self.note.show()

    def strip_number_spaces(self, text: str):
        cleaned = "".join(text.split())
        if text != cleaned:
            cursor = len("".join(text[:self.rnw.cursorPosition()].split()))
            self.rnw.setText(cleaned)
            self.rnw.setCursorPosition(cursor)

    def values(self) -> dict:
        codes = [self.grnti_first.text()]
        if self.grnti_second.text().replace(".", ""):
            codes.append(self.grnti_second.text())
        return {
            "codvuz": self.vuz.currentData(), "rnw": self.rnw.text(),
            "f1": self.character.currentData(), "f2": self.title.toPlainText(),
            "f6": self.leader.text(), "f7": self.position.text(),
            "f10": "; ".join(codes), "f18": self.money.text(),
        }

    def save(self):
        try:
            self.saved_id = save_nir(self.values(), self.original["id"] if self.original else None,
                                     path=self.path, expected=self.original)
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "НИР не сохранена", str(exc))
            return
        self.accept()


class ChangeLogDialog(QDialog):
    """Читаемый журнал, цветовая маркировка и безопасная отмена добавления/удаления."""
    FIELD_NAMES = {
        "id": "ID записи", "codvuz": "Код вуза", "rnw": "Регистрационный номер",
        "f1": "Характер НИР", "z2": "Вуз (кратко)", "f6": "Руководитель (ФИО)",
        "f10": "Коды ГРНТИ", "f2": "Наименование НИР",
        "f7": "Должность руководителя", "f18": "Плановое финансирование, ₽",
        "z18": "Фактическое финансирование, ₽",
    }

    def __init__(self, path: Path, parent=None, on_change=None):
        super().__init__(parent)
        self.path, self.on_change = path, on_change
        self.setWindowTitle("Журнал изменений — история и отмена действий")
        self.resize(1120, 740)
        self.setMinimumSize(740, 480)
        self.rows = []
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["Дата и время", "Действие", "Таблица", "Запись", "Рег. номер / вуз"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((180, 130, 155, 85)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(self.show_details)

        self.heading = QLabel("Выберите запись журнала, чтобы увидеть её изменения.")
        self.heading.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details = QTableWidget(0, 3)
        self.details.setHorizontalHeaderLabels(["Поле", "До", "После"])
        self.details.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.details.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.details.setWordWrap(True)
        self.details.verticalHeader().hide()
        self.details.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.details.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.details.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.details.setStyleSheet("QTableWidget { gridline-color: #50545b; }")

        self.undo_button = QPushButton("Отменить действие")
        self.undo_button.clicked.connect(self.undo_selected)
        self.undo_button.setEnabled(False)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        controls = QHBoxLayout()
        controls.addWidget(self.undo_button)
        controls.addStretch()
        controls.addWidget(close)
        layout = QVBoxLayout(self)
        info = QLabel("Красный — удалено  ·  Синий — добавлено  ·  Серый — изменено. "
                      "Отмена разрешена только для последней операции над НИР.")
        info.setWordWrap(True)
        layout.addWidget(info)
        layout.addWidget(self.table, 3)
        layout.addWidget(self.heading)
        layout.addWidget(self.description)
        layout.addWidget(self.details, 2)
        layout.addLayout(controls)
        self.load_rows()

    def colors(self, operation):
        dark = self.palette().window().color().lightness() < 128
        shades = {
            "DELETE": ((92, 34, 43), (255, 211, 214)) if dark else ((255, 219, 224), (115, 15, 29)),
            "INSERT": ((24, 66, 103), (217, 238, 255)) if dark else ((217, 236, 255), (20, 62, 116)),
            "UPDATE": ((59, 62, 70), (238, 238, 238)) if dark else ((238, 239, 242), (43, 45, 52)),
        }
        bg, fg = shades.get(operation, shades["UPDATE"])
        return QColor(*bg), QColor(*fg)

    @staticmethod
    def display_value(field, value):
        if value is None:
            return "—"
        if field in ("f18", "z18") and isinstance(value, int):
            return f"{value:,} ₽".replace(",", " ")
        return str(value) if str(value).strip() else "(пусто)"

    def load_rows(self, select_log_id=None):
        self.rows = read_rows("SELECT * FROM change_log ORDER BY id DESC", path=self.path)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        names = {"INSERT": "Добавлено", "UPDATE": "Изменено", "DELETE": "Удалено"}
        selected_index = 0
        for i, row in enumerate(self.rows):
            if row["id"] == select_log_id:
                selected_index = i
            snapshot = json.loads(row["after_data"] or row["before_data"] or "{}")
            timestamp = datetime.fromisoformat(row["changed_at"].replace("Z", "+00:00")).astimezone()
            values = [timestamp.strftime("%d.%m.%Y %H:%M:%S"),
                      names.get(row["operation"], row["operation"]),
                      "НИР" if row["table_name"] == "nir" else "Финансирование",
                      str(row["row_id"]),
                      f"{snapshot.get('rnw', '—')} / {snapshot.get('codvuz', '—')}"]
            background, foreground = self.colors(row["operation"])
            for j, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setBackground(QBrush(background))
                item.setForeground(QBrush(foreground))
                item.setToolTip("Нажмите на строку для просмотра подробностей")
                self.table.setItem(i, j, item)
        self.table.blockSignals(False)
        if self.rows:
            self.table.setCurrentCell(selected_index, 0)
            self.show_details(selected_index)
        else:
            self.show_details(-1)

    def show_details(self, current_row, *_):
        self.details.setRowCount(0)
        if current_row < 0 or current_row >= len(self.rows):
            self.heading.setText("Журнал пуст")
            self.description.clear()
            self.undo_button.setEnabled(False)
            return
        entry = self.rows[current_row]
        before = json.loads(entry["before_data"] or "{}")
        after = json.loads(entry["after_data"] or "{}")
        label = {"INSERT": "Добавление", "DELETE": "Удаление", "UPDATE": "Изменение"}.get(entry["operation"], "Действие")
        self.heading.setText(f"{label} · {entry['table_name']} · запись №{entry['row_id']}")
        reason = entry.get("reason")
        self.description.setText("Причина: " + reason if reason else
                                 "Сравнение значений до и после операции. Изменённые поля выделены цветом.")
        fields = list(dict.fromkeys([*before.keys(), *after.keys()]))
        for field in fields:
            b, a = before.get(field), after.get(field)
            if entry["operation"] == "UPDATE" and b == a:
                continue
            i = self.details.rowCount()
            self.details.insertRow(i)
            old_item = QTableWidgetItem(self.display_value(field, b))
            new_item = QTableWidgetItem(self.display_value(field, a))
            if b != a:
                # Показываем изменения в привычной цветовой семантике.
                dark = self.palette().window().color().lightness() < 128
                if b is not None:
                    old_item.setBackground(QBrush(QColor("#5b2830" if dark else "#ffdee1")))
                    old_item.setForeground(QBrush(QColor("#ffe0e2" if dark else "#671823")))
                if a is not None:
                    new_item.setBackground(QBrush(QColor("#1e476a" if dark else "#dcecff")))
                    new_item.setForeground(QBrush(QColor("#dcedff" if dark else "#174a7c")))
            for j, item in enumerate((QTableWidgetItem(self.FIELD_NAMES.get(field, field)), old_item, new_item)):
                self.details.setItem(i, j, item)
        self.details.resizeRowsToContents()
        available = entry["table_name"] == "nir" and entry["operation"] in ("INSERT", "DELETE")
        newest = next((row for row in self.rows
                       if row["table_name"] == "nir" and row["row_id"] == entry["row_id"]), None)
        current_is_latest = newest is not None and newest["id"] == entry["id"]
        self.undo_button.setEnabled(available and current_is_latest)
        self.undo_button.setText("Восстановить удалённую НИР" if entry["operation"] == "DELETE"
                                 else "Удалить добавленную НИР" if entry["operation"] == "INSERT"
                                 else "Отмена недоступна")
        self.undo_button.setToolTip("Можно отменить только последнюю операцию над этой НИР."
                                    if available and not current_is_latest else "")

    def undo_selected(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self.rows):
            return
        entry = self.rows[index]
        action = "восстановить удалённую НИР" if entry["operation"] == "DELETE" else "удалить добавленную НИР"
        answer = QMessageBox.question(self, "Подтвердите отмену",
            f"Вы действительно хотите {action}?\n\n"
            "Изменение попадёт в журнал как новая операция. При конфликте база останется без изменений.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            message = undo_nir_change(entry["id"], path=self.path)
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "Действие не отменено", str(exc))
            return
        if self.on_change is not None:
            self.on_change()
        self.load_rows()
        QMessageBox.information(self, "Действие отменено", message)
