from __future__ import annotations

import json
import subprocess

import pytest

from redline_core.check.baseline import (
    Baseline,
    BaselineError,
    default_baseline_path,
    git_info,
    redact_capture,
)
from redline_core.check.diff import capture


def make(**kw):
    data = {
        "id": "b1",
        "suite": {"name": "s"},
        "requests": [
            {
                "name": "r",
                "response": capture(
                    200,
                    {"Content-Type": "application/json", "Authorization": "x"},
                    json.dumps({"password": "hunter2", "ok": 1}).encode(),
                ),
            }
        ],
        "created_at": 1767000000.0,
        "label": "before",
        "git": {"sha": "abc", "branch": "main", "dirty": False},
    }
    data.update(kw)
    return Baseline(**data)


def test_round_trip(tmp_path):
    b = make()
    path = b.write(tmp_path / "sub" / "x.baseline.json")
    assert b.file == str(path)
    data = json.loads(path.read_text())
    assert data["format"] == "redline-check-baseline" and data["version"] == 1
    loaded = Baseline.from_file(path)
    assert loaded.to_dict() == b.redacted().to_dict()
    assert loaded.file == str(path) and loaded.ref()["file"] == str(path)
    assert loaded.request("r") is not None and loaded.request("nope") is None
    assert not list(tmp_path.glob("sub/.x*"))  # no temp files left


def test_bodies_redacted_on_write(tmp_path):
    raw = {
        "name": "r",
        "response": {
            "status": 200,
            "content_type": "application/json",
            "headers": {"x-api-key": "k"},
            "body_kind": "json",
            "body": {"token": "tok-zz-91", "nested": {"secret": "s"}, "keep": "v"},
            "size": 10,
        },
    }
    text_req = {
        "name": "t",
        "response": {"status": 200, "body_kind": "text", "body": "password=hunter2 ok", "size": 1},
    }
    b = make(requests=[raw, text_req])
    text = b.write(tmp_path / "b.json").read_text()
    assert "tok-zz-91" not in text and "hunter2" not in text and '"k"' not in text
    assert '"keep": "v"' in text
    assert redact_capture(None) is None


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"format": "other"}, "not a Redline check baseline"),
        ({"version": 2}, "unsupported baseline version 2"),
        ({"version": None}, "unsupported baseline version"),
        ({"requests": "x"}, "requests"),
        ({"requests": [{"no": "name"}]}, "requests"),
        ({"suite": None}, "suite"),
        ({"created_at": "today"}, "created_at"),
    ],
)
def test_rejects(patch, match):
    data = {**make().to_dict(), **patch}
    with pytest.raises(BaselineError, match=match):
        Baseline.from_dict(data)


def test_file_errors(tmp_path):
    with pytest.raises(BaselineError, match="file not found"):
        Baseline.from_file(tmp_path / "missing.json")
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    with pytest.raises(BaselineError, match="invalid JSON"):
        Baseline.from_file(p)
    p.write_text("[]")
    with pytest.raises(BaselineError, match=f"{p}: a baseline must be a JSON object"):
        Baseline.from_file(p)


def test_default_path():
    assert str(default_baseline_path("checks/orders.toml")) == "checks/orders.baseline.json"
    assert str(default_baseline_path(None)) == "check.baseline.json"


def test_summary():
    b = make(
        requests=[
            {"name": "a", "calls": 4, "failed": 2, "broken": True},
            {"name": "b", "calls": 4, "server": {"q": 1}},
        ]
    )
    assert b.summary() == {"requests": 2, "broken": 1, "calls": 8, "failed": 2, "server_side": True}


def test_git_info(tmp_path):
    assert git_info(tmp_path) == {"sha": None, "branch": None, "dirty": None}
    try:
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    env = ["-c", "user.email=a@b", "-c", "user.name=t"]
    (tmp_path / "f").write_text("1")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), *env, "commit", "-qm", "x"], check=True)
    info = git_info(tmp_path)
    assert len(info["sha"]) == 40 and info["branch"] == "main" and info["dirty"] is False
    (tmp_path / "f").write_text("2")
    assert git_info(tmp_path)["dirty"] is True
