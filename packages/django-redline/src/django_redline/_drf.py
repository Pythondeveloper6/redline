"""Django REST framework serialization as ``serializer`` spans.

``BaseSerializer.data`` is where DRF turns an instance (or queryset) into primitives, for
``Serializer``, ``ModelSerializer`` and ``ListSerializer`` alike (their ``data`` properties
call it through ``super()``), so that one property is wrapped. Only the *outermost* access
is recorded: nested serializers run inside their parent's ``to_representation`` and
serializers built by hand inside a ``SerializerMethodField`` are covered by the parent's
span. DRF is optional – without it nothing happens – and outside a traced request the
wrapper costs one ``ContextVar.get()``.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from redline_core import record_span
from redline_core.recorder import current

__all__ = ["install_drf", "uninstall_drf"]

_perf = time.perf_counter
_original: list[Any] = []
_inside: ContextVar[bool] = ContextVar("redline_drf", default=False)


def install_drf() -> bool:
    """Patch ``BaseSerializer.data``; ``False`` when DRF is not installed. Idempotent."""
    from django.apps import apps

    if not apps.is_installed("rest_framework"):
        return False
    try:
        from rest_framework.serializers import BaseSerializer
    except ImportError:
        return False
    if _original:
        return True
    prop = BaseSerializer.__dict__["data"]
    fget = prop.fget
    _original.append(prop)

    def data(self: Any) -> Any:
        if _inside.get() or current() is None:
            return fget(self)
        token = _inside.set(True)
        start = _perf()
        result = None
        try:
            result = fget(self)
            return result
        finally:
            _inside.reset(token)
            duration = _perf() - start
            many = hasattr(self, "child")
            name = type(self.child if many else self).__name__
            items = None
            if many and result is not None:
                try:
                    items = len(result)
                except TypeError:
                    items = None
            record_span(
                "serializer",
                f"{name}.data",
                start,
                duration,
                {"library": "drf", "serializer": name, "items": items, "many": many},
            )

    BaseSerializer.data = property(data, prop.fset, prop.fdel, prop.__doc__)  # type: ignore[assignment]
    return True


def uninstall_drf() -> None:
    """Undo :func:`install_drf`."""
    if not _original:
        return
    from rest_framework.serializers import BaseSerializer

    BaseSerializer.data = _original.pop()  # type: ignore[assignment]
