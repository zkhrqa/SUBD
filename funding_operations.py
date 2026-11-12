"""Расчёт траншей и утверждение распоряжений в SQLite."""
from contextlib import closing
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from database import DB_PATH, _open_sqlite


MAX_RUBLES = 2**63 - 1


@dataclass(frozen=True)
class UniversityFunding:
    code: int
    name: str
    planned: int
    actual: int


@dataclass(frozen=True)
class FundingSnapshot:
    universities: tuple[UniversityFunding, ...]
    unassigned_works: int

    @property
    def planned(self) -> int:
        return sum(vuz.planned for vuz in self.universities)

    @property
    def actual(self) -> int:
        return sum(vuz.actual for vuz in self.universities)


@dataclass(frozen=True)
class FundingOrder:
    number: int | None
    issued_on: str | None
    quarter: int
    requested: int
    planned: int
    allocations: tuple[tuple[int, str, int], ...]
    token: str | None = None

    @property
    def total(self) -> int:
        return sum(amount for _, _, amount in self.allocations)


@dataclass(frozen=True)
class FundingPreview:
    token: str
    snapshot: FundingSnapshot
    order: FundingOrder


def prepare_funding(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS funding_orders (
            number INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            issued_on TEXT NOT NULL,
            quarter INTEGER NOT NULL CHECK(quarter BETWEEN 1 AND 4),
            requested INTEGER NOT NULL CHECK(requested > 0),
            planned INTEGER NOT NULL CHECK(planned > 0),
            allocations TEXT NOT NULL
        );
    """ + "\n".join(f"""
        CREATE TRIGGER IF NOT EXISTS nir_funding_lock_{event.lower()}
        BEFORE {event} ON nir
        WHEN EXISTS (SELECT 1 FROM funding_orders)
        BEGIN SELECT RAISE(ABORT, 'После утверждения первого распоряжения изменения НИР заблокированы.'); END;
    """ for event in ("INSERT", "UPDATE", "DELETE")) + "COMMIT;")


def read_snapshot(conn: sqlite3.Connection) -> FundingSnapshot:
    universities = []
    unassigned_works = 0
    for code, name, planned, actual, works in conn.execute("""
        SELECT codvuz, z2, z3, z18, numworks FROM financing_display ORDER BY codvuz
    """):
        if code is None:
            unassigned_works = works
        else:
            universities.append(UniversityFunding(code, name or f"Вуз №{code}", planned, actual or 0))
    return FundingSnapshot(tuple(universities), unassigned_works)


def calculate_order(snapshot: FundingSnapshot, requested: int, quarter: int) -> FundingOrder:
    if type(quarter) is not int or not 1 <= quarter <= 4:
        raise ValueError("Выберите квартал от 1 до 4.")
    if type(requested) is not int or not 0 < requested <= MAX_RUBLES:
        raise ValueError("Введите положительную сумму в целых рублях допустимого размера.")
    if snapshot.unassigned_works:
        raise ValueError("Перед распределением укажите вузы для всех НИР.")
    planned = snapshot.planned
    if not 0 < planned <= MAX_RUBLES:
        raise ValueError("Для распределения нужен положительный общий план допустимого размера.")
    allocations = tuple((vuz.code, vuz.name, requested * vuz.planned // planned)
                        for vuz in snapshot.universities)
    return FundingOrder(None, None, quarter, requested, planned, allocations)


def preview_order(snapshot: FundingSnapshot, requested: int, quarter: int) -> FundingPreview:
    return FundingPreview(uuid4().hex, snapshot, calculate_order(snapshot, requested, quarter))


ORDER_COLUMNS = "number, issued_on, quarter, requested, planned, allocations, token"


def decode_order(row) -> FundingOrder:
    number, issued_on, quarter, requested, planned, allocations, token = row
    return FundingOrder(number, issued_on, quarter, requested, planned,
                        tuple(tuple(line) for line in json.loads(allocations)), token)


def load_funding(path: Path = DB_PATH) -> tuple[FundingSnapshot, list[FundingOrder]]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        snapshot = read_snapshot(conn)
        orders = [decode_order(row) for row in conn.execute(
            f"SELECT {ORDER_COLUMNS} FROM funding_orders ORDER BY number DESC"
        )]
        return snapshot, orders


def approve_order(preview: FundingPreview, *, path: Path = DB_PATH) -> FundingOrder:
    with closing(_open_sqlite(path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            f"SELECT {ORDER_COLUMNS} FROM funding_orders WHERE token=?", (preview.token,)
        ).fetchone()
        if existing is not None:
            return decode_order(existing)

        current = read_snapshot(conn)
        if current != preview.snapshot:
            raise ValueError("План или факт изменились. Обновите сводку и рассчитайте транш заново.")
        order = calculate_order(current, preview.order.requested, preview.order.quarter)
        if order.total == 0:
            raise ValueError("После округления ни одному вузу не выделены средства. Увеличьте сумму транша.")
        for vuz, (_, _, amount) in zip(current.universities, order.allocations):
            if vuz.actual + amount > MAX_RUBLES:
                raise ValueError(f"Фактическое финансирование вуза «{vuz.name}» превысит допустимый размер.")

        issued_on = date.today().isoformat()
        cursor = conn.execute("""
            INSERT INTO funding_orders(token,issued_on,quarter,requested,planned,allocations)
            VALUES (?,?,?,?,?,?)
        """, (preview.token, issued_on, order.quarter, order.requested, order.planned,
              json.dumps(order.allocations, ensure_ascii=False)))
        conn.executemany("UPDATE financing SET z18=COALESCE(z18,0)+? WHERE codvuz=?",
                         [(amount, code) for code, _, amount in order.allocations if amount])
        return FundingOrder(cursor.lastrowid, issued_on, order.quarter, order.requested,
                            order.planned, order.allocations, preview.token)
