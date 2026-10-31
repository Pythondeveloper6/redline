from __future__ import annotations

import io

import pytest

from redline_core import cli
from redline_core.config import find_config_file, load_config_file, resolve_settings
from redline_core.project_init import detect_framework, render_config


@pytest.fixture(autouse=True)
def _no_config_env(monkeypatch):
    monkeypatch.delenv("REDLINE_CONFIG", raising=False)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.COMMANDS["init"][2](_parse(argv), out, err)
    return code, out.getvalue(), err.getvalue()


def _parse(argv):
    import argparse

    p = argparse.ArgumentParser()
    cli.COMMANDS["init"][1](p)
    return p.parse_args(list(argv))


def test_detects_from_manage_py_then_dependencies(tmp_path):
    (tmp_path / "manage.py").write_text("")
    assert detect_framework(tmp_path, installed=["flask"]) == "django"
    (tmp_path / "manage.py").unlink()
    (tmp_path / "requirements.txt").write_text("litestar==2.0\nuvicorn\n")
    assert detect_framework(tmp_path, installed=["flask"]) == "litestar"
    (tmp_path / "requirements.txt").unlink()
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi>=0.100"]\n')
    assert detect_framework(tmp_path, installed=[]) == "fastapi"
    (tmp_path / "pyproject.toml").unlink()
    assert detect_framework(tmp_path, installed=["flask"]) == "flask"
    assert detect_framework(tmp_path, installed=[]) is None


def test_does_not_confuse_similar_names(tmp_path):
    (tmp_path / "requirements.txt").write_text("django-redline\nflask-cors\n")
    assert detect_framework(tmp_path, installed=[]) is None


def test_writes_config_and_gitignore(tmp_path):
    (tmp_path / "manage.py").write_text("")
    code, out, _ = run("--dir", str(tmp_path))
    assert code == 0 and "framework: django" in out
    path = tmp_path / "redline.toml"
    assert path.is_file()
    # the file is a valid Redline config that resolves to the starter values
    assert find_config_file(tmp_path) == path
    table, _ = load_config_file(path)
    assert table["preset"] == "dev" and table["storage_path"] == ".redline/redline.db"
    resolve_settings(None, project_root=str(tmp_path), environ={})
    assert (tmp_path / ".gitignore").read_text().splitlines()[-1] == "/.redline/"


def test_never_overwrites_without_force(tmp_path):
    (tmp_path / "redline.toml").write_text("# mine\n")
    code, out, _ = run("--dir", str(tmp_path), "--framework", "flask")
    assert code == 0 and "left unchanged" in out
    assert (tmp_path / "redline.toml").read_text() == "# mine\n"
    run("--dir", str(tmp_path), "--framework", "flask", "--force")
    assert "preset" in (tmp_path / "redline.toml").read_text()


def test_gitignore_is_appended_once(tmp_path):
    (tmp_path / ".gitignore").write_text("*.pyc")
    run("--dir", str(tmp_path))
    run("--dir", str(tmp_path), "--force")
    text = (tmp_path / ".gitignore").read_text()
    assert text.startswith("*.pyc\n") and text.count("/.redline/") == 1


def test_gitignore_recognises_existing_entry(tmp_path):
    (tmp_path / ".gitignore").write_text(".redline/\n")
    _, out, _ = run("--dir", str(tmp_path))
    assert (tmp_path / ".gitignore").read_text() == ".redline/\n"
    assert "already ignores" in out


def test_dry_run_writes_nothing(tmp_path):
    code, out, _ = run("--dir", str(tmp_path), "--framework", "fastapi", "--dry-run")
    assert code == 0 and "would write" in out and "preset" in out
    assert list(tmp_path.iterdir()) == []


def test_existing_pyproject_table_is_respected(tmp_path):
    original = '[tool.redline]\npreset = "staging"\n'
    (tmp_path / "pyproject.toml").write_text(original)
    _, out, _ = run("--dir", str(tmp_path))
    assert not (tmp_path / "redline.toml").exists()
    assert (tmp_path / "pyproject.toml").read_text() == original
    assert "already has [tool.redline]" in out


def test_pyproject_flag_appends_a_valid_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    code, _, _ = run("--dir", str(tmp_path), "--pyproject", "--framework", "flask")
    assert code == 0 and not (tmp_path / "redline.toml").exists()
    path = find_config_file(tmp_path)
    assert path is not None and path.name == "pyproject.toml"
    table, _ = load_config_file(path)
    assert table["preset"] == "dev"
    assert 'name = "x"' in path.read_text()


def test_pyproject_flag_needs_a_pyproject(tmp_path):
    code, _, err = run("--dir", str(tmp_path), "--pyproject")
    assert code == 2 and "no pyproject.toml" in err


def test_render_mentions_the_adapter():
    assert "pip install litestar-redline" in render_config("litestar")
    assert "pip install" not in render_config(None)


def test_main_dispatch(tmp_path, capsys):
    assert cli.main(["init", "--dir", str(tmp_path), "--framework", "django", "--dry-run"]) == 0
    assert "framework: django" in capsys.readouterr().out
