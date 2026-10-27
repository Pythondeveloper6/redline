"""Deliberately bad views: every one of them is a Redline demo."""

from __future__ import annotations

import os
import time
import uuid

import httpx
from flask import Blueprint, current_app, render_template, request
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .db import Session
from .models import AuditLog, Customer, Order, OrderItem, Product

bp = Blueprint("shop", __name__)

# SHOP_REFACTOR=1 simulates a refactor for `redline check` (see the README):
# N+1 fixed, one endpoint slower, one response field changed, one endpoint broken.
REFACTOR = os.environ.get("SHOP_REFACTOR") == "1"


@bp.get("/")
def index() -> dict:
    return {
        "try": [
            "/orders  (N+1 on order items)",
            "/orders/customers  (N+1 on customers, many-to-one)",
            "/orders/tags  (lazy-loaded many-to-many tags)",
            "/orders/page  (lazy loading inside a Jinja template)",
            "/orders/1  (a healthy endpoint)",
            "/report/duplicates  (the same query, repeatedly)",
            "/audit/search?q=refund  (slow query, full table scan)",
            "/slow  (slow endpoint)",
            "/boom  (500)",
            "/pricing  (outbound httpx call)",
            "/orders/2?_redline=profile  (flamegraph)",
        ],
        "redline": "/_redline/",
    }


@bp.get("/orders")
def list_orders() -> list[dict]:
    """N+1: one query for the orders, then one per order for its items."""
    with Session() as session:
        if REFACTOR:  # refactor: one extra query instead of one per order
            stmt = select(Order).options(selectinload(Order.items)).order_by(Order.id).limit(30)
            return [
                {"id": o.id, "status": o.status, "items": len(o.items)}
                for o in session.scalars(stmt).all()
            ]
        orders = session.scalars(select(Order).order_by(Order.id).limit(30)).all()
        out = []
        for order in orders:
            items = session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))
            out.append({"id": order.id, "status": order.status, "items": len(items.all())})
        return out


@bp.get("/orders/customers")
def orders_with_customers() -> list[dict]:
    """N+1, many-to-one: a customer lookup per order."""
    with Session() as session:
        orders = session.scalars(select(Order).order_by(Order.id).limit(25)).all()
        rows = []
        for order in orders:
            customer = session.get(Customer, order.customer_id)
            rows.append({"order": order.id, "customer": customer.name if customer else None})
        return rows


@bp.get("/orders/tags")
def orders_with_tags() -> list[dict]:
    """Lazy-loading a collection (many-to-many) in a loop."""
    if REFACTOR:  # refactor: a regression that breaks the endpoint
        raise RuntimeError("tags backend not configured")
    with Session() as session:
        orders = session.scalars(select(Order).order_by(Order.id).limit(20)).all()
        return [{"id": o.id, "tags": [t.name for t in o.tags]} for o in orders]


@bp.get("/orders/page")
def orders_page() -> str:
    """The same N+1, hidden in a template: the queries nest under the template span."""
    with Session() as session:
        orders = session.scalars(select(Order).order_by(Order.id).limit(15)).all()
        return render_template("orders.html", orders=orders)


@bp.get("/orders/<int:order_id>")
def get_order(order_id: int) -> dict:
    if REFACTOR:  # refactor: a slower code path
        time.sleep(0.05)
    with Session() as session:
        order = session.execute(select(Order).where(Order.id == order_id)).scalar_one()
        total = session.scalar(
            select(func.sum(OrderItem.quantity)).where(OrderItem.order_id == order_id)
        )
        return {"id": order.id, "status": order.status, "units": total}


@bp.get("/report/duplicates")
def duplicates() -> dict:
    """The same query several times in one request."""
    with Session() as session:
        counts = []
        for _ in range(4):
            counts.append(session.scalar(select(func.count()).select_from(Product)))
        if REFACTOR:  # refactor: the field is now a string
            return {"products": str(counts[-1])}
        return {"products": counts[-1]}


@bp.get("/audit/search")
def audit_search() -> dict:
    """Slow query: LIKE on an unindexed text column of a big table."""
    q = request.args.get("q", "refund")
    with Session() as session:
        hits = session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.message.like(f"%{q}%"))
        )
        return {"q": q, "hits": hits}


@bp.get("/slow")
def slow() -> dict:
    """Slow endpoint with almost no database time: the time is spent sleeping."""
    with Session() as session:
        session.scalar(select(func.count()).select_from(Customer))
    time.sleep(0.8)
    return {"slept": 0.8}


@bp.get("/status")
def status() -> dict:
    """A health check with volatile fields (changes on every call)."""
    return {"ok": True, "now": time.time(), "request_id": uuid.uuid4().hex}


@bp.get("/boom")
def boom() -> dict:
    with Session() as session:
        session.scalar(select(func.count()).select_from(Order))
    return {"ratio": 1 / 0}


@bp.get("/internal/price/<int:product_id>")
def internal_price(product_id: int) -> dict:
    with Session() as session:
        product = session.get(Product, product_id)
        return {"id": product_id, "price": float(product.price) if product else None}


@bp.get("/pricing")
def pricing() -> dict:
    """Calls another service over HTTP - here an internal route, in-process."""
    transport = httpx.WSGITransport(app=current_app.wsgi_app)
    with httpx.Client(transport=transport, base_url="http://pricing.local") as client:
        prices = []
        for pid in (1, 2, 3):
            r = client.get(f"/internal/price/{pid}", params={"api_key": "demo-key-123"})
            prices.append(r.json()["price"])
        return {"prices": prices}


@bp.post("/checkout")
def checkout() -> dict:
    """Send a body with secrets to see Redline redact them."""
    payload = request.get_json(force=True)
    return {"ok": True, "email": payload.get("email")}
