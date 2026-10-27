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
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from . import __version__
from .conf import Settings, editor_url_template, resolve_release
from .explain import ExplainError, ExplainExecutor, run_explain
from .model import ISSUE_KINDS
from .profiler import is_available as profiler_available
from .storage.base import FilterError, Filters, Store, parse_window

__all__ = ["ExecutorFactory", "RedlineAPI", "Response", "error"]

logger = logging.getLogger("redline")

Response = tuple[int, Any]
ExecutorFactory = Callable[[str, str], "ExplainExecutor | None"]

_TRACE = re.compile(r"^api/traces/([A-Za-z0-9_-]{1,64})$")
_PROFILE = re.compile(r"^api/traces/([A-Za-z0-9_-]{1,64})/profile$")
_ISSUE_KINDS = ISSUE_KINDS


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
    ) -> None:
        self.store = store
        self.settings = settings or Settings()
        self.framework = framework
        self.explain_executor = explain_executor

    # routing ---------------------------------------------------------------
    def dispatch(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        body: bytes | str | Mapping[str, Any] | None = None,
    ) -> Response:
        """Route ``method`` + ``path`` (relative to the mount, e.g. ``api/traces``)."""
        method = method.upper()
        path = path.strip("/")
        params = params or {}
        try:
            if path == "api/meta":
                return self.meta() if method == "GET" else _not_allowed()
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
        except Exception as exc:  # the profiler UI must never take the app down
            logger.exception("Redline API error on %s %s", method, path)
            return error(500, "internal_error", f"{type(exc).__name__}: {exc}")

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
            "features": {"load_from_ui": self.settings.load_from_ui, "sse": True, "snapshot": True},
            "settings": settings,
            "storage": {
                "rows": self.store.count(),
                "path": getattr(self.store, "path", None),
                "dropped": getattr(self.store, "dropped", 0),
            },
        }

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
        data = trace.to_dict(include_profile=False)
        data["has_profile"] = self.store.has_profile(trace_id)
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
        return 200, {"items": [e.to_dict() for e in self.store.endpoints(window)]}

    def issues(self, params: Mapping[str, Any]) -> Response:
        try:
            window = parse_window(_str_param(params, "window"))
        except FilterError as exc:
            return error(400, "invalid_window", str(exc))
        kind = _str_param(params, "kind")
        if kind is not None and kind not in _ISSUE_KINDS:
            return error(400, "invalid_kind", f"kind must be one of {', '.join(_ISSUE_KINDS)}.")
        return 200, {"items": [g.to_dict() for g in self.store.issues(window, kind=kind)]}

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
        if analyze and not self.settings.explain_analyze:
            return error(
                403,
                "analyze_disabled",
                "EXPLAIN ANALYZE executes the query; enable it with the explain_analyze setting.",
            )
        trace = self.store.get(trace_id, include_profile=False)
        if trace is None:
            return error(404, "trace_not_found", f"No trace {trace_id!r}.")
        span = trace.span(span_id)
        if span is None:
            return error(404, "span_not_found", f"No span {span_id!r} in trace {trace_id!r}.")
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
