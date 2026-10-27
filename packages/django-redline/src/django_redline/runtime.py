"""The process-wide Redline runtime: settings, store, recorder and JSON API.

Built lazily on first use (the first recorded request or UI hit), not in
``AppConfig.ready()``, so management commands such as ``migrate`` never create the
``.redline/`` directory. ``override_settings(REDLINE=...)`` (and changes to ``DEBUG`` or
``BASE_DIR``) rebuild it through the ``setting_changed`` signal.
"""

from __future__ import annotations

import atexit
import logging
import threading
from dataclasses import dataclass

from django.core.signals import setting_changed
from django.dispatch import receiver

from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.recorder import Recorder, configure
from redline_core.storage import Store

from .conf import DjangoOptions, build_store, get_core_settings, get_options

__all__ = ["Runtime", "get_runtime", "reset_runtime"]

logger = logging.getLogger("redline")


@dataclass
class Runtime:
    settings: Settings
    options: DjangoOptions
    store: Store
    recorder: Recorder
    api: RedlineAPI
    #: False for a ``STORE`` given as a ready instance: it outlives runtime rebuilds.
    owns_store: bool = True

    def close(self, *, final: bool = False) -> None:
        try:
            self.store.flush(2.0)
        except Exception:  # pragma: no cover - defensive
            pass
        if not (self.owns_store or final):
            return
        try:
            self.store.close()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Redline failed to close its store")


_runtime: Runtime | None = None
_lock = threading.Lock()


def build_runtime(settings: Settings | None = None) -> Runtime:
    """A fresh runtime for ``settings`` (default: from ``settings.REDLINE``)."""
    from .explain import executor_factory
    from .suggest import DjangoFixSuggester

    core = settings or get_core_settings()
    options = get_options()
    store, owned = build_store(options.store, core)
    suggester = DjangoFixSuggester(project_root=core.project_root)
    recorder = configure(core, store, [suggester])
    launcher = stopper = None
    if core.load_from_ui:  # importing the load tester only when it can be used
        from redline_core.load.control import run_hooks

        launcher, stopper = run_hooks(store, core)
    api = RedlineAPI(
        store,
        core,
        framework="django",
        explain_executor=executor_factory,
        run_launcher=launcher,
        run_stopper=stopper,
    )
    return Runtime(
        settings=core,
        options=options,
        store=store,
        recorder=recorder,
        api=api,
        owns_store=owned,
    )


def get_runtime() -> Runtime:
    """The runtime, built on first call."""
    global _runtime
    rt = _runtime
    if rt is not None:
        return rt
    with _lock:
        if _runtime is None:
            _runtime = build_runtime()
        return _runtime


def peek_runtime() -> Runtime | None:
    """The runtime if it has been built, without building it."""
    return _runtime


def reset_runtime() -> None:
    """Close the current runtime (flushing pending traces); the next use rebuilds it."""
    global _runtime
    with _lock:
        rt, _runtime = _runtime, None
    if rt is not None:
        rt.close()


@atexit.register
def _close_at_exit() -> None:
    rt = _runtime
    if rt is not None:
        rt.close(final=True)


@receiver(setting_changed)
def _settings_changed(setting: str, **kwargs: object) -> None:
    if setting in ("REDLINE", "DEBUG", "BASE_DIR"):
        reset_runtime()
