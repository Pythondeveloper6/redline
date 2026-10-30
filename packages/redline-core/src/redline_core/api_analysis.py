"""UI-accessible analysis and observation reports using the shared core engines."""

from __future__ import annotations

import json

from .analyze import MAX_SOURCE_BYTES, analyze_project, analyze_source
from .check.coverage import coverage
from .check.suite import Suite
from .storage.base import Filters


def route(api, method, path, params, body):
    from .api import error

    if path in {"api/analysis/sql", "api/analysis/redaction"} and method == "POST":
        enabled = api.settings.analysis_from_ui
        if not (enabled is True or (enabled == "auto" and api.debug_mode())):
            return error(403, "analysis_disabled", "Enable analysis_from_ui to use analysis tools.")
        try:
            if isinstance(body, (bytes, str)) and len(body) > MAX_SOURCE_BYTES + 4096:
                return error(413, "payload_too_large", "Analysis input is too large.")
            data = json.loads(body) if isinstance(body, (bytes, str)) else body
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            if path.endswith("/sql"):
                if set(data) - {"source", "dialect"} or not isinstance(data.get("source"), str):
                    raise ValueError("Provide Python source and an optional dialect")
                if data.get("dialect", "other") not in {
                    "other",
                    "mssql",
                    "postgresql",
                    "mysql",
                    "sqlite",
                }:
                    raise ValueError("Unsupported dialect")
                from .check_sql import analyze_sql_source

                return 200, analyze_sql_source(data["source"], dialect=data.get("dialect", "other"))
            if set(data) - {"sample", "method", "route"} or not isinstance(
                data.get("sample"), dict
            ):
                raise ValueError("Provide a sample JSON object, method and route")
            from .model import Trace
            from .privacy import sanitize_trace
            from .redact import Redactor

            trace = Trace(
                "preview", "other", str(data.get("method", "POST")), str(data.get("route", "/"))
            )
            trace.request["body"] = json.dumps(Redactor(api.settings).mapping(data["sample"]))
            sanitize_trace(trace, api.settings)
            return 200, {
                "sample": json.loads(trace.request["body"])
                if trace.request["body"] != "[redacted]"
                else "[redacted]",
                "saved": False,
                "note": (
                    "Uses current key/profile/route rules. Capture switches and "
                    "arbitrary free-text sensitivity are separate; this is not a "
                    "compliance guarantee."
                ),
            }
        except (ValueError, TypeError, RecursionError):
            return error(400, "invalid_analysis", "Provide bounded, valid source or a JSON object.")
    if path == "api/analysis/demo" and method == "POST":
        enabled = api.settings.analysis_from_ui
        if not (enabled is True or (enabled == "auto" and api.debug_mode())):
            return error(403, "analysis_disabled", "Detector samples require analysis_from_ui.")
        from .demo import CASES, detector_sample

        items = []
        for case in CASES:
            trace = detector_sample(case, api.settings)
            api.store.save(trace)
            items.append({"id": trace.id, "case": case, "issue_kinds": trace.issue_kinds})
        flush = getattr(api.store, "flush", None)
        if callable(flush):
            flush()
        return 201, {"items": items, "synthetic": True}
    if path == "api/analysis/trace" and method == "POST":
        try:
            data = json.loads(body) if isinstance(body, (bytes, str)) else body
            allowed = {
                "repeated_writes_threshold",
                "deep_offset_rows",
                "round_trip_count",
                "round_trip_ms",
                "pool_wait_ms",
                "large_response_bytes",
                "large_cache_bytes",
                "orm_instances_threshold",
                "repeated_scans_threshold",
                "json_decode_ms",
                "cache_decode_ms",
            }
            if not isinstance(data, dict) or set(data) - {"trace_id", "settings"}:
                raise ValueError("Choose a trace and detector thresholds")
            overrides = data.get("settings", {})
            if not isinstance(overrides, dict) or set(overrides) - allowed:
                raise ValueError("Only detector thresholds may be overridden")
            settings = api.settings.replace(**overrides)
            trace = api.store.get(
                str(data.get("trace_id", "")), include_profile=api.settings.feature("profile")
            )
            if trace is None:
                return error(404, "trace_not_found", "Choose an existing request.")
            from .detect import Detector

            return 200, {
                "issues": [i.to_dict() for i in Detector(settings).detect(trace)],
                "preview": True,
                "stored_findings_changed": False,
            }
        except (ValueError, TypeError) as exc:
            return error(400, "invalid_thresholds", str(exc))
    if path == "api/analysis/database" and method == "POST":
        if not api.settings.feature("explain"):
            return error(404, "feature_disabled", "Database inspection is disabled.")
        try:
            data = json.loads(body) if isinstance(body, (bytes, str)) else body
            if not isinstance(data, dict) or set(data) != {"trace_id", "span_id"}:
                raise ValueError("Choose a recorded SQL span")
            trace = api.store.get(str(data["trace_id"]), include_profile=False)
            span = trace.span(str(data["span_id"])) if trace else None
            if trace and trace.tags.get("secondary_store"):
                return error(
                    403,
                    "secondary_store",
                    "Inspect this SQL through the worker’s own database binding.",
                )
            if trace and trace.tags.get("synthetic_demo"):
                return error(400, "synthetic_sample", "Synthetic samples have no real database.")
            if span is not None and span.data.get("auto_engine"):
                return error(
                    501, "unregistered_engine", "Register this engine before database inspection."
                )
            if span is None or span.kind != "sql":
                return error(404, "span_not_found", "Choose an existing SQL span.")
            if api.explain_executor is None:
                return error(
                    501, "explain_unavailable", "Database catalog inspection is unavailable."
                )
            executor = api.explain_executor(
                str(span.data.get("db_alias", "default")), str(span.data.get("vendor", "other"))
            )
            if executor is None:
                return error(501, "explain_unavailable", "No executor for this database.")
            from .database_checks import inspect_query

            return 200, inspect_query(
                str(span.data.get("sql", "")), str(span.data.get("vendor", "other")), executor
            )
        except (ValueError, TypeError):
            return error(400, "invalid_database_check", "Choose a recorded SQL span.")
    if path == "api/analysis/health" and method == "GET":
        from .integration_status import integration_status, integration_warnings

        page = api.store.list(Filters(), limit=200)
        _, meta = api.meta()
        items = page.items
        integrations = integration_status()
        warnings = []
        if meta["storage"]["dropped"]:
            warnings.append("The writer dropped traces; retained counts are incomplete.")
        if api.settings.sample_rate < 1:
            warnings.append("Retained counts reflect sampling and keep rules, not total traffic.")
        warnings.extend(integration_warnings(integrations))
        return 200, {
            "meta": meta,
            "capture": {
                "sql_params": api.settings.capture_sql_params,
                "request_body": api.settings.capture_request_body,
                "response_body": api.settings.capture_response_body,
                "locals": api.settings.capture_locals,
                "engine_mode": api.settings.instrument_engines,
                "secondary_stores": len(api.settings.extra_stores),
            },
            "observed_traces": len(items),
            "profiled_traces": sum(bool(t.get("has_profile")) for t in items),
            "test_traces": sum(bool(t.get("tags", {}).get("test_name")) for t in items),
            "test_requests": [
                {
                    "id": t["id"],
                    "name": t["tags"]["test_name"],
                    "method": t["method"],
                    "path": t["path"],
                }
                for t in items
                if t.get("tags", {}).get("test_name")
            ],
            "integrations": integrations,
            "warnings": warnings,
            "note": (
                "Latest 200 kept traces. Integration installation and instrumentation status "
                "do not indicate whether calls were observed."
            ),
        }
    if path == "api/analysis/coverage" and method == "GET":
        if not api.settings.feature("checks"):
            return error(404, "feature_disabled", "Checks are disabled.")
        get = getattr(api.store, "get_check_suite", None)
        if not callable(get):
            return error(501, "not_supported", "This store does not support check suites.")
        saved = get(str(params.get("suite_id") or ""))
        if saved is None:
            return error(404, "suite_not_found", "Select an existing suite.")
        page = api.store.list(Filters(), limit=500)
        return 200, coverage(Suite.from_dict(saved["suite"]), page.items)
    if path == "api/analysis/source" and method == "POST":
        enabled = api.settings.analysis_from_ui
        if not (enabled is True or (enabled == "auto" and api.debug_mode())):
            return error(
                403,
                "analysis_disabled",
                "Enable analysis_from_ui for source analysis, or use framework debug mode.",
            )
        try:
            if isinstance(body, (bytes, str)) and len(body) > MAX_SOURCE_BYTES + 4096:
                return error(413, "payload_too_large", "Source analysis input is too large.")
            data = json.loads(body) if isinstance(body, (bytes, str)) else body
            if not isinstance(data, dict) or set(data) - {"source", "file", "project"}:
                raise ValueError(
                    "Use source/file or project=true; arbitrary filesystem paths are not accepted."
                )
            if data.get("project") is True:
                if "source" in data or "file" in data:
                    raise ValueError("Choose project scanning or source text, not both.")
                if not api.settings.project_root:
                    raise ValueError("Set project_root before scanning the application.")
                return 200, analyze_project(api.settings.project_root)
            source = data.get("source")
            file = data.get("file", "input.py")
            if not isinstance(source, str) or not isinstance(file, str) or len(file) > 200:
                raise ValueError("Provide Python source text and a short file label.")
            return 200, analyze_source(source, file)
        except (ValueError, RecursionError):
            return error(
                400,
                "invalid_source",
                "Use bounded Python source text or the configured project root.",
            )
    return error(405, "method_not_allowed", "Unsupported analysis route or method.")
