"""Двусторонние географические фильтры: вуз → город → субъект → округ."""
from __future__ import annotations

from nir_operations import read_rows

GEOGRAPHY_FIELDS = (("region", "Округ"), ("oblname", "Субъект РФ"),
                    ("city", "Город"), ("codvuz", "Вуз"))
FIELDS = tuple(field for field, _ in GEOGRAPHY_FIELDS)


def _where(selected: dict) -> tuple[str, tuple]:
    active = [(field, selected[field]) for field in FIELDS if selected.get(field) is not None]
    return (" AND ".join(f"{field} = ?" for field, _ in active) or "1=1",
            tuple(value for _, value in active))


def resolve_geography(path, selected: dict, changed: str | None = None) -> dict:
    """Оставляет совместимые значения и выводит однозначные РОДИТЕЛЬСКИЕ уровни.

    Последнее действие пользователя имеет приоритет, поэтому смена города
    сбрасывает устаревший вуз, а смена округа — несовместимый город.
    """
    if changed is not None and changed not in FIELDS:
        raise ValueError("Неизвестное географическое поле")
    values = {field: selected.get(field) for field in FIELDS}
    if changed is None:
        # При инициализации не придумываем пользовательские ограничения.
        return values
    chosen = values.get(changed)
    if chosen is None:
        # «Все» на уровне очищает этот уровень; остальные совместимые выборы
        # сохраняем, но не навязываем автоматически старое значение обратно.
        return values

    candidate = dict(values)
    query, params = _where(candidate)
    if not read_rows(f"SELECT codvuz FROM vuz WHERE {query} LIMIT 1", params, path=path):
        # Выбранное поле важнее несовместимых ранее установленных полей.
        candidate = {field: (chosen if field == changed else None) for field in FIELDS}

    # Только родительские уровни, а не все дочерние: выбранный город не должен
    # превращаться в один вуз только потому, что он единственный в справочнике.
    index = FIELDS.index(changed)
    for parent in FIELDS[:index]:
        query, params = _where({f: v for f, v in candidate.items() if f != parent})
        rows = read_rows(f"SELECT DISTINCT {parent} AS value FROM vuz WHERE {query}", params, path=path)
        distinct = [row["value"] for row in rows if row["value"] not in (None, "")]
        if len(distinct) == 1:
            candidate[parent] = distinct[0]
        else:
            candidate[parent] = None
    return candidate


def populate_geography(combos: dict, path, values=None, after=None):
    """Обновить зависимые списки: сверху вниз, с выводом родителей снизу вверх.

    Параметр after сохранён для обратной совместимости; при его передаче
    выбранный уровень обрабатывается как последнее действие пользователя.
    """
    selected = ({f: c.currentData() for f, c in combos.items()}
                if values is None else {f: values.get(f) for f in FIELDS})
    selected = resolve_geography(path, selected, changed=after)
    for field, combo in combos.items():
        # Выпадающий список зависит от ВЕРХНИХ уровней, но не блокируется
        # выбранным дочерним значением. Так можно сменить город после вуза
        # или округ после города без обязательного предварительного сброса.
        ancestors = {f: selected.get(f) for f in FIELDS[:FIELDS.index(field)]}
        query, parameters = _where(ancestors)
        label = "COALESCE(NULLIF(z2,''),z1)" if field == "codvuz" else field
        rows = read_rows(
            f"SELECT DISTINCT {field} AS value, {label} AS label FROM vuz "
            f"WHERE {query} AND {field} IS NOT NULL AND {field} != '' ORDER BY {field}",
            parameters, path=path,
        )
        old_block = combo.blockSignals(True)
        combo.clear()
        combo.addItem("Все", None)
        for row in rows:
            caption = (f"{row['value']} — {row['label']}" if field == "codvuz"
                       else str(row["label"]))
            combo.addItem(caption, row["value"])
        combo.setCurrentIndex(max(0, combo.findData(selected.get(field))))
        combo.blockSignals(old_block)
    return selected
