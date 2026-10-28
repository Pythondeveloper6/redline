"""Refactor-check runs started from the UI, in a background process (``CONTRACT.md`` §19.5).

Same process control as UI load runs (§13, :mod:`redline_core.load.control`), in the same
``<store directory>/runs/`` directory:

``<id>.check.json``  what to run: the suite with its raw header values, kind, baseline id,
                     target (mode 0600, removed when the run ends)
``<id>.pid``         pid of the check process (removed when it ends)
``<id>.stop``        written by :func:`stop_check`; the runner polls it and stops (``stopped``)
``<id>.log``         stdout/stderr of the background process

The child is ``python -m redline_core.check.control run <config> --store PATH --run-id ID``:
it runs the suite with :class:`~redline_core.check.sink.StoreCheckSink` and
:class:`~redline_core.check.sink.StoreServerMetrics` against the same store file, so the
API reads progress and results from there. A run whose process dies is marked ``failed`` by
the store (``_reap_dead_check_runs``).

Caps for runs started here: at most :data:`UI_MAX_REQUESTS` requests, :data:`UI_MAX_CALLS`
calls (requests × (warmup + repeat)) and :data:`UI_MAX_DURATION_S` seconds (then the run
is stopped). The target is always the app's own URL (the ``base_url`` setting or
:func:`redline_core.api.own_base_url`), never the request's ``Host`` header.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..storage.private import ensure_private_dir, open_private

__all__ = [
    "UI_MAX_CALLS",
    "UI_MAX_DURATION_S",
    "UI_MAX_REQUESTS",
    "check_hooks",
    "check_ui_limits",
    "launch_check",
    "main",
    "run_check",
    "stop_check",
]

UI_MAX_REQUESTS = 200
UI_MAX_CALLS = 5_000
UI_MAX_DURATION_S = 900.0
#: Suite defaults when the suite does not say (``CONTRACT.md`` §19.1).
DEFAULT_REPEAT = 20
DEFAULT_WARMUP = 3
READ_ONLY_METHODS = ("GET", "HEAD", "OPTIONS")
TIMEOUT_MESSAGE = f"stopped: runs started from the UI are limited to {UI_MAX_DURATION_S / 60:g} min"


def _int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("repeat and warmup must be integers")
    return value


def planned_calls(suite: Mapping[str, Any]) -> int:
    """Calls a suite makes: per request ``warmup + repeat`` (request-level values win)."""
    repeat = _int(suite.get("repeat"), DEFAULT_REPEAT)
    warmup = _int(suite.get("warmup"), DEFAULT_WARMUP)
    total = 0
    for r in suite.get("requests") or []:
        r = r if isinstance(r, Mapping) else {}
        total += _int(r.get("repeat"), repeat) + _int(r.get("warmup"), warmup)
    return total


def check_ui_limits(suite: Mapping[str, Any]) -> None:
    """Raise :class:`ValueError` when a suite exceeds the caps of UI-started runs or holds a
    request that is not read-only or not a path on the app."""
    requests = suite.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError("the suite has no requests")
    if len(requests) > UI_MAX_REQUESTS:
        raise ValueError(f"runs started here are limited to {UI_MAX_REQUESTS} requests")
    for r in requests:
        if not isinstance(r, Mapping):
            raise ValueError("each request must be an object")
        method = str(r.get("method") or "GET").upper()
        if method not in READ_ONLY_METHODS:
            raise ValueError(f"only {', '.join(READ_ONLY_METHODS)} requests can be checked")
        path = r.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("request paths must start with a single '/'")
    calls = planned_calls(suite)
    if calls > UI_MAX_CALLS:
        raise ValueError(
            f"runs started here are limited to {UI_MAX_CALLS} calls "
            f"(requests × (warmup + repeat)); this suite makes {calls}"
        )


def _runs_dir(store_path: str) -> Path:
    from ..load.control import runs_dir

    return runs_dir(store_path)


def _safe_id(run_id: str) -> str:
    if not run_id or len(run_id) > 64 or not all(c.isalnum() or c in "-_" for c in run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    return run_id


def _write_private(path: Path, text: str) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def launch_check(
    suite: Mapping[str, Any],
    base_url: str,
    store_path: str | os.PathLike[str],
    *,
    kind: str,
    suite_id: str | None = None,
    baseline_id: str | None = None,
    label: str | None = None,
    run_id: str | None = None,
    python: str | None = None,
) -> str:
    """Start the check process (detached) and return the run id at once.

    ``suite`` is the stored suite with its raw header values; only the child sees them,
    through ``<id>.check.json`` (0600). Raises :class:`ValueError` for a suite over the caps.
    """
    check_ui_limits(suite)
    if kind not in ("baseline", "compare"):
        raise ValueError("kind must be 'baseline' or 'compare'")
    run_id = _safe_id(run_id or uuid.uuid4().hex)
    store = os.fspath(store_path)
    rdir = ensure_private_dir(_runs_dir(store), gitignore=False)
    config_path = rdir / f"{run_id}.check.json"
    _write_private(
        config_path,
        json.dumps(
            {
                "suite": dict(suite),
                "base_url": base_url,
                "kind": kind,
                "suite_id": suite_id,
                "baseline_id": baseline_id,
                "label": label,
                "max_duration_s": UI_MAX_DURATION_S,
            },
            indent=2,
        ),
    )
    cmd = [
        python or sys.executable,
        "-m",
        "redline_core.check.control",
        "run",
        str(config_path),
        "--store",
        store,
        "--run-id",
        run_id,
    ]
    log = os.fdopen(open_private(rdir / f"{run_id}.log", "ab"), "ab")
    try:
        kwargs: dict[str, Any] = {}
        if os.name == "nt":  # pragma: no cover
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kwargs
        )
    finally:
        log.close()
    with os.fdopen(open_private(rdir / f"{run_id}.pid", "wb"), "wb") as fh:
        fh.write(str(proc.pid).encode("ascii"))
    return run_id


def stop_check(run_id: str, store_path: str | os.PathLike[str]) -> bool:
    """Ask a check run to stop (its process polls the stop file); ``True`` when a live
    process was found."""
    from ..load.control import stop_run

    return stop_run(_safe_id(run_id), store_path)


def _cleanup(run_id: str, store_path: str) -> None:
    rdir = _runs_dir(store_path)
    for suffix in (".pid", ".stop", ".check.json"):
        try:
            (rdir / f"{run_id}{suffix}").unlink()
        except FileNotFoundError:
            pass


class _Stop:
    """The runner's stop event: the stop file was written, or the time cap passed."""

    def __init__(self, run_id: str, store_path: str, max_duration_s: float | None) -> None:
        from ..load.control import StopFile

        self._file = StopFile(run_id, store_path)
        self._deadline = time.monotonic() + max_duration_s if max_duration_s else None
        self.timed_out = False
        self._checked = 0.0
        self._set = False

    def is_set(self) -> bool:
        if self._set:
            return True
        now = time.monotonic()
        if self._deadline is not None and now >= self._deadline:
            self.timed_out = self._set = True
            return True
        if now - self._checked >= 0.1:  # one stat() per 0.1 s at most
            self._checked = now
            self._set = self._file.is_set()
        return self._set

    def __call__(self) -> bool:
        return self.is_set()

    def set(self) -> None:
        self._set = True


