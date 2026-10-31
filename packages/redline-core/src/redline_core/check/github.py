"""GitHub Actions glue for ``redline check``: the job summary and one sticky PR comment.

The comment is identified by a hidden HTML marker, so re-running the workflow updates the
same comment instead of adding a new one. Only the standard library is used; the REST API
is called with the token the workflow passes in, and only on ``pull_request`` events.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

__all__ = [
    "DEFAULT_MARKER_ID",
    "CommentError",
    "marker",
    "pull_request_number",
    "sticky_body",
    "upsert_comment",
    "write_job_summary",
]

DEFAULT_MARKER_ID = "redline-check"
#: GitHub rejects comments above 65536 characters; stay below with room for the notice.
MAX_BODY = 60_000
PER_PAGE = 100
MAX_PAGES = 20
#: Upper bound on one API answer (a page of 100 comments is far below it).
MAX_RESPONSE = 8 * 1024 * 1024
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class CommentError(RuntimeError):
    """The comment could not be created or updated."""


def marker(marker_id: str = DEFAULT_MARKER_ID) -> str:
    """The hidden HTML comment that identifies Redline's sticky comment."""
    safe = "".join(c for c in marker_id if c.isalnum() or c in "-_.") or DEFAULT_MARKER_ID
    return f"<!-- redline-sticky-comment:{safe} -->"


def sticky_body(markdown: str, marker_id: str = DEFAULT_MARKER_ID) -> str:
    """``markdown`` with the marker on the first line, truncated to fit a comment."""
    text = markdown.strip()
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY].rstrip() + "\n\n_Report truncated; see the job summary._"
    return f"{marker(marker_id)}\n{text}\n"


def write_job_summary(markdown: str, env: Mapping[str, str] | None = None) -> bool:
    """Append ``markdown`` to ``$GITHUB_STEP_SUMMARY``; ``False`` when it is not set."""
    path = (os.environ if env is None else env).get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown.rstrip() + "\n")
    return True


def pull_request_number(env: Mapping[str, str] | None = None) -> int | None:
    """The PR number when running for a ``pull_request`` event, else ``None``."""
    env = os.environ if env is None else env
    if not str(env.get("GITHUB_EVENT_NAME", "")).startswith("pull_request"):
        return None
    path = env.get("GITHUB_EVENT_PATH")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        return None
    number = (event.get("pull_request") or {}).get("number") or event.get("number")
    return number if isinstance(number, int) and not isinstance(number, bool) else None


class _NoRedirect(HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the ``Authorization`` header to it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_api_url(api_url: str) -> None:
    """The token goes only over https (plain http only to a loopback mock), with no
    credentials, query or fragment in the URL."""
    parts = urlsplit(api_url)
    host = parts.hostname or ""
    if (
        not host
        or parts.username is not None
        or parts.query
        or parts.fragment
        or not (parts.scheme == "https" or (parts.scheme == "http" and _loopback(host)))
    ):
        raise CommentError("invalid GitHub API URL (https required)")


def _api(method: str, url: str, token: str, payload: Any = None) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "redline-check",
        },
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except HTTPError as exc:
        raise CommentError(f"GitHub API {method} failed: HTTP {exc.code}") from None
    except (URLError, OSError) as exc:
        raise CommentError(f"GitHub API {method} failed: {exc}") from None
    if len(raw) > MAX_RESPONSE:
        raise CommentError(f"GitHub API {method} answer too large")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        raise CommentError(f"GitHub API {method} answered invalid JSON") from None


def upsert_comment(
    body: str,
    *,
    token: str,
    repository: str,
    pr_number: int,
    marker_id: str = DEFAULT_MARKER_ID,
    api_url: str = "https://api.github.com",
) -> tuple[str, int]:
    """Update the comment carrying the marker, or create it. Returns ``(action, id)``."""
    if not token:
        raise CommentError("no token given")
    _check_api_url(api_url)
    if not _REPOSITORY.fullmatch(repository) or ".." in repository:
        raise CommentError("invalid repository (expected owner/name)")
    base = f"{api_url.rstrip('/')}/repos/{repository}"
    wanted = marker(marker_id)
    for page in range(1, MAX_PAGES + 1):
        comments = _api(
            "GET",
            f"{base}/issues/{pr_number}/comments?per_page={PER_PAGE}&page={page}",
            token,
        )
        for comment in comments or []:
            if wanted in str(comment.get("body") or ""):
                _api("PATCH", f"{base}/issues/comments/{comment['id']}", token, {"body": body})
                return "updated", int(comment["id"])
        if len(comments or []) < PER_PAGE:
            break
    created = _api("POST", f"{base}/issues/{pr_number}/comments", token, {"body": body})
    return "created", int((created or {}).get("id", 0))
