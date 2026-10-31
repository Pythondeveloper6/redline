import contextlib
import io

import pytest

from redline_core import cli
from redline_core.analyze import analyze_project
from redline_core.check_sql import scan


def make_python_files(root, count: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"file_{index:03}.py").write_text("value = 1\n")


def run_scanner(scanner, root, **limits):
    paths = root if scanner is analyze_project else [str(root)]
    return scanner(paths, **limits)


@pytest.mark.parametrize(
    ("scanner", "count_key"),
    [(analyze_project, "files_analyzed"), (scan, "files")],
)
def test_default_file_cap_and_explicit_higher_cap(tmp_path, scanner, count_key):
    make_python_files(tmp_path, 201)

    default_report = run_scanner(scanner, tmp_path)
    expanded_report = run_scanner(scanner, tmp_path, max_files=201)

    assert default_report[count_key] == 200
    assert default_report["limited"] if "limited" in default_report else default_report["skipped"]
    assert expanded_report[count_key] == 201


@pytest.mark.parametrize("scanner", [analyze_project, scan])
def test_byte_cap_returns_partial_coverage(tmp_path, scanner):
    (tmp_path / "a.py").write_text("a = 1\n")
    (tmp_path / "b.py").write_text("b = 2\n")

    report = run_scanner(scanner, tmp_path, max_project_bytes=7)

    count_key = "files_analyzed" if scanner is analyze_project else "files"
    assert report[count_key] == 1
    if "limited" in report:
        assert report["limited"] is True
    else:
        assert report["skipped"]


@pytest.mark.parametrize("scanner", [analyze_project, scan])
@pytest.mark.parametrize(
    "limits",
    [
        {"max_files": 0},
        {"max_files": 10_001},
        {"max_project_bytes": 0},
        {"max_project_bytes": 64_000_001},
        {"max_files": True},
        {"max_project_bytes": False},
    ],
)
def test_api_rejects_invalid_scan_limits(tmp_path, scanner, limits):
    with pytest.raises(ValueError):
        run_scanner(scanner, tmp_path, **limits)


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("analyze", ["--max-files", "0"]),
        ("analyze", ["--max-project-bytes", "64000001"]),
        ("check-sql", ["--max-files", "10001"]),
        ("check-sql", ["--max-project-bytes", "0"]),
    ],
)
def test_cli_rejects_invalid_scan_limits(tmp_path, command, args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main([command, str(tmp_path), *args])

    assert code == 2


@pytest.mark.parametrize("scanner", [analyze_project, scan])
def test_higher_limits_still_exclude_symlink_files(tmp_path, scanner):
    target = tmp_path / "target.py"
    target.write_text("value = 1\n")
    (tmp_path / "linked.py").symlink_to(target)

    report = run_scanner(scanner, tmp_path, max_files=1_000, max_project_bytes=1_000_000)

    count_key = "files_analyzed" if scanner is analyze_project else "files"
    assert report[count_key] == 1
