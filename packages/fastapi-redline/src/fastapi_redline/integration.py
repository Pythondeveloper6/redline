"""``install(app, ...)``: wire Redline into a FastAPI/Starlette application."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.params import Depends as DependsParam

from redline_core import Recorder, Settings, configure
from redline_core.api import RedlineAPI
from redline_core.explain import ExplainExecutor
from redline_core.storage import SQLiteStore, Store

from ._middleware import RedlineMiddleware
from ._security import local_only
from ._sqlalchemy import (
    EngineLike,
    alias_for,
    build_executor,
    instrument_engine,
    name_engines,
    vendor_of,
)
from .router import build_router
from .suggest import SQLAlchemyFixSuggester

__all__ = ["Redline", "install"]

logger = logging.getLogger("redline")


class Redline:
    """The handle ``install()`` returns."""

    def __init__(
        self,
        *,
        app: FastAPI,
        prefix: str,
        settings: Settings,
        recorder: Recorder,
        store: Store,
        suggester: SQLAlchemyFixSuggester,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self.settings = settings
        self.recorder = recorder
        self.store = store
        self.suggester = suggester
        self.engines: dict[str, EngineLike] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.api = RedlineAPI(
            store, settings, framework="fastapi", explain_executor=self._executor_for
        )

    def add_engine(self, engine: EngineLike, alias: str | None = None) -> str:
        """Instrument another engine; returns its ``db_alias``."""
        if alias is None:
            alias = alias_for(engine, self.engines)
        self.engines[alias] = engine
        instrument_engine(engine, alias)
        return alias

    def _executor_for(self, alias: str, vendor: str) -> ExplainExecutor | None:
        engine = self.engines.get(alias)
        if engine is None:
            return None
        return build_executor(engine, vendor_of(engine), lambda: self.loop)

    def close(self) -> None:
        """Flush pending traces and close the store (called on app shutdown)."""
        with contextlib.suppress(Exception):
            self.store.flush(5.0)
        self.store.close()


def install(
    app: FastAPI,
    *,
    engine: Any = None,
    settings: Mapping[str, Any] | Settings | None = None,
    prefix: str = "/_redline",
    dependencies: Sequence[Callable[..., Any] | DependsParam] | None = None,
    models: Any = None,
    store: Store | None = None,
) -> Redline:
    """Install Redline on ``app``.

    ``engine``
        A sync ``Engine``, an ``AsyncEngine``, a list of them, or ``{alias: engine}``.
        The first of a list is the ``"default"`` db alias (used by EXPLAIN).
    ``settings``
        A ``Settings`` or a dict of settings (case-insensitive keys).
    ``prefix``
        Where the UI and API live (``/_redline/``). Never recorded.
    ``dependencies``
        FastAPI dependencies protecting the UI and API, e.g.
        ``[Depends(require_admin)]``. When omitted only loopback clients are allowed and a
        warning is logged; pass ``[]`` to deliberately leave it open.
    ``models``
        Declarative ``Base``/``registry`` (or a list) for fix suggestions; by default every
        registry SQLAlchemy knows about.
    ``store``
        A custom ``Store``; default is the SQLite file at ``storage_path``.
    """
    if getattr(app.state, "redline", None) is not None:
        raise RuntimeError("Redline is already installed on this app.")
    prefix = "/" + prefix.strip("/")
    if prefix == "/":
        raise ValueError("prefix must be a sub-path such as '/_redline'.")

    cfg = settings if isinstance(settings, Settings) else Settings.from_mapping(settings)
    own = rf"^{re.escape(prefix)}(/|$)"
    if own not in cfg.ignore_paths:
        cfg = cfg.replace(ignore_paths=[*cfg.ignore_paths, own])

    store = store if store is not None else SQLiteStore.from_settings(cfg)
    suggester = SQLAlchemyFixSuggester(models, project_root=cfg.project_root)
    recorder = configure(cfg, store, [suggester])
    redline = Redline(
        app=app, prefix=prefix, settings=cfg, recorder=recorder, store=store, suggester=suggester
    )
    for alias, eng in name_engines(engine).items():
        redline.engines[alias] = eng
        instrument_engine(eng, alias)

    if dependencies is None:
        deps: list[Any] = [Depends(local_only)]
        logger.warning(
            "Redline is mounted at %s with no auth dependency: only loopback clients are "
            "allowed. Pass install(app, dependencies=[...]) before exposing it.",
            prefix,
        )
    else:
        deps = [d if isinstance(d, DependsParam) else Depends(d) for d in dependencies]

    router = build_router(redline, deps)
    before = len(app.router.routes)
    app.include_router(router, prefix=prefix)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = added  # win over catch-all routes declared earlier

    app.add_middleware(RedlineMiddleware, recorder=recorder)

    original = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any):
        async with original(a) as state:
            try:
                yield state
            finally:
                await asyncio.to_thread(redline.close)

    app.router.lifespan_context = lifespan
    app.state.redline = redline
    return redline
