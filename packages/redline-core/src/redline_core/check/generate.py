"""``redline check init --from-traces``: a suite from what Redline has seen (section 19.1).

Every read-only ``(route, method)`` the store has traffic for becomes one request, with
the most recent concrete path recorded for that route (2xx/3xx responses only; a path or
query with a redacted value is skipped, never copied). Header values are never copied:
when the recorded request carried ``Authorization`` or ``Cookie``, the request gets a
``${env:REDLINE_CHECK_AUTHORIZATION}`` / ``${env:REDLINE_CHECK_COOKIE}`` placeholder to
fill in from the environment at run time.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from .suite import READ_ONLY_METHODS, Suite

__all__ = ["DEFAULT_BASE_URL", "ENV_HEADERS", "from_traces"]

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_WINDOW_S = 7 * 86_400.0
#: Recorded request headers that get an ``${env:…}`` placeholder (never the value).
ENV_HEADERS = {
    "authorization": ("Authorization", "REDLINE_CHECK_AUTHORIZATION"),
    "cookie": ("Cookie", "REDLINE_CHECK_COOKIE"),
}
_SAMPLES = 10


def _clean(value: Any) -> bool:
    return isinstance(value, str) and "redacted" not in value.lower() and "{" not in value


def _query_string(query: Any) -> str | None:
    """The recorded query as a string, ``None`` when a value is redacted."""
    if not query:
        return ""
    if not isinstance(query, Mapping):
        return None
    pairs: list[tuple[str, str]] = []
    for key, value in query.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            if not _clean(str(key)) or not _clean(str(v)):
                return None
            pairs.append((str(key), str(v)))
    return urlencode(pairs)


def _matches(route: str, patterns: Sequence[str]) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(route, p) or route == p for p in patterns)


def _origin(request: Mapping[str, Any]) -> str | None:
    """``scheme://host`` of a recorded request (its ``Host`` header), or ``None``."""
    headers = request.get("headers")
    if not isinstance(headers, Mapping):
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    host = lowered.get("host")
    if not isinstance(host, str) or not host or not _clean(host) or "/" in host or " " in host:
        return None
    proto = lowered.get("x-forwarded-proto")
    scheme = "https" if isinstance(proto, str) and proto.strip().lower() == "https" else "http"
    return f"{scheme}://{host}"


def _sample(store: Any, route: str, method: str) -> tuple[str, dict[str, str], str | None] | None:
    """``(path?query, {header: placeholder}, origin)`` of the most recent clean trace of a
    route."""
    from ..storage import Filters

    # Ask for successful responses only: a route whose latest calls all failed (a refactor
    # that broke it) must still be sampled from its older, healthy traces.
    items: list[Any] = []
    for status in ("2xx", "3xx"):
        page = store.list(Filters(route=route, method=method, status=status), limit=_SAMPLES)
        if not page.items:  # endpoints() falls back to the path when there is no route
            page = store.list(Filters(q=route, method=method, status=status), limit=_SAMPLES)
        items.extend(page.items)
    for item in items:  # newest first within each class
        path = item.get("path")
        status = int(item.get("status") or 0)
        if not _clean(path) or not path.startswith("/") or path.startswith("//"):
            continue
        if not 200 <= status < 400:
            continue
        trace = store.get(item["id"], include_profile=False)
        request = getattr(trace, "request", None) or {}
        query = _query_string(request.get("query"))
        if query is None:
            continue
        headers: dict[str, str] = {}
        recorded = request.get("headers") or {}
        for name in recorded if isinstance(recorded, Mapping) else ():
            hit = ENV_HEADERS.get(str(name).lower())
            if hit:
                headers[hit[0]] = f"${{env:{hit[1]}}}"
        return (
            (f"{path}?{query}" if query and "?" not in path else path),
            headers,
            _origin(request),
        )
    return None


def from_traces(
    store: Any,
    *,
    routes: Sequence[str] = (),
    limit: int = 20,
    base_url: str | None = None,
    window_s: float = DEFAULT_WINDOW_S,
    mount_prefix: str = "/_redline",
    name: str = "recorded traffic",
) -> dict[str, Any]:
    """A suite dict of up to ``limit`` read-only endpoints (busiest first), optionally
    only routes matching one of ``routes`` (``fnmatch`` patterns or exact routes)."""
    requests: list[dict[str, Any]] = []
    stats = sorted(store.endpoints(window_s), key=lambda e: e.count, reverse=True)
    seen: set[str] = set()
    origins: set[str | None] = set()
    for ep in stats:
        if len(requests) >= limit:
            break
        method = str(ep.method).upper()
        route = ep.route or ""
        if method not in READ_ONLY_METHODS or not route or route.startswith(mount_prefix):
            continue
        if not _matches(route, routes):
            continue
        sample = _sample(store, route, method)
        if sample is None:
            continue
        path, headers, origin = sample
        origins.add(origin)
        req_name = f"{method} {route}"
        if req_name in seen:
            continue
        seen.add(req_name)
        item: dict[str, Any] = {"name": req_name, "method": method, "path": path}
        if headers:
            item["headers"] = headers
        requests.append(item)
    if not requests:
        raise ValueError(
            "no recorded read-only traffic"
            + (" for those routes" if routes else "")
            + "; browse the app with Redline installed first"
        )
    data: dict[str, Any] = {"name": name}
    # Never guess the app's address: it is the --base-url, or the one host the traces show.
    if base_url:
        data["base_url"] = base_url
    elif len(origins) == 1 and None not in origins:
        data["base_url"] = next(iter(origins))
    data.update(repeat=20, warmup=3, requests=requests)
    Suite.from_dict(data)  # never write a suite we could not run
    return data
