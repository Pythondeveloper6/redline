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

from redline_core import Recorder, Settings, configure
from redline_core.api import RedlineAPI
from redline_core.explain import ExplainExecutor
from redline_core.storage import SQLiteStore, Store

from ._hooks import install_hooks
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
        permission: Callable[[Any], Any],
        explicit_engines: bool,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self.settings = settings
        self.recorder = recorder
        self.store = store
        self.suggester = suggester
        self.permission = permission
        self.engines: dict[str, EngineLike] = {}
        self._discover = not explicit_engines
        self._lock = threading.Lock()
        self.api = RedlineAPI(
            store, settings, framework="flask", explain_executor=self._executor_for
        )

    def add_engine(self, engine: EngineLike, alias: str | None = None) -> str:
        """Instrument another engine; returns its ``db_alias``."""
        if alias is None:
            alias = alias_for(engine, self.engines)
        self.engines[alias] = engine
        if self.settings.enabled:
            instrument_engine(engine, alias)
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
        engine = self.engines.get(alias)
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
    ``settings``
        A ``Settings`` or a dict of settings (case-insensitive keys). ``app.config["REDLINE"]``
        (a dict) is merged underneath a dict given here; a ``Settings`` instance wins outright.
    ``url_prefix``
        Where the UI and API live (``/_redline/``). Never recorded.
    ``permission``
        ``callable(request) -> bool`` protecting the UI and API, e.g.
        ``lambda request: current_user.is_admin``. When omitted, :data:`local_only` applies
        (loopback clients addressing ``localhost``/``127.0.0.1``/``[::1]``) and a warning is
        logged. Raise ``werkzeug.exceptions.HTTPException`` (``abort(401)``) to answer with
        another status; it is rendered in the API's error shape.
    ``store``
        A custom ``Store``; default is the SQLite file at ``storage_path``.
    """

    def __init__(
        self,
        app: Flask | None = None,
        *,
        engine: Any = None,
        settings: Mapping[str, Any] | Settings | None = None,
        url_prefix: str = "/_redline",
        permission: Callable[[Any], Any] | None = None,
        store: Store | None = None,
    ) -> None:
        prefix = "/" + url_prefix.strip("/")
        if prefix == "/":
            raise ValueError("url_prefix must be a sub-path such as '/_redline'.")
        self.url_prefix = prefix
        self.permission = permission
        self._engine = engine
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
        prefix = self.url_prefix
        settings = self._settings
        if isinstance(settings, Settings):
            cfg = settings
        else:
            merged = {**(app.config.get(CONFIG_KEY) or {}), **(settings or {})}
            cfg = Settings.from_mapping(merged)
        own = rf"^{re.escape(prefix)}(/|$)"
        if own not in cfg.ignore_paths:
            cfg = cfg.replace(ignore_paths=[*cfg.ignore_paths, own])

        store = self._store if self._store is not None else SQLiteStore.from_settings(cfg)
        suggester = SQLAlchemyFixSuggester(None, project_root=cfg.project_root)
        recorder = configure(cfg, store, [suggester])

        permission = self.permission
        if permission is None:
            permission = local_only
            logger.warning(
                "Redline is mounted at %s with no permission check: only loopback clients "
                "are allowed. Pass Redline(app, permission=...) before exposing it.",
                prefix,
            )
        runtime = _Runtime(
            app=app,
            prefix=prefix,
            settings=cfg,
            recorder=recorder,
            store=store,
            suggester=suggester,
            permission=permission,
            explicit_engines=self._engine is not None,
        )
        for alias, eng in name_engines(self._engine).items():
            runtime.add_engine(eng, alias)
        app.extensions["redline"] = runtime
        self.app = app

        app.register_blueprint(build_blueprint(runtime, prefix))
        app.cli.add_command(redline_cli)
        if cfg.enabled:  # disabled: no middleware, hooks or SQLAlchemy listeners at all
            app.wsgi_app = RedlineMiddleware(app.wsgi_app, recorder, prefix)  # type: ignore[method-assign]
            install_hooks(app, runtime.discover_engines)
            runtime.discover_engines()
        atexit.register(runtime.close)
