from __future__ import annotations

from typing import Any

from django.apps import AppConfig


class RedlineConfig(AppConfig):
    name = "django_redline"
    label = "django_redline"
    verbose_name = "Redline"

    def ready(self) -> None:
        from django.core.signals import setting_changed
        from django.db.models.signals import post_init

        from . import (
            checks,  # noqa: F401  (registers the system checks)
            runtime,  # noqa: F401  (rebuilds the runtime on REDLINE changes)
        )
        from .instrument import install

        post_init.connect(_model_initialized, dispatch_uid="django_redline.model_count", weak=False)
        install()
        setting_changed.connect(_reset_on_urlconf, dispatch_uid="django_redline.urlconf")


def _reset_on_urlconf(setting: str, **kwargs: Any) -> None:
    if setting == "ROOT_URLCONF":
        from .middleware import _reset_mount

        _reset_mount()


def _model_initialized(sender, instance, **kwargs):
    from redline_core.orm import note_instance

    note_instance()
