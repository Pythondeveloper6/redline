"""The prebuilt web UI shipped inside the wheel.

The React app in ``frontend/`` is built with ``base: './'`` into ``static/`` next to this
file. Adapters serve :func:`index_html` at their mount root, files under ``assets/`` and
top-level files such as ``badge.js`` / ``favicon.svg`` through :func:`asset_path`, which
refuses anything outside the static directory. The UI uses hash routing, so no catch-all
route is needed.

The floating badge (``CONTRACT.md`` §14/§15) is injected into the app's own HTML pages by
the adapters with :func:`badge_snippet` + :func:`inject_badge`, gated by
:func:`badge_enabled` and :func:`badge_allowed`.
"""

from __future__ import annotations

import html
import json
import mimetypes
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..conf import Settings
    from ..recorder import TraceBuilder

__all__ = [
    "BADGE_MAX_BYTES",
    "asset_path",
    "badge_allowed",
    "badge_enabled",
    "badge_snippet",
    "badge_summary",
    "content_type",
    "csp_allows_badge",
    "index_html",
    "inject_badge",
    "is_built",
    "static_dir",
]

#: Adapters never buffer an HTML body larger than this to inject the badge.
BADGE_MAX_BYTES = 2 * 1024 * 1024

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


# ---------------------------------------------------------------------------- badge
_BODY_CLOSE = re.compile(rb"</body\s*>", re.IGNORECASE)
_DIRECTIVE = re.compile(r"\s*;\s*")


def badge_enabled(settings: Settings, debug: bool) -> bool:
    """Whether the badge is on: ``badge=True/False`` as given, ``"auto"`` = ``debug``."""
    if not settings.enabled:
        return False
    if settings.badge == "auto":
        return bool(debug)
    return bool(settings.badge)


def csp_allows_badge(csp: str | None) -> bool:
    """Whether a ``Content-Security-Policy`` lets the page load ``<mount>/badge.js``.

    No policy, or one without ``script-src`` / ``default-src``, allows it. Otherwise the
    effective source list (``script-src``, else ``default-src``) must contain ``'self'``
    or ``*``; anything stricter (nonces, hashes, other hosts only) skips the injection
    rather than producing a CSP violation in the console.
    """
    if not csp:
        return True
    directives: dict[str, list[str]] = {}
    for policy in csp.split(","):  # several policies: every one must allow it
        for part in _DIRECTIVE.split(policy.strip()):
            name, _, value = part.strip().partition(" ")
            if name:
                directives.setdefault(name.lower(), []).append(value.lower())
    sources = directives.get("script-src") or directives.get("default-src")
    if not sources:
        return True
    return all(("'self'" in v.split() or "*" in v.split()) for v in sources)


def badge_allowed(
    status: int | None,
    content_type: str | None,
    *,
    content_encoding: str | None = None,
    csp: str | None = None,
) -> bool:
    """The response-side checks: 2xx, ``text/html``, not compressed, CSP allows it."""
    if status is None or not 200 <= status < 300:
        return False
    if not content_type or not content_type.split(";")[0].strip().lower() == "text/html":
        return False
    if content_encoding and content_encoding.strip().lower() not in ("", "identity"):
        return False
    return csp_allows_badge(csp)


def badge_summary(builder: TraceBuilder, status: int | None) -> dict[str, Any]:
    """What the badge shows before the trace is finished (``data-redline-summary``).

    Issues are only known once ``finish_trace`` ran detection, so ``issue_count`` is
    ``None`` and ``issue_kinds`` empty here: the badge fetches ``<mount>/api/traces/<id>``
    for them when it is opened (the trace is saved a moment after the response).
    """
    return {
        "duration_ms": round(builder.elapsed_ms(), 1),
        "query_count": builder.query_count,
        "query_ms": round(builder.query_ms, 1),
        "issue_count": None,
        "issue_kinds": [],
        "status": status,
    }


def badge_snippet(trace_id: str, mount_url: str, summary: Mapping[str, Any]) -> str:
    """The ``<script>`` tag that loads the badge (``CONTRACT.md`` §14).

    ``mount_url`` is Redline's mount as the browser sees it (``/_redline`` or
    ``/app/_redline``, no trailing slash). Every attribute is HTML-escaped.
    """
    mount = mount_url.rstrip("/")
    data = json.dumps(dict(summary), separators=(",", ":"), default=str)
    esc = html.escape
    return (
        f'<script src="{esc(mount)}/badge.js" defer'
        f' data-redline-trace="{esc(trace_id)}"'
        f' data-redline-mount="{esc(mount)}"'
        f" data-redline-summary='{esc(data)}'></script>"
    )


def inject_badge(body: bytes, snippet: str) -> bytes | None:
    """``body`` with ``snippet`` inserted before the last ``</body>`` (any case);
    ``None`` when there is no ``</body>`` (fragments, non-HTML)."""
    matches = list(_BODY_CLOSE.finditer(body))
    if not matches:
        return None
    i = matches[-1].start()
    return body[:i] + snippet.encode("utf-8") + body[i:]
