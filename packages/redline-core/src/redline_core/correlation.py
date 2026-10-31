"""Correlate logs and error reports with the Redline recording they happened in.

* :class:`TraceIdFilter` stamps ``record.redline_trace_id`` on log records (``"-"`` outside a
  recorded request or job), so ``%(redline_trace_id)s`` works in any log format.
* :func:`sentry_before_send` is a Sentry ``before_send`` hook that adds the tag
  ``redline_trace_id`` and a ``redline`` context with an "Open in Redline" URL to events
  raised inside a recording. :func:`tag_sentry_scope` does the same on the current scope.
  ``sentry_sdk`` is never imported by the hook and is optional for the scope helper.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from typing import Any

from .recorder import current

__all__ = [
    "TRACE_ATTRIBUTE",
    "TraceIdFilter",
    "chain_before_send",
    "current_trace_id",
    "install_log_filter",
    "redline_url",
    "sentry_before_send",
    "tag_sentry_scope",
]

TRACE_ATTRIBUTE = "redline_trace_id"
PUBLIC_URL_ENV = "REDLINE_PUBLIC_URL"


def current_trace_id() -> str | None:
    """The id of the recording in progress in this context, or ``None``."""
    builder = current()
    return builder.id if builder is not None and builder.active else None


class TraceIdFilter(logging.Filter):
    """Adds ``redline_trace_id`` to every record; never drops one.

    Attach it to a handler (a logger's filter only sees records logged on that logger)::

        handler.addFilter(TraceIdFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s [%(redline_trace_id)s] %(message)s"))
    """

    def __init__(self, name: str = "", *, default: str = "-") -> None:
        super().__init__(name)
        self.default = default

    def filter(self, record: logging.LogRecord) -> bool:
        record.redline_trace_id = current_trace_id() or self.default
        return True


def install_log_filter(handlers: Iterable[logging.Handler] | None = None) -> TraceIdFilter:
    """Add one :class:`TraceIdFilter` to ``handlers`` (default: the root logger's handlers)."""
    flt = TraceIdFilter()
    for handler in list(logging.getLogger().handlers) if handlers is None else handlers:
        if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
            handler.addFilter(flt)
    return flt


def redline_url(trace_id: str, *, base_url: str | None = None, mount: str = "/_redline") -> str:
    """Link to a recording in the UI. ``base_url`` defaults to ``$REDLINE_PUBLIC_URL``; with
    neither, the link is the path on the app's own origin."""
    base = (base_url or os.environ.get(PUBLIC_URL_ENV) or "").rstrip("/")
    return f"{base}/{mount.strip('/')}/#/requests/{trace_id}"


def sentry_before_send(
    event: dict[str, Any],
    hint: Any = None,
    *,
    base_url: str | None = None,
    mount: str = "/_redline",
) -> dict[str, Any]:
    """Sentry ``before_send``: tag and link events that happened inside a recording."""
    trace_id = current_trace_id()
    if trace_id is None:
        return event
    tags = event.get("tags")
    if isinstance(tags, list):
        tags.append([TRACE_ATTRIBUTE, trace_id])
    else:
        event["tags"] = {**(tags or {}), TRACE_ATTRIBUTE: trace_id}
    event.setdefault("contexts", {})["redline"] = {
        "trace_id": trace_id,
        "open_in_redline": redline_url(trace_id, base_url=base_url, mount=mount),
    }
    return event


def tag_sentry_scope(*, base_url: str | None = None, mount: str = "/_redline") -> bool:
    """Set the tag and context on Sentry's current scope. ``False`` when ``sentry_sdk`` is
    not installed or no recording is in progress."""
    trace_id = current_trace_id()
    if trace_id is None:
        return False
    try:
        import sentry_sdk
    except ImportError:
        return False
    sentry_sdk.set_tag(TRACE_ATTRIBUTE, trace_id)
    sentry_sdk.set_context(
        "redline",
        {
            "trace_id": trace_id,
            "open_in_redline": redline_url(trace_id, base_url=base_url, mount=mount),
        },
    )
    return True


def chain_before_send(
    previous: Callable[[dict[str, Any], Any], dict[str, Any] | None] | None,
    **kwargs: Any,
) -> Callable[[dict[str, Any], Any], dict[str, Any] | None]:
    """A ``before_send`` that runs ``previous`` first, then :func:`sentry_before_send`."""

    def hook(event: dict[str, Any], hint: Any) -> dict[str, Any] | None:
        if previous is not None:
            event = previous(event, hint)  # type: ignore[assignment]
            if event is None:
                return None
        return sentry_before_send(event, hint, **kwargs)

    return hook
