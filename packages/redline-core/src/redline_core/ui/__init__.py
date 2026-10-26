"""The prebuilt web UI shipped inside the wheel.

The React app in ``frontend/`` is built with ``base: './'`` into ``static/`` next to this
file. Adapters serve :func:`index_html` at their mount root and files under ``assets/``
through :func:`asset_path`, which refuses anything outside the static directory. The UI
uses hash routing, so no catch-all route is needed.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

__all__ = ["asset_path", "content_type", "index_html", "is_built", "static_dir"]

_STATIC = Path(__file__).resolve().parent / "static"

_PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Redline</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; padding: 2rem">
<h1>Redline</h1>
<p>The UI bundle is missing from this installation. Build it with
<code>scripts/build_ui.sh</code>. The JSON API is available under <code>./api/</code>.</p>
</body></html>
"""


def static_dir() -> Path:
    """Directory holding ``index.html`` and ``assets/``."""
    return _STATIC


def is_built() -> bool:
    """Whether the UI bundle is present."""
    return (_STATIC / "index.html").is_file()


def index_html() -> str:
    """The SPA entry page (a small placeholder if the bundle was not built)."""
    try:
        return (_STATIC / "index.html").read_text(encoding="utf-8")
    except OSError:
        return _PLACEHOLDER


def asset_path(relative: str) -> Path | None:
    """Resolve ``relative`` inside the static dir; ``None`` if missing or outside it."""
    if not relative or "\x00" in relative:
        return None
    candidate = (_STATIC / relative.lstrip("/")).resolve()
    try:
        candidate.relative_to(_STATIC)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def content_type(path: Path | str) -> str:
    """A ``Content-Type`` for a static file (UTF-8 for text types)."""
    name = str(path)
    if name.endswith(".js") or name.endswith(".mjs"):
        return "text/javascript; charset=utf-8"
    guessed, _ = mimetypes.guess_type(name)
    guessed = guessed or "application/octet-stream"
    if guessed.startswith("text/") or guessed in ("application/json", "image/svg+xml"):
        return f"{guessed}; charset=utf-8"
    return guessed
