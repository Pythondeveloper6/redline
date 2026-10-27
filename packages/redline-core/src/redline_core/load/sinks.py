"""Where a run's results go.

The engine talks to a :class:`RunSink`: ``start(meta)`` once, ``step(run_id, step)`` after
every completed step, ``finish(run_id, status, summary)`` once at the end (also after a
failure or a stop). Implementations:

* :class:`StoreSink` – the Redline store (``save_run`` / ``save_run_step`` /
  ``finish_run``, ``CONTRACT.md`` section 11), so the run shows up in the UI.
* :class:`JsonSink` – one JSON document, rewritten after every step.
* :class:`MemorySink` – keeps everything in memory (tests, embedding).

Sink errors are logged and never stop a run.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .analysis import StepResult

__all__ = [
    "JsonSink",
    "MemorySink",
    "MultiSink",
    "RunMeta",
    "RunSink",
    "StoreSink",
    "store_supports_runs",
]

log = logging.getLogger("redline.load")

STORE_RUN_METHODS = ("save_run", "save_run_step", "finish_run")


@dataclass
class RunMeta:
    id: str
    name: str
    created_at: float
    config: dict[str, Any] = field(default_factory=dict)
    status: str = "running"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "finished_at": None,
            "status": self.status,
            "config": self.config,
            "summary": None,
        }


@runtime_checkable
class RunSink(Protocol):
    def start(self, meta: RunMeta) -> None: ...

    def step(self, run_id: str, step: StepResult) -> None: ...

    def finish(self, run_id: str, status: str, summary: dict[str, Any]) -> None: ...


class MemorySink:
    """Keeps the run in memory: ``meta``, ``steps``, ``status``, ``summary``."""

    def __init__(self) -> None:
        self.meta: RunMeta | None = None
        self.steps: list[StepResult] = []
        self.status: str | None = None
        self.summary: dict[str, Any] | None = None

    def start(self, meta: RunMeta) -> None:
        self.meta = meta

    def step(self, run_id: str, step: StepResult) -> None:
        self.steps.append(step)

    def finish(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        self.status = status
        self.summary = summary


class JsonSink:
    """Writes ``{run, steps, status, summary}`` to ``path`` (atomically) after every event.

    Steps carry their histograms base64-encoded (``hist``, ``service_hist``) so the file can
    be re-analysed or imported later.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        self._doc: dict[str, Any] = {}

    def start(self, meta: RunMeta) -> None:
        self._doc = {"run": meta.to_dict(), "steps": [], "status": "running", "summary": None}
        self._write()

    def step(self, run_id: str, step: StepResult) -> None:
        self._doc.setdefault("steps", []).append(step.to_dict(include_hist=True))
        self._write()

    def finish(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        import time

        self._doc["status"] = status
        self._doc["summary"] = summary
        run = self._doc.setdefault("run", {"id": run_id})
        run["status"] = status
        run["finished_at"] = time.time()
        run["summary"] = summary
        self._write()

    def _write(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".redline-run-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._doc, fh, indent=2, default=str)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def store_supports_runs(store: Any) -> bool:
    """True when ``store`` has the run methods of ``CONTRACT.md`` section 11."""
    return all(callable(getattr(store, name, None)) for name in STORE_RUN_METHODS)


class StoreSink:
    """Writes the run into a Redline store.

    It writes the store's own row types (:mod:`redline_core.runs`): a
    :class:`~redline_core.runs.Run`
    on start, one :class:`~redline_core.runs.RunStep` per finished step, then
    ``store.finish_run(run_id, status, summary)`` with ``status`` one of ``done``, ``stopped``,
    ``failed`` and the summary of :func:`~redline_core.load.analysis.analyze`.
    """

    def __init__(self, store: Any, *, close: bool = False) -> None:
        if not store_supports_runs(store):
            raise TypeError(
                f"{type(store).__name__} does not implement save_run/save_run_step/finish_run"
            )
        self.store = store
        self._close = close

    def start(self, meta: RunMeta) -> None:
        from ..runs import Run

        row = meta.to_dict()
        self.store.save_run(
            Run(
                id=row["id"],
                name=row.get("name") or "",
                status="running",
                config=row.get("config") or {},
                created_at=row["created_at"],
            )
        )

    def step(self, run_id: str, step: StepResult) -> None:
        self.store.save_run_step(run_id, self.step_row(step))

    def finish(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        try:
            self.store.finish_run(run_id, status, summary)
        finally:
            if self._close:
                self.store.close()

    @staticmethod
    def step_row(step: StepResult) -> Any:
        from ..runs import RunStep

        return RunStep(
            step=step.step,
            target_rps=step.target_rps,
            started_at=step.started_at,
            ended_at=step.ended_at,
            achieved_rps=round(step.achieved_rps, 3),
            sent=step.sent,
            ok=step.ok,
            errors=step.errors,
            dropped=step.dropped,
            hist=step.hist.copy(),
            error_breakdown=dict(step.error_breakdown),
        )


class MultiSink:
    """Fans out to several sinks; one failing sink is logged and does not affect others."""

    def __init__(self, sinks: Iterable[RunSink]) -> None:
        self.sinks = [s for s in sinks if s is not None]

    def _each(self, method: str, *args: Any) -> None:
        for sink in self.sinks:
            try:
                getattr(sink, method)(*args)
            except Exception:  # never let a sink kill the run
                log.exception("redline load: %s.%s failed", type(sink).__name__, method)

    def start(self, meta: RunMeta) -> None:
        self._each("start", meta)

    def step(self, run_id: str, step: StepResult) -> None:
        self._each("step", run_id, step)

    def finish(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        self._each("finish", run_id, status, summary)
