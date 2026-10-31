"""Optional RQ integration; job arguments and results are not captured.

``instrument_rq(recorder)`` patches two RQ entry points process-wide, so it works with
forking and simple workers alike:

* ``Queue.create_job`` stores the enqueuing request's trace context in ``job.meta["redline"]``;
* ``Job.perform`` runs the job inside :func:`redline_core.jobs.job`, linked to that context.

Call it in the web process (to propagate) and in the worker process (to record).
"""

from __future__ import annotations

from typing import Any

from rq.job import Job
from rq.queue import Queue

from ..jobs import job, propagation_headers

__all__ = ["RQIntegration", "instrument_rq"]

_active: RQIntegration | None = None


class RQIntegration:
    def __init__(self, recorder: Any = None, *, task_filter: Any = None) -> None:
        self.recorder = recorder
        self.task_filter = task_filter
        self._create_job = Queue.create_job
        self._perform = Job.perform
        integration = self

        def create_job(queue: Queue, *args: Any, **kwargs: Any) -> Job:
            created = integration._create_job(queue, *args, **kwargs)
            try:
                headers = propagation_headers()
                if headers and isinstance(created.meta, dict):
                    created.meta["redline"] = headers
            except Exception:  # propagation must never break enqueueing
                pass
            return created

        def perform(self_job: Job) -> Any:
            name = self_job.func_name or "rq.job"
            if integration.task_filter and not integration.task_filter(name):
                return integration._perform(self_job)
            meta = self_job.meta if isinstance(self_job.meta, dict) else {}
            with job(
                name,
                recorder=integration.recorder,
                task_id=self_job.id,
                queue=self_job.origin,
                parent=meta.get("redline"),
            ):
                return integration._perform(self_job)

        Queue.create_job = create_job  # type: ignore[method-assign]
        Job.perform = perform  # type: ignore[method-assign]
        self._installed = True

    def uninstall(self) -> None:
        global _active
        if self._installed:
            Queue.create_job = self._create_job  # type: ignore[method-assign]
            Job.perform = self._perform  # type: ignore[method-assign]
            self._installed = False
        if _active is self:
            _active = None


def instrument_rq(recorder: Any = None, *, task_filter: Any = None) -> RQIntegration:
    """Patch RQ once per process; returns an object with ``uninstall()``."""
    global _active
    if _active is not None and _active._installed:
        return _active
    _active = RQIntegration(recorder, task_filter=task_filter)
    return _active
