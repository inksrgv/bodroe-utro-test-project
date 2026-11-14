"""HTTP API единого сервиса лояльности (FR-23: интеграция с кассой и приложением).

Контракт одинаков для обоих каналов: приложение и касса — равноправные клиенты
одного API, поэтому баланс физически не может разойтись между каналами.
"""
from __future__ import annotations

import time
from typing import Literal

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import loyalty
from .db import balance_of, init_db, transaction

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Бодрое утро · единый сервис лояльности",
    version="1.0.0-mvp",
    description="Прототип инкремента 1: единый бонусный баланс для кассы и приложения.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- модели API
class AccrualRequest(BaseModel):
    customer_id: str
    order_amount: float = Field(gt=0, le=100000)
    order_id: str
    channel: Literal["pos", "app"]
    terminal_id: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=64)


class RedeemRequest(BaseModel):
    customer_id: str
    points: int = Field(gt=0, le=100000)
    order_amount: float = Field(gt=0, le=100000)
    order_id: str
    channel: Literal["pos", "app"]
    terminal_id: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=64)


class ReversalRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64)
    reason: str = "Отмена заказа"


class OpResponse(BaseModel):
    operation_id: str | None
    points: int
    balance: int
    duplicate: bool = False
    message: str | None = None
    elapsed_ms: float


# ------------------------------------------------------------- идентификация
@app.get("/api/customers/lookup")
def lookup(q: str = Query(..., min_length=2, description="Карта, телефон или его часть")):
    """FR-02 / NFR-03: быстрый поиск клиента на кассе."""
    started = time.perf_counter()
    with transaction() as conn:
        found = loyalty.find_customer(conn, q)
        balance = balance_of(conn, found["id"]) if found else 0
    return {
        "found": bool(found),
        "customer": found,
        "balance": balance,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@app.get("/api/customers/{customer_id}/balance")
def get_balance(customer_id: str):
    """FR-03/FR-04: одинаковый баланс для приложения и кассы."""
    started = time.perf_counter()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not row:
            raise HTTPException(404, {"error_code": "CUSTOMER_NOT_FOUND",
                                      "message": "Клиент не найден"})
        return {
            "customer_id": customer_id,
            "name": row["name"],
            "status": row["status"],
            "balance": balance_of(conn, customer_id),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }


@app.get("/api/customers/{customer_id}/operations")
def operations(customer_id: str, limit: int = Query(20, ge=1, le=200)):
    """История операций — одна и та же для кассы и приложения (FR-10)."""
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM operations WHERE customer_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (customer_id, limit),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


# ---------------------------------------------------------------- проводки
@app.post("/api/operations/accrual", response_model=OpResponse)
def post_accrual(req: AccrualRequest):
    return _run(lambda conn: loyalty.accrue(
        conn, customer_id=req.customer_id, order_amount=req.order_amount,
        order_id=req.order_id, channel=req.channel,
        idempotency_key=req.idempotency_key, terminal_id=req.terminal_id))


@app.post("/api/operations/redemption", response_model=OpResponse)
def post_redemption(req: RedeemRequest):
    return _run(lambda conn: loyalty.redeem(
        conn, customer_id=req.customer_id, points=req.points,
        order_amount=req.order_amount, order_id=req.order_id, channel=req.channel,
        idempotency_key=req.idempotency_key, terminal_id=req.terminal_id))


@app.post("/api/operations/{operation_id}/reverse", response_model=OpResponse)
def post_reversal(operation_id: str, req: ReversalRequest):
    return _run(lambda conn: loyalty.reverse(
        conn, operation_id=operation_id, idempotency_key=req.idempotency_key,
        reason=req.reason))


def _run(fn) -> dict:
    started = time.perf_counter()
    try:
        with transaction() as conn:
            res = fn(conn)
    except loyalty.LoyaltyError as e:
        raise HTTPException(422, {"error_code": e.code, "message": e.message})
    return {
        "operation_id": res.operation_id,
        "points": res.points,
        "balance": res.balance,
        "duplicate": res.duplicate,
        "message": res.message,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


# ------------------------------------------------------------------ утилиты
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index():
    import os
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
