"""``RedlinePlugin``: wire Redline into a Litestar application."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from litestar.concurrency import sync_to_thread
from litestar.config.app import AppConfig
from litestar.middleware import DefineMiddleware
from litestar.plugins import InitPluginProtocol

from redline_core import Recorder, Settings, configure
from redline_core.access import Access
from redline_core.api import RedlineAPI
from redline_core.config import resolve_settings
from redline_core.explain import ExplainExecutor
from redline_core.load.control import run_hooks
from redline_core.storage import Store, store_from_settings

from ._middleware import EXCEPTION_KEY, RedlineMiddleware
from ._security import redline_guard, scope_info
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

__all__ = ["RedlinePlugin"]

logger = logging.getLogger("redline")


def _sqlalchemy_engines(plugins: Sequence[Any]) -> list[Any]:
    """Engines of Litestar's ``SQLAlchemyPlugin`` / ``SQLAlchemyInitPlugin`` configs.

    Duck-typed (a plugin with a ``config`` list whose items have ``get_engine``) so that
    ``advanced-alchemy`` stays optional. ``get_engine()`` caches, so this is the very
    engine the application's sessions use.
    """
    engines: list[Any] = []
    for plugin in plugins:
        configs = getattr(plugin, "config", None)
        if configs is None:
            continue
        if not isinstance(configs, (list, tuple)):
            configs = [configs]
        for config in configs:
            get_engine = getattr(config, "get_engine", None)
            if get_engine is None:
                continue
            with contextlib.suppress(Exception):
                engine = get_engine()
                if not any(engine is e for e in engines):
                    engines.append(engine)
    return engines


class RedlinePlugin(InitPluginProtocol):
    """Litestar plugin: ``Litestar(plugins=[RedlinePlugin(engine=engine)])``.

    ``settings``
        A ``Settings`` or a dict of settings (case-insensitive keys): the top layer over
        ``redline.toml`` / ``[tool.redline]`` and ``REDLINE_*`` variables.
    ``engine``
        A sync ``Engine``, an ``AsyncEngine``, a list of them, or ``{alias: engine}``.
        The first of a list is the ``"default"`` db alias (used by EXPLAIN). When omitted
        and the app has Litestar's ``SQLAlchemyPlugin``, its engine(s) are instrumented.
    ``path``
        Where the UI and API live (default: the ``mount_path`` setting, else
        ``/_redline``). Never recorded.
    ``guards``
        Litestar guards protecting the UI and API, e.g. ``[require_admin]``; giving them
        implies ``access="framework"`` unless the ``access`` setting is set explicitly.
        When omitted (or ``[]``), the ``access`` setting decides; when that is not set
        either, Redline answers 404 unless the app is in debug mode (then ``local``:
        loopback clients addressing ``localhost``/``127.0.0.1``/``[::1]``).
        ``HTTPException`` raised by guards is answered in the API's error shape.
    ``store``
        A custom ``Store``; default is the SQLite file at ``storage_path`` (or
        the shared PostgreSQL store at ``storage_url``).

    The plugin object is also the handle: ``plugin.recorder``, ``plugin.store``,
    ``plugin.settings``, ``plugin.add_engine(...)``, ``plugin.close()``.
    """

    def __init__(
        self,
        settings: Mapping[str, Any] | Settings | None = None,
        engine: Any = None,
        path: str | None = None,
        guards: Sequence[Any] | None = None,
        store: Store | None = None,
    ) -> None:
        # An empty list is no hook: the normal default rules apply, not "framework" with
        # nothing to check.
        self.config = resolve_settings(settings, native_hook=bool(guards))
        cfg = self.config.settings
        path = path if path is not None else (cfg.mount_path or "/_redline")
        path = "/" + path.strip("/")
        if path == "/":
            raise ValueError("path must be a sub-path such as '/_redline'.")
        own = rf"^{re.escape(path)}(/|$)"
        if own not in cfg.ignore_paths:
            cfg = cfg.replace(ignore_paths=[*cfg.ignore_paths, own])
        self.path = path
        self.settings = cfg
        self.access = Access(cfg, access_explicit=self.config.access_explicit)
        self.guards = guards
        self._engine_arg = engine
        self.engines: dict[str, EngineLike] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.store: Store = store if store is not None else store_from_settings(cfg)
        self.suggester = SQLAlchemyFixSuggester(project_root=cfg.project_root)
        self.recorder: Recorder = configure(cfg, self.store, [self.suggester])
        # POST api/runs: only with load_from_ui and a file-backed SQLiteStore (else 501)
        launcher, stopper = run_hooks(self.store, cfg)
        self.api = RedlineAPI(
            self.store,
            cfg,
            framework="litestar",
            explain_executor=self._executor_for,
            run_launcher=launcher,
            run_stopper=stopper,
            config=self.config,
            access=self.access,
        )
        self._installed = False

    # -- engines -------------------------------------------------------------
    def add_engine(self, engine: EngineLike, alias: str | None = None) -> str:
        """Instrument another engine; returns its ``db_alias``."""
        if alias is None:
            alias = alias_for(engine, self.engines)
        self.engines[alias] = engine
        if self.settings.enabled:
            instrument_engine(engine, alias, recorder=self.recorder)
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

    # -- Litestar hooks ------------------------------------------------------
    async def _after_exception(self, exc: Exception, scope: Any) -> None:
        # Litestar turns the exception into a response *inside* our middleware; keep it so
        # the trace gets its ``error`` (core ignores it for 4xx responses).
        scope[EXCEPTION_KEY] = exc

    async def _on_shutdown(self) -> None:
        await sync_to_thread(self.close)

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        if self._installed:
            raise RuntimeError("This RedlinePlugin is already installed on an app.")
        self._installed = True

        engine = self._engine_arg
        if engine is None:
            engine = _sqlalchemy_engines(app_config.plugins)
        for alias, eng in name_engines(engine).items():
            self.add_engine(eng, alias)

        guards = [redline_guard(self.access, self.path), *(self.guards or [])]
        self.access.debug = lambda: bool(app_config.debug)
        self.access.log_startup(self.path, "pass RedlinePlugin(guards=[...])")
        app_config.route_handlers.append(build_router(self, self.path, guards))

        if self.settings.enabled:  # disabled: no middleware and no SQLAlchemy listeners
            access = self.access

            def force_ok(scope: Any) -> bool:
                debug = bool(getattr(scope.get("app"), "debug", False))
                return access.may_force(scope_info(scope), debug=debug)

            app_config.middleware.insert(
                0,
                DefineMiddleware(
                    RedlineMiddleware, recorder=self.recorder, mount=self.path, force_ok=force_ok
                ),
            )
            app_config.after_exception.append(self._after_exception)
        app_config.on_shutdown.append(self._on_shutdown)
        return app_config
