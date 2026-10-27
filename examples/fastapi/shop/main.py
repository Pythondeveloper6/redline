from __future__ import annotations

from fastapi import FastAPI

from fastapi_redline import install, instrument_httpx

from .api import router
from .db import async_engine, sync_engine
from .models import Base

app = FastAPI(title="Redline demo shop")
app.include_router(router)

install(
    app,
    engine=[async_engine, sync_engine],  # "default" = async, the sync one gets its own alias
    models=Base,
    settings={"slow_query_ms": 40, "always_keep_slower_than_ms": 300, "access": "local"},
    # No `dependencies=`; access="local" (set explicitly – unset, Redline answers 404
    # outside debug mode): the UI is reachable from localhost only.
)
instrument_httpx()  # outbound httpx calls become `http_out` spans
