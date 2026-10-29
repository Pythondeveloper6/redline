from __future__ import annotations

import os
import uuid
from pathlib import Path

from flask import Flask, request

from flask_redline import Redline, instrument_httpx
from redline_core.users import from_attr

from .db import sync_engine
from .views import bp
from .workflows import bp as workflows_bp

app = Flask(__name__)
app.register_blueprint(bp)
app.register_blueprint(workflows_bp)


@app.before_request
def demo_user():
    # A demo identifier, not authentication. Real applications resolve their authenticated user.
    value = request.headers.get("X-Demo-User-ID", "42")
    request.person_id = int(value) if len(value) <= 18 and value.isdecimal() else None


@app.after_request
def correlation(response):
    response.headers["X-Request-ID"] = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    return response


Redline(
    app,
    engine=sync_engine,
    settings={
        "instrument_engines": "all",
        "user_resolver": from_attr("person_id"),
        "redact_profiles": ["credentials", "pii", "health"],
        "redact_routes": {"POST /workflows/private": "values"},
        "extra_stores": [os.environ["SHOP_WORKER_STORE"]]
        if os.environ.get("SHOP_WORKER_STORE")
        else [],
        "slow_query_ms": 40,
        "always_keep_slower_than_ms": 300,
        "access": os.environ.get("REDLINE_ACCESS", "local"),
        "checks_from_ui": True,
        "analysis_from_ui": True,
        "project_root": str(Path(__file__).resolve().parent.parent),
        "orm_instances_threshold": 20,
    },
    # No `permission=`; access="local" (set explicitly – unset, Redline answers 404
    # outside debug mode): the UI is reachable from localhost only.
)
instrument_httpx()  # outbound httpx calls become `http_out` spans
