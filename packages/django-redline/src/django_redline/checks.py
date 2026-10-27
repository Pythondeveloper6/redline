"""System checks for a Redline installation.

========================  ==============================================================
id                        problem
========================  ==============================================================
``django_redline.E001``   ``REDLINE`` is invalid (unknown key, bad value, bad PERMISSION)
``django_redline.E002``   the storage directory cannot be created or is not writable
``django_redline.W001``   ``RedlineMiddleware`` is not in ``MIDDLEWARE``
``django_redline.W002``   ``GZipMiddleware`` runs inside Redline (bodies would be gzip)
``django_redline.W003``   ``RedlineMiddleware`` is listed after middleware it should time
``django_redline.W004``   ``django_redline.urls`` is not mounted, so the UI is unreachable
``django_redline.W005``   ``DEBUG`` is False and ``SAMPLE_RATE`` is 1.0 (every request)
========================  ==============================================================
"""

from __future__ import annotations

import os
from typing import Any

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register
from django.core.exceptions import ImproperlyConfigured
from django.urls import NoReverseMatch, reverse

from redline_core.access import Access
from redline_core.conf import SettingsError

from .conf import get_core_settings, get_options

MIDDLEWARE_PATH = "django_redline.middleware.RedlineMiddleware"
GZIP = "django.middleware.gzip.GZipMiddleware"
#: Middleware that should run *inside* Redline so their time is part of the trace.
SHOULD_FOLLOW = (
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
)


@register(Tags.compatibility)
def check_redline(app_configs: Any = None, **kwargs: Any) -> list:
    try:
        core = get_core_settings()
        get_options()
        Access(core)  # a missing token_file, a weak token, a bad access_check path...
    except SettingsError as exc:
        return [Error(f"settings.REDLINE: {exc}", id="django_redline.E001")]
    except ImproperlyConfigured as exc:
        return [Error(str(exc), id="django_redline.E001")]
    problems: list = []
    problems += _middleware()
    problems += _urls()
    if core.enabled and not settings.DEBUG and core.sample_rate >= 1.0:
        problems.append(
            Warning(
                "Redline records every request (SAMPLE_RATE = 1.0) while DEBUG is False.",
                hint=(
                    "In production lower REDLINE['SAMPLE_RATE'] (e.g. 0.05); slow requests "
                    "and 5xx errors are always kept anyway. Leave SAMPLE_RATE unset to get "
                    "the production default of 0.1."
                ),
                id="django_redline.W005",
            )
        )
    if core.enabled:
        problems += _storage(core.storage_path)
    return problems


def _middleware() -> list:
    middleware = list(getattr(settings, "MIDDLEWARE", None) or [])
    if MIDDLEWARE_PATH not in middleware:
        return [
            Warning(
                "RedlineMiddleware is not installed, so no request is recorded.",
                hint=f"Add {MIDDLEWARE_PATH!r} near the top of MIDDLEWARE.",
                id="django_redline.W001",
            )
        ]
    problems = []
    position = middleware.index(MIDDLEWARE_PATH)
    if GZIP in middleware[position + 1 :]:
        problems.append(
            Warning(
                "GZipMiddleware is listed after RedlineMiddleware, so Redline sees "
                "compressed response bodies.",
                hint="Move RedlineMiddleware below GZipMiddleware.",
                id="django_redline.W002",
            )
        )
    before = [m for m in middleware[:position] if m in SHOULD_FOLLOW]
    if before:
        problems.append(
            Warning(
                "RedlineMiddleware is listed after " + ", ".join(before) + "; their time is "
                "not part of the recorded requests.",
                hint="Place RedlineMiddleware right after SecurityMiddleware/GZipMiddleware.",
                id="django_redline.W003",
            )
        )
    return problems


def _urls() -> list:
    try:
        reverse("redline:index")
    except NoReverseMatch:
        return [
            Warning(
                "Redline's URLs are not mounted, so the UI is unreachable.",
                hint='Add path("redline/", include("django_redline.urls")) to your URLconf.',
                id="django_redline.W004",
            )
        ]
    except Exception:  # a broken URLconf is reported by Django's own checks
        return []
    return []


def _storage(path: str) -> list:
    if path == ":memory:" or path.startswith("file:"):
        return []
    directory = os.path.dirname(os.path.abspath(path)) or "."
    probe = directory
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    target = path if os.path.exists(path) else probe
    if not os.access(target, os.W_OK):
        return [
            Error(
                f"Redline cannot write its storage at {path!r}.",
                hint="Point REDLINE['STORAGE_PATH'] at a writable location.",
                id="django_redline.E002",
            )
        ]
    return []
