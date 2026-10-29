"""Optional Celery integration; broker calls and task arguments are not captured."""

from __future__ import annotations

from contextvars import ContextVar

from ..jobs import job, propagation_headers, tag

_active = ContextVar("redline_celery_job", default=())


class CeleryIntegration:
    def __init__(self, app, recorder, *, task_filter=None):
        from celery import signals

        self.app = app
        self.recorder = recorder
        self.task_filter = task_filter
        self.listeners = []
        for signal, handler in (
            (signals.task_prerun, self.pre),
            (signals.task_postrun, self.post),
            (signals.task_failure, self.failure),
            (signals.task_retry, self.retry),
            (signals.before_task_publish, self.publish),
        ):
            signal.connect(handler, weak=False)
            self.listeners.append((signal, handler))

    def pre(self, task_id=None, task=None, **kwargs):
        if (
            task is None
            or task.app is not self.app
            or (self.task_filter and not self.task_filter(task.name))
        ):
            return
        request = task.request
        info = request.delivery_info or {}
        headers = request.headers or {}
        ctx = job(
            task.name,
            recorder=self.recorder,
            task_id=task_id,
            queue=info.get("routing_key"),
            attempt=request.retries,
            parent=headers.get("redline", {}),
        )
        ctx.__enter__()
        _active.set((*_active.get(), (task_id, ctx, None)))

    def failure(self, task_id=None, exception=None, **kwargs):
        stack = _active.get()
        if stack and stack[-1][0] == task_id:
            _active.set((*stack[:-1], (task_id, stack[-1][1], exception)))

    def retry(self, request=None, **kwargs):
        stack = _active.get()
        if stack and request and stack[-1][0] == request.id:
            tag("job.outcome", "retry")

    def post(self, task_id=None, state=None, **kwargs):
        stack = _active.get()
        if not stack or stack[-1][0] != task_id:
            return
        _, ctx, exc = stack[-1]
        _active.set(stack[:-1])
        if state == "RETRY":
            tag("job.outcome", "retry")
        elif state in {"REVOKED", "IGNORED"}:
            tag("job.outcome", state.lower())
        if state == "FAILURE" and exc is None:
            exc = RuntimeError("Celery task failed; its exception signal was unavailable")
        if exc is not None:
            ctx.__exit__(type(exc), exc, exc.__traceback__)
        else:
            ctx.__exit__(None, None, None)

    def publish(self, sender=None, headers=None, **kwargs):
        if headers is not None and sender in self.app.tasks:
            value = propagation_headers()
            if value:
                headers["redline"] = value

    def uninstall(self):
        for signal, handler in self.listeners:
            signal.disconnect(handler)
        self.listeners.clear()


def instrument_celery(app, recorder, *, task_filter=None):
    """Install once per app; returns an object with uninstall()."""
    existing = getattr(app, "_redline_integration", None)
    if existing is not None and existing.listeners:
        return existing
    integration = CeleryIntegration(app, recorder, task_filter=task_filter)
    app._redline_integration = integration
    return integration
