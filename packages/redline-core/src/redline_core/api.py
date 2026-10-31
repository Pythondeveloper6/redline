"""Framework-neutral JSON API (``CONTRACT.md`` §5).

Every handler takes already-parsed input and returns ``(status, payload)`` where
``payload`` is JSON-serialisable. Adapters only translate their request object, call
:meth:`RedlineAPI.dispatch` (or a specific handler) and turn the tuple into a response::

    api = RedlineAPI(store, settings, framework="fastapi", explain_executor=factory)
    status, payload = api.dispatch("GET", "api/traces", params=dict(request.query_params))

Errors always have the shape ``{"error": {"code": str, "message": str}}``.

``explain_executor`` is a factory ``(db_alias, vendor) -> executor | None`` (see
:mod:`redline_core.explain`); returning ``None`` – or not passing a factory at all – makes
``POST api/explain`` answer 501 ``explain_unavailable``.

v0.2 (``CONTRACT.md`` §12) endpoints read the optional
:class:`~redline_core.storage.base.ExtendedStore` methods; a store without one (or raising
``NotImplementedError``) answers 501 ``not_supported``. ``GET api/traces/{id}/snapshot``
is the one non-JSON answer: ``dispatch`` returns a :class:`RawResponse`, which adapters send
as-is (it also unpacks as ``status, payload = ...`` with ``payload`` the response itself).
Load runs are started and stopped through the injectable ``run_launcher(config, base_url)
-> run_id`` and ``run_stopper(run_id) -> bool``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from redline_core.diagnostics import capture_failure

from . import __version__, ui
from .access import is_local_host, route_policy
from .conf import FEATURES, Settings, editor_url_template, resolve_release
from .explain import ExplainError, ExplainExecutor, run_explain
from .index_advisor import advise
from .model import ISSUE_KINDS, Trace
from .profiler import is_available as profiler_available
from .storage.base import FilterError, Filters, Store, parse_period, parse_window, percentile
from .storage.derive import fingerprint_key

__all__ = [
    "MAX_API_BODY",
    "ExecutorFactory",
    "RawResponse",
    "RedlineAPI",
    "Response",
    "RunLauncher",
    "RunStopper",
    "error",
    "own_base_url",
    "snapshot_html",
]

logger = logging.getLogger("redline")


def own_base_url(scheme: str | None, server: Any, root_path: str = "") -> str | None:
    """The application's own root URL for UI-started load runs, built from the port the
    server is *listening* on – ASGI ``scope["server"]`` ``(host, port)``, WSGI
    ``SERVER_PORT`` – never from the ``Host`` header (which a client chooses: it could aim a
    run at any other service on this machine).

    ``server`` is ``(host, port)``, a bare port, or ``None`` (unix socket / unknown →
    ``None``: the ``base_url`` setting is then required). The host is always loopback:
    ``127.0.0.1``, or ``[::1]`` when the server listens on an IPv6 address.
    """
    host: Any = None
    port: Any = server
    if isinstance(server, (tuple, list)):
        if len(server) != 2:
            return None
        host, port = server
    try:
        number = int(port)
    except (TypeError, ValueError):
        return None
    if not 0 < number < 65536:
        return None
    loopback = "[::1]" if isinstance(host, str) and ":" in host else "127.0.0.1"
    scheme = (scheme or "http").lower()
    if scheme not in ("http", "https"):
        scheme = "http"
    return f"{scheme}://{loopback}:{number}{(root_path or '').rstrip('/')}"


Response = tuple[int, Any]
ExecutorFactory = Callable[[str, str], "ExplainExecutor | None"]
#: ``run_launcher(config, base_url) -> run_id``: start a load run against the app's own base
#: URL (given by the adapter, ``None`` when it could not tell). ``ValueError`` → 400.
RunLauncher = Callable[[dict[str, Any], "str | None"], str]
#: ``run_stopper(run_id) -> bool``: ask a running run to stop; ``False`` if it was not running.
RunStopper = Callable[[str], bool]

_ID = r"([A-Za-z0-9_-]{1,64})"
_TRACE = re.compile(rf"^api/traces/{_ID}$")
_PROFILE = re.compile(rf"^api/traces/{_ID}/profile$")
_SNAPSHOT = re.compile(rf"^api/traces/{_ID}/snapshot$")
_RUN = re.compile(rf"^api/runs/{_ID}$")
_RUN_BOTTLENECKS = re.compile(rf"^api/runs/{_ID}/bottlenecks$")
_RUN_STOP = re.compile(rf"^api/runs/{_ID}/stop$")
_ISSUE_KINDS = ISSUE_KINDS
_BUCKETS = {"1h": 3_600.0, "1d": 86_400.0}
_LOAD_MODES = ("constant", "ramp", "steps")
#: Largest request body adapters should hand to :meth:`RedlineAPI.dispatch` (an imported
#: check baseline may be 10 MiB; the core answers 413 ``payload_too_large`` beyond it).
MAX_API_BODY = 11 * 1024 * 1024
#: ``GET api/indexes`` answers are cached this long per ``(window, db)``.
INDEX_CACHE_S = 30.0


class _NotSupported(Exception):
    pass


@dataclass
class RawResponse:
    """A non-JSON answer (the snapshot page). Adapters send ``body`` with ``content_type``
    and ``headers`` as-is."""

    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def __iter__(self) -> Iterator[Any]:
        # ``status, payload = api.dispatch(...)`` keeps working: payload is this object.
        return iter((self.status, self))


def error(status: int, code: str, message: str) -> Response:
    """An error response in the contract's shape."""
    return status, {"error": {"code": code, "message": message}}


