"""``Redline(app)``: wire Redline into a Flask application."""

from __future__ import annotations

import atexit
import contextlib
import logging
import re
import threading
from collections.abc import Callable, Mapping
from typing import Any

from flask import Flask, current_app, has_app_context

from redline_core import Recorder, Settings, configure, ui
from redline_core.access import Access
from redline_core.api import RedlineAPI
from redline_core.config import ResolvedConfig, resolve_settings
from redline_core.explain import ExplainExecutor
from redline_core.load.control import run_hooks
from redline_core.storage import SQLiteStore, Store

from ._hooks import install_hooks
from ._middleware import RedlineMiddleware
from ._security import environ_info
from ._sqlalchemy import (
    EngineLike,
    alias_for,
    auto_engine,
    build_executor,
    instrument_engine,
    name_engines,
    vendor_of,
)
from .blueprint import build_blueprint
from .cli import redline_cli
from .suggest import SQLAlchemyFixSuggester

__all__ = ["Redline"]

logger = logging.getLogger("redline")

#: ``app.config`` key holding a dict of Redline settings (merged under explicit kwargs).
CONFIG_KEY = "REDLINE"


class _Runtime:
    """Everything Redline owns for one app (``app.extensions["redline"]``)."""

    def __init__(
        self,
        *,
        app: Flask,
        prefix: str,
        settings: Settings,
        recorder: Recorder,
        store: Store,
        suggester: SQLAlchemyFixSuggester,
        permission: Callable[[Any], Any] | None,
        explicit_engines: bool,
        config: ResolvedConfig | None = None,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self.settings = settings
        self.recorder = recorder
        self.store = store
        self.suggester = suggester
        self.permission = permission
        self.config = config
        self.access = Access(
            settings,
            access_explicit=config.access_explicit if config is not None else True,
            debug=lambda: bool(app.debug),
        )
        self.engines: dict[str, EngineLike] = {}
        self._discover = not explicit_engines
        self._lock = threading.Lock()
        launcher, stopper = run_hooks(store, settings)  # (None, None) unless load_from_ui
        self.api = RedlineAPI(
            store,
            settings,
            framework="flask",
            explain_executor=self._executor_for,
            run_launcher=launcher,
            run_stopper=stopper,
            config=config,
            access=self.access,
        )

    def badge_on(self) -> bool:
        """The floating badge: ``Settings.badge``, where ``"auto"`` follows ``app.debug``."""
        return ui.badge_enabled(self.settings, self.app.debug)

    def add_engine(self, engine: EngineLike, alias: str | None = None) -> str:
        """Instrument another engine; returns its ``db_alias``."""
        if alias is None:
            alias = alias_for(engine, self.engines)
        self.engines[alias] = engine
        if self.settings.enabled:
            instrument_engine(engine, alias, recorder=self.recorder)
        return alias

    def discover_engines(self) -> None:
        """Instrument Flask-SQLAlchemy's engines once its extension exists.

        Engines are created in ``db.init_app``, which may run after ``Redline.init_app``,
        so this is retried (cheaply) on the first requests until the extension is there.
        """
        if not self._discover:
            return
        ext = self.app.extensions.get("sqlalchemy")
        if ext is None or not hasattr(type(ext), "engines"):
            return
        with self._lock:
            if not self._discover:
                return
            try:
                with self.app.app_context():
                    found = dict(ext.engines)
            except (KeyError, RuntimeError):  # db.init_app(app) has not run yet
                return
            for key, engine in found.items():
                alias = "default" if key is None else str(key)
                if alias in self.engines:
                    alias = alias_for(engine, self.engines)
                self.add_engine(engine, alias)
            self._discover = False

    def _executor_for(self, alias: str, vendor: str) -> ExplainExecutor | None:
        self.discover_engines()
        engine = self.engines.get(alias) or auto_engine(alias)
        if engine is None:
            return None
        return build_executor(engine, vendor_of(engine), lambda: None)

    def close(self) -> None:
        """Flush pending traces and close the store (registered with ``atexit``)."""
        with contextlib.suppress(Exception):
            self.store.flush(5.0)
        with contextlib.suppress(Exception):
            self.store.close()


class Redline:
    """The Flask extension.

    ``Redline(app, engine=...)`` or, with an application factory, ``Redline(engine=...)``
    followed by ``redline.init_app(app)`` for each app.

    ``engine``
        A SQLAlchemy ``Engine``, a list of them, or ``{alias: engine}``; the first of a list
        is the ``"default"`` db alias (used by EXPLAIN). When omitted and Flask-SQLAlchemy
        is set up on the app, its engines are instrumented automatically.
    ``engines``
        ``"auto"`` instruments every SQLAlchemy engine created after install (per-user or
        per-request engines) without registering it, by setting
        ``instrument_engines="all"``. Engines are held weakly. Default: only the engines
        passed as ``engine``.
    ``settings``
        A ``Settings`` or a dict of settings (case-insensitive keys). ``app.config["REDLINE"]``
        (a dict) is merged underneath a dict given here; a ``Settings`` instance replaces it.
        This is the top layer over ``redline.toml`` / ``[tool.redline]`` and ``REDLINE_*``
        variables (``redline_core.config``).
    ``url_prefix``
        Where the UI and API live (default: the ``mount_path`` setting, else
        ``/_redline``). Never recorded.
    ``permission``
        ``callable(request) -> bool`` protecting the UI and API, e.g.
        ``lambda request: current_user.is_admin``; giving it implies ``access="framework"``
        unless the ``access`` setting is set explicitly. When omitted, the ``access``
        setting decides; when that is not set either, Redline answers 404 unless
        ``app.debug`` is on (then ``local``: loopback clients addressing
        ``localhost``/``127.0.0.1``/``[::1]``). Raise
        ``werkzeug.exceptions.HTTPException`` (``abort(401)``) to answer with another
        status; it is rendered in the API's error shape.
    ``store``
        A custom ``Store``; default is the SQLite file at ``storage_path``.
    """

    def __init__(
        self,
        app: Flask | None = None,
        *,
        engine: Any = None,
        engines: str | None = None,
        settings: Mapping[str, Any] | Settings | None = None,
        url_prefix: str | None = None,
        permission: Callable[[Any], Any] | None = None,
        store: Store | None = None,
        recorder: Recorder | None = None,
        mode: str = "standalone",
    ) -> None:
        if mode not in {"standalone", "nested"}:
            raise ValueError("mode must be standalone or nested")
        if mode == "nested" and (recorder is None or recorder.store is None):
            raise ValueError("nested mode requires the outer recorder with a configured store")
        if recorder is not None and (settings is not None or store is not None):
            raise ValueError("A shared recorder owns its settings and store")
        self._shared_recorder = recorder
        self._mode = mode
        if url_prefix is not None:
            url_prefix = "/" + url_prefix.strip("/")
            if url_prefix == "/":
                raise ValueError("url_prefix must be a sub-path such as '/_redline'.")
        self._url_prefix = url_prefix
        self.url_prefix = url_prefix or "/_redline"
        self.permission = permission
        self._engine = engine
        if engines not in (None, "auto"):
            raise ValueError('engines must be "auto" or None')
        self._auto_engines = engines == "auto"
        self._settings = settings
        self._store = store
        self.app: Flask | None = None
        if app is not None:
            self.init_app(app)

    # per-app state -----------------------------------------------------------
    def _runtime(self, app: Flask | None = None) -> _Runtime:
        if app is None:
            app = current_app._get_current_object() if has_app_context() else self.app  # type: ignore[attr-defined]
        runtime = None if app is None else app.extensions.get("redline")
        if runtime is None:
            raise RuntimeError("Redline is not initialised for this app; call init_app(app).")
        return runtime

    @property
    def settings(self) -> Settings:
        return self._runtime().settings

    @property
    def recorder(self) -> Recorder:
        return self._runtime().recorder

    @property
    def store(self) -> Store:
        return self._runtime().store

    @property
    def api(self) -> RedlineAPI:
        return self._runtime().api

    @property
    def engines(self) -> dict[str, EngineLike]:
        return self._runtime().engines

    @property
    def suggester(self) -> SQLAlchemyFixSuggester:
        return self._runtime().suggester

    def add_engine(self, engine: EngineLike, alias: str | None = None) -> str:
        """Instrument another engine on the current (or only) app; returns its ``db_alias``."""
        return self._runtime().add_engine(engine, alias)

    def close(self) -> None:
        """Flush pending traces and close the store (also done at interpreter exit)."""
        self._runtime().close()

    # setup -------------------------------------------------------------------
    def init_app(self, app: Flask) -> None:
        if "redline" in app.extensions:
            raise RuntimeError("Redline is already installed on this app.")
        settings = self._shared_recorder.settings if self._shared_recorder else self._settings
        if not isinstance(settings, Settings):
            settings = {**(app.config.get(CONFIG_KEY) or {}), **(settings or {})}
        resolved = resolve_settings(settings, native_hook=self.permission is not None)
        cfg = resolved.settings
        if self._auto_engines:
            cfg = cfg.replace(instrument_engines="all")
        prefix = self._url_prefix or "/" + (cfg.mount_path or "/_redline").strip("/")
        self.url_prefix = prefix
        own = rf"^{re.escape(prefix)}(/|$)"
        if own not in cfg.ignore_paths:
            cfg = cfg.replace(ignore_paths=[*cfg.ignore_paths, own])

        store = (
            self._shared_recorder.store
            if self._shared_recorder
            else self._store
            if self._store is not None
            else SQLiteStore.from_settings(cfg)
        )
        suggester = SQLAlchemyFixSuggester(None, project_root=cfg.project_root)
        recorder = self._shared_recorder or configure(cfg, store, [suggester])

        permission = self.permission
        runtime = _Runtime(
            app=app,
            prefix=prefix,
            settings=cfg,
            recorder=recorder,
            store=store,
            suggester=suggester,
            permission=permission,
            explicit_engines=self._engine is not None,
            config=resolved,
        )
        for alias, eng in name_engines(self._engine).items():
            runtime.add_engine(eng, alias)
        runtime.access.log_startup(prefix, "pass Redline(app, permission=...)")
        app.extensions["redline"] = runtime
        self.app = app

        app.register_blueprint(build_blueprint(runtime, prefix))
        app.cli.add_command(redline_cli)
        if cfg.enabled:  # disabled: no middleware, hooks or SQLAlchemy listeners at all
            app.wsgi_app = RedlineMiddleware(  # type: ignore[method-assign]
                app.wsgi_app,
                recorder,
                prefix,
                nested=self._mode == "nested",
                badge=runtime.badge_on,
                debug=lambda: bool(app.debug),
                force_ok=lambda environ: runtime.access.may_force(
                    environ_info(environ), debug=bool(app.debug)
                ),
            )
            install_hooks(app, runtime.discover_engines)
            runtime.discover_engines()
        if self._shared_recorder is None:
            atexit.register(runtime.close)
