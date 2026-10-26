from __future__ import annotations

import time

import pytest

from redline_core.conf import Settings
from redline_core.model import Callsite, Frame, Span, Trace, new_id
from redline_core.storage import SQLiteStore


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "redline.db"), background=False)
    yield s
    s.close()


@pytest.fixture
def settings():
    return Settings()


def make_callsite(file: str = "shop/views.py", line: int = 41) -> Callsite:
    return Callsite(
        file=file,
        line=line,
        function="order_detail",
        code="for item in order.items.all():",
        stack=(Frame(file, line, "order_detail"),),
    )


def sql_span(
    sql: str,
    params=None,
    *,
    duration_ms: float = 1.0,
    callsite: Callsite | None = None,
    start_ms: float = 0.0,
) -> Span:
    from redline_core import sql as sqlmod

    return Span(
        id=new_id(),
        parent_id=None,
        kind="sql",
        name=sqlmod.fingerprint(sql)[:120],
        start_ms=start_ms,
        duration_ms=duration_ms,
        callsite=callsite,
        data={
            "sql": sql,
            "params": list(params or []),
            "fingerprint": sqlmod.fingerprint(sql),
            "db_alias": "default",
            "vendor": "sqlite",
            "rows": 1,
            "many": False,
            "explainable": sqlmod.is_explainable(sql),
        },
    )


def make_trace(
    *,
    path: str = "/orders/1/",
    route: str | None = "/orders/<int:pk>/",
    method: str = "GET",
    status: int = 200,
    duration_ms: float = 10.0,
    started_at: float | None = None,
    spans: list[Span] | None = None,
    tags: dict[str, str] | None = None,
    profile: dict | None = None,
) -> Trace:
    spans = spans or []
    sql = [s for s in spans if s.kind == "sql"]
    return Trace(
        id=new_id(),
        framework="other",
        method=method,
        path=path,
        route=route,
        view="shop.views.order_detail",
        status=status,
        started_at=time.time() if started_at is None else started_at,
        duration_ms=duration_ms,
        query_count=len(sql),
        query_ms=sum(s.duration_ms for s in sql),
        spans=spans,
        tags=tags or {},
        profile=profile,
    )
