"""Detectors that turn a finished trace into :class:`~redline_core.model.Issue` objects.

=====================  ===============================================================
kind                   fires when
=====================  ===============================================================
``n_plus_one``         the same read (``SELECT``/``WITH``) fingerprint comes from the same
                       callsite ``>= n_plus_one_threshold`` times (default 5)
``duplicate_query``    identical SQL *and* identical params run ``>= duplicate_threshold``
                       times (default 2) – unless every copy already belongs to an N+1
``slow_query``         a query takes ``>= slow_query_ms`` (default 100 ms); grouped by
                       fingerprint + callsite
``too_many_queries``   the request ran ``>= too_many_queries`` queries (default 50)
=====================  ===============================================================

Severity rules (``T`` = the relevant threshold):

* ``n_plus_one`` – *critical* at ``>= 4·T`` repeats or when the repeats together take
  ``>= slow_query_ms``; otherwise *warning*.
* ``duplicate_query`` – *warning* at ``>= 5`` copies or ``>= slow_query_ms`` in total;
  otherwise *info*.
* ``slow_query`` – *critical* at ``>= 5·T`` ms for the slowest copy; otherwise *warning*.
* ``too_many_queries`` – *critical* at ``>= 2·T`` queries; otherwise *warning*.

Issue ids are stable across requests (:func:`issue_id`) so the Issues page can count
occurrences of "the same" problem. Adapters plug framework knowledge in through the
:class:`FixSuggester` protocol (Django: ``prefetch_related``; SQLAlchemy:
``selectinload``); the :class:`GenericSuggester` is the framework-free fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from . import sql as sqlmod
from .conf import Settings
from .model import Callsite, Issue, IssueKind, Severity, Span, Suggestion, Trace

__all__ = [
    "Detector",
    "FixSuggester",
    "GenericSuggester",
    "Issue",
    "Suggestion",
    "detect",
    "issue_id",
]

if TYPE_CHECKING:
    from .recorder import OverflowGroup

logger = logging.getLogger("redline")


@runtime_checkable
class FixSuggester(Protocol):
    """Proposes a fix for an issue; returns ``None`` when it has nothing useful to say."""

    def suggest(self, issue: Issue, spans: list[Span]) -> Suggestion | None: ...


def issue_id(kind: str, fingerprint: str | None, location: str | None) -> str:
    """``sha1("kind|fingerprint|file:line")[:16]`` – stable for the same problem.

    For ``too_many_queries`` (no fingerprint, no callsite) callers pass the route as the
    ``location`` so each endpoint gets its own issue.
    """
    key = f"{kind}|{fingerprint or ''}|{location or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _where(callsite: Callsite | None) -> str:
    return f" from {callsite.location}" if callsite else ""


def _on(fp: str) -> str:
    table = sqlmod.primary_table(fp)
    return f" on {table}" if table else ""


def _params_key(params: object) -> str:
    try:
        return json.dumps(params, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(params)


class GenericSuggester:
    """Framework-free advice; used after the adapters' suggesters have declined."""

    def suggest(self, issue: Issue, spans: list[Span]) -> Suggestion | None:
        if issue.kind == "n_plus_one":
            return Suggestion(
                summary="Load the related rows in one query (a JOIN or an IN (...) batch) "
                "before the loop instead of one query per item.",
                confidence="low",
            )
        if issue.kind == "duplicate_query":
            return Suggestion(
                summary="Run the query once and reuse the result, or cache it for the "
                "duration of the request.",
                confidence="low",
            )
        if issue.kind == "slow_query":
            return Suggestion(
                summary="Open EXPLAIN on this query to see whether it scans a large table "
                "or sorts on disk; an index on the filtered columns usually helps.",
                confidence="low",
            )
        if issue.kind == "too_many_queries":
            return Suggestion(
                summary="Look for loops that query per item and batch them; check the N+1 "
                "and duplicate issues on this request first.",
                confidence="low",
            )
        return None