class RedlineAPI:
    """The JSON API over one store."""

    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        *,
        framework: str = "other",
        explain_executor: ExecutorFactory | None = None,
        run_launcher: RunLauncher | None = None,
        run_stopper: RunStopper | None = None,
        config: Any = None,
        access: Any = None,
        check_launcher: Callable[..., str] | None = None,
        check_stopper: RunStopper | None = None,
        debug: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or Settings()
        #: :class:`~redline_core.config.ResolvedConfig` behind ``GET api/config``.
        self.config = config
        #: :class:`~redline_core.access.Access` (meta's ``access`` block, runtime warnings).
        self.access = access
        self.framework = framework
        self.explain_executor = explain_executor
        self.run_launcher = run_launcher
        self.run_stopper = run_stopper
        self._index_cache: dict[tuple[float, str], tuple[float, dict[str, Any]]] = {}
        self._index_lock = threading.Lock()
        #: The framework's debug flag (``checks_from_ui="auto"``); default: the gate's.
        self.debug = debug
        from .api_checks import CheckRoutes

        #: ``api/checks`` (§19.5). Without injected hooks they are built on first use from
        #: the store (:func:`redline_core.check.control.check_hooks`).
        self.checks = CheckRoutes(self, check_launcher, check_stopper)

    def debug_mode(self) -> bool:
        """Whether the framework is in debug mode right now (``debug`` callable, else the
        access gate's ``debug``); errors count as "not debug"."""
        fn = self.debug
        if fn is None and self.access is not None:
            fn = getattr(self.access, "debug", None)
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    # routing ---------------------------------------------------------------
    def dispatch(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        body: bytes | str | Mapping[str, Any] | None = None,
        *,
        base_url: str | None = None,
    ) -> Response | RawResponse:
        """Route ``method`` + ``path`` (relative to the mount, e.g. ``api/traces``).

        ``base_url`` is the application's own root URL (``POST api/runs`` targets it).
        """
        method = method.upper()
        path = path.strip("/")
        params = params or {}
        refusal = route_policy(method, path, self.settings)
        if refusal is not None:  # also enforced by the adapters' access check
            return refusal.status, refusal.body()
        try:
            return self._route(method, path, params, body, base_url)
        except (_NotSupported, NotImplementedError) as exc:
            return error(501, "not_supported", str(exc) or "This store does not support it.")
        except Exception as exc:  # the profiler UI must never take the app down
            capture_failure("Redline API processing failed")
            from .redact import Redactor

            # driver errors can quote a DSN or a secret value
            detail = Redactor(self.settings).text(str(exc)[:2000])[:500]
            return error(500, "internal_error", f"{type(exc).__name__}: {detail}")

    def _route(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any],
        body: bytes | str | Mapping[str, Any] | None,
        base_url: str | None,
    ) -> Response | RawResponse:
        if path.startswith("api/analysis/"):
            from .api_analysis import route

            return route(self, method, path, params, body)
        if path == "api/suppressions" or path.startswith("api/suppressions/"):
            return self.suppression_routes(method, path, body)
        if path == "api/metrics":
            return self.metrics() if method == "GET" else _not_allowed()
        get_routes: dict[str, Callable[[Mapping[str, Any]], Response]] = {
            "api/compare": self.compare,
            "api/sql": self.sql,
            "api/indexes": self.indexes,
            "api/cache": self.cache,
            "api/external": self.external,
            "api/history": self.history,
            "api/history/compare": self.history_compare,
            "api/releases": self.releases,
            "api/lifecycle": self.lifecycle,
            "api/jobs/summary": self.job_summary,
        }
        if path in get_routes:
            return get_routes[path](params) if method == "GET" else _not_allowed()
        if path == "api/checks" or path.startswith("api/checks/"):
            return self.checks.route(method, path, params, body, base_url)
        if path == "api/runs":
            if method == "GET":
                return self.list_runs(params)
            if method == "POST":
                return self.start_run(body, base_url)
            return _not_allowed()
        if m := _RUN_BOTTLENECKS.match(path):
            return self.run_bottlenecks(m.group(1)) if method == "GET" else _not_allowed()
        if m := _RUN_STOP.match(path):
            return self.stop_run(m.group(1)) if method == "POST" else _not_allowed()
        if m := _RUN.match(path):
            if method == "GET":
                return self.get_run(m.group(1))
            if method == "DELETE":
                return self.delete_run(m.group(1))
            return _not_allowed()
        if m := _SNAPSHOT.match(path):
            return self.snapshot(m.group(1)) if method == "GET" else _not_allowed()
        return self._route_v01(method, path, params, body)

    def _route_v01(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any],
        body: bytes | str | Mapping[str, Any] | None,
    ) -> Response:
        if path == "api/meta":
            return self.meta() if method == "GET" else _not_allowed()
        if path == "api/config":
            return self.effective_config() if method == "GET" else _not_allowed()
        if path == "api/traces":
            if method == "GET":
                return self.list_traces(params)
            if method == "DELETE":
                return self.clear()
            return _not_allowed()
        if m := _PROFILE.match(path):
            return self.get_profile(m.group(1)) if method == "GET" else _not_allowed()
        if m := _TRACE.match(path):
            if method == "GET":
                return self.get_trace(m.group(1))
            if method == "DELETE":
                return self.delete_trace(m.group(1))
            return _not_allowed()
        if path == "api/endpoints":
            return self.endpoints(params) if method == "GET" else _not_allowed()
        if path == "api/issues":
            return self.issues(params) if method == "GET" else _not_allowed()
        if path == "api/explain":
            return self.explain(body) if method == "POST" else _not_allowed()
        return error(404, "not_found", f"No API endpoint at {path!r}.")

    # handlers --------------------------------------------------------------
    def meta(self) -> Response:
        settings = self.settings.public_dict()
        settings["profiler_available"] = profiler_available()
        return 200, {
            "version": __version__,
            "framework": self.framework,
            "project_root": self.settings.project_root or os.getcwd(),
            "editor": self.settings.editor,
            "editor_url_template": editor_url_template(self.settings),
            "release": resolve_release(self.settings),
            "features": self._features(),
            "access": self._access_meta(),
            "settings": settings,
            "storage": {
                "rows": self.store.count(),
                "path": getattr(self.store, "path", None),
                "dropped": getattr(self.store, "dropped", 0),
            },
        }

    def metrics(self) -> RawResponse:
        """``GET api/metrics``: recorder health in the Prometheus text exposition format."""
        from .metrics import render_metrics

        return RawResponse(
            200,
            "text/plain; version=0.0.4; charset=utf-8",
            render_metrics(self.store, self._recorder()).encode("utf-8"),
            {"Cache-Control": "no-store"},
        )

    def _recorder(self) -> Any:
        from .recorder import get_recorder

        try:
            recorder = get_recorder()
        except Exception:
            return None
        return recorder if recorder.store is self.store else None

    def _features(self) -> dict[str, bool]:
        s = self.settings
        features = {name: s.feature(name) for name in FEATURES}
        features["load_from_ui"] = s.load_from_ui and s.feature("load") and not s.read_only
        features["checks_from_ui"] = self.checks.from_ui_allowed()
        return features

    def _access_meta(self) -> dict[str, Any]:
        if self.access is not None:
            return self.access.meta()
        s = self.settings
        login = s.access if s.access in ("token", "basic") else None
        return {"policy": s.access, "login": login, "read_only": s.read_only, "preset": s.preset}

    def effective_config(self) -> Response:
        """``GET api/config``: effective settings (secrets as ``"***"``), sources, warnings."""
        if self.config is None:
            from .config import ResolvedConfig, config_warnings

            sources = dict.fromkeys(Settings.field_names(), "framework")
            config = ResolvedConfig(
                self.settings, sources, warnings=tuple(config_warnings(self.settings))
            )
        else:
            config = self.config
        extra = self.access.runtime_warnings() if self.access is not None else []
        return 200, config.public(extra)

    def _visible(self, data: dict[str, Any]) -> dict[str, Any]:
        """Drop logs/locals from a trace dict when those features are switched off."""
        if not self.settings.feature("logs"):
            data["logs"] = []
            data["logs_dropped"] = 0
        if not self.settings.feature("locals") and isinstance(data.get("error"), dict):
            frames = data["error"].get("frames")
            if isinstance(frames, list):
                data["error"] = {
                    **data["error"],
                    "frames": [
                        {k: v for k, v in f.items() if k != "locals"} if isinstance(f, dict) else f
                        for f in frames
                    ],
                }
        return data

    def list_traces(self, params: Mapping[str, Any]) -> Response:
        try:
            filters = Filters.from_params(params)
            limit = _int_param(params, "limit", 50, lo=1, hi=500)
            cursor = _str_param(params, "cursor")
            page = self.store.list(filters, limit=limit, cursor=cursor)
        except FilterError as exc:
            return error(400, "invalid_filter", str(exc))
        return 200, page.to_dict()

    def get_trace(self, trace_id: str) -> Response:
        trace = self.store.get(trace_id, include_profile=False)
        if trace is None:
            return error(404, "trace_not_found", f"No trace {trace_id!r}.")
        data = self._visible(trace.to_dict(include_profile=False))
        data["has_profile"] = self.store.has_profile(trace_id)
        from .diagnostics import trace_diagnostics

        data["diagnostics"] = trace_diagnostics(trace)
        data["diagnostics"]["profile_available"] = data["has_profile"]
        return 200, data

    def get_profile(self, trace_id: str) -> Response:
        profile = self.store.get_profile(trace_id)
        if profile is None:
            return error(404, "profile_not_found", f"No profile for trace {trace_id!r}.")
        return 200, profile

    def delete_trace(self, trace_id: str) -> Response:
        return 200, {"deleted": self.store.delete(trace_id)}

    def clear(self) -> Response:
        return 200, {"deleted": self.store.clear()}

    def endpoints(self, params: Mapping[str, Any]) -> Response:
        try:
            window = parse_window(_str_param(params, "window"))
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        group_by = _str_param(params, "group_by")
        if not group_by:
            return 200, {"items": [e.to_dict() for e in self.store.endpoints(window)]}
        try:
            stats = self.store.endpoints(window, group_by=group_by)  # type: ignore[call-arg]
        except ValueError as exc:
            return error(400, "invalid_group_by", str(exc))
        except TypeError:  # a custom store without grouping
            return error(501, "not_supported", "This store cannot group endpoints.")
        return 200, {"group_by": group_by, "items": [e.to_dict() for e in stats]}

    def suppression_routes(self, method, path, body):
        from .suppressions import validate

        list_rules = getattr(self.store, "list_suppressions", None)
        if not callable(list_rules):
            return error(501, "not_supported", "This store does not support suppressions.")
        if path == "api/suppressions" and method == "GET":
            return 200, {
                "items": list_rules(),
                "ci_effect": "none; test and check policies still inspect raw findings",
            }
        if path == "api/suppressions" and method == "POST":
            try:
                if isinstance(body, (bytes, str)) and len(body) > 8192:
                    raise ValueError("Suppression input is too large")
                data = json.loads(body) if isinstance(body, (bytes, str)) else body
                rule = validate(data)
                from .redact import Redactor

                rule["reason"] = Redactor(self.settings).text(rule["reason"])
                self.store.save_suppression(rule)
                return 201, rule
            except ValueError as exc:
                return error(400, "invalid_suppression", str(exc))
        match = re.fullmatch(r"api/suppressions/([a-f0-9]{32})", path)
        if match and method == "DELETE":
            self.store.delete_suppression(match[1])
            return 200, {"deleted": True}
        return _not_allowed()

    def issues(self, params: Mapping[str, Any]) -> Response:
        try:
            window = parse_window(_str_param(params, "window"))
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        kind = _str_param(params, "kind")
        if kind is not None and kind not in _ISSUE_KINDS:
            return error(400, "invalid_kind", f"kind must be one of {', '.join(_ISSUE_KINDS)}.")
        from .suppressions import matches

        items = [g.to_dict() for g in self.store.issues(window, kind=kind)]
        rules = getattr(self.store, "list_suppressions", lambda: [])()
        for item in items:
            site = item.get("callsite")
            location = f"{site['file']}:{site['line']}" if site else None
            item["suppressed_routes"] = [
                route
                for route in item["routes"]
                if any(
                    matches(
                        rule,
                        kind=item["kind"],
                        route=route,
                        fingerprint=item.get("fingerprint"),
                        location=location,
                    )
                    for rule in rules
                )
            ]
            item["suppressed"] = bool(item["routes"]) and len(item["suppressed_routes"]) == len(
                item["routes"]
            )
        return 200, {"items": items, "sampled": self.settings.sample_rate < 1}

    def explain(self, body: bytes | str | Mapping[str, Any] | None) -> Response:
        if isinstance(body, (bytes, bytearray, str)):
            try:
                body = json.loads(body or "{}")
            except ValueError:
                return error(400, "invalid_json", "The request body is not valid JSON.")
        if not isinstance(body, Mapping):
            return error(400, "invalid_body", "Expected a JSON object.")
        trace_id, span_id = body.get("trace_id"), body.get("span_id")
        if not isinstance(trace_id, str) or not isinstance(span_id, str):
            return error(400, "invalid_body", "trace_id and span_id are required strings.")
        analyze = bool(body.get("analyze", False))
        if analyze and self.settings.read_only:
            return error(403, "read_only", "This Redline instance is read-only (no ANALYZE).")
        if analyze and not self.settings.explain_analyze:
            return error(
                403,
                "analyze_disabled",
                "EXPLAIN ANALYZE executes the query; enable it with the explain_analyze setting.",
            )
        trace = self.store.get(trace_id, include_profile=False)
        if trace is None:
            return error(404, "trace_not_found", f"No trace {trace_id!r}.")
        if trace.tags.get("secondary_store"):
            return error(
                403,
                "secondary_store",
                (
                    "Open EXPLAIN in the worker’s own Redline instance; this UI cannot "
                    "verify its database binding."
                ),
            )
        span = trace.span(span_id)
        if span is None:
            return error(404, "span_not_found", f"No span {span_id!r} in trace {trace_id!r}.")
        if span.data.get("auto_engine"):
            return error(
                501,
                "unregistered_engine",
                "Register this engine with the application before database inspection; "
                "its recorded alias is observation-only.",
            )
        if span.kind != "sql" or not span.data.get("explainable"):
            return error(
                400, "not_explainable", "Only single read-only SELECT queries can be explained."
            )
        vendor = str(span.data.get("vendor", "other"))
        alias = str(span.data.get("db_alias", "default"))
        executor = self.explain_executor(alias, vendor) if self.explain_executor else None
        if executor is None:
            return error(
                501,
                "explain_unavailable",
                f"EXPLAIN is not available for database {alias!r} ({vendor}).",
            )
        try:
            result = run_explain(
                str(span.data.get("sql", "")),
                span.data.get("params"),
                vendor,
                executor,
                analyze=analyze,
                allow_analyze=self.settings.explain_analyze,
            )
        except ExplainError as exc:
            return error(exc.status, exc.code, exc.message)
        return 200, result.to_dict()

    # v0.2 ------------------------------------------------------------------
    def _ext(self, name: str) -> Callable[..., Any]:
        """An optional store method, or 501 ``not_supported`` when the store lacks it."""
        fn = getattr(self.store, name, None)
        if not callable(fn):
            raise _NotSupported(f"This store does not support {name}().")

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except NotImplementedError:
                raise _NotSupported(f"This store does not support {name}().") from None

        return call

    def poll(
        self,
        since: float,
        last_id: str | None = None,
        limit: int = 100,
        after_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """Trace summaries newer than the cursor, oldest first (SSE live tail).

        The cursor is ``after_seq`` (insertion order; every summary carries its ``seq``)
        or, for the initial position, ``(since, last_id)``. Uses ``store.traces_since``; a
        store without it falls back to a ``since`` list.
        """
        fn = getattr(self.store, "traces_since", None)
        if callable(fn):
            try:
                if after_seq is not None:
                    return list(fn(since, last_id or "", limit, after_seq=after_seq))
                return list(fn(since, last_id or "", limit))
            except NotImplementedError:
                pass
        return list(reversed(self.store.list(Filters(since=since), limit=limit).items))

    def snapshot(self, trace_id: str) -> Response | RawResponse:
        trace = self.store.get(trace_id, include_profile=True)
        if trace is None:
            return error(404, "trace_not_found", f"No trace {trace_id!r}.")
        data = self._visible(trace.to_dict(include_profile=False))
        profile = trace.profile if self.settings.feature("profile") else None
        data["has_profile"] = profile is not None
        page = snapshot_html({"trace": data, "profile": profile})
        return RawResponse(
            200,
            "text/html; charset=utf-8",
            page.encode("utf-8"),
            {"Content-Disposition": f'attachment; filename="redline-{trace.id}.html"'},
        )

    def compare(self, params: Mapping[str, Any]) -> Response:
        a_id, b_id = _str_param(params, "a"), _str_param(params, "b")
        if not a_id or not b_id:
            return error(400, "invalid_filter", "Both a and b (trace ids) are required.")
        traces: list[Trace] = []
        for tid in (a_id, b_id):
            trace = self.store.get(tid, include_profile=False)
            if trace is None:
                return error(404, "trace_not_found", f"No trace {tid!r}.")
            traces.append(trace)
        a, b = traces
        summaries = []
        for t in traces:
            s = t.summary()
            s["has_profile"] = self.store.has_profile(t.id)
            summaries.append(s)
        by_kind: dict[str, dict[str, float]] = defaultdict(
            lambda: {"a_ms": 0.0, "b_ms": 0.0, "a_count": 0, "b_count": 0}
        )
        sql: dict[str, dict[str, Any]] = {}
        for side, t in (("a", a), ("b", b)):
            for span in t.spans:
                k = by_kind[span.kind]
                k[f"{side}_ms"] += span.duration_ms
                k[f"{side}_count"] += 1
                if span.kind == "sql":
                    fp = span.data.get("fingerprint") or span.name
                    key = fingerprint_key(fp)
                    row = sql.setdefault(
                        key,
                        {"fingerprint": key, "sql": fp, "a_count": 0, "b_count": 0,
                         "a_ms": 0.0, "b_ms": 0.0},
                    )  # fmt: skip
                    row[f"{side}_count"] += 1
                    row[f"{side}_ms"] += span.duration_ms
        for k in by_kind.values():
            k["a_ms"], k["b_ms"] = round(k["a_ms"], 3), round(k["b_ms"], 3)
        sql_rows = sorted(
            sql.values(),
            key=lambda r: (-abs(r["b_ms"] - r["a_ms"]), -abs(r["b_count"] - r["a_count"])),
        )
        for r in sql_rows:
            r["a_ms"], r["b_ms"] = round(r["a_ms"], 3), round(r["b_ms"], 3)
        ia = {i.id: i for i in a.issues}
        ib = {i.id: i for i in b.issues}

        def brief(i: Any) -> dict[str, Any]:
            return {"id": i.id, "kind": i.kind, "severity": i.severity, "title": i.title}

        return 200, {
            "a": summaries[0],
            "b": summaries[1],
            "delta": {
                "duration_ms": round(b.duration_ms - a.duration_ms, 3),
                "query_count": b.query_count - a.query_count,
                "query_ms": round(b.query_ms - a.query_ms, 3),
            },
            "by_kind": dict(by_kind),
            "sql": sql_rows,
            "issues": {
                "only_a": [brief(i) for k, i in ia.items() if k not in ib],
                "only_b": [brief(i) for k, i in ib.items() if k not in ia],
                "both": [brief(i) for k, i in ib.items() if k in ia],
            },
        }

    def sql(self, params: Mapping[str, Any]) -> Response:
        try:
            window = parse_window(_str_param(params, "window"))
            limit = _int_param(params, "limit", 100, lo=1, hi=500)
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        items = self._ext("search_sql")(_str_param(params, "q"), window, limit)
        return 200, {"items": [s.to_dict() for s in items]}

    def indexes(self, params: Mapping[str, Any]) -> Response:
        try:
            window = parse_window(_str_param(params, "window"))
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        db = _str_param(params, "db") or "default"
        key = (window, db)
        now = time.monotonic()
        with self._index_lock:
            hit = self._index_cache.get(key)
        if hit is not None and hit[0] > now and _str_param(params, "refresh") not in ("1", "true"):
            return 200, {**hit[1], "cached": True}
        workload = self._ext("sql_workload")(window)
        payload = advise(
            workload,
            get_trace=lambda tid: self.store.get(tid, include_profile=False),
            db=db,
            executor_factory=self.explain_executor,
        )
        payload["window"] = window
        with self._index_lock:
            self._index_cache = {k: v for k, v in self._index_cache.items() if v[0] > now}
            self._index_cache[key] = (now + INDEX_CACHE_S, payload)
        return 200, {**payload, "cached": False}

    def cache(self, params: Mapping[str, Any]) -> Response:
        try:
            window = parse_window(_str_param(params, "window"))
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        return 200, {"items": [c.to_dict() for c in self._ext("cache_stats")(window)]}

    def external(self, params: Mapping[str, Any]) -> Response:
        try:
            window = parse_window(_str_param(params, "window"))
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        return 200, {"items": [e.to_dict() for e in self._ext("external_stats")(window)]}

    def lifecycle(self, params: Mapping[str, Any]) -> Response:
        try:
            window = parse_window(_str_param(params, "window"))
            limit = _int_param(params, "limit", 200, lo=1, hi=500)
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        items = self._ext("lifecycle")(window, limit)
        return 200, {"items": items, "window": window}

    def job_summary(self, params: Mapping[str, Any]) -> Response:
        """Outcome distribution and first failing stage for retained task recordings."""
        try:
            window = parse_window(_str_param(params, "window"))
            limit = _int_param(params, "limit", 200, lo=1, hi=500)
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        page = self.store.list(
            Filters(method="TASK", since=time.time() - window),
            limit=limit,
        )
        jobs: dict[str, dict[str, Any]] = {}
        totals: dict[str, int] = {}
        stages: dict[str, int] = {}
        unattributed = 0
        for item in page.items:
            tags = item.get("tags") or {}
            name = str(tags.get("job.name") or item.get("path") or "(unknown)")
            outcome = str(tags.get("job.outcome") or "unknown")
            row = jobs.setdefault(
                name,
                {
                    "name": name,
                    "queue": str(tags.get("job.queue") or "unknown"),
                    "total": 0,
                    "outcomes": {},
                    "first_failing_stages": {},
                    "durations": [],
                    "last_started_at": 0.0,
                },
            )
            row["total"] += 1
            row["outcomes"][outcome] = row["outcomes"].get(outcome, 0) + 1
            row["durations"].append(float(item.get("duration_ms") or 0.0))
            row["last_started_at"] = max(
                row["last_started_at"], float(item.get("started_at") or 0.0)
            )
            totals[outcome] = totals.get(outcome, 0) + 1
            if outcome == "failed":
                stage = tags.get("job.first_failing_stage")
                if stage:
                    row["first_failing_stages"][stage] = (
                        row["first_failing_stages"].get(stage, 0) + 1
                    )
                    stages[str(stage)] = stages.get(str(stage), 0) + 1
                else:
                    unattributed += 1
        result = []
        for row in jobs.values():
            durations = sorted(row.pop("durations"))
            row["p50_ms"] = round(percentile(durations, 50), 3)
            row["p95_ms"] = round(percentile(durations, 95), 3)
            result.append(row)
        result.sort(key=lambda job: (-job["total"], job["name"]))
        return 200, {
            "window": window,
            "sampled": len(page.items),
            "total": page.total,
            "truncated": page.total > len(page.items),
            "outcomes": dict(sorted(totals.items())),
            "first_failing_stages": dict(sorted(stages.items(), key=lambda kv: -kv[1])),
            "unattributed_failures": unattributed,
            "jobs": result,
            "limitation": (
                "Only retained task recordings inside the window; missing jobs do not "
                "prove no jobs ran. Stage names are bounded labels, not arguments."
            ),
        }

    def history(self, params: Mapping[str, Any]) -> Response:
        try:
            window = parse_window(_str_param(params, "window"), default="30d")
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        bucket_text = _str_param(params, "bucket") or ("1h" if window <= 2 * 86_400 else "1d")
        if bucket_text not in _BUCKETS:
            return error(400, "invalid_filter", "bucket must be 1h or 1d.")
        method = _str_param(params, "method")
        end = time.time()
        result = self._ext("history")(
            _str_param(params, "route"),
            method.upper() if method else None,
            end - window,
            end,
            _BUCKETS[bucket_text],
        )
        return 200, result.to_dict()

    def releases(self, params: Mapping[str, Any]) -> Response:
        return 200, {"items": [r.to_dict() for r in self._ext("releases")()]}

    def history_compare(self, params: Mapping[str, Any]) -> Response:
        try:
            a = parse_period(_str_param(params, "a"))
            b = parse_period(_str_param(params, "b"))
        except FilterError as exc:
            return error(400, "invalid_period", str(exc))
        return 200, self._ext("compare_periods")(a, b).to_dict()

    def list_runs(self, params: Mapping[str, Any]) -> Response:
        try:
            limit = _int_param(params, "limit", 50, lo=1, hi=500)
        except FilterError as exc:
            return error(400, "invalid_filter", str(exc))
        return 200, {"items": [r.to_dict() for r in self._ext("list_runs")(limit)]}

    def get_run(self, run_id: str) -> Response:
        run = self._ext("get_run")(run_id)
        if run is None:
            return error(404, "run_not_found", f"No run {run_id!r}.")
        return 200, run.to_dict()

    def run_bottlenecks(self, run_id: str) -> Response:
        result = self._ext("run_bottlenecks")(run_id)
        if result is None:
            return error(404, "run_not_found", f"No run {run_id!r}.")
        return 200, result.to_dict()

    def delete_run(self, run_id: str) -> Response:
        run = self._ext("get_run")(run_id)
        if run is None:
            return error(404, "run_not_found", f"No run {run_id!r}.")
        if run.status == "running":
            return error(409, "run_active", "Stop the run before deleting it.")
        return 200, {"deleted": self._ext("delete_run")(run_id)}

    def start_run(
        self, body: bytes | str | Mapping[str, Any] | None, base_url: str | None
    ) -> Response:
        if not self.settings.load_from_ui:
            return error(
                403,
                "load_disabled",
                "Starting load runs from the UI is off; enable the load_from_ui setting.",
            )
        parsed = _json_body(body)
        if not isinstance(parsed, Mapping):
            return parsed
        config, problem = _run_config(parsed)
        if problem:
            return error(400, "invalid_body", problem)
        if self.run_launcher is None:
            return error(501, "not_supported", "This adapter cannot start load runs.")
        target = self._run_target(base_url)
        if target is None:
            return error(
                400,
                "invalid_body",
                "Cannot tell where to send the load run: the server's listening port is "
                "unknown (e.g. a unix socket). Set the base_url setting (e.g. "
                "'http://127.0.0.1:8000') to the application's own URL.",
            )
        try:
            run_id = self.run_launcher(config, target)
        except ValueError as exc:
            return error(400, "invalid_body", str(exc))
        return 202, {"id": run_id, "name": config["name"], "status": "running"}

    def _run_target(self, derived: str | None) -> str | None:
        """Where a UI-started run goes: the ``base_url`` setting, else the URL the adapter
        derived from the server's own listening port (:func:`own_base_url`) – only when its
        host is loopback or in ``allowed_hosts``. The ``Host`` header is never used: it
        could aim a load test at another machine or another local service."""
        if self.settings.base_url:
            return self.settings.base_url.rstrip("/")
        if not derived:
            return None
        parts = urlsplit(derived)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        if "@" in parts.netloc or not is_local_host(parts.netloc, self.settings.allowed_hosts):
            return None
        return derived.rstrip("/")

    def stop_run(self, run_id: str) -> Response:
        get_run = getattr(self.store, "get_run", None)
        if callable(get_run):
            try:
                if get_run(run_id) is None:
                    return error(404, "run_not_found", f"No run {run_id!r}.")
            except NotImplementedError:
                pass
        if self.run_stopper is None:
            return error(501, "not_supported", "This adapter cannot stop load runs.")
        return 200, {"stopped": bool(self.run_stopper(run_id))}


def _json_body(body: bytes | str | Mapping[str, Any] | None) -> Mapping[str, Any] | Response:
    if isinstance(body, (bytes, bytearray, str)):
        try:
            body = json.loads(body or "{}")
        except ValueError:
            return error(400, "invalid_json", "The request body is not valid JSON.")
    if not isinstance(body, Mapping):
        return error(400, "invalid_body", "Expected a JSON object.")
    return body


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _run_config(body: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Validate a ``POST api/runs`` body; returns ``(config, problem)``.

    Request paths must be absolute paths on the app (``/...``): the target host is always
    the application's own base URL, never something the browser chose.
    """
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return {}, "name is required."
    requests = body.get("requests")
    if not isinstance(requests, list) or not requests:
        return {}, "requests must be a non-empty list."
    clean = []
    for r in requests:
        if not isinstance(r, Mapping):
            return {}, "each request must be an object."
        method, path = r.get("method", "GET"), r.get("path")
        if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z]{1,10}", method):
            return {}, "request method must be an HTTP method."
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            return {}, "request path must start with a single '/'."
        weight = _number(r.get("weight", 1))
        if weight is None or weight <= 0:
            return {}, "request weight must be a positive number."
        clean.append({**r, "method": method.upper(), "path": path, "weight": weight})
    profile = body.get("profile")
    if not isinstance(profile, Mapping) or profile.get("mode") not in _LOAD_MODES:
        return {}, f"profile.mode must be one of {', '.join(_LOAD_MODES)}."
    duration = _number(profile.get("duration_s"))
    if duration is None or duration <= 0:
        return {}, "profile.duration_s must be a positive number."
    needed = {"constant": ("rps",), "ramp": ("start_rps", "end_rps"),
              "steps": ("start_rps", "end_rps", "step_rps")}[profile["mode"]]  # fmt: skip
    for key in needed:
        value = _number(profile.get(key))
        if value is None or value <= 0:
            return {}, f"profile.{key} must be a positive number for mode {profile['mode']!r}."
    # The target is never taken from the body: the adapter's base URL always wins.
    extra = {k: v for k, v in body.items() if k not in ("base_url", "workers")}
    config = {**extra, "name": name.strip(), "requests": clean, "profile": dict(profile)}
    return config, None


_ASSET_REF = re.compile(
    r"""<(script|link)\b[^>]*?\b(?:src|href)=["']\./(assets/[^"']+)["'][^>]*>(?:</script>)?"""
)
# Quoted ``"./chunk.js"`` / ``'./chunk.js'``, or a template literal without substitutions
# (Vite 8 writes lazy imports as ``import(`./chunk.js`)``).
_CHUNK_REF = re.compile(r"""(["'`])\./([A-Za-z0-9_.-]+\.m?js)\1""")
_CSS_URL = re.compile(r"""url\((["']?)\./([^"')]+)\1\)""")


def _script_json(value: Any) -> str:
    """JSON that is safe inside ``<script>``: no ``</script>``, ``<!--`` or line separators."""
    text = json.dumps(value, default=str, ensure_ascii=False)
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _data_url(path: Any) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{ui.content_type(path).split(';')[0]};base64,{data}"


def snapshot_html(snapshot: Any) -> str:
    """A self-contained copy of the UI with ``window.__REDLINE_SNAPSHOT__ = snapshot``.

    The stylesheet is inlined as ``<style>`` (its ``url(./...)`` assets as data URLs). The
    JS chunks are rewritten to import each other through bare ``@redline/<chunk>``
    specifiers, which an import map resolves to ``data:`` URLs; that keeps Vite's lazily
    loaded chunks (and their circular imports of the entry chunk) working from a file on
    disk with no server. The snapshot global is set by a classic script placed before the
    import map and the module scripts, so it exists before the app runs.
    """
    html = ui.index_html()
    entries: list[str] = []
    styles: list[str] = []

    def replace(m: re.Match[str]) -> str:
        tag, rel = m.group(1), m.group(2)
        path = ui.asset_path(rel)
        if path is None:
            return m.group(0)
        if tag == "script" and rel.endswith((".js", ".mjs")):
            entries.append(rel.rsplit("/", 1)[-1])
            return ""
        if tag == "link" and "modulepreload" in m.group(0):
            return ""
        if tag == "link" and rel.endswith(".css"):
            css = _CSS_URL.sub(
                lambda u: (
                    f"url({_data_url(p)})"
                    if (p := ui.asset_path("assets/" + u.group(2))) is not None
                    else u.group(0)
                ),
                path.read_text(encoding="utf-8"),
            )
            styles.append("<style>" + css.replace("</style", "<\\/style") + "</style>")
            return ""
        return m.group(0)

    html = _ASSET_REF.sub(replace, html)
    modules: dict[str, str] = {}
    pending = list(entries)
    while pending:  # every chunk reachable from the entries, lazily loaded ones included
        name = pending.pop()
        if name in modules:
            continue
        path = ui.asset_path("assets/" + name)
        if path is None:
            continue
        text = path.read_text(encoding="utf-8")
        for m in _CHUNK_REF.finditer(text):
            pending.append(m.group(2))
        modules[name] = text
    imports = {}
    for name, text in modules.items():
        text = _CHUNK_REF.sub(
            lambda m: (
                f"{m.group(1)}@redline/{m.group(2)}{m.group(1)}"
                if m.group(2) in modules
                else m.group(0)
            ),
            text,
        )
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        imports[f"@redline/{name}"] = f"data:text/javascript;base64,{encoded}"
    head = [f"<script>window.__REDLINE_SNAPSHOT__ = {_script_json(snapshot)};</script>"]
    head += styles
    if imports:
        head.append(f'<script type="importmap">{_script_json({"imports": imports})}</script>')
    head += [f'<script type="module">import "@redline/{n}";</script>' for n in entries]
    block = "\n".join(head) + "\n"
    if "</head>" in html:
        return html.replace("</head>", block + "</head>", 1)
    return block + html


def _not_allowed() -> Response:
    return error(405, "method_not_allowed", "Method not allowed.")


def _str_param(params: Mapping[str, Any], name: str) -> str | None:
    value = params.get(name)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_param(params: Mapping[str, Any], name: str, default: int, *, lo: int, hi: int) -> int:
    text = _str_param(params, name)
    if text is None:
        return default
    try:
        value = int(text)
    except ValueError:
        raise FilterError(f"{name} must be an integer.") from None
    return max(lo, min(hi, value))
