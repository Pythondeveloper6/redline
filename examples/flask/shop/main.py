from __future__ import annotations

from flask import Flask

from flask_redline import Redline, instrument_httpx

from .db import sync_engine
from .views import bp

app = Flask(__name__)
app.register_blueprint(bp)

Redline(
    app,
    engine=sync_engine,
    settings={"slow_query_ms": 40, "always_keep_slower_than_ms": 300, "access": "local"},
    # No `permission=`; access="local" (set explicitly – unset, Redline answers 404
    # outside debug mode): the UI is reachable from localhost only.
)
instrument_httpx()  # outbound httpx calls become `http_out` spans
