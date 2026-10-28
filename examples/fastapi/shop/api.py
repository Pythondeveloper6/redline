"""Deliberately bad endpoints: every one of them is a Redline demo."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from .db import AsyncSession, SyncSession
from .models import AuditLog, Customer, Order, OrderItem, Product

router = APIRouter()

# SHOP_REFACTOR=1 simulates a refactor for `redline check` (see the README):
# N+1 fixed, one endpoint slower, one response field changed, one endpoint broken.
REFACTOR = os.environ.get("SHOP_REFACTOR") == "1"


@router.get("/")
async def index() -> dict:
    return {
        "try": [
            "/patterns  (aggregation, deep pagination, ORM instances; check-suite rules)",
            "/orders  (N+1 on order items)",
            "/orders/customers  (N+1 on customers, many-to-one)",
            "/orders/1  (a healthy endpoint)",
            "/report/duplicates  (the same query, repeatedly)",
            "/audit/search?q=refund  (slow query, full table scan)",
            "/slow  (slow endpoint)",
            "/sync/orders  (sync def endpoint, lazy-loaded tags)",
            "/boom  (500)",
            "/pricing  (outbound httpx call)",
            "/orders/1?_redline=profile  (flamegraph)",
        ],
        "redline": "/_redline/",
    }


@router.get("/orders")
async def list_orders() -> list[dict]:
    """N+1: one query for the orders, then one per order for its items."""
    async with AsyncSession() as session:
        if REFACTOR:  # refactor: one extra query instead of one per order
            stmt = select(Order).options(selectinload(Order.items)).order_by(Order.id).limit(30)
            orders = (await session.scalars(stmt)).all()
            return [{"id": o.id, "status": o.status, "items": len(o.items)} for o in orders]
        orders = (await session.scalars(select(Order).order_by(Order.id).limit(30))).all()
        out = []
        for order in orders:
            items = await session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))
            out.append({"id": order.id, "status": order.status, "items": len(items.all())})
        return out


@router.get("/orders/customers")
async def orders_with_customers() -> list[dict]:
    """N+1, many-to-one: a customer lookup per order."""
    async with AsyncSession() as session:
        orders = (await session.scalars(select(Order).order_by(Order.id).limit(25))).all()
        rows = []
        for order in orders:
            customer = await session.get(Customer, order.customer_id)
            rows.append({"order": order.id, "customer": customer.name if customer else None})
        return rows


@router.get("/orders/{order_id}")
async def get_order(order_id: int) -> dict:
    if REFACTOR:  # refactor: a slower code path
        await asyncio.sleep(0.05)
    async with AsyncSession() as session:
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        total = await session.scalar(
            select(func.sum(OrderItem.quantity)).where(OrderItem.order_id == order_id)
        )
        return {"id": order.id, "status": order.status, "units": total}


@router.get("/report/duplicates")
async def duplicates() -> dict:
    """The same query several times in one request."""
    async with AsyncSession() as session:
        counts = []
        for _ in range(4):
            counts.append(await session.scalar(select(func.count()).select_from(Product)))
        if REFACTOR:  # refactor: the field is now a string
            return {"products": str(counts[-1])}
        return {"products": counts[-1]}


@router.get("/audit/search")
async def audit_search(q: str = "refund") -> dict:
    """Slow query: LIKE on an unindexed text column of a big table."""
    async with AsyncSession() as session:
        hits = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.message.like(f"%{q}%"))
        )
        return {"q": q, "hits": hits}


@router.get("/slow")
async def slow() -> dict:
    """Slow endpoint with almost no database time: the time is spent awaiting."""
    async with AsyncSession() as session:
        await session.scalar(select(func.count()).select_from(Customer))
    await asyncio.sleep(0.8)
    return {"slept": 0.8}


@router.get("/sync/orders")
def sync_orders() -> list[dict]:
    """A plain `def` endpoint (runs in the threadpool) lazy-loading a collection."""
    if REFACTOR:  # refactor: a regression that breaks the endpoint
        raise RuntimeError("tags backend not configured")
    with SyncSession() as session:
        orders = session.scalars(select(Order).order_by(Order.id).limit(20)).all()
        return [{"id": o.id, "tags": [t.name for t in o.tags]} for o in orders]


@router.get("/status")
async def status() -> dict:
    """A health check with volatile fields (changes on every call)."""
    return {"ok": True, "now": time.time(), "request_id": uuid.uuid4().hex}


@router.get("/boom")
async def boom() -> dict:
    async with AsyncSession() as session:
        await session.scalar(select(func.count()).select_from(Order))
    return {"ratio": 1 / 0}


@router.get("/internal/price/{product_id}")
async def internal_price(product_id: int) -> dict:
    async with AsyncSession() as session:
        product = await session.get(Product, product_id)
        return {"id": product_id, "price": float(product.price) if product else None}


@router.get("/pricing")
async def pricing(request: Request) -> dict:
    """Calls another service over HTTP - here an internal route, in-process."""
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://pricing.local") as client:
        prices = []
        for pid in (1, 2, 3):
            r = await client.get(f"/internal/price/{pid}", params={"api_key": "demo-key-123"})
            prices.append(r.json()["price"])
        return {"prices": prices}


class Checkout(BaseModel):
    email: str
    card_number: str
    password: str | None = None


@router.post("/checkout")
async def checkout(payload: Checkout) -> dict:
    """Send a body with secrets to see Redline redact them."""
    return {"ok": True, "email": payload.email}


@router.get("/patterns")
def patterns() -> dict:
    """Aggregates, deep pagination and ORM hydration; refactor mode fixes the extra work."""
    with SyncSession() as db:
        if REFACTOR:
            count, total = db.execute(
                select(func.count(Product.id), func.sum(Product.price)).where(Product.price >= 0)
            ).one()
            rows = db.execute(
                select(Product.id, Product.name, Product.price).order_by(Product.id).limit(30)
            ).all()
        else:
            count = db.scalar(select(func.count(Product.id)).where(Product.price >= 0))
            total = db.scalar(select(func.sum(Product.price)).where(Product.price >= 0))
            db.execute(select(Product.id).order_by(Product.id).limit(5).offset(10000)).all()
            rows = db.scalars(select(Product).order_by(Product.id).limit(30)).all()
        items = [{"id": row.id, "name": row.name, "price": float(row.price)} for row in rows]
        if REFACTOR:
            items.reverse()
        return {
            "items": items,
            "count": count,
            "total": float(total or 0) + (0.005 if REFACTOR else 0),
        }


@router.post("/patterns/writes")
def pattern_writes() -> dict:
    """Execute six individual writes, then roll them all back. No changes are committed."""
    with SyncSession() as db:
        ids = db.scalars(select(Product.id).order_by(Product.id).limit(6)).all()
        for ident in ids:
            db.execute(update(Product).where(Product.id == ident).values(price=Product.price))
        db.rollback()
    return {"writes": len(ids), "rolled_back": True}
