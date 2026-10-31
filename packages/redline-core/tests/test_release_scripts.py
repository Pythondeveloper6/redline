"""The release-verification scripts under scripts/ (store upgrade, clean install)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    if not path.is_file():  # an sdist of redline-core alone has no scripts/
        pytest.skip(f"{path} not present")
    spec = importlib.util.spec_from_file_location(f"_script_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_store_upgrade_round_trip(tmp_path, capsys):
    upgrade = _load("check_store_upgrade")
    db = tmp_path / "traces.db"
    assert upgrade.main(["write", str(db)]) == 0
    manifest = json.loads((tmp_path / "traces.db.manifest.json").read_text())
    assert set(manifest["traces"]) == {"upgrade-n1", "upgrade-slow", "upgrade-tagged"}
    assert "n_plus_one" in manifest["traces"]["upgrade-n1"]["issue_kinds"]
    assert upgrade.main(["read", str(db)]) == 0
    assert ": OK" in capsys.readouterr().out


def test_store_upgrade_detects_a_lost_trace(tmp_path, capsys):
    upgrade = _load("check_store_upgrade")
    db = tmp_path / "traces.db"
    assert upgrade.main(["write", str(db)]) == 0
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM traces WHERE id = 'upgrade-slow'")
    conn.commit()
    conn.close()
    assert upgrade.main(["read", str(db)]) == 1
    assert "upgrade-slow: missing after upgrade" in capsys.readouterr().err


def test_store_upgrade_usage_errors(capsys):
    upgrade = _load("check_store_upgrade")
    assert upgrade.main([]) == 2
    assert upgrade.main(["delete", "x.db"]) == 2


def test_clean_install_resolves_local_wheel_requirements(tmp_path):
    clean = _load("check_clean_install")
    for name in ("redline_core-0.5.0a1-py3-none-any.whl", "flask_redline-0.5.0a1-py3-none-any.whl"):
        (tmp_path / name).write_bytes(b"")
    args = clean.argparse.Namespace(wheels=str(tmp_path), version=None)
    reqs = clean._requirement("flask-redline", args)
    assert sorted(Path(r).name for r in reqs) == [
        "flask_redline-0.5.0a1-py3-none-any.whl",
        "redline_core-0.5.0a1-py3-none-any.whl",
    ]
    pypi = clean.argparse.Namespace(wheels=None, version="0.5.0a1")
    assert clean._requirement("django-redline", pypi) == ["django-redline==0.5.0a1"]
    with pytest.raises(SystemExit):
        clean._requirement("litestar-redline", args)
