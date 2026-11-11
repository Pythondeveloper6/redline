"""LLM-call findings from ``llm_call`` spans (see :mod:`redline_core.contrib.anthropic`).

* ``duplicate_llm_call`` - the same model got the same prompt (by hash) repeatedly in a request.
* ``serial_llm_calls`` - calls that ran back to back. A candidate only: the trace shows timing,
  not whether a later prompt needs an earlier answer.
* ``llm_in_transaction`` - an LLM call started while a database transaction was open.
* ``llm_call_loop`` - one request made many LLM calls (agent loop, per-item calls).
* ``uncached_large_prompt`` - a large Anthropic prompt with no prompt-cache tokens.

Only span metadata is used; prompt text is never available here. A threshold of 0 disables a
detector.
"""

from __future__ import annotations

from collections import defaultdict
from typing import cast

from ..conf import Settings
from ..model import Issue, IssueKind, Span, Suggestion, Trace
from ._common import issue_id, plural, where


def _issue(
    kind: str,
    spans: list[Span],
    *,
    title: str,
    detail: str,
    suggestion: str,
    fingerprint: str,
    evidence: dict,
    trace: Trace,
) -> Issue:
    site = spans[0].callsite
    return Issue(
        id=issue_id(kind, fingerprint, site.location if site else trace.route or trace.path),
        kind=cast(IssueKind, kind),
        severity="warning",
        title=title + where(site),
        detail=detail,
        span_ids=[s.id for s in spans],
        fingerprint=fingerprint,
        callsite=site,
        count=len(spans),
        total_ms=round(sum(s.duration_ms for s in spans), 3),
        suggestion=Suggestion(summary=suggestion, confidence="low"),
        extra={**evidence, "observed_spans": len(spans), "incomplete_trace": bool(trace.truncated)},
    )


def _end(span: Span) -> float:
    return span.start_ms + span.duration_ms


def detect(trace: Trace, settings: Settings) -> list[Issue]:
    calls = sorted((s for s in trace.spans if s.kind == "llm_call"), key=lambda s: s.start_ms)
    if not calls:
        return []
    issues: list[Issue] = []

    dup_limit = settings.llm_duplicate_threshold
    if dup_limit:
        groups: dict[tuple, list[Span]] = defaultdict(list)
        for s in calls:
            groups[(s.data.get("model"), s.data.get("prompt_hash"))].append(s)
        for (model, digest), spans in groups.items():
            if digest and len(spans) >= dup_limit:
                issues.append(
                    _issue(
                        "duplicate_llm_call",
                        spans,
                        title=f"Identical {model} prompt sent {len(spans)} times",
                        detail=(
                            f"{plural(len(spans), 'identical call')} to {model} in one request "
                            "paid for the same input repeatedly. Responses may differ between "
                            "calls, so reuse is only safe when the output is not meant to vary."
                        ),
                        suggestion="Reuse the first response within the request, or cache it "
                        "keyed on the input, if the output does not need to vary.",
                        fingerprint=f"{model}:{digest}",
                        evidence={"calls": len(spans), "threshold": dup_limit, "model": model},
                        trace=trace,
                    )
                )

    serial_min = settings.llm_serial_min
    if serial_min and len(calls) >= serial_min:
        if all(calls[i].start_ms >= _end(calls[i - 1]) for i in range(1, len(calls))):
            issues.append(
                _issue(
                    "serial_llm_calls",
                    calls,
                    title=f"{len(calls)} LLM calls ran one after another",
                    detail=(
                        f"{plural(len(calls), 'LLM call')} ran with no overlap, taking "
                        f"{sum(s.duration_ms for s in calls):.0f} ms in total. If a later "
                        "prompt does not depend on an earlier answer they could run "
                        "concurrently; the trace cannot show whether they depend on each other."
                    ),
                    suggestion="Check whether these calls are independent; if so, run them "
                    "concurrently (asyncio.gather or a thread pool) with a concurrency limit.",
                    fingerprint=trace.route or trace.path,
                    evidence={"calls": len(calls), "threshold": serial_min},
                    trace=trace,
                )
            )

    txs = [s for s in trace.spans if s.kind == "transaction"]
    inside = [c for c in calls if any(t.start_ms <= c.start_ms < _end(t) for t in txs)]
    if inside:
        issues.append(
            _issue(
                "llm_in_transaction",
                inside,
                title=f"{plural(len(inside), 'LLM call')} inside a database transaction",
                detail=(
                    "A slow or failing model API holds the transaction's locks and connection "
                    "open until it answers."
                ),
                suggestion="Call the model before opening the transaction or after it commits.",
                fingerprint=trace.route or trace.path,
                evidence={"calls": len(inside)},
                trace=trace,
            )
        )

    loop_limit = settings.llm_loop_calls
    if loop_limit and len(calls) >= loop_limit:
        issues.append(
            _issue(
                "llm_call_loop",
                calls,
                title=f"{len(calls)} LLM calls in one request",
                detail=(
                    f"{plural(len(calls), 'LLM call')} in a single request add latency and cost "
                    "per call. This is expected for an agent loop but a smell for per-item calls."
                ),
                suggestion="Batch items into one prompt, cap the loop, or move the work to a "
                "background job.",
                fingerprint=trace.route or trace.path,
                evidence={"calls": len(calls), "threshold": loop_limit},
                trace=trace,
            )
        )

    cache_chars = settings.llm_prompt_cache_chars
    if cache_chars:
        for s in calls:
            d = s.data
            if (
                d.get("provider") == "anthropic"
                and not d.get("stream")
                and d.get("status") == "ok"
                and (d.get("prompt_chars") or 0) >= cache_chars
                and not d.get("cache_read_tokens")
                and not d.get("cache_creation_tokens")
            ):
                issues.append(
                    _issue(
                        "uncached_large_prompt",
                        [s],
                        title=f"Large {d.get('model')} prompt without prompt caching",
                        detail=(
                            f"A prompt of about {d.get('prompt_chars')} characters used no "
                            "prompt cache. A stable prefix (system prompt, tools, documents) "
                            "can be cached to cut latency and input cost."
                        ),
                        suggestion="Mark the stable prefix with cache_control so repeated "
                        "requests read it from the cache.",
                        fingerprint=str(d.get("prompt_hash")),
                        evidence={"prompt_chars": d.get("prompt_chars"), "threshold": cache_chars},
                        trace=trace,
                    )
                )
    return issues
