"""Evidence-based round-trip, pagination, pool, payload and aggregate findings."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import cast

from .. import sql as sqlmod
from ..conf import Settings
from ..model import Issue, IssueKind, Span, Trace
from ._common import issue_id, where


def _finding(
    kind: str,
    title: str,
    detail: str,
    spans: list[Span],
    *,
    fingerprint: str,
    evidence: dict,
    trace: Trace,
) -> Issue:
    site = spans[0].callsite if spans else None
    identity = fingerprint
    if spans and spans[0].kind == "sql":
        identity = str(spans[0].data.get("db_alias") or "default") + ":" + fingerprint
    return Issue(
        id=issue_id(kind, identity, site.location if site else trace.route),
        kind=cast(IssueKind, kind),
        severity="warning",
        title=title + where(site),
        detail=detail,
        span_ids=[s.id for s in spans],
        fingerprint=fingerprint,
        callsite=site,
        count=len(spans) or 1,
        total_ms=round(sum(s.duration_ms for s in spans), 3),
        extra={**evidence, "observed_spans": len(spans), "incomplete_trace": bool(trace.truncated)},
    )


def detect(trace: Trace, settings: Settings) -> list[Issue]:
    issues = []
    writes: dict[tuple, list[Span]] = defaultdict(list)
    aggregates: dict[tuple, list[tuple[str, Span]]] = defaultdict(list)
    sql = [s for s in trace.spans if s.kind == "sql"]
    for span in sql:
        text = str(span.data.get("sql") or "")
        clean = sqlmod.structural_sql(text).strip()
        fp = span.data.get("fingerprint") or sqlmod.fingerprint(text)
        site = span.callsite.location if span.callsite else None
        if (
            any(k in {"INSERT", "UPDATE", "DELETE", "MERGE"} for k in sqlmod.effective_kinds(text))
            and not span.data.get("many")
            and not span.data.get("error")
        ):
            writes[(span.data.get("db_alias") or "default", fp, site)].append(span)
        offset = sqlmod.offset_value(text, span.data.get("params"))
        if settings.deep_offset_rows and offset is not None and offset >= settings.deep_offset_rows:
            issues.append(
                _finding(
                    "deep_offset",
                    "Deep OFFSET pagination",
                    "The database must compute skipped rows. Evaluate keyset pagination with a "
                    "stable unique ordering; preserve the endpoint's navigation semantics.",
                    [span],
                    fingerprint=fp,
                    evidence={"offset": offset, "threshold": settings.deep_offset_rows},
                    trace=trace,
                )
            )
        match = re.match(r"^SELECT\s+(.+?)\s+FROM\s+([\w.\"`]+)(.*)$", clean, re.I | re.S)
        if match and not re.search(
            r"\b(JOIN|UNION|SELECT|GROUP|HAVING|LIMIT|OFFSET)\b", match[3], re.I
        ):
            projection = match[1].strip()
            if re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", projection, re.I):
                key = (span.data.get("db_alias"), match[2].lower(), sqlmod.fingerprint(match[3]))
                aggregates[key].append((sqlmod.fingerprint(projection), span))
    for (_db, fp, _site), group in writes.items():
        if settings.repeated_writes_threshold and len(group) >= settings.repeated_writes_threshold:
            issues.append(
                _finding(
                    "repeated_writes",
                    "Repeated individual writes",
                    "Repeated writes from one callsite add database round trips. Evaluate a bulk "
                    "operation, checking signals, validation, return values and ordering first.",
                    group,
                    fingerprint=fp,
                    evidence={"threshold": settings.repeated_writes_threshold},
                    trace=trace,
                )
            )
    for (_db, table, predicate), pairs in aggregates.items():
        if (
            settings.repeated_scans_threshold
            and len(pairs) >= settings.repeated_scans_threshold
            and len({p for p, _ in pairs}) > 1
        ):
            issues.append(
                _finding(
                    "repeated_scan",
                    "Repeated aggregate scans",
                    "Different aggregates queried the same table with the same predicate shape. "
                    "Review whether one combined or conditional aggregate preserves the parameter "
                    "values and semantics. Similar shape alone does not prove redundant work.",
                    [s for _, s in pairs],
                    fingerprint=f"{table}:{predicate}",
                    evidence={
                        "table": table,
                        "predicate_shape": predicate,
                        "threshold": settings.repeated_scans_threshold,
                    },
                    trace=trace,
                )
            )
    if (
        settings.round_trip_count
        and len(sql) >= settings.round_trip_count
        and trace.query_ms >= settings.round_trip_ms
        and not (settings.too_many_queries and trace.query_count >= settings.too_many_queries)
    ):
        counts: dict[str, int] = defaultdict(int)
        for span in sql:
            counts[str(span.data.get("fingerprint") or span.name)] += 1
        if max(counts.values(), default=0) < settings.n_plus_one_threshold:
            issues.append(
                _finding(
                    "database_round_trips",
                    "Database round trips add up",
                    "Many individually fast queries consumed substantial cumulative query time. "
                    "Review batching, joins and request-scoped reuse. Cumulative query time may "
                    "overlap and is not an estimate of achievable wall-time savings.",
                    sql,
                    fingerprint=trace.route or trace.path,
                    evidence={
                        "count_threshold": settings.round_trip_count,
                        "time_threshold_ms": settings.round_trip_ms,
                    },
                    trace=trace,
                )
            )
    for kind, selected, threshold, title, detail in (
        (
            "connection_pool_wait",
            [s for s in trace.spans if s.kind == "pool_wait"],
            settings.pool_wait_ms,
            "Connection pool wait",
            "A request waited for a database connection. "
            "Inspect long-held transactions and concurrency before increasing pool size.",
        ),
    ):
        slow = [s for s in selected if threshold and s.duration_ms >= threshold]
        if slow:
            issues.append(
                _finding(
                    kind,
                    title,
                    detail,
                    slow,
                    fingerprint=trace.route or trace.path,
                    evidence={"threshold_ms": threshold},
                    trace=trace,
                )
            )
    try:
        count = int(trace.tags.get("orm_instances", "0"))
    except (ValueError, TypeError):
        count = 0
    if settings.orm_instances_threshold and count >= settings.orm_instances_threshold:
        issues.append(
            _finding(
                "many_orm_instances",
                "Many ORM instances constructed",
                "This request constructed many model objects. For reports, review projections such "
                "as values() or column-only selects. Django also counts newly constructed objects; "
                "the count alone does not prove models are unnecessary.",
                [],
                fingerprint=trace.route or trace.path,
                evidence={"instances": count, "threshold": settings.orm_instances_threshold},
                trace=trace,
            )
        )
    size = trace.response.get("size")
    if (
        isinstance(size, int)
        and settings.large_response_bytes
        and size >= settings.large_response_bytes
    ):
        issues.append(
            _finding(
                "large_response",
                "Large response payload",
                "The response exceeds its byte threshold. Review pagination and selected fields. "
                "Observed response bytes; network compression is not measured.",
                [],
                fingerprint=trace.route or trace.path,
                evidence={"bytes": size, "threshold": settings.large_response_bytes},
                trace=trace,
            )
        )
    for span in trace.spans:
        decode = span.data.get("decode_ms")
        if (
            span.kind == "cache"
            and isinstance(decode, (int, float))
            and settings.cache_decode_ms
            and decode >= settings.cache_decode_ms
        ):
            issues.append(
                _finding(
                    "slow_cache_decode",
                    "Slow cache decoding",
                    "An explicit decode block exceeded its threshold. Review payload "
                    "shape, serialization format and repeated decoding; verify response semantics.",
                    [span],
                    fingerprint=str(span.data.get("key") or span.name),
                    evidence={"decode_ms": decode, "threshold_ms": settings.cache_decode_ms},
                    trace=trace,
                )
            )
        size = span.data.get("value_bytes")
        if (
            span.kind == "cache"
            and isinstance(size, int)
            and settings.large_cache_bytes
            and size >= settings.large_cache_bytes
        ):
            issues.append(
                _finding(
                    "large_cache_entry",
                    "Large cache entry",
                    "An observed cache value exceeds its byte threshold. Review payload shape and "
                    "decode cost. Serialization and decoding time are not measured here.",
                    [span],
                    fingerprint=str(span.data.get("key") or span.name),
                    evidence={"bytes": size, "threshold": settings.large_cache_bytes},
                    trace=trace,
                )
            )
    merged = {}
    for issue in issues:
        previous = merged.get(issue.id)
        if previous is None:
            merged[issue.id] = issue
            continue
        previous.span_ids.extend(sid for sid in issue.span_ids if sid not in previous.span_ids)
        previous.count += issue.count
        previous.total_ms = round(previous.total_ms + issue.total_ms, 3)
        previous.extra["observed_spans"] = len(previous.span_ids)
        for key in ("offset", "bytes", "decode_ms"):
            if key in issue.extra:
                previous.extra[key] = max(previous.extra.get(key, 0), issue.extra[key])
    return list(merged.values())
