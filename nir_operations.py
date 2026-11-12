"""Сохранение НИР, резервная копия и история операций."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from database import (
    DB_PATH, FUNDING_LOCK_MESSAGE, _normalize_rnw, _open_sqlite, ensure_financing_view,
    funding_started, rebuild_financing, rnw_comparison_key,
)
from funding_operations import prepare_funding

FIELDS = ("codvuz", "rnw", "f1", "z2", "f6", "f10", "f2", "f7", "f18")
GRNTI_PATTERN = re.compile(r"[0-9]{2}\.[0-9]{2}\.[0-9]{2}")
UNIQUE_KEY_MIGRATION = "nir_codvuz_rnw_unique_v1"


def backup_database(path: Path, backup: Path) -> Path:
    """Однократный снимок SQLite вместе с данными WAL."""
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        temporary = backup.with_suffix(".tmp")
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        temporary.replace(backup)
    return backup


def duplicate_number_plan(conn: sqlite3.Connection) -> list[tuple[int, int | None, str, str]]:
    """Сохранить первую запись пары; повторы нумеровать по максимуму суффикса."""
    rows = conn.execute("SELECT id,codvuz,rnw FROM nir ORDER BY id").fetchall()
    maxima = {}
    used = set()
    for _, codvuz, number in rows:
        used.add((codvuz, rnw_comparison_key(number)))
        match = re.fullmatch(r"(.*?)([0-9]+)", number)
        if match:
            prefix, suffix = match.groups()
            key = (codvuz, prefix)
            maxima[key] = max(maxima.get(key, 0), int(suffix))
    seen = set()
    changes = []
    for row_id, codvuz, number in rows:
        pair = (codvuz, number)
        if pair not in seen:
            seen.add(pair)
            continue
        match = re.fullmatch(r"(.*?)([0-9]+)", number)
        prefix, suffix = match.groups() if match else ("", "1")
        key = (codvuz, prefix)
        next_number = maxima.get(key, 0) + 1
        replacement = prefix + str(next_number).zfill(len(suffix))
        while (codvuz, rnw_comparison_key(replacement)) in used:
            next_number += 1
            replacement = prefix + str(next_number).zfill(len(suffix))
        maxima[key] = next_number
        used.add((codvuz, rnw_comparison_key(replacement)))
        changes.append((row_id, codvuz, number, replacement))
    return changes


def enforce_nir_unique_key(path: Path = DB_PATH) -> list[tuple[int, int | None, str, str]]:
    """Однократно исправить номера и создать составной уникальный ключ."""
    path = Path(path)
    conn = _open_sqlite(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        applied = conn.execute("SELECT 1 FROM schema_migrations WHERE name=?",
                               (UNIQUE_KEY_MIGRATION,)).fetchone()
        changes = []
        if not applied:
            backup_database(path, path.parent / "backups" / f"{path.stem}_before_unique_nir_key.db")
            changes = duplicate_number_plan(conn)
            for row_id, _, _, replacement in changes:
                # UPDATE-триггер сохраняет старый и новый номер в журнале.
                conn.execute("UPDATE nir SET rnw=? WHERE id=?", (replacement, row_id))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS nir_codvuz_rnw_unique ON nir(codvuz,rnw)")
        # SQLite допускает повторы NULL в UNIQUE. НИР с неизвестным вузом
        # сохраняем, но их номера также не должны повторяться между собой.
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS nir_unknown_vuz_rnw_unique
                        ON nir(rnw) WHERE codvuz IS NULL""")
        if not applied:
            conn.execute("INSERT INTO schema_migrations(name) VALUES(?)", (UNIQUE_KEY_MIGRATION,))
        conn.commit()
        return changes
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def prepare_database(path: Path = DB_PATH) -> Path:
    """Создать однократный снимок до миграции, затем установить журнал и проверки."""
    path = Path(path)
    backup = path.parent / "backups" / f"{path.stem}_before_changes.db"
    backup_database(path, backup)

    conn = _open_sqlite(path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                operation TEXT NOT NULL,
                table_name TEXT NOT NULL,
                row_id INTEGER NOT NULL,
                before_data TEXT,
                after_data TEXT
            );
            CREATE TRIGGER IF NOT EXISTS nir_unique_insert
            BEFORE INSERT ON nir
            WHEN EXISTS (SELECT 1 FROM nir WHERE codvuz IS NEW.codvuz AND rnw = NEW.rnw)
            BEGIN SELECT RAISE(ABORT, 'Рег. номер уже существует в выбранном вузе.'); END;
            CREATE TRIGGER IF NOT EXISTS nir_unique_update
            BEFORE UPDATE OF codvuz, rnw ON nir
            WHEN (OLD.codvuz IS NOT NEW.codvuz OR OLD.rnw IS NOT NEW.rnw)
                 AND EXISTS (SELECT 1 FROM nir WHERE id != OLD.id
                             AND codvuz IS NEW.codvuz AND rnw = NEW.rnw)
            BEGIN SELECT RAISE(ABORT, 'Рег. номер уже существует в выбранном вузе.'); END;
        """)
        if "reason" not in {row[1] for row in conn.execute("PRAGMA table_info(change_log)")}:
            conn.execute("ALTER TABLE change_log ADD COLUMN reason TEXT")
        def snapshot(prefix):
            return "json_object(" + ",".join(
                f"'{field}',{prefix}.{field}" for field in ("id", *FIELDS)
            ) + ")"

        for operation, before, after in (
            ("INSERT", "NULL", snapshot("NEW")),
            ("UPDATE", snapshot("OLD"), snapshot("NEW")),
            ("DELETE", snapshot("OLD"), "NULL"),
        ):
            row = "OLD.id" if operation == "DELETE" else "NEW.id"
            # Не записываем UPDATE, фактически не изменивший значения.
            condition = ""
            if operation == "UPDATE":
                condition = "WHEN " + " OR ".join(f"OLD.{f} IS NOT NEW.{f}" for f in FIELDS)
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS nir_log_{operation.lower()}
                AFTER {operation} ON nir {condition}
                BEGIN
                    INSERT INTO change_log(operation,table_name,row_id,before_data,after_data)
                    VALUES ('{operation}','nir',{row},{before},{after});
                END
            """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS financing_log_update
            AFTER UPDATE OF z18 ON financing WHEN OLD.z18 IS NOT NEW.z18
            BEGIN
                INSERT INTO change_log(operation,table_name,row_id,before_data,after_data)
                VALUES ('UPDATE','financing',NEW.codvuz,
                        json_object('codvuz',OLD.codvuz,'z18',OLD.z18),
                        json_object('codvuz',NEW.codvuz,'z18',NEW.z18));
            END
        """)
        ensure_financing_view(conn)
        conn.commit()
    finally:
        conn.close()
    enforce_nir_unique_key(path)
    with closing(_open_sqlite(path)) as conn:
        install_number_checks(conn)
        prepare_funding(conn)
    return backup


def install_number_checks(conn: sqlite3.Connection) -> None:
    def key(field):
        # SQLite получает нормализованные номера из формы. Сравниваем и номера
        # из внешнего SQL, в том числе с обычными пробелами и табуляцией.
        value = f"replace(replace({field}, ' ', ''), char(9), '')"
        return (f"CASE WHEN {value} <> '' AND {value} NOT GLOB '*[^0-9]*' "
                f"THEN COALESCE(NULLIF(ltrim({value}, '0'), ''), '0') ELSE {value} END")

    same_number = f"codvuz IS NEW.codvuz AND ({key('rnw')}) = ({key('NEW.rnw')})"
    conn.executescript(f"""
        CREATE TRIGGER IF NOT EXISTS nir_compare_insert
        BEFORE INSERT ON nir
        WHEN EXISTS (SELECT 1 FROM nir WHERE {same_number})
        BEGIN SELECT RAISE(ABORT, 'Рег. номер уже существует в выбранном вузе.'); END;

        CREATE TRIGGER IF NOT EXISTS nir_compare_update
        BEFORE UPDATE OF codvuz, rnw ON nir
        WHEN (OLD.codvuz IS NOT NEW.codvuz OR ({key('OLD.rnw')}) IS NOT ({key('NEW.rnw')}))
             AND EXISTS (SELECT 1 FROM nir WHERE id != OLD.id AND {same_number})
        BEGIN SELECT RAISE(ABORT, 'Рег. номер уже существует в выбранном вузе.'); END;
    """)


def number_in_use(conn, codvuz, number, row_id):
    key = rnw_comparison_key(number)
    return any(rnw_comparison_key(row[0]) == key for row in conn.execute(
        "SELECT rnw FROM nir WHERE codvuz IS ? AND id != ?", (codvuz, row_id or -1)
    ))


def normalize_grnti_input(text: str) -> str:
    """Без потери данных распознать старую запись с лишней точкой/разделителем."""
    text = text.strip()
    codes = re.findall(r"[0-9]{2}\.[0-9]{2}\.[0-9]{2}", text)
    residue = re.sub(r"[0-9]{2}\.[0-9]{2}\.[0-9]{2}", "", text)
    if 1 <= len(codes) <= 2 and not re.sub(r"[\s;,.]", "", residue):
        return "; ".join(codes)
    return text


def validate_values(conn: sqlite3.Connection, values: dict, row_id: int | None = None) -> dict:
    result = {field: values.get(field, "") for field in FIELDS}
    vuz = conn.execute("SELECT z2 FROM vuz WHERE codvuz=?", (result["codvuz"],)).fetchone()
    if not vuz:
        raise ValueError("Выберите вуз из справочника.")
    result["z2"] = vuz[0] or ""
    result["rnw"] = _normalize_rnw(result["rnw"])
    if not result["rnw"]:
        raise ValueError("Введите регистрационный номер.")
    old = conn.execute("SELECT codvuz,rnw FROM nir WHERE id=?", (row_id,)).fetchone()
    same_key = (old and old[0] == result["codvuz"]
                and rnw_comparison_key(old[1]) == rnw_comparison_key(result["rnw"]))
    if not same_key and number_in_use(conn, result["codvuz"], result["rnw"], row_id):
        raise ValueError("Такой рег. номер уже есть у выбранного вуза.")
    if result["f1"] not in {"Ф", "П", "Р"}:
        raise ValueError("Выберите характер НИР: Ф, П или Р.")
    for field in ("f2", "f6", "f7"):
        result[field] = str(result[field]).strip()
    if not result["f2"]:
        raise ValueError("Введите наименование НИР.")
    if not result["f6"]:
        raise ValueError("Введите имя руководителя.")
    if not result["f7"]:
        raise ValueError("Введите должность руководителя.")
    parts = [part.strip() for part in str(result["f10"]).split(";")]
    if not 1 <= len(parts) <= 2 or any(not GRNTI_PATTERN.fullmatch(p) for p in parts):
        raise ValueError("ГРНТИ: один или два кода вида 34.31.15 через точку с запятой.")
    valid_rubrics = {f"{r[0]:02d}" for r in conn.execute("SELECT codrub FROM grnti")}
    if any(p[:2] not in valid_rubrics for p in parts):
        raise ValueError("Первые две цифры ГРНТИ должны соответствовать рубрике справочника.")
    result["f10"] = "; ".join(parts)
    if not re.fullmatch(r"[0-9]+", str(result["f18"]).strip()):
        raise ValueError("Плановое финансирование — целое неотрицательное число рублей.")
    result["f18"] = int(result["f18"])
    if result["f18"] > 9223372036854775807:
        raise ValueError("Сумма финансирования слишком велика.")
    return result


def save_nir(values: dict, row_id: int | None = None, *, path: Path = DB_PATH,
             expected: dict | None = None) -> int:
    conn = _open_sqlite(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        if funding_started(conn):
            raise ValueError(FUNDING_LOCK_MESSAGE)
        if row_id is not None:
            current = conn.execute("SELECT * FROM nir WHERE id=?", (row_id,)).fetchone()
            if current is None:
                raise ValueError("Запись уже удалена. Обновите таблицу.")
            if expected is not None and dict(current) != expected:
                raise ValueError("Запись была изменена после открытия формы. Откройте её заново.")
        clean = validate_values(conn, values, row_id)
        if row_id is None:
            cursor = conn.execute(
                f"INSERT INTO nir({','.join(FIELDS)}) VALUES ({','.join('?' for _ in FIELDS)})",
                tuple(clean[f] for f in FIELDS),
            )
            row_id = cursor.lastrowid
        else:
            conn.execute(f"UPDATE nir SET {','.join(f'{f}=?' for f in FIELDS)} WHERE id=?",
                         (*[clean[f] for f in FIELDS], row_id))
        rebuild_financing(conn)
        conn.commit()
        return row_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_nir(row_id: int, *, path: Path = DB_PATH, expected: dict | None = None) -> None:
    conn = _open_sqlite(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        if funding_started(conn):
            raise ValueError(FUNDING_LOCK_MESSAGE)
        row = conn.execute("SELECT * FROM nir WHERE id=?", (row_id,)).fetchone()
        if row is None:
            raise ValueError("Запись уже удалена. Обновите таблицу.")
        if expected is not None and dict(row) != expected:
            raise ValueError("Запись изменилась. Обновите таблицу перед удалением.")
        conn.execute("DELETE FROM nir WHERE id=?", (row_id,))
        rebuild_financing(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def repair_vuz_links(assignments: dict[int, int], *, expected: dict[int, dict],
                     reason: str, path: Path = DB_PATH) -> None:
    """Восстановить проверенные связи одной транзакцией, без изменения самих НИР."""
    if not assignments or assignments.keys() != expected.keys() or not reason.strip():
        raise ValueError("Укажите записи, их исходные значения и причину исправления.")
    path = Path(path)
    backup_database(path, path.parent / "backups" / f"{path.stem}_before_vuz_repair.db")
    conn = _open_sqlite(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        if funding_started(conn):
            raise ValueError(FUNDING_LOCK_MESSAGE)
        for row_id, codvuz in assignments.items():
            row = conn.execute("SELECT * FROM nir WHERE id=?", (row_id,)).fetchone()
            if row is None or dict(row) != expected[row_id]:
                raise ValueError("Исходная НИР изменилась. Повторно проверьте назначение вуза.")
            vuz = conn.execute("SELECT z2 FROM vuz WHERE codvuz=?", (codvuz,)).fetchone()
            if vuz is None:
                raise ValueError("Назначенный вуз отсутствует в справочнике.")
            if row["codvuz"] == codvuz and row["z2"] == vuz[0]:
                continue
            if row["codvuz"] != codvuz and number_in_use(conn, codvuz, row["rnw"], row_id):
                raise sqlite3.IntegrityError("Рег. номер уже существует в выбранном вузе.")
            conn.execute("UPDATE nir SET codvuz=?,z2=? WHERE id=?", (codvuz, vuz[0], row_id))
            conn.execute("""UPDATE change_log SET reason=? WHERE id=(
                SELECT MAX(id) FROM change_log
                WHERE table_name='nir' AND row_id=? AND operation='UPDATE')""", (reason, row_id))
        rebuild_financing(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_actual_financing(changes: list[tuple[int, int]], *, path: Path = DB_PATH) -> None:
    """Сохранить фактические суммы вместе с журналом одной транзакцией."""
    conn = _open_sqlite(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if funding_started(conn):
            raise ValueError("Факт финансирования обновляется при утверждении распоряжения.")
        for codvuz, amount in changes:
            if not isinstance(amount, int) or not 0 <= amount <= 9223372036854775807:
                raise ValueError("Фактическое финансирование — целое неотрицательное число рублей.")
            cursor = conn.execute("UPDATE financing SET z18=? WHERE codvuz=?", (amount, codvuz))
            if cursor.rowcount != 1:
                raise ValueError("Строка финансирования больше не существует. Обновите таблицу.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_rows(query: str, parameters: tuple = (), *, path: Path = DB_PATH) -> list[dict]:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(query, parameters)]
    finally:
        conn.close()
