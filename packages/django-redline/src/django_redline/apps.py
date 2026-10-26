from __future__ import annotations

from typing import Any

from django.apps import AppConfig


class RedlineConfig(AppConfig):
    name = "django_redline"
    label = "django_redline"
    verbose_name = "Redline"

    def ready(self) -> None:
        from django.core.signals import setting_changed

        from . import (
            checks,  # noqa: F401  (registers the system checks)
            runtime,  # noqa: F401  (rebuilds the runtime on REDLINE changes)
        )
        from .instrument import install

        install()
        setting_changed.connect(_reset_on_urlconf, dispatch_uid="django_redline.urlconf")


def _reset_on_urlconf(setting: str, **kwargs: Any) -> None:
    if setting == "ROOT_URLCONF":
        from .middleware import _reset_mount

        _reset_mount()
