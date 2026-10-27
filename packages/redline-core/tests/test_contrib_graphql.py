"""GraphQL integrations: resolver spans, SQL parenting and the N+1 resolver path."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time

import pytest

graphene = pytest.importorskip("graphene")
strawberry = pytest.importorskip("strawberry")

from redline_core.conf import Settings  # noqa: E402
from redline_core.contrib import _resolver  # noqa: E402
from redline_core.contrib.graphene import RedlineMiddleware  # noqa: E402
from redline_core.contrib.strawberry import RedlineExtension  # noqa: E402
from redline_core.model import Issue  # noqa: E402
from redline_core.recorder import Recorder, record_query  # noqa: E402

N_ORDERS = 8


def make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript(
        "CREATE TABLE shop_customer (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE shop_order (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL);"
    )
    for i in range(1, N_ORDERS + 1):
        db.execute("INSERT INTO shop_customer VALUES (?, ?)", (i, f"c{i}"))
        db.execute("INSERT INTO shop_order VALUES (?, ?, ?)", (i, i, i * 1.5))
    return db


@pytest.fixture(autouse=True)
def _only_resolvers_with_children(monkeypatch):
    # The duration rule keeps any resolver slower than MIN_KEPT_MS; on a busy CI machine
    # even a trivial attribute resolver can cross it. These tests check the children rule,
    # so the duration rule is taken out of the picture.
    monkeypatch.setattr(_resolver, "MIN_KEPT_MS", 1e9)


def run_sql(db, sql, params=()):
    t0 = time.perf_counter()
    rows = db.execute(sql, params).fetchall()
    record_query(sql, list(params), start=t0, duration=time.perf_counter() - t0, vendor="sqlite")
    return rows


def recorder() -> Recorder:
    return Recorder(Settings.from_mapping({"project_root": os.path.dirname(__file__)}))


def traced(rec, fn):
    b = rec.start_trace(method="POST", path="/graphql")
    try:
        fn()
    finally:
        trace = rec.finish_trace(b, status=200)
    return trace


def check_trace(trace):
    resolvers = [s for s in trace.spans if s.kind == "resolver"]
    by_path = {}
    for s in resolvers:
        by_path.setdefault(s.data["path"], []).append(s)
    # Trivial attribute resolvers (id, total, name) are dropped.
    assert set(by_path) == {"orders", "orders.*.customer"}
    assert by_path["orders"][0].data["field"] == "orders"
    assert by_path["orders"][0].data["parent_type"].endswith("Query")
    customers = by_path["orders.*.customer"]
    assert len(customers) == N_ORDERS
    assert all(s.data["parent_type"].endswith("Order") for s in customers)
    root = by_path["orders"][0]
    assert root.parent_id is None
    assert all(s.parent_id is None for s in customers)  # resolvers of a list run as siblings
    ids = {s.id for s in customers}
    customer_sql = [s for s in trace.sql_spans() if "shop_customer" in s.data["sql"]]
    assert len(customer_sql) == N_ORDERS
    assert {s.parent_id for s in customer_sql} == ids  # one query per resolver span
    orders_sql = [s for s in trace.sql_spans() if "shop_order" in s.data["sql"]]
    assert [s.parent_id for s in orders_sql] == [root.id]
    (issue,) = [i for i in trace.issues if i.kind == "n_plus_one"]
    assert issue.extra["resolver_path"] == "orders.*.customer"
    assert issue.title == (
        f"N+1: {N_ORDERS} queries on shop_customer from resolver orders.*.customer"
    )
    assert Issue.from_dict(issue.to_dict()).extra == issue.extra


DB: dict[str, sqlite3.Connection] = {}


@strawberry.type
class SCustomer:
    id: int
    name: str


@strawberry.type
class Order:
    id: int
    total: float
    customer_id: strawberry.Private[int]

    @strawberry.field
    def customer(self) -> SCustomer:
        sql = "SELECT id, name FROM shop_customer WHERE id = ?"
        (row,) = run_sql(DB["db"], sql, (self.customer_id,))
        return SCustomer(id=row[0], name=row[1])


@strawberry.type
class AOrder:
    id: int
    customer_id: strawberry.Private[int]

    @strawberry.field
    async def customer(self) -> SCustomer:
        await asyncio.sleep(0)
        sql = "SELECT id, name FROM shop_customer WHERE id = ?"
        (row,) = run_sql(DB["db"], sql, (self.customer_id,))
        return SCustomer(id=row[0], name=row[1])


@strawberry.type
class SQuery:
    @strawberry.field
    def orders(self) -> list[Order]:
        rows = run_sql(DB["db"], "SELECT id, total, customer_id FROM shop_order")
        return [Order(id=r[0], total=r[1], customer_id=r[2]) for r in rows]


@strawberry.type
class AQuery:
    @strawberry.field
    async def orders(self) -> list[AOrder]:
        await asyncio.sleep(0)
        rows = run_sql(DB["db"], "SELECT id, total, customer_id FROM shop_order")
        return [AOrder(id=r[0], customer_id=r[2]) for r in rows]


def test_strawberry_sync_n_plus_one():
    DB["db"] = make_db()
    schema = strawberry.Schema(SQuery, extensions=[RedlineExtension])
    rec = recorder()

    def go():
        result = schema.execute_sync("{ orders { id total customer { id name } } }")
        assert result.errors is None
        assert len(result.data["orders"]) == N_ORDERS

    check_trace(traced(rec, go))


def test_strawberry_async_n_plus_one():
    DB["db"] = make_db()
    schema = strawberry.Schema(AQuery, extensions=[RedlineExtension])
    rec = recorder()

    async def go():
        b = rec.start_trace(method="POST", path="/graphql")
        result = await schema.execute("{ orders { id customer { id name } } }")
        assert result.errors is None
        return rec.finish_trace(b, status=200)

    check_trace(asyncio.run(go()))


def test_graphene_n_plus_one_sync_and_async():
    db = make_db()

    class Customer(graphene.ObjectType):
        id = graphene.Int()
        name = graphene.String()

    class Order(graphene.ObjectType):
        id = graphene.Int()
        customer = graphene.Field(Customer)

        def resolve_customer(self, info):
            (row,) = run_sql(
                db, "SELECT id, name FROM shop_customer WHERE id = ?", (self.customer_id,)
            )
            return Customer(id=row[0], name=row[1])

    class Query(graphene.ObjectType):
        orders = graphene.List(Order)

        def resolve_orders(self, info):
            rows = run_sql(db, "SELECT id, total, customer_id FROM shop_order")
            out = []
            for r in rows:
                o = Order(id=r[0])
                o.customer_id = r[2]
                out.append(o)
            return out

    schema = graphene.Schema(query=Query)
    rec = recorder()

    def go():
        result = schema.execute(
            "{ orders { id customer { id name } } }", middleware=[RedlineMiddleware()]
        )
        assert result.errors is None

    check_trace(traced(rec, go))


def test_graphene_async_resolver_parenting():
    db = make_db()

    class Query(graphene.ObjectType):
        n = graphene.Int()

        async def resolve_n(self, info):
            await asyncio.sleep(0)
            return len(run_sql(db, "SELECT id FROM shop_order"))

    schema = graphene.Schema(query=Query)
    rec = recorder()

    async def go():
        b = rec.start_trace(method="POST", path="/graphql")
        result = await schema.execute_async("{ n }", middleware=[RedlineMiddleware()])
        assert result.errors is None and result.data == {"n": N_ORDERS}
        return rec.finish_trace(b, status=200)

    trace = asyncio.run(go())
    (res,) = [s for s in trace.spans if s.kind == "resolver"]
    assert res.data["path"] == "n"
    (q,) = trace.sql_spans()
    assert q.parent_id == res.id


@strawberry.type
class Hello:
    hello: str


@strawberry.type
class Slow:
    @strawberry.field
    def slow(self) -> int:
        time.sleep(0.003)
        return 1

    @strawberry.field
    def boom(self) -> int:
        raise ValueError("no")


def test_no_trace_is_a_passthrough_and_trivial_resolvers_are_dropped():
    Query = Hello
    schema = strawberry.Schema(Query, extensions=[RedlineExtension])
    assert schema.execute_sync("{ hello }", root_value=Query(hello="x")).data == {"hello": "x"}
    trace = traced(
        recorder(), lambda: schema.execute_sync("{ hello }", root_value=Query(hello="x"))
    )
    assert [s for s in trace.spans if s.kind == "resolver"] == []


def test_slow_resolver_is_kept_and_errors_recorded(monkeypatch):
    monkeypatch.setattr(_resolver, "MIN_KEPT_MS", 0.5)  # this one checks the duration rule
    schema = strawberry.Schema(Slow, extensions=[RedlineExtension])
    trace = traced(recorder(), lambda: schema.execute_sync("{ slow boom }"))
    by = {s.data["path"]: s for s in trace.spans if s.kind == "resolver"}
    assert by["slow"].duration_ms >= 3
    assert by["boom"].data["error"] == "ValueError"
