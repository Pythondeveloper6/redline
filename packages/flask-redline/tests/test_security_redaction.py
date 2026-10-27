"""Flask end-to-end checks for the redaction fixes of the 0.2 security review."""

from __future__ import annotations

import json

from flask import Flask, request

from flask_redline import Redline
from redline_core.redact import MASK

from .conftest import BufferedClient


def _traces(tmp_path, calls):
    app = Flask("sec")
    app.test_client_class = BufferedClient

    @app.post("/login")
    def login():
        request.get_data()
        return {"ok": True}

    @app.get("/reset/<uidb64>/<token>/")
    def reset(uidb64, token):
        return {"ok": True}

    rl = Redline(app, settings={"storage_path": str(tmp_path / "r.db")})
    client = app.test_client()
    for call in calls:
        call(client)
    rl.store.flush(5)
    items = rl.store.list(None, limit=50).items
    return [rl.store.get(t["id"]) for t in reversed(items)]


def test_text_bodies_headers_and_multipart_are_not_stored_raw(tmp_path):
    plain, multipart = _traces(
        tmp_path,
        [
            lambda c: c.post(
                "/login",
                data=b"<login><password>hunter2</password></login>",
                headers={"content-type": "application/xml", "X-Amz-Security-Token": "aws1"},
            ),
            lambda c: c.post(
                "/login",
                data={"username": "bob", "password": "hunter2"},
                content_type="multipart/form-data",
            ),
        ],
    )
    for t in (plain, multipart):
        blob = json.dumps(t.to_dict())
        assert "hunter2" not in blob and "aws1" not in blob
    assert plain.request["headers"]["X-Amz-Security-Token"] == MASK


def test_path_params_and_unmatched_token_paths_are_masked(tmp_path):
    token = "c9x1k2-5f0d7a8e3b2c1d4e5f6a7b8c9d0e1f2a"
    reset, missing = _traces(
        tmp_path,
        [
            lambda c: c.get(f"/reset/MQ/{token}/"),
            lambda c: c.get("/nope/a8Fk2LmQ9zX0pR7tY4wB1nC6/"),
        ],
    )
    assert reset.path == f"/reset/{MASK}/{MASK}/"
    assert missing.status == 404 and missing.path == f"/nope/{MASK}/"
