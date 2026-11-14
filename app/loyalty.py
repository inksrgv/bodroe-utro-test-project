"""Ядро программы лояльности. Реализует FR-05…FR-10 и FR-15.

Правила начисления вынесены в настройки, чтобы маркетолог мог их менять
без релиза (FR-11/FR-12 — инкремент 3, здесь только каркас).
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from .db import balance_of

# Правила программы (в прототипе — константы; в целевой системе — справочник)
ACCRUAL_SHARE_OF_ORDER = 0.1   # начисляем 10% стоимости чека баллами (1 б. = 10 ₽)
MIN_REDEEM_POINTS = 50         # минимальный шаг списания
POINTS_STEP = 50               # списывать кратно 50 баллам


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class OpResult:
    ok: bool
    operation_id: str | None = None
    points: int = 0
    balance: int = 0
    error_code: str | None = None
    message: str | None = None
    duplicate: bool = False


class LoyaltyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fetch_customer(conn, *, customer_id=None, card_code=None, phone=None):
    if customer_id:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    elif card_code:
        row = conn.execute("SELECT * FROM customers WHERE card_code = ?", (card_code,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
    if not row:
        raise LoyaltyError("CUSTOMER_NOT_FOUND", "Клиент не найден в программе лояльности")
    return row


def find_customer(conn, query: str):
    """FR-02: идентификация клиента на кассе (NFR-03 — ≤2 с).

    Поиск по номеру карты, телефону или последним цифрам телефона.
    Индексы по card_code и phone обеспечивают выполнение NFR-03.
    """
    q = (query or "").strip()
    if not q:
        return None
    row = conn.execute(
        "SELECT * FROM customers WHERE card_code = ? OR phone = ? OR phone LIKE ? LIMIT 1",
        (q, q, f"%{q}"),
    ).fetchone()
    return dict(row) if row else None


def accrue(conn, *, customer_id: str, order_amount: float, order_id: str,
           channel: str, idempotency_key: str, terminal_id: str | None = None) -> OpResult:
    """FR-05: начисление бонусов на единый баланс после подтверждения покупки."""
    cust = _fetch_customer(conn, customer_id=customer_id)

    # FR-15: если ключ уже использован — возвращаем прежний результат, баланс не меняем
    prev = conn.execute(
        "SELECT * FROM operations WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    if prev:
        return OpResult(
            ok=prev["status"] == "applied",
            operation_id=prev["id"],
            points=prev["points"],
            balance=balance_of(conn, prev["customer_id"]),
            duplicate=True,
            message="Операция уже была проведена ранее",
        )

    points = int(order_amount * ACCRUAL_SHARE_OF_ORDER // 1)
    if points <= 0:
        raise LoyaltyError("NOTHING_TO_ACCRUE", "Сумма чека слишком мала для начисления бонусов")

    op_id = _write_op(
        conn, cust, op_type="accrual", points=points, channel=channel,
        order_id=order_id, order_amount=order_amount, idempotency_key=idempotency_key,
        terminal_id=terminal_id,
    )
    return OpResult(ok=True, operation_id=op_id, points=points,
                    balance=balance_of(conn, cust["id"]))


def redeem(conn, *, customer_id: str, points: int, order_amount: float, order_id: str,
           channel: str, idempotency_key: str, terminal_id: str | None = None) -> OpResult:
    """FR-06/FR-07/FR-08: списание с проверкой достаточности и запретом минуса."""
    cust = _fetch_customer(conn, customer_id=customer_id)

    prev = conn.execute(
        "SELECT * FROM operations WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    if prev:  # FR-15
        return OpResult(
            ok=prev["status"] == "applied",
            operation_id=prev["id"],
            points=prev["points"],
            balance=balance_of(conn, prev["customer_id"]),
            duplicate=True,
            message="Операция уже была проведена ранее",
        )

    if points <= 0:
        raise LoyaltyError("INVALID_POINTS", "Количество бонусов должно быть больше нуля")
    if points % POINTS_STEP != 0:
        raise LoyaltyError("INVALID_STEP", f"Бонусы списываются кратно {POINTS_STEP}")
    if points < MIN_REDEEM_POINTS:
        raise LoyaltyError("BELOW_MIN", f"Минимальное списание — {MIN_REDEEM_POINTS} бонусов")

    balance = balance_of(conn, cust["id"])
    if points > balance:  # FR-07 + FR-08
        raise LoyaltyError(
            "INSUFFICIENT_BALANCE",
            f"Недостаточно бонусов: доступно {balance}, запрошено {points}",
        )

    # Ограничение программы: бонусами можно закрыть не более доли чека
    max_points_by_order = int(order_amount * cust["limit_share"] / 1.0)
    if points > max_points_by_order:
        raise LoyaltyError(
            "LIMIT_EXCEEDED",
            f"Бонусами можно оплатить не более {int(cust['limit_share'] * 100)}% чека "
            f"({max_points_by_order} б.)",
        )

    op_id = _write_op(
        conn, cust, op_type="redemption", points=-points, channel=channel,
        order_id=order_id, order_amount=order_amount, idempotency_key=idempotency_key,
        terminal_id=terminal_id,
    )
    return OpResult(ok=True, operation_id=op_id, points=-points,
                    balance=balance_of(conn, cust["id"]))


def reverse(conn, *, operation_id: str, idempotency_key: str,
            reason: str = "Отмена заказа") -> OpResult:
    """Отмена ошибочно проведённой операции (компенсирующая запись, баланс не переписываем)."""
    orig = conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
    if not orig:
        raise LoyaltyError("OP_NOT_FOUND", "Операция не найдена")
    if orig["status"] != "applied":
        raise LoyaltyError("OP_NOT_APPLIED", "Операция уже отменена")

    prev = conn.execute(
        "SELECT * FROM operations WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    if prev:
        return OpResult(ok=True, operation_id=prev["id"], duplicate=True,
                        balance=balance_of(conn, orig["customer_id"]),
                        message="Отмена уже выполнена")

    cust = _fetch_customer(conn, customer_id=orig["customer_id"])
    op_id = _write_op(
        conn, cust, op_type="reversal", points=-orig["points"], channel=orig["channel"],
        order_id=orig["order_id"], order_amount=orig["order_amount"],
        idempotency_key=idempotency_key, terminal_id=orig["terminal_id"],
    )
    conn.execute("UPDATE operations SET status='reversed', reverses_op_id = ? WHERE id = ?",
                 (op_id, operation_id))
    return OpResult(ok=True, operation_id=op_id, points=-orig["points"],
                    balance=balance_of(conn, cust["id"]), message=reason)


def _write_op(conn, cust, *, op_type: str, points: int, channel: str, order_id: str,
              order_amount: float, idempotency_key: str, terminal_id: str | None) -> str:
    op_id = f"OP-{uuid.uuid4().hex[:12].upper()}"
    conn.execute(
        """INSERT INTO operations
           (id, customer_id, op_type, points, channel, terminal_id, order_id,
            order_amount, idempotency_key, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?, 'applied', ?)""",
        (op_id, cust["id"], op_type, points, channel, terminal_id,
         order_id, order_amount, idempotency_key, utcnow()),
    )
    return op_id
