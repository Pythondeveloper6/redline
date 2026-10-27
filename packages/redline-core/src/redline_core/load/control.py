"""Background runs and stopping them from another process (the API's run launcher/stopper).

Files live in ``<store directory>/runs/`` (``.redline/runs/`` for the default store):

``<id>.scenario.json``  the scenario the background run was started with
``<id>.pid``            pid of the ``redline load`` process (removed when it ends)
``<id>.stop``           created by :func:`stop_run`; the running process polls for it
                        (every 0.1 s) and stops gracefully with status ``stopped``
``<id>.log``            stdout/stderr of the background process
``<id>.json``           JSON results, when the store cannot hold runs

A file is used instead of a signal so stopping works the same on every OS and from any
process that can see the store directory (another worker of the web app, a shell).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .scenario import load_scenario, validate_base_url

__all__ = ["StopFile", "launch_background", "runs_dir", "stop_run"]

DEFAULT_STORE = ".redline/redline.db"


def runs_dir(store_path: str | os.PathLike[str] | None = None) -> Path:
    """``<directory of the store>/runs`` (created on demand by the writers)."""
    path = os.fspath(store_path or DEFAULT_STORE)
    if path == ":memory:" or path.startswith("file:"):
        path = DEFAULT_STORE
    return Path(os.path.dirname(os.path.abspath(path))) / "runs"


class StopFile:
    """A stop event backed by ``<runs dir>/<run id>.stop`` (``is_set()`` / ``set()``)."""

    def __init__(self, run_id: str, store_path: str | os.PathLike[str] | None = None) -> None:
        self.path = runs_dir(store_path) / f"{_safe_id(run_id)}.stop"

    def is_set(self) -> bool:
        return self.path.exists()

    def set(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _safe_id(run_id: str) -> str:
    if not run_id or not all(c.isalnum() or c in "-_" for c in run_id) or len(run_id) > 64:
        raise ValueError(f"invalid run id {run_id!r}")
    return run_id


def scenario_from_config(config: Mapping[str, Any], base_url: str) -> dict[str, Any]:
    """The ``POST api/runs`` body (``{name, requests, profile, ...}``) → a scenario dict.

    The target is always ``base_url`` (the app's own URL, given by the adapter); a
    ``base_url`` in the body is ignored.
    """
    data = {k: v for k, v in dict(config).items() if not str(k).startswith("_")}
    data["base_url"] = validate_base_url(base_url, "base_url")
    data.setdefault("name", "UI run")
    return data


def launch_background(
    config: Mapping[str, Any],
    base_url: str,
    store_path: str | os.PathLike[str] | None = None,
    *,
    run_id: str | None = None,
    workers: int | None = None,
    python: str | None = None,
) -> str:
    """Start ``redline load`` in a detached subprocess and return the run id at once.

    ``config`` is validated first (raises :class:`~redline_core.load.ScenarioError`, which an
    API maps to 400). The child writes the run to ``store_path`` under that id; its row
    appears once the child has started (well under a second). Stop it with
    :func:`stop_run`.
    """
    data = scenario_from_config(config, base_url)
    load_scenario(data)  # validate before spawning anything
    run_id = run_id or uuid.uuid4().hex
    store = os.fspath(store_path or DEFAULT_STORE)
    rdir = runs_dir(store)
    rdir.mkdir(parents=True, exist_ok=True)
    scenario_path = rdir / f"{_safe_id(run_id)}.scenario.json"
    scenario_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    cmd = [
        python or sys.executable,
        "-m",
        "redline_core.cli",
        "load",
        str(scenario_path),
        "--store",
        store,
        "--run-id",
        run_id,
        "--quiet",
    ]
    if workers:
        cmd += ["--workers", str(workers)]
    log = open(rdir / f"{run_id}.log", "ab")
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
    (rdir / f"{run_id}.pid").write_text(str(proc.pid), encoding="ascii")
    return run_id


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists, owned by someone else
        return True
    except OSError:  # pragma: no cover
        return False
    return True


def stop_run(run_id: str, store_path: str | os.PathLike[str] | None = None) -> bool:
    """Ask the run ``run_id`` to stop gracefully (status ``stopped``, results kept).

    Returns ``True`` when a running process was found (its pid file exists and the process
    is alive); the stop file is written either way, so a run that is just starting stops as
    soon as it polls.
    """
    StopFile(run_id, store_path).set()
    pid_file = runs_dir(store_path) / f"{_safe_id(run_id)}.pid"
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def write_pid(run_id: str, store_path: str | os.PathLike[str] | None) -> Path:
    path = runs_dir(store_path) / f"{_safe_id(run_id)}.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="ascii")
    return path


def cleanup(run_id: str, store_path: str | os.PathLike[str] | None) -> None:
    """Remove the pid and stop files of a finished run (results and logs stay)."""
    rdir = runs_dir(store_path)
    for suffix in (".pid", ".stop"):
        try:
            (rdir / f"{_safe_id(run_id)}{suffix}").unlink()
        except FileNotFoundError:
            pass
