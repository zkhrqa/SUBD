"""Ввод транша, просмотр и выпуск распоряжения."""
from datetime import date
from fractions import Fraction
import re
import sqlite3

from PyQt6.QtCore import QSignalBlocker
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHeaderView, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableView, QVBoxLayout,
)

from analysis_forms import ReportModel
from funding_operations import MAX_RUBLES, approve_order, load_funding, preview_order
from report_export import find_order_file, save_order_file, save_workbook


COLUMNS = (("number", "№ п/п"), ("vuz", "Вуз (кратко)"), ("amount", "Сумма, руб."))
ORDER_NAME = "Распоряжение о финансировании вузов"


def rubles(amount):
    return f"{amount:,}".replace(",", " ")


def percent(amount, planned):
    if not planned:
        return "—"
    scaled = (amount * 100_000_000 + planned // 2) // planned
    whole, fraction = divmod(scaled, 1_000_000)
    return f"{whole},{fraction:06d}".rstrip("0").rstrip(",")


def order_title(order):
    if order.number is None:
        return "Проект распоряжения о финансировании вузов"
    issued = date.fromisoformat(order.issued_on).strftime("%d.%m.%Y")
    return f"{ORDER_NAME} № {order.number} от {issued}"


def order_file_name(order):
    name = f"{ORDER_NAME} № {order.number}" if order.number is not None else order_title(order)
    return f"{name} — {order.quarter}-й квартал"


def order_workbook(order):
    document_date = date.fromisoformat(order.issued_on) if order.issued_on else date.today()
    status = "Утверждённое распоряжение" if order.number is not None else "Проект для согласования"
    details = (status, f"Дата документа: {document_date:%d.%m.%Y}",
               f"Квартал финансирования: {order.quarter}",
               f"Сумма к распределению: {rubles(order.requested)} ₽ "
               f"({percent(order.requested, order.planned)}% от плана)",
               f"Остаток после округления: {rubles(order.requested - order.total)} ₽")
    return dict(document_date=document_date, title=order_title(order), details=details,
                columns=[label for _, label in COLUMNS],
                rows=[(i, name, amount) for i, (_, name, amount) in enumerate(order.allocations, 1)],
                totals=("", "Итого, руб.", order.total))


class FundingDialog(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.preview = self.order = None
        self.has_approved = False
        self.input_source = "amount"
        self.requested = None

        self.setWindowTitle("Распределение финансирования вузов")
        self.resize(850, 690)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        form = QFormLayout()
        self.amount = QLineEdit()
        self.amount.setMaxLength(30)
        self.amount.setPlaceholderText("Целые рубли")
        self.amount.textEdited.connect(lambda: self.update_input("amount"))
        self.rate = QLineEdit()
        self.rate.setMaxLength(30)
        self.rate.setPlaceholderText("Процент от общего плана")
        self.rate.textEdited.connect(lambda: self.update_input("rate"))
        self.quarter = QComboBox()
        for quarter in range(1, 5):
            self.quarter.addItem(f"{quarter}-й квартал", quarter)
        self.quarter.setPlaceholderText("Выберите квартал")
        self.quarter.setCurrentIndex(-1)
        self.quarter.currentIndexChanged.connect(self.reset_preview)
        self.history = QComboBox()
        self.history.currentIndexChanged.connect(self.select_history)
        form.addRow("Сумма к распределению, руб.", self.amount)
        form.addRow("Или процент от плана", self.rate)
        form.addRow("Квартал финансирования", self.quarter)
        form.addRow("Распоряжения", self.history)
        layout.addLayout(form)

        self.calculate_button = QPushButton("Распределить")
        self.calculate_button.clicked.connect(self.calculate)
        layout.addWidget(self.calculate_button)
        self.caption = QLabel()
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.table = QTableView()
        self.model = ReportModel(COLUMNS, self)
        self.table.setModel(self.model)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.total = QLabel()
        self.total.setWordWrap(True)
        layout.addWidget(self.total)

        controls = QHBoxLayout()
        self.approve_button = QPushButton("Утвердить")
        self.approve_button.clicked.connect(self.approve)
        self.save_button = QPushButton("Сохранить проект")
        self.save_button.clicked.connect(self.save_document)
        self.recover_button = QPushButton("Восстановить файл")
        self.recover_button.clicked.connect(self.recover_order_file)
        self.cancel_button = QPushButton("Отмена расчёта")
        self.cancel_button.clicked.connect(self.reset_preview)
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.reject)
        for button in (self.approve_button, self.save_button, self.recover_button, self.cancel_button):
            controls.addWidget(button)
        controls.addStretch()
        controls.addWidget(close_button)
        layout.addLayout(controls)
        self.refresh_history()

    def refresh_history(self, number=None):
        snapshot, orders = load_funding(self.path)
        self.snapshot = snapshot
        actual_rate = percent(snapshot.actual, snapshot.planned)
        rate_label = f"{actual_rate}%" if snapshot.planned else actual_rate
        self.summary.setText(
            f"Вузов с НИР: {len(snapshot.universities)}\n"
            f"План: {rubles(snapshot.planned)} ₽ · Факт: {rubles(snapshot.actual)} ₽ · К плану: {rate_label}"
        )
        self.history.blockSignals(True)
        self.history.clear()
        self.history.addItem("Новый транш")
        for order in orders:
            self.history.addItem(order_title(order), order)
            if order.number == number:
                self.history.setCurrentIndex(self.history.count() - 1)
        self.history.blockSignals(False)
        self.update_input(self.input_source, reset=False)
        self.select_history()

    def update_input(self, source, *, reset=True):
        self.input_source = source
        planned = self.snapshot.planned
        self.requested = None
        if source == "amount":
            value = self.amount.text().replace(" ", "").replace("\u00a0", "")
            if re.fullmatch(r"[0-9]+", value):
                self.requested = int(value)
            self.rate.setText(percent(self.requested, planned) if self.requested is not None and planned else "")
        else:
            value = self.rate.text().strip().replace(",", ".")
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) and planned:
                self.requested = int(planned * Fraction(value) / 100)
            self.amount.setText(rubles(self.requested) if self.requested is not None else "")
        if reset:
            self.reset_preview()

    def reset_preview(self):
        self.history.setCurrentIndex(0)
        self.select_history()

    def select_history(self):
        order = self.history.currentData()
        with QSignalBlocker(self.quarter):
            if order is not None:
                self.quarter.setCurrentIndex(self.quarter.findData(order.quarter))
            elif self.order is not None and self.order.number is not None:
                self.quarter.setCurrentIndex(-1)
        self.preview = None
        self.show_order(order)

    def calculate(self):
        try:
            self.preview = preview_order(self.snapshot, self.requested, self.quarter.currentData())
        except ValueError as exc:
            QMessageBox.warning(self, "Транш не рассчитан", str(exc))
            return
        self.show_order(self.preview.order)

    def show_order(self, order):
        self.order = order
        is_draft = self.preview is not None
        is_archived = order is not None and order.number is not None
        self.approve_button.setEnabled(is_draft and order.total > 0)
        self.cancel_button.setEnabled(is_draft)
        self.save_button.setVisible(not is_archived)
        self.save_button.setEnabled(is_draft)
        self.recover_button.setVisible(is_archived and not self.order_file_exists(order))
        self.calculate_button.setEnabled(not is_archived and not self.snapshot.unassigned_works
                                         and self.quarter.currentData() is not None
                                         and self.requested is not None and 0 < self.requested <= MAX_RUBLES)
        self.amount.setEnabled(not is_archived)
        self.rate.setEnabled(not is_archived)
        self.quarter.setEnabled(not is_archived)
        if order is None:
            hint = ("Перед распределением укажите вузы для всех НИР."
                    if self.snapshot.unassigned_works else "Введите сумму или процент и выберите квартал.")
            self.caption.setText(hint)
            self.model.replace([])
            self.total.clear()
            return
        self.caption.setText(f"{order_title(order)} · {order.quarter}-й квартал")
        self.model.replace([dict(number=i, vuz=name, amount=amount)
                            for i, (_, name, amount) in enumerate(order.allocations, 1)])
        details = (
            f"К распределению: {rubles(order.requested)} ₽ · "
            f"Распределено: {rubles(order.total)} ₽ · Остаток: {rubles(order.requested - order.total)} ₽"
        )
        if is_draft and self.history.count() == 1:
            details += "\nПосле первого утверждения изменения НИР будут заблокированы."
        self.total.setText(details)

    def approve(self):
        if self.preview is None:
            return
        try:
            order = approve_order(self.preview, path=self.path)
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Распоряжение не утверждено", str(exc))
            self.reset_preview()
            number = None
        else:
            self.has_approved = True
            self.preview = None
            self.show_order(order)
            number = order.number
            self.export_order(order)
        try:
            self.refresh_history(number)
        except sqlite3.Error as exc:
            QMessageBox.warning(self, "Сводка не обновлена", str(exc))

    def save_document(self):
        if self.preview is not None:
            order = self.preview.order
            save_workbook(self, "funding", order_file_name(order), **order_workbook(order))

    def order_file_exists(self, order):
        try:
            return find_order_file(order_file_name(order), date.fromisoformat(order.issued_on),
                                   order.token) is not None
        except OSError:
            return False

    def recover_order_file(self):
        if self.order is not None and self.order.number is not None:
            self.export_order(self.order)

    def export_order(self, order):
        try:
            path = save_order_file(order_file_name(order), identifier=order.token, **order_workbook(order))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self, "Распоряжение утверждено",
                f"Файл сохранить не удалось. Повторите выгрузку кнопкой «Восстановить файл».\n{exc}",
            )
            return
        self.recover_button.hide()
        QMessageBox.information(self, "Распоряжение сохранено", str(path))
