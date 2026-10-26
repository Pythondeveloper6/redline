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
    Default: active staff users (:func:`default_permission`).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.utils.module_loading import import_string

from redline_core.conf import Settings, SettingsError

__all__ = [
    "DJANGO_KEYS",
    "PRODUCTION_SAMPLE_RATE",
    "SETTING_NAME",
    "DjangoOptions",
    "default_permission",
    "get_core_settings",
    "get_options",
]

SETTING_NAME = "REDLINE"
DJANGO_KEYS = ("permission",)
#: Default ``SAMPLE_RATE`` when ``DEBUG`` is False.
PRODUCTION_SAMPLE_RATE = 0.1

Permission = Callable[[HttpRequest], bool]


def default_permission(request: HttpRequest) -> bool:
    """Active staff users only (the default ``PERMISSION``)."""
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_active and user.is_staff)


@dataclass(frozen=True)
class DjangoOptions:
    """The Django-only part of ``REDLINE``."""

    permission: Permission = default_permission


def _raw() -> dict[str, Any]:
    raw = getattr(settings, SETTING_NAME, None) or {}
    if not isinstance(raw, dict):
        raise ImproperlyConfigured(f"settings.{SETTING_NAME} must be a dict.")
    for key in raw:
        if not isinstance(key, str):
            raise ImproperlyConfigured(f"settings.{SETTING_NAME} keys must be strings: {key!r}.")
    return raw


def get_core_settings() -> Settings:
    """``settings.REDLINE`` (minus Django-only keys) as core :class:`Settings`.

    Raises :class:`~django.core.exceptions.ImproperlyConfigured` for unknown keys or bad
    values (the core's message, including its "did you mean" hint).
    """
    raw = {k.lower(): v for k, v in _raw().items() if k.lower() not in DJANGO_KEYS}
    base_dir = getattr(settings, "BASE_DIR", None)
    if "sample_rate" not in raw and not settings.DEBUG:
        raw["sample_rate"] = PRODUCTION_SAMPLE_RATE
    if base_dir is not None:
        raw.setdefault("storage_path", os.path.join(str(base_dir), ".redline", "redline.db"))
        raw.setdefault("project_root", str(base_dir))
    try:
        return Settings.from_mapping(raw)
    except SettingsError as exc:
        raise ImproperlyConfigured(f"settings.{SETTING_NAME}: {exc}") from exc


def get_options() -> DjangoOptions:
    """The Django-only options (``PERMISSION``)."""
    raw = {k.lower(): v for k, v in _raw().items()}
    permission = raw.get("permission")
    if permission is None:
        return DjangoOptions()
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
    return DjangoOptions(permission=permission)
