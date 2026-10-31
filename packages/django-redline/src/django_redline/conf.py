"""Settings: the ``REDLINE`` dict, mapped onto the core :class:`redline_core.Settings`.

Every key of ``settings.REDLINE`` is a core setting (case-insensitive, see
``redline_core.conf``) except the Django-only ones listed in :data:`DJANGO_KEYS`.
Defaults follow ``DEBUG``:

* ``SAMPLE_RATE`` is ``1.0`` (keep every request) with ``DEBUG = True`` and ``0.1`` with
  ``DEBUG = False``. Slow requests and 5xx errors are always kept either way.
* ``STORAGE_PATH`` is ``<BASE_DIR>/.redline/redline.db`` when ``BASE_DIR`` is set, and
  ``PROJECT_ROOT`` (used to tell your code from library code) is ``BASE_DIR``.

Django-only keys:

``PERMISSION``
    Who may open the UI and the API: a callable ``(request) -> bool`` or its dotted path.
    Default: active superusers (:func:`default_permission`; staff before 0.2).
``STORE``
    Where traces go (the counterpart of FastAPI's ``install(store=...)``): a
    :class:`~redline_core.storage.Store` instance, a callable (or class) taking the core
    :class:`~redline_core.Settings` and returning one, or the dotted path of either.
    Default: ``store_from_settings(settings)`` (the SQLite file at ``STORAGE_PATH``, or
    the shared PostgreSQL store at ``STORAGE_URL``).
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.utils.module_loading import import_string

from redline_core.conf import Settings, SettingsError
from redline_core.config import ResolvedConfig, resolve_settings
from redline_core.storage import Store, store_from_settings

__all__ = [
    "DJANGO_KEYS",
    "PRODUCTION_SAMPLE_RATE",
    "SETTING_NAME",
    "DjangoOptions",
    "build_store",
    "default_permission",
    "get_core_settings",
    "get_options",
    "get_resolved_config",
]

SETTING_NAME = "REDLINE"
DJANGO_KEYS = ("permission", "store")
#: Default ``SAMPLE_RATE`` when ``DEBUG`` is False.
PRODUCTION_SAMPLE_RATE = 0.1

Permission = Callable[[HttpRequest], bool]


def default_permission(request: HttpRequest) -> bool:
    """Active superusers only (the default ``PERMISSION``; before 0.2 it was staff).

    Staff status is often handed out for the admin of a single model; Redline shows every
    captured request (headers, SQL, bodies) of every user, so the default asks for more.
    Use ``REDLINE = {"PERMISSION": "django_redline.conf.staff_permission"}`` (or your own
    callable) for the old behaviour."""
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_active and user.is_superuser)


def staff_permission(request: HttpRequest) -> bool:
    """Active staff users (the default ``PERMISSION`` before 0.2)."""
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_active and user.is_staff)


@dataclass(frozen=True)
class DjangoOptions:
    """The Django-only part of ``REDLINE``."""

    permission: Permission = default_permission
    #: The ``STORE`` value after importing a dotted path (``None``: the SQLite default).
    store: Any = None


def _raw() -> dict[str, Any]:
    raw = getattr(settings, SETTING_NAME, None) or {}
    if not isinstance(raw, dict):
        raise ImproperlyConfigured(f"settings.{SETTING_NAME} must be a dict.")
    for key in raw:
        if not isinstance(key, str):
            raise ImproperlyConfigured(f"settings.{SETTING_NAME} keys must be strings: {key!r}.")
    return raw


def get_resolved_config() -> ResolvedConfig:
    """The layered configuration (``redline_core.config``) with ``settings.REDLINE``
    (minus Django-only keys) as the framework layer.

    Adapter defaults (below a preset, the config file and ``REDLINE_*`` variables):
    ``sample_rate`` 0.1 without ``DEBUG``, ``storage_path``/``project_root`` from
    ``BASE_DIR``, and ``access="framework"`` (the ``PERMISSION`` check, default active
    superusers) unless ``ACCESS`` is set explicitly.

    Raises :class:`~django.core.exceptions.ImproperlyConfigured` for unknown keys or bad
    values (the core's message, including its "did you mean" hint).
    """
    raw = {k.lower(): v for k, v in _raw().items() if k.lower() not in DJANGO_KEYS}
    defaults: dict[str, Any] = {}
    base_dir = getattr(settings, "BASE_DIR", None)
    if not settings.DEBUG:
        defaults["sample_rate"] = PRODUCTION_SAMPLE_RATE
    if base_dir is not None:
        defaults["storage_path"] = os.path.join(str(base_dir), ".redline", "redline.db")
        defaults["project_root"] = str(base_dir)
    try:
        return resolve_settings(raw, adapter_defaults=defaults, native_hook=True)
    except SettingsError as exc:
        raise ImproperlyConfigured(f"settings.{SETTING_NAME}: {exc}") from exc


def get_core_settings() -> Settings:
    """``settings.REDLINE`` (minus Django-only keys) as core :class:`Settings` (see
    :func:`get_resolved_config`)."""
    return get_resolved_config().settings


def get_options() -> DjangoOptions:
    """The Django-only options (``PERMISSION``, ``STORE``)."""
    raw = {k.lower(): v for k, v in _raw().items()}
    store = raw.get("store")
    if isinstance(store, str):
        try:
            store = import_string(store)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"settings.{SETTING_NAME}['STORE'] could not be imported: {exc}"
            ) from exc
    if store is not None and not callable(store) and not isinstance(store, Store):
        raise ImproperlyConfigured(
            f"settings.{SETTING_NAME}['STORE'] must be a Store, a callable (settings) -> "
            f"Store, or the dotted path of either; got {store!r}."
        )
    permission = raw.get("permission")
    if permission is None:
        return DjangoOptions(store=store)
    if isinstance(permission, str):
        try:
            permission = import_string(permission)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"settings.{SETTING_NAME}['PERMISSION'] could not be imported: {exc}"
            ) from exc
    if not callable(permission):
        raise ImproperlyConfigured(
            f"settings.{SETTING_NAME}['PERMISSION'] must be a callable (request) -> bool "
            "or its dotted path."
        )
    return DjangoOptions(permission=permission, store=store)


def build_store(spec: Any, core: Settings) -> tuple[Store, bool]:
    """``(store, owned)`` for a ``STORE`` value; ``owned`` stores are closed on rebuilds.

    ``None`` builds the default store (SQLite, or PostgreSQL with ``storage_url``). A class
    or plain callable is called with the core settings. A ready instance is used as-is and
    is *not* closed when the runtime is rebuilt (``override_settings``), only flushed – it
    outlives the runtime.
    """
    if spec is None:
        return store_from_settings(core), True
    if inspect.isclass(spec) or (callable(spec) and not isinstance(spec, Store)):
        store = spec(core)
        owned = True
    else:
        store, owned = spec, False
    if not isinstance(store, Store):
        raise ImproperlyConfigured(
            f"settings.{SETTING_NAME}['STORE'] produced {store!r}, which does not implement "
            "the redline_core.storage.Store protocol."
        )
    return store, owned
