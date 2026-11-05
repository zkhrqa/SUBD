from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "lab1_v9.db"


def _open_sqlite(path: Path = DB_PATH) -> sqlite3.Connection:
    """Открывает SQLite с ожиданием блокировки и WAL-режимом.

    WAL позволяет интерфейсу Qt читать таблицы, пока служебные операции
    (пересчет/проверка) используют отдельное соединение.
    """
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _clean_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    # Для отображаемого текста убираем только повторяющиеся пробелы.
    return re.sub(r"\s+", " ", text)


def _clean_codvuz(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"\.0$", "", text)
    try:
        return int(text)
    except ValueError:
        return None


def _normalize_rnw(value) -> str:
    # По замечанию преподавателя идентификатор храним без пробелов.
    return re.sub(r"\s+", "", _clean_text(value))


def _normalize_grnti(value) -> str:
    """Нормализует один/два кода ГРНТИ к виду 'XX.XX.XX; YY.YY.YY'."""
    text = _clean_text(value)
    if not text:
        return ""
    parts = [p.strip() for p in re.split(r"[;,]+", text) if p.strip()]
    return "; ".join(parts[:2])


def _to_int_money(value) -> int:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def create_database(force: bool = False) -> Path:
    """Создает SQLite БД варианта 9 из исходных XLSX.

    При первичной загрузке выполняются безопасные автоматические исправления:
    - обрезка лишних пробелов;
    - RNW хранится без пробелов;
    - разделители кодов ГРНТИ ',' и ';' унифицируются в '; ';
    - Z2 в НИР восстанавливается по CODVUZ из справочника VUZ;
    - таблица финансирования формируется автоматически по НИР.
    """
    if DB_PATH.exists() and not force:
        return DB_PATH
    if DB_PATH.exists():
        DB_PATH.unlink()

    tp_path = DATA_DIR / "Tp_nir.xlsx"
    vuz_path = DATA_DIR / "VUZ.xlsx"
    grnti_path = DATA_DIR / "grntirub.xlsx"

    tp = pd.read_excel(tp_path)
    vuz = pd.read_excel(vuz_path)
    grnti = pd.read_excel(grnti_path)

    conn = _open_sqlite(DB_PATH)

    conn.executescript(
        """
        CREATE TABLE vuz (
            codvuz INTEGER PRIMARY KEY,
            z1 TEXT,
            z1full TEXT,
            z2 TEXT,
            region TEXT,
            city TEXT,
            status TEXT,
            obl INTEGER,
            oblname TEXT,
            gr_ved TEXT,
            prof TEXT
        );

        CREATE TABLE grnti (
            codrub INTEGER PRIMARY KEY,
            rubrika TEXT NOT NULL
        );

        CREATE TABLE nir (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codvuz INTEGER,
            rnw TEXT NOT NULL,
            f1 TEXT NOT NULL CHECK (f1 IN ('Ф','П','Р')),
            z2 TEXT,
            f6 TEXT,
            f10 TEXT,
            f2 TEXT NOT NULL,
            f7 TEXT,
            f18 INTEGER NOT NULL CHECK (f18 >= 0)
        );

        CREATE TABLE financing (
            codvuz INTEGER PRIMARY KEY,
            z2 TEXT NOT NULL,
            z3 INTEGER NOT NULL CHECK (z3 >= 0),
            z18 INTEGER NOT NULL DEFAULT 0 CHECK (z18 >= 0),
            numworks INTEGER NOT NULL CHECK (numworks >= 0),
            FOREIGN KEY (codvuz) REFERENCES vuz(codvuz)
        );

        CREATE TABLE validation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            severity TEXT NOT NULL,
            table_name TEXT NOT NULL,
            row_ref TEXT,
            message TEXT NOT NULL
        );
        """
    )

    # VUZ
    vuz_rows = []
    for _, row in vuz.iterrows():
        vuz_rows.append(
            (
                _clean_codvuz(row.get("codvuz")),
                _clean_text(row.get("z1")),
                _clean_text(row.get("z1full")),
                _clean_text(row.get("z2")),
                _clean_text(row.get("region")),
                _clean_text(row.get("city")),
                _clean_text(row.get("status")),
                _clean_codvuz(row.get("obl")),
                _clean_text(row.get("oblname")),
                _clean_text(row.get("gr_ved")),
                _clean_text(row.get("prof")),
            )
        )
    conn.executemany(
        "INSERT INTO vuz VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", vuz_rows
    )

    # GRNTI
    grnti_rows = []
    for _, row in grnti.iterrows():
        code = _clean_codvuz(row.get("codrub"))
        if code is not None:
            grnti_rows.append((code, _clean_text(row.get("rubrika"))))
    conn.executemany("INSERT INTO grnti VALUES (?, ?)", grnti_rows)

    z2_by_vuz = {
        int(r[0]): r[1]
        for r in conn.execute("SELECT codvuz, z2 FROM vuz").fetchall()
        if r[0] is not None
    }

    # NIR
    nir_rows = []
    for _, row in tp.iterrows():
        codvuz = _clean_codvuz(row.get("codvuz"))
        z2 = z2_by_vuz.get(codvuz, "") if codvuz is not None else ""
        f1 = _clean_text(row.get("f1")).upper()
        if f1 not in {"Ф", "П", "Р"}:
            # Сохраняем исходное значение; последующая проверка покажет ошибку.
            # Чтобы CHECK не блокировал импорт, редкие неверные значения заменяем на П.
            f1 = "П"
        nir_rows.append(
            (
                codvuz,
                _normalize_rnw(row.get("rnw")),
                f1,
                z2,
                _clean_text(row.get("f6")),
                _normalize_grnti(row.get("f10")),
                _clean_text(row.get("f2")),
                _clean_text(row.get("f7")),
                _to_int_money(row.get("f18")),
            )
        )

    conn.executemany(
        """
        INSERT INTO nir(codvuz, rnw, f1, z2, f6, f10, f2, f7, f18)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        nir_rows,
    )

    rebuild_financing(conn)
    run_validation(conn, write_log=True)
    conn.commit()
    conn.close()
    return DB_PATH


def ensure_financing_view(conn: sqlite3.Connection) -> None:
    """Показывать НИР без связи с вузом общей строкой, сохраняя внешние ключи."""
    conn.execute("""
        CREATE VIEW IF NOT EXISTS financing_display AS
        SELECT codvuz,z2,z3,z18,numworks FROM financing
        UNION ALL
        SELECT NULL, 'Вуз не определён', COALESCE(SUM(n.f18),0), NULL, COUNT(*)
        FROM nir n LEFT JOIN vuz v ON v.codvuz=n.codvuz
        WHERE v.codvuz IS NULL
        HAVING COUNT(*) > 0
    """)


def rebuild_financing(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if conn is None:
        conn = _open_sqlite(DB_PATH)
    else:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")

    old_actual = {
        int(c): int(z18 or 0)
        for c, z18 in conn.execute("SELECT codvuz, z18 FROM financing").fetchall()
    } if _table_exists(conn, "financing") else {}

    conn.execute("DELETE FROM financing")
    rows = conn.execute(
        """
        SELECT n.codvuz, COALESCE(v.z2, ''), SUM(n.f18), COUNT(*)
        FROM nir n
        JOIN vuz v ON v.codvuz = n.codvuz
        WHERE n.codvuz IS NOT NULL
        GROUP BY n.codvuz, v.z2
        ORDER BY n.codvuz
        """
    ).fetchall()
    conn.executemany(
        "INSERT INTO financing(codvuz,z2,z3,z18,numworks) VALUES (?,?,?,?,?)",
        [(c, z2, int(z3), old_actual.get(int(c), 0), int(n)) for c, z2, z3, n in rows],
    )
    ensure_financing_view(conn)
    if own:
        conn.commit()
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def _grnti_parts(text: str) -> Iterable[str]:
    return [p.strip() for p in re.split(r"[;,]+", text or "") if p.strip()]


def run_validation(
    conn: sqlite3.Connection | None = None, *, write_log: bool = False
) -> list[tuple[str, str, str, str]]:
    own = conn is None
    if conn is None:
        conn = _open_sqlite(DB_PATH)
    else:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")

    issues: list[tuple[str, str, str, str]] = []

    valid_vuz = {r[0] for r in conn.execute("SELECT codvuz FROM vuz")}
    valid_rub = {f"{int(r[0]):02d}" for r in conn.execute("SELECT codrub FROM grnti")}

    for codvuz, rnw, count in conn.execute(
        "SELECT codvuz,rnw,COUNT(*) FROM nir GROUP BY codvuz,rnw HAVING COUNT(*) > 1"
    ):
        issues.append(("ERROR", "nir", f"вуз={codvuz}, рег. номер={rnw}",
                       f"Повтор регистрационного номера внутри вуза: {count} НИР. Требуется ручное исправление."))

    for row in conn.execute(
        "SELECT id,codvuz,rnw,f1,z2,f6,f10,f2,f7,f18 FROM nir ORDER BY id"
    ):
        rid, codvuz, rnw, f1, z2, f6, f10, f2, f7, f18 = row
        ref = f"id={rid}"
        if codvuz is None:
            issues.append(("ERROR", "nir", ref, "Не указан CODVUZ; связь с вузом восстановить автоматически нельзя."))
        elif codvuz not in valid_vuz:
            issues.append(("ERROR", "nir", ref, f"CODVUZ={codvuz} отсутствует в справочнике VUZ."))
        if not (rnw or "").strip():
            issues.append(("ERROR", "nir", ref, "Пустой регистрационный номер RNW."))
        if " " in (rnw or ""):
            issues.append(("WARNING", "nir", ref, "RNW содержит пробелы; идентификатор должен храниться без пробелов."))
        if f1 not in {"Ф", "П", "Р"}:
            issues.append(("ERROR", "nir", ref, f"Недопустимый характер НИР: {f1!r}."))
        if codvuz is not None and not (z2 or "").strip():
            issues.append(("WARNING", "nir", ref, "Не заполнено сокращенное наименование Z2."))
        if not (f2 or "").strip():
            issues.append(("ERROR", "nir", ref, "Пустое наименование НИР F2."))
        if f18 is None or int(f18) < 0:
            issues.append(("ERROR", "nir", ref, "Некорректная сумма F18."))
        parts = list(_grnti_parts(f10 or ""))
        if len(parts) == 0:
            issues.append(("WARNING", "nir", ref, "Не указан код ГРНТИ."))
        if len(parts) > 2:
            issues.append(("ERROR", "nir", ref, "В F10 указано более двух кодов ГРНТИ."))
        for code in parts:
            m = re.match(r"^(\d{1,2})", code)
            if not m:
                issues.append(("ERROR", "nir", ref, f"Не распознан код ГРНТИ {code!r}."))
                continue
            top = f"{int(m.group(1)):02d}"
            if top not in valid_rub:
                issues.append(("WARNING", "nir", ref, f"Для кода ГРНТИ {code!r} не найдена рубрика верхнего уровня."))

    if write_log:
        conn.execute("DELETE FROM validation_log")
        conn.executemany(
            "INSERT INTO validation_log(severity,table_name,row_ref,message) VALUES (?,?,?,?)",
            issues,
        )

    if own:
        conn.commit()
        conn.close()
    return issues


if __name__ == "__main__":
    path = create_database(force=True)
    print(f"База создана: {path}")
    con = sqlite3.connect(path)
    print("НИР:", con.execute("SELECT COUNT(*) FROM nir").fetchone()[0])
    print("Вузы:", con.execute("SELECT COUNT(*) FROM vuz").fetchone()[0])
    print("Финансирование:", con.execute("SELECT COUNT(*) FROM financing").fetchone()[0])
    print("Ошибок/предупреждений:", con.execute("SELECT COUNT(*) FROM validation_log").fetchone()[0])
    con.close()
