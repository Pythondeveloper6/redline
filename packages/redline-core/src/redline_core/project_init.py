"""``redline init``: detect the framework and write a starter configuration.

Writes ``redline.toml`` (or a ``[tool.redline]`` table with ``--pyproject``, or when the
project already keeps its Redline settings in ``pyproject.toml``) and adds the store
directory to ``.gitignore``. Existing files are never overwritten without ``--force``.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

from .conf import Settings

__all__ = ["FRAMEWORKS", "command", "configure", "detect_framework", "render_config"]

#: Distribution name -> framework, in detection priority order.
FRAMEWORKS: dict[str, tuple[str, str]] = {
    "django": ("django", "django-redline"),
    "fastapi": ("fastapi", "fastapi-redline"),
    "flask": ("flask", "flask-redline"),
    "litestar": ("litestar", "litestar-redline"),
}

_SETUP = {
    "django": (
        'INSTALLED_APPS += ["django_redline"]; MIDDLEWARE.insert(0, '
        '"django_redline.middleware.RedlineMiddleware"); '
        'path("redline/", include("django_redline.urls"))'
    ),
    "fastapi": "from fastapi_redline import install_if_dev; install_if_dev(app, engine=engine)",
    "flask": "from flask_redline import install_if_dev; install_if_dev(app, engine=engine)",
    "litestar": (
        "from litestar_redline import install_if_dev; plugin = install_if_dev(engine=engine)"
    ),
}


def _declared(root: Path) -> set[str]:
    """Distribution names mentioned in ``pyproject.toml`` and ``requirements*.txt``."""
    texts = []
    for name in ("pyproject.toml", "Pipfile"):
        path = root / name
        if path.is_file():
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
    for path in root.glob("requirements*.txt"):
        texts.append(path.read_text(encoding="utf-8", errors="replace"))
    found = set()
    for text in texts:
        for key in FRAMEWORKS:
            if re.search(rf"(?im)(^|[\"'\s,\[]){key}(?![\w-])", text):
                found.add(key)
    return found


def _installed() -> set[str]:
    from importlib import metadata

    found = set()
    for key in FRAMEWORKS:
        try:
            metadata.version(key)
        except metadata.PackageNotFoundError:
            continue
        found.add(key)
    return found


def detect_framework(root: Path, installed: Iterable[str] | None = None) -> str | None:
    """The framework of the project in ``root``: ``manage.py``, then declared
    dependencies, then installed packages. ``None`` when nothing matches."""
    if (root / "manage.py").is_file():
        return "django"
    declared = _declared(root)
    present = set(_installed() if installed is None else installed)
    for pool in (declared, present):
        for key in FRAMEWORKS:
            if key in pool:
                return key
    return None


def render_config(framework: str | None, *, storage_path: str | None = None) -> str:
    """The starter settings as TOML (top-level keys, valid in ``redline.toml``)."""
    storage = storage_path or Settings().storage_path
    lines = [
        "# Redline settings (written by `redline init`). Local development only.",
        "# Every key: https://pythondeveloper6.github.io/redline/settings/",
        "",
        'preset = "dev"',
        f'storage_path = "{storage}"',
        "",
        "# Keep every request while developing; lower it on a shared environment.",
        "sample_rate = 1.0",
        "# Requests slower than this are always stored, even when sampled out.",
        "always_keep_slower_than_ms = 500",
        "",
        "# Paths that are never recorded (prefixes, or regular expressions starting with ^).",
        'ignore_paths = ["/static/", "/media/", "/favicon.ico", "/health"]',
    ]
    if framework:
        pkg = FRAMEWORKS[framework][1]
        lines += ["", f"# {framework}: pip install {pkg}", f"# {_SETUP[framework]}"]
    return "\n".join(lines) + "\n"


def _pyproject_snippet(config: str) -> str:
    body = [ln for ln in config.splitlines() if not ln.startswith("# Redline settings")]
    return "\n[tool.redline]\n" + "\n".join(body).strip("\n") + "\n"


def _has_table(path: Path) -> bool:
    return bool(
        path.is_file()
        and re.search(
            r"^\s*\[\s*tool\s*\.\s*redline\s*[\].]",
            path.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
    )


def _ignore_entry(storage_path: str) -> str:
    """``/.redline/`` for ``.redline/redline.db``; the file itself when it has no directory."""
    parent = Path(storage_path).parent.as_posix()
    if parent in ("", "."):
        return "/" + Path(storage_path).as_posix().lstrip("/")
    return "/" + parent.strip("/") + "/"


def _ignored(text: str, entry: str) -> bool:
    wanted = entry.strip("/")
    return any(
        line.strip().strip("/") == wanted
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dir", default=".", metavar="DIR", help="project directory (default .)")
    parser.add_argument(
        "--framework",
        choices=sorted(FRAMEWORKS),
        help="skip detection (manage.py, dependencies, installed packages)",
    )
    parser.add_argument(
        "--pyproject",
        action="store_true",
        help="append a [tool.redline] table to pyproject.toml instead of writing redline.toml",
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing redline.toml")
    parser.add_argument("--dry-run", action="store_true", help="print what would be written")


def command(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    root = Path(args.dir)
    if not root.is_dir():
        err.write(f"redline init: {args.dir}: not a directory\n")
        return 2
    framework = args.framework or detect_framework(root)
    out.write(f"framework: {framework or 'not detected'}\n")
    config = render_config(framework)
    storage = Settings().storage_path
    pyproject = root / "pyproject.toml"
    toml_path = root / "redline.toml"
    writes: list[tuple[Path, str, str]] = []  # (path, mode, content)

    if toml_path.exists() or _has_table(pyproject):
        use_pyproject = not toml_path.exists()  # settings already live in pyproject.toml
    else:
        use_pyproject = args.pyproject
    if use_pyproject:
        if not pyproject.is_file():
            err.write("redline init: no pyproject.toml here; omit --pyproject\n")
            return 2
        if _has_table(pyproject):
            out.write("pyproject.toml already has [tool.redline]: left unchanged\n")
        else:
            writes.append((pyproject, "a", _pyproject_snippet(config)))
    elif toml_path.exists() and not args.force:
        out.write("redline.toml already exists: left unchanged (use --force to overwrite)\n")
    else:
        writes.append((toml_path, "w", config))

    gitignore = root / ".gitignore"
    entry = _ignore_entry(storage)
    current = gitignore.read_text(encoding="utf-8", errors="replace") if gitignore.exists() else ""
    if not _ignored(current, entry):
        prefix = "" if not current or current.endswith("\n") else "\n"
        writes.append((gitignore, "a", f"{prefix}# Redline recordings\n{entry}\n"))
    else:
        out.write(f".gitignore already ignores {entry}\n")

    for path, mode, content in writes:
        verb = "append to" if mode == "a" else "write"
        if args.dry_run:
            out.write(f"--- would {verb} {path} ---\n{content}")
            continue
        with open(path, mode, encoding="utf-8") as fh:
            fh.write(content)
        out.write(f"{'appended to' if mode == 'a' else 'wrote'} {path}\n")
    if framework and writes and not args.dry_run:
        out.write(f"next: pip install {FRAMEWORKS[framework][1]}, then {_SETUP[framework]}\n")
    return 0
