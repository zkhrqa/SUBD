"""Общий географический фильтр и рубрика для ВСЕХ таблиц и отчётов."""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                             QFormLayout, QGroupBox, QLabel, QVBoxLayout)

from geography_filters import FIELDS, GEOGRAPHY_FIELDS, populate_geography, resolve_geography
from nir_operations import read_rows


class SharedFilters(QObject):
    changed = pyqtSignal()

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.geography = {field: None for field in FIELDS}
        self.rubric = None

    def update(self, geography=None, rubric=None):
        geo = {f: (geography or {}).get(f) for f in FIELDS}
        if geo != self.geography or rubric != self.rubric:
            self.geography, self.rubric = geo, rubric
            self.changed.emit()

    def reset(self):
        self.update({}, None)

    def describe(self):
        parts = [f"{label}: {self.geography[field]}" for field, label in GEOGRAPHY_FIELDS
                 if self.geography[field] is not None]
        if self.rubric is not None:
            parts.append(f"Рубрика ГРНТИ: {self.rubric}")
        return "; ".join(parts) or "Без фильтра"


class UnifiedFilterDialog(QDialog):
    def __init__(self, state: SharedFilters, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("Общий фильтр — все таблицы и отчёты")
        self.resize(660, 420)
        layout = QVBoxLayout(self)
        group = QGroupBox("География")
        form = QFormLayout(group)
        self.geography = {}
        for field, label in GEOGRAPHY_FIELDS:
            combo = QComboBox()
            combo.setMinimumContentsLength(24)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.geography[field] = combo
            combo.currentIndexChanged.connect(lambda _, name=field: self.changed(name))
            form.addRow(label, combo)
        populate_geography(self.geography, state.path, state.geography)
        layout.addWidget(group)
        group = QGroupBox("Тематика НИР")
        form = QFormLayout(group)
        self.rubric = QComboBox()
        self.rubric.addItem("Все рубрики", None)
        for row in read_rows("SELECT codrub,rubrika FROM grnti ORDER BY codrub", path=state.path):
            code = f"{row['codrub']:02d}"
            self.rubric.addItem(f"{code} — {row['rubrika']}", code)
        self.rubric.setCurrentIndex(max(0, self.rubric.findData(state.rubric)))
        form.addRow("Рубрика ГРНТИ", self.rubric)
        layout.addWidget(group)
        hint = QLabel("Можно выбирать с любого уровня: вуз автоматически определяет город, "
                      "субъект и округ; город — доступные родительские уровни. "
                      "Фильтр общий для всех вкладок и аналитических отчётов.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        reset_button = buttons.addButton("Сбросить всё", QDialogButtonBox.ButtonRole.ResetRole)
        reset_button.clicked.connect(self.reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def changed(self, field):
        selected = {f: combo.currentData() for f, combo in self.geography.items()}
        # Последний выбранный уровень имеет приоритет, в том числе при снятии
        # ограничения «Все» после ранее автоматически заполненного уровня.
        if selected[field] is None:
            # Сброс выбранного уровня: очищаем зависимые нижние ограничения.
            index = FIELDS.index(field)
            for child in FIELDS[index + 1:]:
                selected[child] = None
        resolved = resolve_geography(self.state.path, selected, changed=field)
        populate_geography(self.geography, self.state.path, resolved)

    def reset(self):
        populate_geography(self.geography, self.state.path, {})
        self.rubric.setCurrentIndex(0)

    def values(self):
        return {f: combo.currentData() for f, combo in self.geography.items()}

    def accept(self):
        self.state.update(self.values(), self.rubric.currentData())
        super().accept()
