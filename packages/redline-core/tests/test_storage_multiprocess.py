"""Two independent OS processes write one SQLite store, not threads and not fork()."""

from __future__ import annotations

import subprocess
import sys

from redline_core.storage import SQLiteStore

WRITER = """
import sys, time
from redline_core.model import Trace
from redline_core.storage import SQLiteStore

path, role, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
store = SQLiteStore(path, background=False)
try:
    for i in range(count):
        store.save(Trace(f"{role}-{i}-{time.time_ns()}", "other", "TASK", "job"))
    if not store.flush(15):
        raise SystemExit(4)
finally:
    store.close()
"""


def test_two_processes_share_one_file_without_lock_errors(tmp_path):
    path = str(tmp_path / "shared.db")
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, path, role, "100"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for role in ("web", "worker")
    ]
    results = [process.communicate(timeout=60) for process in processes]
    for process, (_, stderr) in zip(processes, results, strict=True):
        assert process.returncode == 0, stderr
        assert "database is locked" not in stderr.lower()
    store = SQLiteStore(path, background=False)
    try:
        assert store.count() == 200
    finally:
        store.close()
