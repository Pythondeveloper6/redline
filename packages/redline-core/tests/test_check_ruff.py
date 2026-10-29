from __future__ import annotations

import io
import json
from argparse import Namespace

from redline_core.check.report import Report
from redline_core.cli import _attach_ruff


def _report():
    return Report(id="r", suite={"name": "ci"}, baseline={}, run={}, summary={}, requests=[])


def test_ruff_json_is_in_markdown_junit_and_exit_code(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("import os\n")
    path = tmp_path / "ruff.json"
    path.write_text(
        json.dumps(
            [
                {
                    "code": "F401",
                    "message": "`os` imported but unused",
                    "filename": str(source),
                    "location": {"row": 1, "column": 8},
                }
            ]
        )
    )
    report = _report()
    err = io.StringIO()
    _attach_ruff(Namespace(ruff=str(path)), report, err)
    assert report.extra["ruff_total"] == 1
    assert report.extra["ruff"][0]["code"] == "F401"
    assert report.exit_code() == 1
    markdown = report.to_markdown()
    assert "### Ruff (1 finding)" in markdown and "F401" in markdown
    junit = report.to_junit()
    assert 'name="ruff"' in junit and "F401" in junit
    assert json.loads(report.to_json())["extra"]["ruff"][0]["row"] == 1


def test_missing_ruff_result_is_reported_and_fails(tmp_path):
    report = _report()
    err = io.StringIO()
    _attach_ruff(Namespace(ruff=str(tmp_path / "absent.json")), report, err)
    assert report.extra["ruff_error"] == "FileNotFoundError"
    assert report.exit_code() == 1
    assert "**Ruff:**" in report.to_markdown()
    assert "redline check: Ruff result unavailable" in err.getvalue()
