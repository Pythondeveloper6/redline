from __future__ import annotations

from redline_core.analyze import analyze_project


def _write(root, files):
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)


def _loop_finding(report, file):
    return next(
        finding
        for finding in report["findings"]
        if finding["file"] == file and finding["kind"] == "query_in_loop"
    )


def test_package_relative_import_alias_is_followed(tmp_path):
    _write(
        tmp_path,
        {
            "app/__init__.py": """
from .queries import fetch as alias

def view(keys):
    for key in keys:
        alias(None, key)
""",
            "app/queries.py": """
from sqlalchemy.orm import Session

def fetch(session: Session, key):
    return session.execute(key)
""",
        },
    )

    report = analyze_project(tmp_path)
    finding = _loop_finding(report, "app/__init__.py")
    assert "Batch reads with IN queries" in finding["suggestion"]
    assert report["project_inference"]["resolved_imports"] >= 1


def test_absolute_and_module_import_forms_are_followed(tmp_path):
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/queries.py": """
from sqlalchemy.orm import Session

def fetch(session: Session, key):
    return session.execute(key)
""",
            "app/by_symbol.py": """
from app.queries import fetch as alias
def view(keys):
    for key in keys:
        alias(key)
""",
            "app/by_module.py": """
import app.queries as q
def view(keys):
    for key in keys:
        q.fetch(None, key)
""",
            "app/by_package.py": """
from app import queries as q
def view(keys):
    for key in keys:
        q.fetch(None, key)
""",
        },
    )

    report = analyze_project(tmp_path)
    for file in ("app/by_symbol.py", "app/by_module.py", "app/by_package.py"):
        finding = _loop_finding(report, file)
        assert "Batch reads with IN queries" in finding["suggestion"]


def test_helper_chain_across_three_modules_and_cycle_terminates(tmp_path):
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/first.py": """
from sqlalchemy.orm import Session
from .second import middle

def fetch(session: Session, key):
    return session.execute(key)
""",
            "app/second.py": """
from .first import fetch
from .third import outer

def middle(session, key):
    return fetch(session, key)
""",
            "app/third.py": """
from .second import middle

def outer(session, key):
    return middle(session, key)
""",
            "app/view.py": """
from .third import outer

def view(session, keys):
    for key in keys:
        outer(session, key)
""",
        },
    )

    report = analyze_project(tmp_path)
    finding = _loop_finding(report, "app/view.py")
    assert "Batch reads with IN queries" in finding["suggestion"]
    assert 1 <= report["project_inference"]["passes"] <= 8


def test_module_side_effects_are_parsed_but_never_executed(tmp_path):
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/queries.py": """
raise RuntimeError('module code must not execute')
from sqlalchemy.orm import Session

def fetch(session: Session, key):
    return session.execute(key)
""",
            "app/view.py": """
from app.queries import fetch
def view(session, keys):
    for key in keys:
        fetch(session, key)
""",
        },
    )

    report = analyze_project(tmp_path)
    assert not report["errors"]
    assert _loop_finding(report, "app/view.py")


def test_imported_alias_shadowing_suppresses_inference(tmp_path):
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/queries.py": """
from sqlalchemy.orm import Session
def fetch(session: Session, key):
    return session.execute(key)
""",
            "app/view.py": """
from app.queries import fetch as alias

def parameter_shadow(alias, keys):
    for key in keys:
        alias(key)

def local_shadow(keys):
    alias = lambda key: key
    for key in keys:
        alias(key)
""",
        },
    )

    report = analyze_project(tmp_path)
    assert not [
        finding
        for finding in report["findings"]
        if finding["file"] == "app/view.py" and finding["kind"] == "query_in_loop"
    ]


def test_top_level_rebinding_suppresses_import_inference(tmp_path):
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/queries.py": """
from sqlalchemy.orm import Session
def fetch(session: Session, key):
    return session.execute(key)
""",
            "app/view.py": """
from app.queries import fetch as alias
alias = lambda key: key

def view(keys):
    for key in keys:
        alias(key)
""",
        },
    )

    report = analyze_project(tmp_path)
    assert not [
        finding
        for finding in report["findings"]
        if finding["file"] == "app/view.py" and finding["kind"] == "query_in_loop"
    ]


def test_symlinks_excluded_trees_and_outside_imports_are_not_followed(tmp_path):
    root = tmp_path / "project"
    outside = tmp_path / "outside.py"
    root.mkdir()
    outside.write_text(
        "from sqlalchemy.orm import Session\n"
        "def fetch(session: Session, key):\n"
        "    return session.execute(key)\n"
    )
    _write(
        root,
        {
            "app.py": """
from outside import fetch
def view(session, keys):
    for key in keys:
        fetch(session, key)
""",
            ".venv/hidden.py": "invalid python source",
        },
    )
    (root / "linked.py").symlink_to(outside)

    report = analyze_project(root)
    assert report["files_analyzed"] == 1
    assert report["errors"] == []
    assert report["findings"] == []


def test_inference_limits_name_dynamic_wildcard_and_class_dispatch(tmp_path):
    _write(tmp_path, {"app.py": "value = __import__('plugin')\n"})

    report = analyze_project(tmp_path)
    limitations = " ".join(report["project_inference"]["limitations"])
    assert "Dynamic imports" in limitations
    assert "wildcard imports" in limitations
    assert "class dispatch" in limitations


def test_package_reexported_helper_is_followed_without_importing(tmp_path):
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("from .queries import fetch as load\n")
    (package / "queries.py").write_text(
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
        "async def fetch(session: AsyncSession, key):\n"
        "    return await session.get(Item, key)\n"
    )
    (package / "view.py").write_text(
        "from app import load\n"
        "async def render(session, keys):\n"
        "    for key in keys:\n"
        "        await load(session, key)\n"
    )
    report = analyze_project(package)
    assert any(f["file"] == "view.py" and f["kind"] == "query_in_loop" for f in report["findings"])
    assert not report["project_inference"]["limited"]
