"""Attach django-redline only in development: ``install_if_dev`` for ``settings.py``.

::

    # settings.py
    DEBUG = os.environ.get("DJANGO_DEBUG") == "1"
    ...
    from django_redline import install_if_dev
    install_if_dev(globals())

    # urls.py
    from django_redline import urls_if_dev
    urlpatterns += urls_if_dev()

Nothing is added (and a warning is logged) when ``DEBUG`` is false or the environment
(``REDLINE_ENV``/``APP_ENV``/``ENV``/``ENVIRONMENT``/``DJANGO_ENV``) says ``production``.
``REDLINE_ALLOW_PRODUCTION=1`` overrides. This module never imports Django, so it is safe
to import while ``settings.py`` is still being executed.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from redline_core.environment import refuse_in_production

__all__ = ["APP", "MIDDLEWARE", "install_if_dev", "urls_if_dev"]

APP = "django_redline"
MIDDLEWARE = "django_redline.middleware.RedlineMiddleware"


def _refuse(namespace: MutableMapping[str, Any] | None) -> bool:
    debug = None
    if namespace is not None and "DEBUG" in namespace:
        debug = bool(namespace["DEBUG"])
    else:
        try:
            from django.conf import settings

            if settings.configured:
                debug = bool(settings.DEBUG)
        except Exception:  # pragma: no cover - settings not importable
            debug = None
    return refuse_in_production("django", debug=debug)


def install_if_dev(settings_namespace: MutableMapping[str, Any]) -> bool:
    """Add the app and the middleware to the settings namespace (``globals()``).

    The middleware goes first in ``MIDDLEWARE``. Idempotent. Returns ``True`` when Redline
    was added (or already present), ``False`` when production signals refused it.
    """
    if _refuse(settings_namespace):
        return False
    apps = list(settings_namespace.get("INSTALLED_APPS", ()))
    if APP not in apps:
        apps.append(APP)
    settings_namespace["INSTALLED_APPS"] = apps
    middleware = list(settings_namespace.get("MIDDLEWARE", ()))
    if MIDDLEWARE not in middleware:
        middleware.insert(0, MIDDLEWARE)
    settings_namespace["MIDDLEWARE"] = middleware
    return True


def urls_if_dev(prefix: str = "redline/") -> list[Any]:
    """``[path(prefix, include("django_redline.urls"))]``, or ``[]`` when Redline is not
    installed or the process looks like production (see :func:`install_if_dev`)."""
    from django.conf import settings
    from django.urls import include, path

    if APP not in settings.INSTALLED_APPS and "django_redline.apps.RedlineConfig" not in (
        settings.INSTALLED_APPS
    ):
        return []
    if _refuse(None):
        return []

    return [path(prefix, include("django_redline.urls"))]
