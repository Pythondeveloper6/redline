"""A tiny shop used by the tests. Real source lines matter: the suggester reads this file."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from litestar import Controller, Litestar, Request, get, post
from litestar.exceptions import NotFoundException
from litestar.response import Stream
from sqlalchemy import Column, ForeignKey, String, Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


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
    """Session factories, filled in by the fixtures."""

    sync: sessionmaker[Session]
    asyn: async_sessionmaker[AsyncSession]


holder = Holder()


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


@get("/orders/{order_id:int}")
async def get_order(order_id: int) -> dict:
    async with holder.asyn() as s:
        order = (await s.execute(select(Order).where(Order.id == order_id))).scalar_one()
        return {"id": order.id, "note": order.note}


@get("/nplus1/items")
async def n1_items() -> dict:
    async with holder.asyn() as s:
        orders = (await s.scalars(select(Order))).all()
        total = 0
        for o in orders:
            rows = await s.scalars(select(OrderItem).where(OrderItem.order_id == o.id))
            total += len(rows.all())
        return {"items": total}


@get("/nplus1/customers")
async def n1_customers() -> dict:
    async with holder.asyn() as s:
        orders = (await s.scalars(select(Order).order_by(Order.id))).all()
        names = []
        for o in orders:
            names.append((await s.get(Customer, o.customer_id)).name)
        return {"names": names}


@get("/nplus1/lazy-items", sync_to_thread=True)
def n1_lazy_items() -> dict:
    with holder.sync() as s:
        orders = s.query(Order).order_by(Order.id).all()
        return {"items": sum(len(o.items) for o in orders)}


@get("/nplus1/lazy-customers", sync_to_thread=True)
def n1_lazy_customers() -> dict:
    with holder.sync() as s:
        orders = s.scalars(select(Order)).all()
        return {"names": [o.customer.name for o in orders]}


@get("/nplus1/tags", sync_to_thread=True)
def n1_tags() -> dict:
    with holder.sync() as s:
        orders = (s.execute(select(Order))).scalars().all()
        n = 0
        for o in orders:
            n += len(o.tags)
        return {"tags": n}


@get("/dup")
async def dup() -> dict:
    async with holder.asyn() as s:
        a = (await s.execute(select(Tag).where(Tag.id == 1))).scalar_one()
        b = (await s.execute(select(Tag).where(Tag.id == 1))).scalar_one()
        return {"same": a is b}


@get("/sync-count", sync_to_thread=True)
def sync_count() -> dict:
    with holder.sync() as s:
        n = s.scalars(select(Tag)).all()
        return {"n": len(n)}


@get("/slow/{n:int}")
async def slow(n: int) -> dict:
    async with holder.asyn() as s:
        await s.execute(select(Order).where(Order.id == n))
        await asyncio.sleep(0.05)
        await s.execute(select(OrderItem).where(OrderItem.order_id == n))
        return {"n": n}


@get("/stream")
async def stream() -> Stream:
    async def gen():
        for i in range(200):
            yield (f"line {i} " + "x" * 90 + "\n").encode()

    return Stream(gen(), media_type="text/plain")


@post("/echo", status_code=200)
async def echo(request: Request) -> dict:
    body = await request.body()
    return {"len": len(body), "body": body.decode()}


@get("/boom")
async def boom() -> dict:
    raise RuntimeError("kaboom")


@get("/internal")
async def internal() -> dict:
    return {"ok": True}


@get("/http-out")
async def http_out(request: Request) -> dict:
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://inner") as c:
        r = await c.get("/internal?api_key=hunter2&x=1")
        return r.json()


class ShopController(Controller):
    path = "/shop"

    @get("/{sku:str}")
    async def by_sku(self, sku: str) -> dict:
        async with holder.asyn() as s:
            n = len((await s.scalars(select(OrderItem).where(OrderItem.sku == sku))).all())
        return {"sku": sku, "n": n}


@get("/gone")
async def gone() -> dict:
    raise NotFoundException("no such thing")


HANDLERS = [
    get_order,
    n1_items,
    n1_customers,
    n1_lazy_items,
    n1_lazy_customers,
    n1_tags,
    dup,
    sync_count,
    slow,
    stream,
    echo,
    boom,
    gone,
    internal,
    http_out,
    ShopController,
]


def build_app(*plugins: Any, handlers: list | None = None, **kw: Any) -> Litestar:
    return Litestar(route_handlers=[*HANDLERS, *(handlers or [])], plugins=list(plugins), **kw)
