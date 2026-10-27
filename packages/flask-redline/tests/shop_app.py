"""A tiny shop used by the tests. Real source lines matter: the suggester reads this file."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
from flask import Blueprint, Flask, abort, render_template, request, send_file, stream_with_context
from sqlalchemy import Column, ForeignKey, String, Table, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

TESTS = Path(__file__).resolve().parent


class Base(DeclarativeBase):
    pass


order_tags = Table(
    "order_tags",
    Base.metadata,
    Column("order_id", ForeignKey("orders.id"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id"), primary_key=True),
)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    orders: Mapped[list[Order]] = relationship(back_populates="customer")


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    note: Mapped[str] = mapped_column(String(50), default="")
    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(back_populates="order")
    tags: Mapped[list[Tag]] = relationship(secondary=order_tags)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    sku: Mapped[str] = mapped_column(String(20))
    order: Mapped[Order] = relationship(back_populates="items")


class Holder:
    """Session factory, filled in by the fixtures."""

    sync: sessionmaker[Session]


holder = Holder()
bp = Blueprint("shop", __name__, template_folder="templates")


def seed(session: Session, orders: int = 8) -> None:
    customers = [Customer(name=f"c{i}") for i in range(orders)]
    tags = [Tag(name=f"t{i}") for i in range(3)]
    session.add_all([*customers, *tags])
    session.flush()
    for i in range(orders):
        order = Order(customer_id=customers[i].id, note=f"n{i}")
        order.items = [OrderItem(sku=f"s{i}-{j}") for j in range(2)]
        order.tags = tags[: (i % 3) + 1]
        session.add(order)
    session.commit()


@bp.get("/orders/<int:order_id>")
def get_order(order_id: int) -> dict:
    with holder.sync() as s:
        order = (s.execute(select(Order).where(Order.id == order_id))).scalar_one()
        return {"id": order.id, "note": order.note}


@bp.get("/nplus1/items")
def n1_items() -> dict:
    with holder.sync() as s:
        orders = (s.scalars(select(Order))).all()
        total = 0
        for o in orders:
            rows = s.scalars(select(OrderItem).where(OrderItem.order_id == o.id))
            total += len(rows.all())
        return {"items": total}


@bp.get("/nplus1/customers")
def n1_customers() -> dict:
    with holder.sync() as s:
        orders = (s.scalars(select(Order).order_by(Order.id))).all()
        names = []
        for o in orders:
            names.append(s.get(Customer, o.customer_id).name)
        return {"names": names}


@bp.get("/nplus1/lazy-items")
def n1_lazy_items() -> dict:
    with holder.sync() as s:
        orders = s.query(Order).order_by(Order.id).all()
        return {"items": sum(len(o.items) for o in orders)}


@bp.get("/dup")
def dup() -> dict:
    with holder.sync() as s:
        a = (s.execute(select(Tag).where(Tag.id == 1))).scalar_one()
        b = (s.execute(select(Tag).where(Tag.id == 1))).scalar_one()
        return {"same": a is b}


@bp.get("/scan")
def scan() -> dict:
    with holder.sync() as s:
        return {"n": len(s.scalars(select(Tag)).all())}


@bp.get("/slow/<int:n>")
def slow(n: int) -> dict:
    with holder.sync() as s:
        s.execute(select(Order).where(Order.id == n))
        time.sleep(0.05)
        s.execute(select(OrderItem).where(OrderItem.order_id == n))
        return {"n": n}


@bp.get("/page")
def page() -> str:
    """Lazy loading inside the template: its queries nest under the template span."""
    with holder.sync() as s:
        orders = s.scalars(select(Order).order_by(Order.id).limit(3)).all()
        return render_template("orders.html", orders=orders)


@bp.get("/broken-page")
def broken_page() -> str:
    return render_template("broken.html")


@bp.get("/stream")
def stream():
    @stream_with_context
    def gen():
        for i in range(200):
            yield (f"line {i} " + "x" * 90 + "\n").encode()
        with holder.sync() as s:  # runs while streaming, still attributed to this request
            s.execute(select(Tag)).all()

    return gen(), {"Content-Type": "text/plain"}


@bp.get("/file")
def file() -> object:
    return send_file(TESTS / "templates" / "orders.html", mimetype="text/html")


@bp.post("/echo")
def echo() -> dict:
    body = request.get_data()
    return {"len": len(body), "body": body.decode()}


@bp.get("/boom")
def boom() -> dict:
    raise RuntimeError("kaboom")


@bp.get("/missing")
def missing() -> dict:
    abort(404)


@bp.get("/forbidden")
def forbidden() -> dict:
    abort(403)


@bp.get("/unavailable")
def unavailable() -> dict:
    abort(503)


@bp.get("/async")
async def async_view() -> dict:
    with holder.sync() as s:  # a sync driver call inside a coroutine, on asgiref's loop thread
        order = (s.execute(select(Order).where(Order.id == 1))).scalar_one()
        return {"id": order.id}


@bp.get("/internal")
def internal() -> dict:
    return {"ok": True}


@bp.get("/http-out")
def http_out() -> dict:
    from flask import current_app

    transport = httpx.WSGITransport(app=current_app.wsgi_app)
    with httpx.Client(transport=transport, base_url="http://inner") as c:
        return c.get("/internal?api_key=hunter2&x=1").json()


HTML_PAGE = "<!doctype html><html><head><title>x</title></head><body><h1>hi</h1></BODY></html>"


@bp.get("/html")
def html_page() -> str:
    return HTML_PAGE


@bp.get("/html-csp")
def html_csp() -> object:
    return HTML_PAGE, {"Content-Security-Policy": "script-src 'nonce-abc'"}


@bp.get("/html-fragment")
def html_fragment() -> str:
    return "<p>no body tag</p>"


@bp.get("/html-stream")
def html_stream() -> object:
    def gen():
        yield "<html><body>"
        yield "streamed</body></html>"

    return gen(), {"Content-Type": "text/html; charset=utf-8"}


@bp.get("/timed")
def timed() -> object:
    return {"ok": True}, {"Server-Timing": 'app;dur=1.0;desc="mine"'}


@bp.get("/async-sleep")
async def async_sleep() -> dict:
    time.sleep(0.08)  # blocks asgiref's event loop
    return {"ok": True}


def build_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app
