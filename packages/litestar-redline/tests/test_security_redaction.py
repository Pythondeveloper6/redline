"""Litestar end-to-end checks for the redaction fixes of the 0.2 security review."""

from __future__ import annotations

import json
from typing import Any

from litestar import Litestar, Request, get, post
from litestar.testing import TestClient

from litestar_redline import RedlinePlugin
from redline_core.redact import MASK


@post("/login")
async def login(request: Request) -> dict[str, Any]:
    await request.body()
    return {"ok": True}


@get("/reset/{uidb64:str}/{token:str}")
async def reset(uidb64: str, token: str) -> dict[str, Any]:
    return {"ok": True}


def _traces(tmp_path, calls):
    plugin = RedlinePlugin(settings={"storage_path": str(tmp_path / "r.db")})
    app = Litestar(route_handlers=[login, reset], plugins=[plugin])
    with TestClient(app, base_url="http://localhost") as client:
        for call in calls:
            call(client)
        plugin.store.flush(5)
        items = plugin.store.list(None, limit=50).items
        return [plugin.store.get(t["id"]) for t in reversed(items)]


def test_multipart_and_text_plain_bodies_are_masked(tmp_path):
    multipart, plain = _traces(
        tmp_path,
        [
            lambda c: c.post(
                "/login",
                files={
                    "username": (None, "bob"),
                    "password": (None, "hunter2"),
                    "avatar": ("me.png", b"\x89PNGsecretbytes", "image/png"),
                },
            ),
            lambda c: c.post(
                "/login",
                content=b'{"password":"hunter2"}',
                headers={"content-type": "text/plain;charset=UTF-8", "X-Session-Id": "sess1"},
            ),
        ],
    )
    for t in (multipart, plain):
        blob = json.dumps(t.to_dict())
        assert "hunter2" not in blob and "secretbytes" not in blob and "sess1" not in blob
    assert json.loads(multipart.request["body"]) == {
        "username": "bob",
        "password": MASK,
        "avatar": "[file: me.png, 15 bytes]",
    }


def test_path_params_are_masked(tmp_path):
    # (Litestar's route-level middleware never sees an unmatched path, so no 404 trace.)
    token = "c9x1k2-5f0d7a8e3b2c1d4e5f6a7b8c9d0e1f2a"
    (reset_trace,) = _traces(tmp_path, [lambda c: c.get(f"/reset/MQ/{token}")])
    assert reset_trace.path == f"/reset/{MASK}/{MASK}"
    assert token not in json.dumps(reset_trace.to_dict())
