"""Operation-level spans for boto3 clients, without recording request or response data.

Call :func:`instrument_boto3` explicitly at startup. Each AWS SDK operation is represented
as an ``http_out`` span; lower-level HTTP capture may also record its network request.
"""

from __future__ import annotations

import re
import time
from functools import wraps
from typing import Any

from redline_core import record_span
from redline_core.recorder import current

__all__ = ["instrument_boto3", "uninstrument_boto3"]

_perf = time.perf_counter
_original: Any = None
_wrapper: Any = None
_IDENTIFIER = re.compile(r"[^A-Za-z0-9_.-]")


def _bounded_identifier(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    safe = _IDENTIFIER.sub("_", value)[:64]
    return safe or fallback


def _record(service: str, operation: str, start: float, status: int | None) -> None:
    try:
        url = f"https://{service}.amazonaws.com/__sdk__/{operation}"
        record_span(
            "http_out",
            f"AWS {service} {operation}",
            start,
            _perf() - start,
            {
                "method": f"SDK:{operation}",
                "url": url,
                "status": status,
                "service": service,
                "sdk": "boto3",
            },
            capture_callsite=False,
        )
    except Exception:
        # Profiling must never alter the application's result or exception.
        return


def instrument_boto3() -> bool:
    """Patch botocore's client operation entry point; return False if it is unavailable."""
    global _original, _wrapper
    if _wrapper is not None:
        return True
    try:
        from botocore.client import BaseClient
    except ImportError:
        return False

    original = BaseClient._make_api_call

    @wraps(original)
    def make_api_call(self: Any, operation_name: str, api_params: Any) -> Any:
        try:
            builder = current()
        except Exception:
            builder = None
        if builder is None:
            return original(self, operation_name, api_params)
        try:
            service = _bounded_identifier(
                getattr(getattr(self, "meta", None), "service_model", None)
                and self.meta.service_model.service_name,
                "aws",
            )
            operation = _bounded_identifier(operation_name, "operation")
            start = _perf()
        except Exception:
            return original(self, operation_name, api_params)
        status = None
        try:
            result = original(self, operation_name, api_params)
            try:
                response = result["ResponseMetadata"]["HTTPStatusCode"]
                if isinstance(response, int) and 100 <= response <= 599:
                    status = response
            except Exception:
                pass
            return result
        finally:
            _record(service, operation, start, status)

    BaseClient._make_api_call = make_api_call
    _original, _wrapper = original, make_api_call
    return True


def uninstrument_boto3() -> None:
    """Restore botocore's operation method if this integration still owns the patch."""
    global _original, _wrapper
    if _wrapper is None:
        return
    try:
        from botocore.client import BaseClient
    except ImportError:
        _original = _wrapper = None
        return
    if BaseClient._make_api_call is _wrapper:
        BaseClient._make_api_call = _original
    _original = _wrapper = None