class Detector:
    """Runs every detector over a trace and attaches suggestions."""

    def __init__(
        self,
        settings: Settings | None = None,
        suggesters: Sequence[FixSuggester] = (),
        *,
        generic_fallback: bool = True,
    ) -> None:
        self.settings = settings or Settings()
        self.suggesters: list[FixSuggester] = list(suggesters)
        if generic_fallback:
            self.suggesters.append(GenericSuggester())

    def add_suggester(self, suggester: FixSuggester) -> None:
        """Register ``suggester`` ahead of the generic fallback."""
        generic = [s for s in self.suggesters if isinstance(s, GenericSuggester)]
        others = [s for s in self.suggesters if not isinstance(s, GenericSuggester)]
        self.suggesters = [*others, suggester, *generic]

    # entry point -----------------------------------------------------------
    def detect(self, trace: Trace, overflow: Sequence[OverflowGroup] | None = None) -> list[Issue]:
        """Every issue in ``trace``.

        ``overflow`` are the counters of SQL spans dropped by ``max_spans_per_trace``
        (from the recorder); N+1 and slow-query issues include them, so a truncated trace
        still reports the full loop. Duplicate detection only sees stored spans.
        """
        sql_spans = [s for s in trace.spans if s.kind == "sql"]
        extra: dict[tuple[str, str | None], OverflowGroup] = {}
        for group in overflow or ():
            sample = group.sample
            fp = sample.data.get("fingerprint") or sqlmod.fingerprint(sample.data.get("sql", ""))
            extra[(fp, sample.callsite.location if sample.callsite else None)] = group
        issues: list[Issue] = []
        samples: dict[str, Span] = {}
        n1 = self._n_plus_one(sql_spans, extra, samples)
        covered = {sid for issue in n1 for sid in issue.span_ids}
        issues.extend(n1)
        issues.extend(self._duplicates(sql_spans, covered))
        issues.extend(self._slow(sql_spans, extra, samples))
        too_many = self._too_many(trace, sql_spans)
        if too_many:
            issues.append(too_many)
        by_id = {s.id: s for s in sql_spans}
        for issue in issues:
            spans = [by_id[i] for i in issue.span_ids if i in by_id]
            if not spans and issue.id in samples:
                spans = [samples[issue.id]]
            issue.suggestion = self._suggest(issue, spans)
        order = {"critical": 0, "warning": 1, "info": 2}
        issues.sort(key=lambda i: (order[i.severity], -i.total_ms))
        return issues

    def _suggest(self, issue: Issue, spans: list[Span]) -> Suggestion | None:
        for suggester in self.suggesters:
            try:
                suggestion = suggester.suggest(issue, spans)
            except Exception:  # a broken suggester must never break a request
                logger.exception("Redline fix suggester %r failed", suggester)
                continue
            if suggestion is not None:
                return suggestion
        return None

    # detectors -------------------------------------------------------------
    def _n_plus_one(
        self,
        spans: Iterable[Span],
        extra: dict[tuple[str, str | None], OverflowGroup] | None = None,
        samples: dict[str, Span] | None = None,
    ) -> list[Issue]:
        threshold = self.settings.n_plus_one_threshold
        groups: dict[tuple[str, str | None], list[Span]] = {}
        for s in spans:
            data = s.data
            if not data.get("explainable", sqlmod.is_explainable(data.get("sql", ""))):
                continue
            fp = data.get("fingerprint") or sqlmod.fingerprint(data.get("sql", ""))
            loc = s.callsite.location if s.callsite else None
            groups.setdefault((fp, loc), []).append(s)
        extra = {k: g for k, g in (extra or {}).items() if g.explainable}
        for key in extra:
            groups.setdefault(key, [])
        issues = []
        for (fp, loc), group in groups.items():
            more = extra.get((fp, loc))
            n = len(group) + (more.count if more else 0)
            if n < threshold:
                continue
            total = sum(s.duration_ms for s in group) + (more.total_ms if more else 0.0)
            critical = n >= 4 * threshold or total >= self.settings.slow_query_ms
            callsite = group[0].callsite if group else more.sample.callsite if more else None
            iid = issue_id("n_plus_one", fp, loc)
            if more is not None and samples is not None:
                samples[iid] = more.sample
            issues.append(
                Issue(
                    id=iid,
                    kind="n_plus_one",
                    severity="critical" if critical else "warning",
                    title=f"N+1: {n} queries{_on(fp)}{_where(callsite)}",
                    detail=(
                        f"The same query ran {_plural(n, 'time')} from one line of code, "
                        f"taking {total:.1f} ms in total. Fetch the related rows once "
                        "instead of once per item."
                    ),
                    span_ids=[s.id for s in group],
                    fingerprint=fp,
                    callsite=callsite,
                    count=n,
                    total_ms=round(total, 3),
                )
            )
        return issues

    def _duplicates(self, spans: Iterable[Span], covered: set[str]) -> list[Issue]:
        threshold = self.settings.duplicate_threshold
        groups: dict[tuple[str, str], list[Span]] = {}
        for s in spans:
            raw = s.data.get("sql", "")
            groups.setdefault((raw, _params_key(s.data.get("params"))), []).append(s)
        issues = []
        for (raw, _), group in groups.items():
            n = len(group)
            if n < threshold or all(s.id in covered for s in group):
                continue
            fp = group[0].data.get("fingerprint") or sqlmod.fingerprint(raw)
            total = sum(s.duration_ms for s in group)
            callsite = group[0].callsite
            loc = callsite.location if callsite else None
            severity: Severity = (
                "warning" if n >= 5 or total >= self.settings.slow_query_ms else "info"
            )
            issues.append(
                Issue(
                    id=issue_id("duplicate_query", fp, loc),
                    kind="duplicate_query",
                    severity=severity,
                    title=f"Duplicate query ×{n}{_on(fp)}{_where(callsite)}",
                    detail=(
                        f"Exactly the same query with the same parameters ran "
                        f"{_plural(n, 'time')} ({total:.1f} ms in total); every run after "
                        "the first returned data the request already had."
                    ),
                    span_ids=[s.id for s in group],
                    fingerprint=fp,
                    callsite=callsite,
                    count=n,
                    total_ms=round(total, 3),
                )
            )
        return issues

    def _slow(
        self,
        spans: Iterable[Span],
        extra: dict[tuple[str, str | None], OverflowGroup] | None = None,
        samples: dict[str, Span] | None = None,
    ) -> list[Issue]:
        limit = self.settings.slow_query_ms
        groups: dict[tuple[str, str | None], list[Span]] = {}
        for s in spans:
            if s.duration_ms < limit:
                continue
            fp = s.data.get("fingerprint") or sqlmod.fingerprint(s.data.get("sql", ""))
            loc = s.callsite.location if s.callsite else None
            groups.setdefault((fp, loc), []).append(s)
        extra = {k: g for k, g in (extra or {}).items() if g.slow_count}
        for key in extra:
            groups.setdefault(key, [])
        issues = []
        for (fp, loc), group in groups.items():
            more = extra.get((fp, loc))
            slowest = max([s.duration_ms for s in group] + ([more.max_ms] if more else []))
            total = sum(s.duration_ms for s in group) + (more.slow_ms if more else 0.0)
            callsite = group[0].callsite if group else more.sample.callsite if more else None
            n = len(group) + (more.slow_count if more else 0)
            iid = issue_id("slow_query", fp, loc)
            if more is not None and samples is not None:
                samples[iid] = more.sample
            issues.append(
                Issue(
                    id=iid,
                    kind="slow_query",
                    severity="critical" if slowest >= 5 * limit else "warning",
                    title=f"Slow query: {slowest:.0f} ms{_on(fp)}{_where(callsite)}",
                    detail=(
                        f"{'This query' if n == 1 else f'{n} runs of this query'} took "
                        f"{'' if n == 1 else 'up to '}{slowest:.1f} ms, over the "
                        f"{limit:g} ms slow-query threshold. Run EXPLAIN to see why."
                    ),
                    span_ids=[s.id for s in group],
                    fingerprint=fp,
                    callsite=callsite,
                    count=n,
                    total_ms=round(total, 3),
                )
            )
        return issues

    def _too_many(self, trace: Trace, spans: list[Span]) -> Issue | None:
        limit = self.settings.too_many_queries
        n = max(len(spans), trace.query_count)
        if limit <= 0 or n < limit:
            return None
        total = sum(s.duration_ms for s in spans)
        kind: IssueKind = "too_many_queries"
        return Issue(
            id=issue_id(kind, None, f"{trace.method} {trace.route or trace.path}"),
            kind=kind,
            severity="critical" if n >= 2 * limit else "warning",
            title=f"{n} queries in one request",
            detail=(
                f"This request ran {n} SQL queries ({total:.1f} ms), over the budget of "
                f"{limit}. Batch per-item queries and remove duplicates."
            ),
            span_ids=[s.id for s in spans],
            fingerprint=None,
            callsite=None,
            count=n,
            total_ms=round(total, 3),
        )


def detect(
    trace: Trace,
    settings: Settings | None = None,
    suggesters: Sequence[FixSuggester] = (),
    overflow: Sequence[OverflowGroup] | None = None,
) -> list[Issue]:
    """Convenience: run a one-off :class:`Detector` over ``trace``."""
    return Detector(settings, suggesters).detect(trace, overflow)
