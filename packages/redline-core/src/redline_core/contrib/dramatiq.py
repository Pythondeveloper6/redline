"""Optional Dramatiq integration; message arguments and results are not captured.

``instrument_dramatiq(broker, recorder)`` adds a middleware that stores the enqueuing
request's trace context in ``message.options["redline"]`` and records each processed
message as a job linked to it. Add it in the web process (propagation) and in the worker
process (recording); both use the same call.
"""

from __future__ import annotations

import threading
from typing import Any

import dramatiq

from ..jobs import job, propagation_headers

__all__ = ["RedlineMiddleware", "instrument_dramatiq"]


class RedlineMiddleware(dramatiq.Middleware):
    def __init__(self, recorder: Any = None, *, task_filter: Any = None) -> None:
        self.recorder = recorder
        self.task_filter = task_filter
        self._open: dict[str, Any] = {}
        self._lock = threading.Lock()

    def before_enqueue(self, broker: Any, message: Any, delay: Any) -> None:
        try:
            headers = propagation_headers()
            if headers:
                message.options["redline"] = headers
        except Exception:  # propagation must never break enqueueing
            pass

    def before_process_message(self, broker: Any, message: Any) -> None:
        if self.task_filter and not self.task_filter(message.actor_name):
            return
        ctx = job(
            message.actor_name,
            recorder=self.recorder,
            task_id=message.message_id,
            queue=message.queue_name,
            attempt=message.options.get("retries", 0),
            parent=message.options.get("redline"),
        )
        ctx.__enter__()
        with self._lock:
            self._open[message.message_id] = ctx

    def _close(self, message: Any, exception: BaseException | None) -> None:
        with self._lock:
            ctx = self._open.pop(message.message_id, None)
        if ctx is None:
            return
        if exception is not None:
            ctx.__exit__(type(exception), exception, exception.__traceback__)
        else:
            ctx.__exit__(None, None, None)

    def after_process_message(
        self, broker: Any, message: Any, *, result: Any = None, exception: Any = None
    ) -> None:
        self._close(message, exception)

    def after_skip_message(self, broker: Any, message: Any) -> None:
        self._close(message, None)

    def uninstall(self, broker: Any) -> None:
        if self in broker.middleware:
            broker.middleware.remove(self)


def instrument_dramatiq(
    broker: Any, recorder: Any = None, *, task_filter: Any = None
) -> RedlineMiddleware:
    """Add the middleware to ``broker`` once; returns it (it has ``uninstall(broker)``)."""
    for existing in broker.middleware:
        if isinstance(existing, RedlineMiddleware):
            return existing
    middleware = RedlineMiddleware(recorder, task_filter=task_filter)
    broker.add_middleware(middleware)
    return middleware
