from __future__ import annotations

import io
import json
from argparse import Namespace

from redline_core.operations import imports_command


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _package(tmp_path, task_source):
    _write(
        tmp_path / "worker_pkg" / "__init__.py",
        "from types import SimpleNamespace\n"
        "class Task:\n"
        "    __module__ = 'worker_pkg.tasks'\n"
        "app = SimpleNamespace(tasks={'run': Task()})\n",
    )
    _write(tmp_path / "worker_pkg" / "tasks.py", task_source)
    _write(tmp_path / "worker_pkg" / "helpers.py", "VALUE = 1\n")


def test_check_imports_runs_lazy_imports(tmp_path, monkeypatch):
    _package(
        tmp_path,
        "def run():\n"
        "    from .helpers import VALUE\n"
        "    from worker_pkg.helpers import VALUE as OTHER\n"
        "    return VALUE + OTHER\n",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    out, err = io.StringIO(), io.StringIO()
    code = imports_command(Namespace(target="worker_pkg:app", timeout=15), out, err)
    payload = json.loads(out.getvalue())
    assert code == 0, (payload, err.getvalue())
    assert payload["imported"] is True and payload["registered_tasks"] == 1
    assert payload["lazy_modules"] == ["worker_pkg.helpers"]
    assert payload["lazy_errors"] == []


def test_check_imports_reports_a_missing_lazy_import(tmp_path, monkeypatch):
    _package(tmp_path, "def run():\n    import worker_pkg.missing\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    out, err = io.StringIO(), io.StringIO()
    code = imports_command(Namespace(target="worker_pkg:app", timeout=15), out, err)
    payload = json.loads(out.getvalue())
    assert code == 1
    assert payload["imported"] is True
    assert payload["lazy_errors"] == [
        {"module": "worker_pkg.missing", "error_type": "ModuleNotFoundError"}
    ]
