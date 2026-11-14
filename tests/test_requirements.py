"""Проверки соответствия требованиям. Запуск: python3 -m pytest -q

Каждый тест помечен ID требования, которое он верифицирует, — это нижний
уровень матрицы трассировки ПР4 (FR/NFR → тест-кейс).
"""
from __future__ import annotations

import concurrent.futures
import os
import statistics
import time

import pytest

os.environ["LOYALTY_DB"] = "/tmp/loyalty_test.db"

from app import loyalty  # noqa: E402
from app.db import balance_of, init_db, transaction  # noqa: E402


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    init_db(reset=True)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO customers (id, name, phone, card_code, status, limit_share)"
            " VALUES ('C1','Тест Клиент','+79000000001','7777 0001','Новичок',0.5)"
        )
        conn.execute(
            "INSERT INTO customers (id, name, phone, card_code, status, limit_share)"
            " VALUES ('C2','Второй Клиент','+79000000002','7777 0002','Новичок',0.5)"
        )
    return TestClient(app)


def accrue(client, cid="C1", amount=420, channel="pos", key=None):
    return client.post("/api/operations/accrual", json={
        "customer_id": cid, "order_amount": amount, "order_id": "ORD-1",
        "channel": channel, "terminal_id": "POS-01" if channel == "pos" else "APP",
        "idempotency_key": key or f"accr-{time.time_ns()}",
    })


def redeem(client, pts=50, cid="C1", amount=420, channel="pos", key=None):
    return client.post("/api/operations/redemption", json={
        "customer_id": cid, "points": pts, "order_amount": amount, "order_id": "ORD-2",
        "channel": channel, "terminal_id": "POS-01", "idempotency_key": key or f"rdm-{time.time_ns()}",
    })


# --- FR-02, NFR-03 -----------------------------------------------------------
@pytest.mark.parametrize("query", ["7777 0001", "+79000000001", "0001"])
def test_lookup_by_card_phone_and_tail(client, query):
    """FR-02 / NFR-03: клиент находится по карте, телефону и его части."""
    r = client.get("/api/customers/lookup", params={"q": query})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["elapsed_ms"] < 2000, "поиск обязан укладываться в 2 с"


# --- FR-05 ------------------------------------------------------------------
def test_accrual_increases_balance(client):
    r = accrue(client, amount=1000)
    assert r.status_code == 200
    assert r.json()["points"] == 100          # 10% от 1000 ₽
    assert r.json()["balance"] == 100


# --- FR-04: единый баланс для обоих каналов ---------------------------------
def test_same_balance_across_channels(client):
    """FR-04: баланс, начисленный на кассе, сразу виден в приложении и наоборот."""
    accrue(client, amount=500, channel="pos")
    pos = client.get("/api/customers/C1/balance").json()["balance"]
    accrue(client, amount=500, channel="app")
    both = client.get("/api/customers/C1/balance").json()["balance"]
    assert pos == 50
    assert both == 100, "приложение не должно вести отдельный баланс"


# --- FR-06, FR-07, FR-08 ----------------------------------------------------
def test_redeem_reduces_balance(client):
    accrue(client, amount=1000)
    r = redeem(client, pts=100)
    assert r.status_code == 200
    assert r.json()["balance"] == 0


def test_redeem_rejected_when_insufficient(client):
    accrue(client, amount=100)                # 10 баллов
    r = redeem(client, pts=300)
    assert r.status_code == 422
    assert r.json()["detail"]["error_code"] == "INSUFFICIENT_BALANCE"


def test_balance_never_negative(client):
    """FR-08: баланс не может уйти в минус ни при каком порядке операций."""
    accrue(client, amount=300)
    for pts in (50, 50, 50, 50):
        redeem(client, pts=pts)
    assert client.get("/api/customers/C1/balance").json()["balance"] >= 0


# --- FR-15: идемпотентность -------------------------------------------------
def test_duplicate_key_does_not_double_charge(client):
    accrue(client, amount=1000)
    first = redeem(client, pts=50, key="same-key-123456")
    second = redeem(client, pts=50, key="same-key-123456")
    assert first.json()["balance"] == 50
    assert second.json()["duplicate"] is True
    assert second.json()["balance"] == 50, "повтор не должен списывать второй раз"


def test_duplicate_accrual_not_doubled(client):
    accrue(client, amount=1000, key="accr-dup-key-1")
    accrue(client, amount=1000, key="accr-dup-key-1")
    assert client.get("/api/customers/C1/balance").json()["balance"] == 100


# --- FR-10: журнал операций -------------------------------------------------
def test_operations_journal(client):
    accrue(client, amount=1000)
    redeem(client, pts=50)
    items = client.get("/api/customers/C1/operations").json()["items"]
    assert len(items) == 2
    assert {i["op_type"] for i in items} == {"accrual", "redemption"}


# --- Отмена операции --------------------------------------------------------
def test_reversal_restores_balance(client):
    acc = accrue(client, amount=1000).json()
    rev = client.post(f"/api/operations/{acc['operation_id']}/reverse",
                      json={"idempotency_key": "rev-key-123456", "reason": "Тест"})
    assert rev.status_code == 200
    assert rev.json()["balance"] == 0


# --- NFR-01 / NFR-03: производительность ------------------------------------
def test_pos_operation_under_5_seconds(client):
    """NFR-01: бонусная операция на кассе ≤ 5 с. Замер по 30 операциям."""
    durations = []
    for i in range(30):
        start = time.perf_counter()
        accrue(client, amount=420 + i)
        durations.append((time.perf_counter() - start) * 1000)
    p95 = statistics.quantiles(durations, n=20)[18]
    assert p95 < 5000, f"p95 = {p95:.0f} мс > 5000 мс"


# --- Конкурентность: защита от двойного списания ----------------------------
def test_concurrent_redemption_cannot_overdraw():
    """FR-08 + FR-15: 10 параллельных списаний по 50 б. при балансе 100 б.

    Ровно две операции должны пройти, остальные — получить отказ.
    Это тот самый дефект, который ловит BEGIN IMMEDIATE + UNIQUE(idempotency_key).
    """
    db = "/tmp/loyalty_concurrency.db"
    os.environ["LOYALTY_DB"] = db
    import importlib

    from app import db as dbmod
    importlib.reload(dbmod)
    from app import loyalty as loy
    importlib.reload(loy)

    dbmod.init_db(reset=True)
    with dbmod.transaction() as conn:
        conn.execute(
            "INSERT INTO customers (id, name, phone, card_code, status, limit_share)"
            " VALUES ('C1','Тест','+79000000001','7777 0001','Новичок',1.0)"
        )
        loy.accrue(conn, customer_id="C1", order_amount=1000, order_id="O1",
                   channel="pos", idempotency_key="setup-key-0001")

    def attempt(i):
        try:
            with dbmod.transaction() as conn:
                return loy.redeem(conn, customer_id="C1", points=50, order_amount=1000,
                                  order_id=f"O{i}", channel="pos",
                                  idempotency_key=f"conc-key-{i:04d}").ok
        except loy.LoyaltyError:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))

    with dbmod.transaction() as conn:
        final = dbmod.balance_of(conn, "C1")

    assert sum(results) == 2, f"прошло {sum(results)} списаний вместо 2"
    assert final == 0, f"баланс {final}, ожидался 0 — ушли в минус"
