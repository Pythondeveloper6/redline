"""Transaction detectors (``CONTRACT.md`` §9).

* ``long_transaction`` – a ``transaction`` span of at least ``long_transaction_ms``: locks
  and a connection are held the whole time. ``critical`` at 5× the threshold.
* ``external_in_transaction`` – outbound HTTP calls made while a transaction was open
  (``data["http_out"] > 0``): a slow or failing third party then holds the transaction,
  and a rollback cannot undo the call.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..conf import Settings
from ..model import Issue, IssueKind, Span, Suggestion, Trace

__all__ = ["detect"]

_LONG: IssueKind = "long_transaction"
_EXTERNAL: IssueKind = "external_in_transaction"


def detect(trace: Trace, settings: Settings) -> list[Issue]:
    from ..detect import issue_id  # here: redline_core.detect imports this package

    txs = [s for s in trace.spans if s.kind == "transaction"]
    if not txs:
        return []
    issues: list[Issue] = []
    endpoint = f"{trace.method} {trace.route or trace.path}"
    limit = settings.long_transaction_ms
    http = [s for s in trace.spans if s.kind == "http_out"]
    for tx in txs:
        alias = str(tx.data.get("db_alias") or "default")
        loc = tx.callsite.location if tx.callsite else None
        if limit > 0 and tx.duration_ms >= limit:
            where = f" at {loc}" if loc else ""
            queries = int(tx.data.get("queries") or 0)
            issues.append(
                Issue(
                    id=issue_id(_LONG, alias, loc or endpoint),
                    kind=_LONG,
                    severity="critical" if tx.duration_ms >= 5 * limit else "warning",
                    title=f"Transaction open {tx.duration_ms:.0f} ms on {alias}{where}",
                    detail=(
                        f"A database transaction stayed open for {tx.duration_ms:.1f} ms "
                        f"({queries} quer{'y' if queries == 1 else 'ies'}, outcome "
                        f"{tx.data.get('outcome', '?')}), over the {limit:g} ms threshold. "
                        "Row locks and the connection are held until it ends."
                    ),
                    span_ids=[tx.id],
                    callsite=tx.callsite,
                    count=1,
                    total_ms=tx.duration_ms,
                    suggestion=Suggestion(
                        summary="Keep only the writes that must be atomic inside the "
                        "transaction; do reads, computation and I/O before it starts or "
                        "after it commits.",
                        confidence="low",
                    ),
                    extra={"db_alias": alias, "queries": queries},
                )
            )
        if not tx.data.get("http_out"):
            continue
        inside = _calls_inside(tx, http)
        first = inside[0] if inside else None
        host = _host(first) if first is not None else None
        callsite = (first.callsite if first is not None else None) or tx.callsite
        call_loc = callsite.location if callsite else None
        n = len(inside) or int(tx.data.get("http_out") or 0)
        title = f"HTTP call to {host}" if host else "HTTP call"
        if n > 1:
            title = f"{n} HTTP calls" + (f" to {host}" if host else "")
        title += " inside a transaction"
        if call_loc:
            title += f" ({call_loc})"
        hosts = sorted({h for h in (_host(s) for s in inside) if h})
        issues.append(
            Issue(
                id=issue_id(_EXTERNAL, host or alias, call_loc or endpoint),
                kind=_EXTERNAL,
                severity="warning",
                title=title,
                detail=(
                    f"{'An outbound call was' if n == 1 else f'{n} outbound calls were'} made "
                    f"while a transaction on {alias} was open. The transaction waits on the "
                    "remote service, and rolling back cannot undo what the call did."
                ),
                span_ids=[tx.id, *(s.id for s in inside)],
                callsite=callsite,
                count=n,
                total_ms=round(sum(s.duration_ms for s in inside), 3),
                suggestion=Suggestion(
                    summary="Move outbound calls outside the transaction (before atomic() / "
                    "session.begin(), or after commit); for side effects that must follow a "
                    "successful commit use transaction.on_commit (Django) or an after-commit "
                    "hook / outbox.",
                    code="transaction.on_commit(lambda: notify(order.id))",
                    confidence="medium",
                ),
                extra={"db_alias": alias, "hosts": hosts, "transaction_span_id": tx.id},
            )
        )
    return issues


def _calls_inside(tx: Span, http: list[Span]) -> list[Span]:
    end = tx.start_ms + tx.duration_ms
    return [s for s in http if tx.start_ms <= s.start_ms <= end]


def _host(span: Span) -> str | None:
    url = span.data.get("url")
    if isinstance(url, str) and url:
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        if host:
            return host
    name = span.name.split(" ", 1)[-1]
    return name.split("/", 1)[0] or None
