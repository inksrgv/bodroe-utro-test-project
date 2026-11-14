"""Слой доступа к данным: SQLite как «единый сервис лояльности».

Ключевая идея прототипа (инкремент 1): единственный источник истины о балансе —
журнал операций (ledger). Никакой отдельный баланс в приложении или на кассе
не хранится (ограничение из UC-01: приложение не ведёт независимый баланс).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("LOYALTY_DB", os.path.join(os.path.dirname(__file__), "loyalty.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL UNIQUE,
    card_code   TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'Новичок',   -- статус лояльности (FR-14, инкремент 3)
    limit_share REAL NOT NULL DEFAULT 0.5          -- доля чека, оплачиваемая бонусами
);

CREATE TABLE IF NOT EXISTS operations (
    id              TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    op_type         TEXT NOT NULL CHECK (op_type IN ('accrual', 'redemption', 'reversal')),
    points          INTEGER NOT NULL,              -- знаковое: + начисление, - списание
    channel         TEXT NOT NULL CHECK (channel IN ('pos', 'app')),
    terminal_id     TEXT,                          -- кофейня/устройство, откуда пришла операция
    order_id        TEXT,
    order_amount    REAL NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,          -- FR-15: защита от двойного проведения
    status          TEXT NOT NULL,                 -- applied | reversed
    reverses_op_id  TEXT,                          -- ссылка на отменяемую проводку
    reject_reason   TEXT,
    created_at      TEXT NOT NULL                  -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS ix_ops_customer ON operations(customer_id, created_at);
CREATE INDEX IF NOT EXISTS ix_ops_created  ON operations(created_at);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def transaction():
    """Транзакция с немедленной блокировкой на запись.

    BEGIN IMMEDIATE + UNIQUE(idempotency_key) — две опоры корректности:
    два параллельных списания не могут одновременно увидеть один и тот же остаток.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db(reset: bool = False) -> None:
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def balance_of(conn, customer_id: str) -> int:
    row = conn.execute(
        # Отменённая проводка остаётся в журнале (аудит), но не влияет на остаток:
        # её исключает компенсирующая запись. Поэтому считаем applied + reversed.
        "SELECT COALESCE(SUM(points), 0) AS b FROM operations "
        "WHERE customer_id = ? AND status IN ('applied','reversed')",
        (customer_id,),
    ).fetchone()
    return int(row["b"])
