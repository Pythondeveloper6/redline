"""``api/checks/…``: refactor-check suites, runs and baselines (``CONTRACT.md`` §19.5).

Routed by :meth:`redline_core.api.RedlineAPI.dispatch` (the access gate, the ``checks``
feature switch and ``read_only`` apply first, see :func:`redline_core.access.route_policy`).
The engine (:mod:`redline_core.check.suite` / ``baseline`` / ``runner``) is imported lazily
and only where it is needed; validation falls back to a built-in check of the §19.1 shape
when the engine is not importable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .redact import MASK, Redactor

if TYPE_CHECKING:  # pragma: no cover
    from .api import RawResponse, RedlineAPI, Response

__all__ = ["CheckRoutes", "suite_to_toml", "validate_suite"]

_ID = r"([A-Za-z0-9_-]{1,64})"
_SUITE = re.compile(rf"^api/checks/suites/{_ID}$")
_SUITE_EXPORT = re.compile(rf"^api/checks/suites/{_ID}/export$")
_RUN = re.compile(rf"^api/checks/runs/{_ID}$")
_RUN_STOP = re.compile(rf"^api/checks/runs/{_ID}/stop$")

#: Body limits: suites and run requests, imported baselines.
MAX_SUITE_BODY = 1024 * 1024
MAX_BASELINE_BODY = 10 * 1024 * 1024
READ_ONLY_METHODS = ("GET", "HEAD", "OPTIONS")
DRAFT_WINDOW_S = 7 * 86_400.0
DRAFT_DEFAULT_LIMIT = 50
DRAFT_MAX_LIMIT = 200
#: ``${env:…}`` placeholders the draft suggests when recorded requests carried these.
DRAFT_SECRET_HEADERS = {
    "authorization": ("Authorization", "${env:REDLINE_CHECK_AUTHORIZATION}"),
    "cookie": ("Cookie", "${env:REDLINE_CHECK_COOKIE}"),
}

_SUITE_KEYS = frozenset(
    ("name", "base_url", "headers", "timeout_s", "repeat", "warmup", "thresholds", "requests")
)
_REQUEST_KEYS = frozenset(
    (
        "name",
        "method",
        "path",
        "headers",
        "perf",
        "response",
        "ignore",
        "ignore_headers",
        "expect_status",
    )
)
_THRESHOLD_KEYS = frozenset(("slower_pct", "slower_ms", "max_query_increase"))


class SuiteInvalid(ValueError):
    """A suite that does not validate (→ 400 ``invalid_suite``)."""


# ── validation ─────────────────────────────────────────────────────────────────────────


def _engine_suite() -> Any:
    try:
        from .check.suite import Suite  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Suite


def _engine_baseline() -> Any:
    try:
        from .check.baseline import Baseline  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Baseline


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_headers(value: Any, where: str) -> None:
    if not isinstance(value, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise SuiteInvalid(f"{where}: headers must be a table of strings")


def _shim_validate(d: Mapping[str, Any]) -> None:
    """The §19.1 shape, for when the engine is not importable."""
    unknown = set(d) - _SUITE_KEYS
    if unknown:
        raise SuiteInvalid(f"unknown suite key(s): {', '.join(sorted(map(str, unknown)))}")
    if "name" in d and not isinstance(d["name"], str):
        raise SuiteInvalid("name must be a string")
    if "base_url" in d and d["base_url"] is not None:
        if not isinstance(d["base_url"], str) or not re.match(r"https?://", d["base_url"]):
            raise SuiteInvalid("base_url must be an http(s) URL")
    if "headers" in d:
        _check_headers(d["headers"], "suite")
    if "timeout_s" in d and (not _is_num(d["timeout_s"]) or d["timeout_s"] <= 0):
        raise SuiteInvalid("timeout_s must be a positive number")
    for key, lo, hi in (("repeat", 1, 1000), ("warmup", 0, 100)):
        if key in d:
            v = d[key]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise SuiteInvalid(f"{key} must be an integer in {lo}..{hi}")
    thresholds = d.get("thresholds", {})
    if not isinstance(thresholds, Mapping):
        raise SuiteInvalid("thresholds must be a table")
    unknown = set(thresholds) - _THRESHOLD_KEYS
    if unknown:
        raise SuiteInvalid(f"unknown threshold(s): {', '.join(sorted(map(str, unknown)))}")
    for key, value in thresholds.items():
        if not _is_num(value) or value < 0:
            raise SuiteInvalid(f"thresholds.{key} must be a non-negative number")
    requests = d.get("requests")
    if not isinstance(requests, list) or not requests:
        raise SuiteInvalid("requests must be a non-empty list")
    names: set[str] = set()
    for i, r in enumerate(requests):
        if not isinstance(r, Mapping):
            raise SuiteInvalid(f"requests[{i}] must be a table")
        unknown = set(r) - _REQUEST_KEYS
        if unknown:
            raise SuiteInvalid(
                f"requests[{i}]: unknown key(s): {', '.join(sorted(map(str, unknown)))}"
            )
        method = r.get("method", "GET")
        if not isinstance(method, str) or method.upper() not in READ_ONLY_METHODS:
            raise SuiteInvalid(f"requests[{i}]: only {', '.join(READ_ONLY_METHODS)} can be checked")
        path = r.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise SuiteInvalid(f"requests[{i}]: path must start with a single '/'")
        name = r.get("name", f"{method.upper()} {path}")
        if not isinstance(name, str) or not name.strip():
            raise SuiteInvalid(f"requests[{i}]: name must be a non-empty string")
        if name in names:
            raise SuiteInvalid(f"requests[{i}]: duplicate name {name!r}")
        names.add(name)
        where = f"request {name!r}"
        if "headers" in r:
            _check_headers(r["headers"], where)
        for key in ("perf", "response"):
            if key in r and not isinstance(r[key], bool):
                raise SuiteInvalid(f"{where}: {key} must be true or false")
        for key in ("ignore", "ignore_headers"):
            if key in r and (
                not isinstance(r[key], list) or not all(isinstance(x, str) for x in r[key])
            ):
                raise SuiteInvalid(f"{where}: {key} must be a list of strings")
        if "expect_status" in r and r["expect_status"] is not None:
            v = r["expect_status"]
            if isinstance(v, bool) or not isinstance(v, int) or not 100 <= v <= 599:
                raise SuiteInvalid(f"{where}: expect_status must be an HTTP status")


def validate_suite(d: Any) -> dict[str, Any]:
    """Validate a suite dict (``CONTRACT.md`` §19.1) with the engine's ``Suite.from_dict``
    when it is importable, else with a built-in check of the same shape. Returns the dict
    as given (placeholders untouched); raises :class:`SuiteInvalid`."""
    if not isinstance(d, Mapping):
        raise SuiteInvalid("suite must be an object")
    d = dict(d)
    suite_cls = _engine_suite()
    if suite_cls is None:
        _shim_validate(d)
        return d
    try:
        suite_cls.from_dict(d)
    except Exception as exc:  # the engine's SuiteError (a ValueError) or similar
        raise SuiteInvalid(str(exc) or type(exc).__name__) from None
    # the engine may accept write methods for the CLI's own error message; the UI never
    for i, r in enumerate(d.get("requests") or []):
        method = r.get("method", "GET") if isinstance(r, Mapping) else "GET"
        if not isinstance(method, str) or method.upper() not in READ_ONLY_METHODS:
            raise SuiteInvalid(f"requests[{i}]: only {', '.join(READ_ONLY_METHODS)} can be checked")
    return d


# ── TOML export ────────────────────────────────────────────────────────────────────────


def suite_to_toml(suite: Mapping[str, Any]) -> str:
    """TOML text of a suite: the engine's ``Suite.to_toml`` when importable, else a small
    writer of the §19.1 layout."""
    suite_cls = _engine_suite()
    if suite_cls is not None:
        try:
            return str(suite_cls.from_dict(dict(suite)).to_toml())
        except Exception:  # fall back to the plain writer
            pass
    from .load.generate import _toml_key, _toml_value

    lines = ["# Redline refactor-check suite – run it with `redline check run <file>`.", ""]
    for key, value in suite.items():
        if key in ("requests", "thresholds") or value is None:
            continue
        lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
    thresholds = suite.get("thresholds")
    if isinstance(thresholds, Mapping) and thresholds:
        lines += ["", "[thresholds]"]
        lines += [f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in thresholds.items()]
    for req in suite.get("requests") or []:
        lines += ["", "[[requests]]"]
        lines += [
            f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in req.items() if v is not None
        ]
    return "\n".join(lines) + "\n"


# ── helpers ────────────────────────────────────────────────────────────────────────────


def _unmask(new: Any, old: Any) -> Any:
    """A suite sent back by the UI (secret header values shown as ``[redacted]``) with
    those values restored from the stored suite."""
    if not isinstance(new, dict) or not isinstance(old, Mapping):
        return new

    def headers(n: Any, o: Any) -> Any:
        if not isinstance(n, dict) or not isinstance(o, Mapping):
            return n
        return {k: o.get(k, v) if v == MASK else v for k, v in n.items()}

    out = dict(new)
    if "headers" in out:
        out["headers"] = headers(out["headers"], old.get("headers"))
    old_requests = {
        str(r.get("name") or f"{r.get('method', 'GET')} {r.get('path')}"): r
        for r in old.get("requests") or []
        if isinstance(r, Mapping)
    }
    if isinstance(out.get("requests"), list):
        items = []
        for r in out["requests"]:
            if isinstance(r, dict) and "headers" in r:
                key = str(r.get("name") or f"{r.get('method', 'GET')} {r.get('path')}")
                r = {
                    **r,
                    "headers": headers(r["headers"], old_requests.get(key, {}).get("headers")),
                }
            items.append(r)
        out["requests"] = items
    return out


def _has_mask(value: Any) -> bool:
    if isinstance(value, str):
        return MASK in value or "%5Bredacted%5D" in value or "redacted" in value.lower()
    if isinstance(value, Mapping):
        return any(_has_mask(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_mask(v) for v in value)
    return False


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:60] or "suite"


# ── routes ─────────────────────────────────────────────────────────────────────────────


class CheckRoutes:
    """The ``api/checks`` handlers of one :class:`~redline_core.api.RedlineAPI`."""

    def __init__(
        self,
        api: RedlineAPI,
        launcher: Callable[..., str] | None = None,
        stopper: Callable[[str], bool] | None = None,
    ) -> None:
        self.api = api
        self._launcher = launcher
        self._stopper = stopper
        self._hooks_built = launcher is not None or stopper is not None

    # plumbing -------------------------------------------------------------------------
    @property
    def store(self) -> Any:
        return self.api.store

    @property
    def redactor(self) -> Redactor:
        return Redactor(self.api.settings)

    def _hooks(self) -> tuple[Callable[..., str] | None, Callable[[str], bool] | None]:
        if not self._hooks_built:
            from .check.control import check_hooks

            self._launcher, self._stopper = check_hooks(self.store, self.api.settings)
            self._hooks_built = True
        return self._launcher, self._stopper

    def _ext(self, name: str) -> Callable[..., Any]:
        return self.api._ext(name)

    def _body(self, body: Any, limit: int) -> Mapping[str, Any] | Response:
        from .api import _json_body, error

        if isinstance(body, (bytes, bytearray, str)):
            size = len(body.encode("utf-8")) if isinstance(body, str) else len(body)
            if size > limit:
                return error(
                    413, "payload_too_large", f"The request body exceeds {limit // 1024} KiB."
                )
        return _json_body(body)

    def from_ui_allowed(self) -> bool:
        """``checks_from_ui``: True/False, or ``"auto"`` = the framework's debug mode."""
        s = self.api.settings
        value = getattr(s, "checks_from_ui", "auto")
        if s.read_only or not s.feature("checks"):
            return False
        if value == "auto":
            return self.api.debug_mode()
        return bool(value)

    def route(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any],
        body: Any,
        base_url: str | None,
    ) -> Response | RawResponse:
        from .api import _not_allowed, error

        if path == "api/checks/suites":
            if method == "GET":
                return self.list_suites()
            if method == "POST":
                return self.create_suite(body)
            return _not_allowed()
        if path == "api/checks/suites/draft":
            return self.draft(body) if method == "POST" else _not_allowed()
        if m := _SUITE_EXPORT.match(path):
            return self.export_suite(m.group(1)) if method == "GET" else _not_allowed()
        if m := _SUITE.match(path):
            if method == "GET":
                return self.get_suite(m.group(1))
            if method == "PUT":
                return self.update_suite(m.group(1), body)
            if method == "DELETE":
                return self.delete_suite(m.group(1))
            return _not_allowed()
        if path == "api/checks/runs":
            if method == "GET":
                return self.list_runs(params)
            if method == "POST":
                return self.start_run(body, base_url)
            return _not_allowed()
        if m := _RUN_STOP.match(path):
            return self.stop_run(m.group(1)) if method == "POST" else _not_allowed()
        if m := _RUN.match(path):
            if method == "GET":
                return self.get_run(m.group(1))
            if method == "DELETE":
                return self.delete_run(m.group(1))
            return _not_allowed()
        if path == "api/checks/baselines/import":
            return self.import_baseline(body, params) if method == "POST" else _not_allowed()
        return error(404, "not_found", f"No API endpoint at {path!r}.")

    # suites -----------------------------------------------------------------------------
    def _suite_record(self, rec: Mapping[str, Any]) -> dict[str, Any]:
        from .check.sink import redact_suite

        suite = redact_suite(rec["suite"], self.redactor)
        return {
            "id": rec["id"],
            "name": rec["name"],
            "suite": suite,
            "requests": len(suite.get("requests") or []) if isinstance(suite, Mapping) else 0,
            "created_at": rec["created_at"],
            "updated_at": rec["updated_at"],
        }

    def _parse_suite_body(
        self, body: Any, old: Mapping[str, Any] | None = None
    ) -> tuple[str, dict[str, Any], None] | tuple[None, None, Response]:
        """``(name, suite, None)`` or ``(None, None, error response)``."""
        from .api import error

        parsed = self._body(body, MAX_SUITE_BODY)
        if not isinstance(parsed, Mapping):
            return None, None, parsed
        raw = parsed.get("suite")
        if not isinstance(raw, Mapping):
            return None, None, error(400, "invalid_body", "Expected {name, suite}.")
        suite = _unmask(dict(raw), old) if old is not None else dict(raw)
        name = parsed.get("name", suite.get("name"))
        if not isinstance(name, str) or not name.strip():
            return None, None, error(400, "invalid_body", "name is required.")
        try:
            suite = validate_suite(suite)
        except SuiteInvalid as exc:
            return None, None, error(400, "invalid_suite", str(exc))
        return name.strip()[:200], suite, None

    def list_suites(self) -> Response:
        return 200, {"items": self._ext("list_check_suites")()}

    def create_suite(self, body: Any) -> Response:
        name, suite, problem = self._parse_suite_body(body)
        if problem is not None or name is None or suite is None:
            return problem  # type: ignore[return-value]
        rec = self._ext("create_check_suite")(name, suite)
        return 201, self._suite_record(rec)

    def get_suite(self, suite_id: str) -> Response:
        from .api import error

        rec = self._ext("get_check_suite")(suite_id)
        if rec is None:
            return error(404, "suite_not_found", f"No suite {suite_id!r}.")
        return 200, self._suite_record(rec)

    def update_suite(self, suite_id: str, body: Any) -> Response:
        from .api import error

        old = self._ext("get_check_suite")(suite_id)
        if old is None:
            return error(404, "suite_not_found", f"No suite {suite_id!r}.")
        name, suite, problem = self._parse_suite_body(body, old["suite"])
        if problem is not None or name is None or suite is None:
            return problem  # type: ignore[return-value]
        rec = self._ext("update_check_suite")(suite_id, name, suite)
        if rec is None:
            return error(404, "suite_not_found", f"No suite {suite_id!r}.")
        return 200, self._suite_record(rec)

    def delete_suite(self, suite_id: str) -> Response:
        from .api import error

        if self._ext("get_check_suite")(suite_id) is None:
            return error(404, "suite_not_found", f"No suite {suite_id!r}.")
        runs = self._ext("list_check_runs")(suite_id=suite_id, limit=500)
        if any(r["status"] == "running" for r in runs):
            return error(409, "run_active", "Stop the suite's running check first.")
        return 200, {"deleted": self._ext("delete_check_suite")(suite_id)}

    def export_suite(self, suite_id: str) -> Response | RawResponse:
        from .api import RawResponse, error
        from .check.sink import redact_suite

        rec = self._ext("get_check_suite")(suite_id)
        if rec is None:
            return error(404, "suite_not_found", f"No suite {suite_id!r}.")
        suite = redact_suite(rec["suite"], self.redactor)
        suite = {"name": rec["name"], **{k: v for k, v in suite.items() if k != "name"}}
        text = suite_to_toml(suite)
        return RawResponse(
            200,
            "text/plain; charset=utf-8",
            text.encode("utf-8"),
            {"Content-Disposition": f'attachment; filename="{_slug(rec["name"])}.toml"'},
        )

    def draft(self, body: Any) -> Response:
        """A suite of read-only endpoints from recent traffic (not saved)."""
        from .api import error
        from .storage import Filters

        parsed = self._body(body, MAX_SUITE_BODY)
        if not isinstance(parsed, Mapping):
            return parsed
        limit = parsed.get("limit", DRAFT_DEFAULT_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return error(400, "invalid_body", "limit must be a positive integer.")
        limit = min(limit, DRAFT_MAX_LIMIT)
        wanted: set[tuple[str | None, str]] | None = None
        routes = parsed.get("routes")
        if routes is not None:
            if not isinstance(routes, list):
                return error(400, "invalid_body", "routes must be a list.")
            wanted = set()
            for item in routes:
                if isinstance(item, Mapping) and isinstance(item.get("route"), str):
                    m = item.get("method")
                    wanted.add((m.upper() if isinstance(m, str) else None, item["route"]))
                elif isinstance(item, str) and item.strip():
                    head, _, tail = item.strip().partition(" ")
                    if tail and head.upper() in READ_ONLY_METHODS:
                        wanted.add((head.upper(), tail.strip()))
                    else:
                        wanted.add((None, item.strip()))
                else:
                    return error(400, "invalid_body", "routes must be strings like 'GET /x'.")
        mount = self.api.settings.mount_path or "/_redline"
        stats = sorted(self.store.endpoints(DRAFT_WINDOW_S), key=lambda e: (-e.count, e.route))
        requests: list[dict[str, Any]] = []
        secret_headers: dict[str, str] = {}
        for ep in stats:
            if len(requests) >= limit:
                break
            method = ep.method.upper()
            route = ep.route or ""
            if method not in READ_ONLY_METHODS or not route.startswith("/"):
                continue
            if route.startswith(mount):
                continue
            if wanted is not None and (method, route) not in wanted and (None, route) not in wanted:
                continue
            sample = self._sample(route, method, Filters)
            if sample is None:
                continue
            path, headers = sample
            for name in headers:
                if name in DRAFT_SECRET_HEADERS:
                    key, placeholder = DRAFT_SECRET_HEADERS[name]
                    secret_headers[key] = placeholder
            requests.append({"name": f"{method} {route}", "method": method, "path": path})
        suite: dict[str, Any] = {
            "name": "checks from recent traffic",
            "timeout_s": 10,
            "repeat": 20,
            "warmup": 3,
            "thresholds": {"slower_pct": 10, "slower_ms": 2, "max_query_increase": 0},
        }
        if secret_headers:
            suite["headers"] = dict(sorted(secret_headers.items()))
        suite["requests"] = requests
        return 200, suite

    def _sample(self, route: str, method: str, filters: Any) -> tuple[str, set[str]] | None:
        """The most recent successful concrete path of a route (with its query, unless a
        value was redacted) and the lower-cased request header names that its last few
        successful requests carried."""
        try:
            page = self.store.list(filters(route=route, method=method), limit=10)
        except Exception:
            return None
        path_out: str | None = None
        names: set[str] = set()
        looked = 0
        for item in page.items:
            path = item.get("path")
            status = int(item.get("status") or 0)
            if not isinstance(path, str) or not 200 <= status < 400 or looked >= 3:
                continue
            trace = self.store.get(item["id"], include_profile=False)
            if trace is None:
                continue
            looked += 1
            request = trace.request or {}
            headers = request.get("headers") or {}
            if isinstance(headers, Mapping):
                names |= {str(k).lower() for k in headers}
            if path_out is not None:
                continue
            if not path.startswith("/") or path.startswith("//") or "{" in path:
                continue
            if _has_mask(path):
                continue
            query = request.get("query") or {}
            if isinstance(query, Mapping) and query:
                if _has_mask(query):
                    continue
                path = f"{path}?{urlencode(query, doseq=True)}"
            path_out = path
        return (path_out, names) if path_out is not None else None

    # runs ---------------------------------------------------------------------------
    def list_runs(self, params: Mapping[str, Any]) -> Response:
        from .api import _int_param, _str_param, error
        from .storage.base import FilterError

        try:
            limit = _int_param(params, "limit", 50, lo=1, hi=500)
        except FilterError as exc:
            return error(400, "invalid_filter", str(exc))
        kind = _str_param(params, "kind")
        if kind is not None and kind not in ("baseline", "compare"):
            return error(400, "invalid_filter", "kind must be baseline or compare.")
        items = self._ext("list_check_runs")(
            suite_id=_str_param(params, "suite_id"), limit=limit, kind=kind
        )
        return 200, {"items": items}

    def get_run(self, run_id: str) -> Response:
        from .api import error

        run = self._ext("get_check_run")(run_id)
        if run is None:
            return error(404, "check_run_not_found", f"No check run {run_id!r}.")
        from .check.sink import redact_suite

        results = self._ext("get_check_results")(run_id)
        out = dict(run)
        run["suite"] = out["suite"] = redact_suite(run.get("suite"), self.redactor)
        requests = [r["data"] for r in results]
        out["report"] = None
        if run["kind"] == "compare":
            baseline = run.get("baseline") or (
                {"id": run["baseline_id"], "label": None, "git": None, "created_at": None}
                if run.get("baseline_id")
                else None
            )
            out["report"] = {
                "format": "redline-check-report",
                "version": 1,
                "suite": run.get("suite"),
                "baseline": baseline,
                "run": {
                    "id": run["id"],
                    "git": run["git"],
                    "created_at": run["created_at"],
                    "base_url": run.get("base_url"),
                },
                "summary": run["summary"],
                "requests": requests,
            }
        else:  # the §19.3 per-request baseline entries
            out["requests"] = requests
        return 200, out

    def delete_run(self, run_id: str) -> Response:
        from .api import error

        run = self._ext("get_check_run")(run_id)
        if run is None:
            return error(404, "check_run_not_found", f"No check run {run_id!r}.")
        if run["status"] == "running":
            return error(409, "run_active", "Stop the check run before deleting it.")
        return 200, {"deleted": self._ext("delete_check_run")(run_id)}

    def stop_run(self, run_id: str) -> Response:
        from .api import error

        run = self._ext("get_check_run")(run_id)
        if run is None:
            return error(404, "check_run_not_found", f"No check run {run_id!r}.")
        if run["status"] != "running":
            return 200, {"stopped": False}
        _, stopper = self._hooks()
        if stopper is None:
            return error(501, "not_supported", "This store cannot stop check runs.")
        return 200, {"stopped": bool(stopper(run_id))}

    def start_run(self, body: Any, base_url: str | None) -> Response:
        from .api import error

        if not self.from_ui_allowed():
            return error(
                403,
                "checks_from_ui_disabled",
                "Starting check runs from the UI is off: it is on by default only in debug "
                "mode; set checks_from_ui = true to allow it (or run `redline check` from a "
                "terminal).",
            )
        parsed = self._body(body, MAX_SUITE_BODY)
        if not isinstance(parsed, Mapping):
            return parsed
        suite_id, kind = parsed.get("suite_id"), parsed.get("kind")
        baseline_id, label = parsed.get("baseline_id"), parsed.get("label")
        if not isinstance(suite_id, str) or not suite_id:
            return error(400, "invalid_body", "suite_id is required.")
        if kind not in ("baseline", "compare"):
            return error(400, "invalid_body", "kind must be 'baseline' or 'compare'.")
        if baseline_id is not None and not isinstance(baseline_id, str):
            return error(400, "invalid_body", "baseline_id must be a string.")
        if label is not None and not isinstance(label, str):
            return error(400, "invalid_body", "label must be a string.")
        rec = self._ext("get_check_suite")(suite_id)
        if rec is None:
            return error(404, "suite_not_found", f"No suite {suite_id!r}.")
        if kind == "compare":
            if baseline_id is None:
                latest = [
                    r
                    for r in self._ext("list_check_runs")(
                        suite_id=suite_id, limit=500, kind="baseline"
                    )
                    if r["status"] == "done"
                ]
                if not latest:
                    return error(
                        400, "invalid_body", "Record a baseline first (or pass baseline_id)."
                    )
                baseline_id = latest[0]["id"]
            base = self._ext("get_check_run")(baseline_id)
            if base is None:
                return error(404, "check_run_not_found", f"No baseline {baseline_id!r}.")
            if base["kind"] != "baseline" or base["status"] != "done":
                return error(400, "invalid_body", "baseline_id is not a finished baseline.")
        else:
            baseline_id = None
        running = [
            r
            for r in self._ext("list_check_runs")(suite_id=suite_id, limit=500)
            if r["status"] == "running"
        ]
        if running:
            return error(
                409, "run_active", "A check of this suite is in progress; wait for it or stop it."
            )
        launcher, _ = self._hooks()
        if launcher is None:
            return error(501, "not_supported", "This store cannot run checks in the background.")
        target = self.api._run_target(base_url)
        if target is None:
            return error(
                400,
                "invalid_body",
                "Cannot tell where to send the check: the server's listening port is unknown "
                "(e.g. a unix socket). Set the base_url setting (e.g. 'http://127.0.0.1:8000') "
                "to the application's own URL.",
            )
        try:
            run_id = launcher(
                rec["suite"],
                target,
                kind=kind,
                suite_id=suite_id,
                baseline_id=baseline_id,
                label=(label or "").strip()[:200] or None,
            )
        except ValueError as exc:
            return error(400, "invalid_suite", str(exc))
        return 202, {
            "id": run_id,
            "status": "running",
            "kind": kind,
            "suite_id": suite_id,
            "baseline_id": baseline_id,
        }

    # baselines ------------------------------------------------------------------------
    def import_baseline(self, body: Any, params: Mapping[str, Any]) -> Response:
        from .api import _str_param, error

        parsed = self._body(body, MAX_BASELINE_BODY)
        if not isinstance(parsed, Mapping):
            return parsed
        if parsed.get("format") != "redline-check-baseline":
            return error(400, "invalid_baseline", "Not a Redline baseline (format).")
        if parsed.get("version") != 1:
            return error(
                400, "invalid_baseline", f"Unsupported baseline version {parsed.get('version')!r}."
            )
        requests = parsed.get("requests")
        if not isinstance(requests, list) or not all(isinstance(r, Mapping) for r in requests):
            return error(400, "invalid_baseline", "requests must be a list of objects.")
        baseline_cls = _engine_baseline()
        if baseline_cls is not None:
            try:
                baseline_cls.from_dict(dict(parsed))
            except Exception as exc:
                return error(400, "invalid_baseline", str(exc) or type(exc).__name__)
        suite_id = _str_param(params, "suite_id")
        if suite_id is not None and self._ext("get_check_suite")(suite_id) is None:
            return error(404, "suite_not_found", f"No suite {suite_id!r}.")
        doc = redact_baseline(parsed, self.redactor)
        run_id = self._ext("import_check_baseline")(doc, suite_id)
        return 201, {"id": run_id, "kind": "baseline", "requests": len(requests)}


def redact_baseline(doc: Mapping[str, Any], redactor: Redactor) -> dict[str, Any]:
    """A baseline document with secrets masked (suite headers, URLs, response headers and
    bodies), as stored on import."""
    from .check.sink import redact_suite

    out = dict(doc)
    out["suite"] = redact_suite(out.get("suite"), redactor)
    for key in ("label",):
        if isinstance(out.get(key), str):
            out[key] = redactor.text(out[key])[:200]
    requests = []
    for req in doc.get("requests") or []:
        req = dict(req)
        if isinstance(req.get("path"), str):
            req["path"] = redactor.url(req["path"])
        response = req.get("response")
        if isinstance(response, Mapping):
            response = dict(response)
            if isinstance(response.get("headers"), Mapping):
                response["headers"] = redactor.headers(response["headers"])
            body = response.get("body")
            kind = response.get("body_kind")
            if kind == "json" and body is not None:
                if isinstance(body, str):
                    response["body"] = redactor.body(body, "application/json")[0]
                else:
                    response["body"] = redactor.mapping(body)
            elif isinstance(body, str):
                response["body"] = redactor.text(body)
            req["response"] = response
        requests.append(json.loads(json.dumps(req, default=str)))
    out["requests"] = requests
    return out
