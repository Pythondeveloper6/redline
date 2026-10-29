"""SQL correctness findings from recorded evidence, including caught failures."""

from __future__ import annotations

from collections import defaultdict

from ..model import Issue
from ..sql import effective_kinds
from ._common import issue_id

_ADVICE = {
    "schema": "Check the query against the deployed schema and migration version.",
    "parameter": "Compare bound names/counts with placeholders, including markers in SQL comments.",
    "trigger_output": (
        "SQL Server rejects bare OUTPUT on a target with enabled triggers. "
        "Review OUTPUT INTO and its result semantics."
    ),
    "disconnect": (
        "The connection was lost. Inspect connection age and pool/server "
        "settings; never blindly retry writes."
    ),
    "deadlock": (
        "Inspect the complete transaction and lock order. Any retry must "
        "repeat a safe transaction, not only this statement."
    ),
    "timeout": "Check query duration, locks and connection-pool pressure before changing timeouts.",
    "permission": "Check the database principal's permissions in the intended environment.",
}


def detect(trace, settings):
    groups = defaultdict(list)
    for span in trace.spans:
        if span.data.get("connection_error"):
            groups[("connection_failure", span.data.get("db_alias"))].append(span)
        if span.kind == "sql":
            connection = span.data.get("connection", {})
            if connection.get("observed_fresh_failures_hour", 0) >= 3:
                groups[("fresh_connection_failure", span.data.get("db_alias"))].append(span)
            if (
                connection.get("observed_connects_hour", 0) >= 100
                and (connection.get("pool_recycle") or 0) > 0
            ):
                groups[("connection_churn", span.data.get("db_alias"))].append(span)
            error = span.data.get("error")
            if error:
                groups[
                    (
                        "sql_error",
                        span.data.get("fingerprint"),
                        error.get("sqlstate"),
                        error.get("native"),
                        span.callsite.location if span.callsite else None,
                    )
                ].append(span)
            if span.data.get("comment_placeholders"):
                groups[("placeholder_in_comment", span.data.get("fingerprint"))].append(span)
            kinds = span.data.get("statement_kinds") or effective_kinds(span.data.get("sql", ""))
            if (
                span.data.get("vendor") == "mssql"
                and span.data.get("rowcount_raw") == -1
                and any(k in {"INSERT", "UPDATE", "DELETE", "MERGE"} for k in kinds)
                and not span.data.get("error")
            ):
                groups[("dml_rowcount_unknown", span.data.get("fingerprint"))].append(span)
        if span.data.get("session_state_leak"):
            groups[("session_state_leak", span.data.get("db_alias"))].append(span)
    issues = []
    for key, spans in groups.items():
        kind = key[0]
        first = spans[0]
        error = first.data.get("error") or first.data.get("connection_error") or {}
        titles = {
            "connection_failure": "Database connection invalidated",
            "fresh_connection_failure": "Repeated failures on fresh connections",
            "connection_churn": "High observed connection creation",
            "sql_error": "SQL statement failed",
            "placeholder_in_comment": "SQL placeholder inside a comment",
            "dml_rowcount_unknown": "DML affected-row count unavailable",
            "session_state_leak": "Session options still observed at pool checkin",
        }
        details = {
            "connection_failure": (
                "The pool invalidated a connection during this recording. "
                "Inspect checkout hooks and driver diagnostics; no SQL statement "
                "is invented for a checkout failure."
            ),
            "fresh_connection_failure": (
                "At least three observed disconnects occurred on the first "
                "statement within five seconds of connection creation in the last "
                "hour. Check server/network diagnostics and pool settings; this is "
                "correlation, not proof of cause."
            ),
            "connection_churn": (
                "At least 100 physical connections were observed in the last hour "
                "while age-based recycling was configured. Review concurrency, "
                "pool disposal and recycle settings; Redline does not change them."
            ),
            "sql_error": _ADVICE.get(
                error.get("category"),
                (
                    "Inspect the driver error and statement; the application may have "
                    "caught this failure."
                ),
            ),
            "placeholder_in_comment": (
                "A driver bind marker occurs in a SQL comment. Some compilers bind "
                "it while the driver ignores it; move bind-like text out of "
                "comments."
            ),
            "dml_rowcount_unknown": (
                "The driver reported -1 for DML. NOCOUNT, result-returning "
                "statements, batches and driver limitations can cause this. This "
                "does not prove a failed write or a NOCOUNT leak."
            ),
            "session_state_leak": (
                "An explicitly observed session switch remained ON at checkin. "
                "Review the setting callsite and intended connection baseline. "
                "Unobserved driver/procedure changes are unknown; Redline does not "
                "reset the connection."
            ),
        }
        identity = repr(key[1:]) + ":" + str(first.data.get("db_alias", "default"))
        issues.append(
            Issue(
                id=issue_id(
                    kind, identity, first.callsite.location if first.callsite else trace.route
                ),
                kind=kind,
                severity="critical"
                if kind == "sql_error"
                and error.get("category") in {"schema", "parameter", "trigger_output"}
                else "warning",
                title=titles[kind],
                detail=details[kind],
                span_ids=[s.id for s in spans],
                fingerprint=first.data.get("fingerprint"),
                callsite=first.callsite,
                count=len(spans),
                total_ms=sum(s.duration_ms for s in spans),
                extra={"error": error, "incomplete_trace": bool(trace.truncated)},
            )
        )
    observed = sum(s.kind == "sql" and bool(s.data.get("error")) for s in trace.spans)
    try:
        missing = int(trace.tags.get("sql_errors", "0")) - observed
    except (ValueError, TypeError):
        missing = 0
    if missing > 0:
        issues.append(
            Issue(
                id=issue_id("sql_error", "dropped", trace.route or trace.path),
                kind="sql_error",
                severity="warning",
                title="SQL failures with incomplete evidence",
                detail="Failed statements were counted after the span limit. "
                "Increase the span budget to inspect them.",
                span_ids=[],
                count=missing,
                extra={"incomplete_trace": True},
            )
        )
    if trace.error:
        origin = trace.error.get("origin", {})
        location = f"{origin.get('file', '')}:{origin.get('line', '')}:{origin.get('function', '')}"
        issues.append(
            Issue(
                id=issue_id("application_error", trace.error.get("type"), location),
                kind="application_error",
                severity="critical",
                title="Application recording failed",
                detail="An exception escaped this request or job. Inspect its traceback and "
                "observed transaction outcomes. Grouping uses exception type and source "
                "location, not parameter values.",
                count=1,
                extra={
                    "error_type": trace.error.get("type"),
                    "origin": origin,
                    "commit_observed_before_failure": trace.tags.get(
                        "commit_observed_before_failure"
                    )
                    == "true",
                },
            )
        )
    return issues
