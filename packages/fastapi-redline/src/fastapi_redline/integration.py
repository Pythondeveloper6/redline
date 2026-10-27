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
from redline_core.access import Access
from redline_core.api import RedlineAPI
from redline_core.config import ResolvedConfig, resolve_settings
from redline_core.explain import ExplainExecutor
from redline_core.load.control import run_hooks
from redline_core.storage import SQLiteStore, Store

from ._middleware import RedlineMiddleware
from ._security import redline_guard
from ._serialize import instrument_serialization
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
        config: ResolvedConfig | None = None,
        access: Access | None = None,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self.settings = settings
        self.recorder = recorder
        self.store = store
        self.suggester = suggester
        self.config = config
        self.access = access if access is not None else Access(settings)
        self.engines: dict[str, EngineLike] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        launcher, stopper = run_hooks(store, settings)
        self.api = RedlineAPI(
            store,
            settings,
            framework="fastapi",
            explain_executor=self._executor_for,
            run_launcher=launcher,
            run_stopper=stopper,
            config=config,
            access=self.access,
        )

    def add_engine(self, engine: EngineLike, alias: str | None = None) -> str:
        """Instrument another engine; returns its ``db_alias``."""
        if alias is None:
            alias = alias_for(engine, self.engines)
        self.engines[alias] = engine
        if self.settings.enabled:
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
    prefix: str | None = None,
    dependencies: Sequence[Callable[..., Any] | DependsParam] | None = None,
    models: Any = None,
    store: Store | None = None,
) -> Redline:
    """Install Redline on ``app``.

    ``engine``
        A sync ``Engine``, an ``AsyncEngine``, a list of them, or ``{alias: engine}``.
        The first of a list is the ``"default"`` db alias (used by EXPLAIN).
    ``settings``
        A ``Settings`` or a dict of settings (case-insensitive keys). They are the top
        layer over ``redline.toml`` / ``[tool.redline]`` and ``REDLINE_*`` variables
        (``redline_core.config``).
    ``prefix``
        Where the UI and API live (default: the ``mount_path`` setting, else
        ``/_redline``). Never recorded.
    ``dependencies``
        FastAPI dependencies protecting the UI and API, e.g.
        ``[Depends(require_admin)]``; giving them implies ``access="framework"`` unless
        the ``access`` setting is set explicitly. When omitted, the ``access`` setting
        decides (default ``local``: loopback clients addressing
        ``localhost``/``127.0.0.1``/``[::1]``); ``[]`` deliberately leaves it open.
        ``HTTPException`` raised by these dependencies is answered in the API's error
        shape.
    ``models``
        Declarative ``Base``/``registry`` (or a list) for fix suggestions; by default every
        registry SQLAlchemy knows about.
    ``store``
        A custom ``Store``; default is the SQLite file at ``storage_path``.
    """
    if getattr(app.state, "redline", None) is not None:
        raise RuntimeError("Redline is already installed on this app.")
    resolved = resolve_settings(settings, native_hook=dependencies is not None)
    cfg = resolved.settings
    prefix = prefix if prefix is not None else (cfg.mount_path or "/_redline")
    prefix = "/" + prefix.strip("/")
    if prefix == "/":
        raise ValueError("prefix must be a sub-path such as '/_redline'.")
    own = rf"^{re.escape(prefix)}(/|$)"
    if own not in cfg.ignore_paths:
        cfg = cfg.replace(ignore_paths=[*cfg.ignore_paths, own])
    access = Access(cfg)

    store = store if store is not None else SQLiteStore.from_settings(cfg)
    suggester = SQLAlchemyFixSuggester(models, project_root=cfg.project_root)
    recorder = configure(cfg, store, [suggester])
    redline = Redline(
        app=app,
        prefix=prefix,
        settings=cfg,
        recorder=recorder,
        store=store,
        suggester=suggester,
        config=resolved,
        access=access,
    )
    for alias, eng in name_engines(engine).items():
        redline.add_engine(eng, alias)

    deps: list[Any] = [Depends(redline_guard(access, prefix))]
    if dependencies is not None:
        deps += [d if isinstance(d, DependsParam) else Depends(d) for d in dependencies]
    if cfg.access == "local":
        logger.warning(
            "Redline is mounted at %s with no auth dependency (access='local'): only "
            "loopback clients are allowed. Set access='token' or 'basic', or pass "
            "install(app, dependencies=[...]) before exposing it.",
            prefix,
        )

    router = build_router(redline, deps)
    before = len(app.router.routes)
    app.include_router(router, prefix=prefix)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = added  # win over catch-all routes declared earlier

    if cfg.enabled:  # disabled: no middleware and no SQLAlchemy listeners at all
        app.add_middleware(
            RedlineMiddleware, recorder=recorder, prefix=prefix, debug=lambda: bool(app.debug)
        )
        instrument_serialization()

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
