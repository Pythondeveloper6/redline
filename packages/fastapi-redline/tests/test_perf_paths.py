"""Per-request fixed cost: what an application request must not pay for."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_redline import install


def test_redline_routes_are_not_matched_for_app_requests(tmp_path):
    """Redline's routes sit first; FastAPI's per-route matching of them is skipped unless
    the path is under the prefix (it was ~5 us on every app request)."""
    app = FastAPI()

    @app.get("/orders/{n}")
    async def order(n: int):
        return {"n": n}

    @app.get("/{rest:path}")  # a catch-all declared before Redline: Redline still wins
    async def catch_all(rest: str):
        return {"rest": rest}

    install(app, settings={"storage_path": str(tmp_path / "r.db")}, dependencies=[])
    redline_route = app.router.routes[0]
    inner = type(redline_route).matches
    seen = []

    def spy(self, scope):
        seen.append(scope["path"])
        return inner(self, scope)

    original = redline_route.matches.__defaults__[0]
    redline_route.matches.__defaults__ = (spy.__get__(redline_route),)
    try:
        with TestClient(app) as c:
            assert c.get("/orders/1").json() == {"n": 1}
            assert c.get("/_redlinex").json() == {"rest": "_redlinex"}
            assert c.get("/_redline/api/meta").status_code == 200
            assert c.get("/_redline").status_code == 200  # redirect to the slash, then the UI
    finally:
        redline_route.matches.__defaults__ = (original,)
    assert "/orders/1" not in seen and "/_redlinex" not in seen
    assert "/_redline/api/meta" in seen


def test_prefix_guard_honours_root_path(tmp_path):
    app = FastAPI()

    @app.get("/x")
    async def x():
        return {"ok": True}

    install(app, settings={"storage_path": str(tmp_path / "r.db")}, dependencies=[])
    with TestClient(app, root_path="/app") as c:
        assert c.get("/_redline/api/meta").status_code == 200
        assert c.get("/x").json() == {"ok": True}
