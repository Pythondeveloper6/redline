"""Deliberately bad endpoints: every one of them is a Redline demo."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from .db import AsyncSession, SyncSession
from .models import AuditLog, Customer, Order, OrderItem, Product

router = APIRouter()


@router.get("/")
async def index() -> dict:
    return {
        "try": [
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
    with SyncSession() as session:
        orders = session.scalars(select(Order).order_by(Order.id).limit(20)).all()
        return [{"id": o.id, "tags": [t.name for t in o.tags]} for o in orders]


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
