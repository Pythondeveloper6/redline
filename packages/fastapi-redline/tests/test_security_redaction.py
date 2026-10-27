"""FastAPI end-to-end checks for the redaction fixes of the 0.2 security review."""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from fastapi_redline import install
from redline_core.redact import MASK


def _traces(tmp_path, calls):
    app = FastAPI()

    @app.post("/login")
    async def login(request: Request):
        await request.body()
        return {"ok": True}

    @app.get("/reset/{uidb64}/{token}")
    async def reset(uidb64: str, token: str):
        return {"ok": True}

    rl = install(app, settings={"storage_path": str(tmp_path / "r.db")})
    with TestClient(app) as client:
        for call in calls:
            call(client)
        rl.store.flush(5)
        items = rl.store.list(None, limit=50).items
        return [rl.store.get(t["id"]) for t in reversed(items)]


def test_multipart_text_plain_and_custom_headers_are_masked(tmp_path):
    traces = _traces(
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
                headers={"content-type": "text/plain;charset=UTF-8"},
            ),
            lambda c: c.post(
                "/login", json={"u": 1}, headers={"X-Auth-Token": "s3cr3t", "X-CSRFToken": "c"}
            ),
        ],
    )
    multipart, plain, hdrs = traces
    for t in traces:
        blob = json.dumps(t.to_dict())
        assert "hunter2" not in blob and "secretbytes" not in blob and "s3cr3t" not in blob
    data = json.loads(multipart.request["body"])
    assert data == {
        "username": "bob",
        "password": MASK,
        "avatar": "[file: me.png, 15 bytes]",
    }
    assert json.loads(plain.request["body"]) == {"password": MASK}
    assert hdrs.request["headers"]["x-auth-token"] == MASK
    assert hdrs.request["headers"]["x-csrftoken"] == MASK


def test_path_params_and_unmatched_token_paths_are_masked(tmp_path):
    token = "c9x1k2-5f0d7a8e3b2c1d4e5f6a7b8c9d0e1f2a"
    reset, missing = _traces(
        tmp_path,
        [
            lambda c: c.get(f"/reset/MQ/{token}"),
            lambda c: c.get("/nope/a8Fk2LmQ9zX0pR7tY4wB1nC6"),
        ],
    )
    assert reset.path == f"/reset/{MASK}/{MASK}" and reset.route == "/reset/{uidb64}/{token}"
    assert missing.status == 404 and missing.path == f"/nope/{MASK}"