class _Progress:
    """``on_progress`` callback: stores ``{done, total, ...}`` at most twice a second."""

    def __init__(self, store: Any, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self._last = 0.0

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        data: dict[str, Any]
        if len(args) == 1 and isinstance(args[0], Mapping):
            data = dict(args[0])
        elif len(args) == 1 and callable(getattr(args[0], "to_dict", None)):
            data = dict(args[0].to_dict())
        elif len(args) >= 2 and all(isinstance(a, int) for a in args[:2]):
            data = {"done": args[0], "total": args[1]}
        else:
            data = {}
        data.update(kwargs)
        now = time.monotonic()
        final = data.get("total") is not None and data.get("done") == data.get("total")
        if not final and now - self._last < 0.5:
            return
        self._last = now
        try:
            self.store.set_check_progress(self.run_id, json.loads(json.dumps(data, default=str)))
        except Exception:  # progress is best effort
            pass


def _engine() -> tuple[Callable[..., Any], Any, Any]:
    """``(run_suite, Suite, Baseline)`` from the engine, imported lazily (needs httpx)."""
    from .runner import run_suite  # type: ignore[import-not-found]

    try:
        from .suite import Suite  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        Suite = None
    try:
        from .baseline import Baseline  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        Baseline = None
    return run_suite, Suite, Baseline


def run_check(config: Mapping[str, Any], store: Any, run_id: str) -> str:
    """Run one check described by ``config`` (the ``<id>.check.json`` document) into
    ``store``; returns the final status. Always leaves the row finished."""
    from .sink import StoreCheckSink, StoreServerMetrics

    store_path = os.fspath(store.path)
    stop = _Stop(run_id, store_path, config.get("max_duration_s"))
    status = "failed"
    try:
        run_suite, suite_cls, baseline_cls = _engine()
        raw_suite = dict(config["suite"])
        suite: Any = suite_cls.from_dict(raw_suite) if suite_cls is not None else raw_suite
        baseline: Any = None
        if config.get("kind") == "compare":
            doc = store.check_baseline(str(config.get("baseline_id") or ""))
            if doc is None:
                raise ValueError(f"baseline {config.get('baseline_id')!r} is not available")
            baseline = baseline_cls.from_dict(doc) if baseline_cls is not None else doc
        run_suite(
            suite,
            base_url=config["base_url"],
            kind=config.get("kind", "compare"),
            baseline=baseline,
            run_id=run_id,
            suite_id=config.get("suite_id"),
            label=config.get("label"),
            sink=StoreCheckSink(store),
            server_metrics=StoreServerMetrics(store),
            on_progress=_Progress(store, run_id),
            stop=stop,
        )
        row = store.get_check_run(run_id)
        status = row["status"] if row else "failed"
        if row is not None and status == "running":  # the engine did not finish it
            status = "stopped" if stop.is_set() else "done"
            store.finish_check_run(run_id, status, None, None)
        if stop.timed_out:
            status = "stopped"
            store.finish_check_run(run_id, "stopped", None, TIMEOUT_MESSAGE)
    except BaseException as exc:
        message = f"{type(exc).__name__}: {exc}"[:2000]
        try:
            row = store.get_check_run(run_id)
            if row is None:  # failed before the engine started it: record the failure
                from ..redact import Redactor
                from .sink import redact_suite

                store.create_check_run(
                    {
                        "id": run_id,
                        "kind": config.get("kind") or "compare",
                        "suite_id": config.get("suite_id"),
                        "baseline_id": config.get("baseline_id"),
                        "label": config.get("label"),
                        "git": None,
                        "suite": redact_suite(config.get("suite"), Redactor()),
                    }
                )
            if row is None or row["status"] == "running":
                store.finish_check_run(run_id, "failed", None, message)
        except Exception:  # pragma: no cover - store gone
            pass
        print(f"redline check run {run_id} failed: {message}", file=sys.stderr)
        status = "failed"
        if not isinstance(exc, Exception):
            raise
    finally:
        _cleanup(run_id, store_path)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m redline_core.check.control run CONFIG --store PATH --run-id ID``."""
    parser = argparse.ArgumentParser(prog="redline-check-background")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("config")
    run.add_argument("--store", required=True)
    run.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    run_id = _safe_id(args.run_id)
    from ..load.control import write_pid
    from ..storage import SQLiteStore

    write_pid(run_id, args.store)
    store = SQLiteStore(args.store, background=False, max_rows=None, max_age_s=None)
    try:
        try:
            config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            store.finish_check_run(run_id, "failed", None, f"cannot read the run file: {exc}")
            _cleanup(run_id, args.store)
            return 1
        status = run_check(config, store, run_id)
    finally:
        store.close()
    return 0 if status in ("done", "stopped") else 1


def check_hooks(
    store: Any, settings: Any
) -> tuple[Callable[..., str] | None, Callable[[str], bool] | None]:
    """``(launcher, stopper)`` for the API, or ``(None, None)`` (→ 501) unless ``store`` is
    a :class:`~redline_core.storage.SQLiteStore` backed by a real file (the background
    process writes into that same file). Whether runs may start at all
    (``checks_from_ui``) is decided by the API per request.

    ``launcher(suite, base_url, *, kind, suite_id, baseline_id, label) -> run_id``
    pre-inserts the ``running`` row (redacted suite), so ``GET api/checks/runs/{id}`` never
    answers a transient 404, then spawns the process.
    """
    from ..storage import SQLiteStore

    if not isinstance(store, SQLiteStore):
        return None, None
    path = os.fspath(store.path)
    if not path or path == ":memory:" or path.startswith("file:"):
        return None, None

    def launcher(
        suite: Mapping[str, Any],
        base_url: str,
        *,
        kind: str,
        suite_id: str | None = None,
        baseline_id: str | None = None,
        label: str | None = None,
    ) -> str:
        from ..redact import Redactor
        from .sink import redact_suite

        check_ui_limits(suite)
        run_id = uuid.uuid4().hex
        store.create_check_run(
            {
                "id": run_id,
                "suite_id": suite_id,
                "kind": kind,
                "baseline_id": baseline_id,
                "label": label,
                "git": None,
                "suite": redact_suite(dict(suite), Redactor(settings)),
                "base_url": Redactor(settings).url(base_url),
                "source": "ui",
            }
        )
        try:
            launch_check(
                suite,
                base_url,
                path,
                kind=kind,
                suite_id=suite_id,
                baseline_id=baseline_id,
                label=label,
                run_id=run_id,
            )
        except BaseException as exc:
            store.finish_check_run(run_id, "failed", None, f"could not start: {exc}")
            raise
        return run_id

    def stopper(run_id: str) -> bool:
        try:
            return stop_check(run_id, path)
        except ValueError:
            return False

    return launcher, stopper


if __name__ == "__main__":  # python -m redline_core.check.control
    raise SystemExit(main())
