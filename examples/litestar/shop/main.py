from __future__ import annotations

from pathlib import Path

from litestar import Litestar

from litestar_redline import RedlinePlugin, instrument_httpx

from .api import ROUTES
from .db import async_engine, sync_engine

app = Litestar(
    route_handlers=ROUTES,
    plugins=[
        # "default" = async, the sync one gets its own alias
        RedlinePlugin(
            engine=[async_engine, sync_engine],
            settings={
                "slow_query_ms": 40,
                "always_keep_slower_than_ms": 300,
                "access": "local",
                "checks_from_ui": True,
                "analysis_from_ui": True,
                "project_root": str(Path(__file__).resolve().parent.parent),
                "orm_instances_threshold": 20,
            },
            # No `guards=`; access="local" (set explicitly – unset, Redline answers 404
            # outside debug mode): the UI is reachable from localhost only.
        ),
    ],
)
instrument_httpx()  # outbound httpx calls become `http_out` spans
