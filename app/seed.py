"""Демо-данные: 5 пилотных кофеен, несколько клиентов, история операций."""
from __future__ import annotations

import datetime as dt
import random

from .db import init_db, transaction
from .loyalty import accrue, redeem

PILOT_TERMINALS = [
    ("POS-01", "Кофейня №1 · Невский, 12"),
    ("POS-02", "Кофейня №2 · Лиговский, 74"),
    ("POS-03", "Кофейня №3 · Рубинштейна, 5"),
    ("POS-04", "Кофейня №4 · Петроградская, 18"),
    ("POS-05", "Кофейня №5 · Васильевский, 33"),
]

CUSTOMERS = [
    ("CUST-0001", "Анна Петрова", "+79110001122", "1000 0001 2345"),
    ("CUST-0002", "Илья Соколов", "+79110003344", "1000 0001 2346"),
    ("CUST-0003", "Мария Ким", "+79110005566", "1000 0001 2347"),
    ("CUST-0004", "Дмитрий Орлов", "+79110007788", "1000 0001 2348"),
]


def seed(reset: bool = True) -> None:
    init_db(reset=reset)
    random.seed(42)
    with transaction() as conn:
        for cid, name, phone, card in CUSTOMERS:
            conn.execute(
                "INSERT OR IGNORE INTO customers (id, name, phone, card_code, status, limit_share)"
                " VALUES (?,?,?,?,?,?)",
                (cid, name, phone, card, "Серебро" if cid.endswith("1") else "Новичок", 0.5),
            )

    counter = 0
    for cid, *_ in CUSTOMERS:
        # имитируем покупки на кассе и в приложении за прошлые дни
        for _ in range(random.randint(12, 20)):
            counter += 1
            amount = round(random.choice([180, 240, 320, 410, 520]) + random.randint(0, 90), 2)
            term = random.choice(PILOT_TERMINALS)
            channel = random.choice(["pos", "app"])
            with transaction() as conn:
                accrue(conn, customer_id=cid, order_amount=amount,
                       order_id=f"ORD-{counter:04d}", channel=channel,
                       idempotency_key=f"seed-accr-{counter:04d}",
                       terminal_id=term[0] if channel == "pos" else "APP")

    # одна показательная операция списания
    with transaction() as conn:
        redeem(conn, customer_id="CUST-0001", points=100, order_amount=420.0,
               order_id="ORD-R001", channel="pos", idempotency_key="seed-redeem-0001",
               terminal_id="POS-01")

    with transaction() as conn:
        for cid, name, phone, card in CUSTOMERS:
            b = conn.execute(
                "SELECT COALESCE(SUM(points),0) b FROM operations "
                "WHERE customer_id=? AND status='applied'", (cid,)).fetchone()["b"]
            print(f"{cid}  {name:<16} {card}  баланс: {b} б.")


if __name__ == "__main__":
    seed()
