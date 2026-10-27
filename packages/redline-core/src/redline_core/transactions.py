"""``transaction`` spans, shared by the Django and SQLAlchemy hooks (``CONTRACT.md`` §8).

A hook calls :func:`begin_transaction` when the outermost transaction opens and
:func:`end_transaction` when it commits or rolls back. Only the span *index* and the
clock are noted at the start; at the end the spans recorded since are scanned once to
count the queries (transaction control statements such as ``SAVEPOINT`` excluded) and
outbound calls made inside, and the SQL spans on that database
get ``data["in_transaction"] = True``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal

from . import recorder as _rec
from .model import Callsite, Span, new_id

if TYPE_CHECKING:
    from .recorder import TraceBuilder

__all__ = ["TransactionState", "begin_transaction", "end_transaction"]

_perf = time.perf_counter
_CONTROL = ("SAVEPOINT", "RELEASE", "ROLLBACK", "BEGIN", "COMMIT", "START TRANSACTION")


def _is_control(sql: object) -> bool:
    """``SAVEPOINT``/``RELEASE``/``BEGIN``…: transaction bookkeeping, not a query."""
    return isinstance(sql, str) and sql.lstrip()[:20].upper().startswith(_CONTROL)


class TransactionState:
    """What :func:`begin_transaction` remembers about an open transaction."""

    __slots__ = ("builder", "callsite", "depth", "index", "parent_id", "start")

    def __init__(self, builder: TraceBuilder, callsite: Callsite | None) -> None:
        self.builder = builder
        self.callsite = callsite
        self.parent_id = _rec._parent.get()
        self.depth = 0  # nested blocks entered inside the outermost one (Django)
        with builder._lock:
            self.index = len(builder.spans)
        self.start = _perf()


def begin_transaction(
    builder: TraceBuilder | None = None, callsite: Callsite | None = None
) -> TransactionState | None:
    """Note the start of a transaction on ``builder`` (default: the current trace)."""
    if builder is None:
        builder = _rec.current()
    if builder is None or not builder.active or builder.finished:
        return None
    return TransactionState(builder, callsite)


def end_transaction(
    state: TransactionState | None,
    *,
    db_alias: str,
    outcome: Literal["commit", "rollback"],
    keep_empty: bool = False,
) -> Span | None:
    """Record the ``transaction`` span for ``state``; ``None`` if nothing ran inside it.

    Transactions with no query and no outbound call are not recorded unless
    ``keep_empty`` – an empty ``BEGIN … ROLLBACK`` is noise.
    """
    if state is None:
        return None
    builder = state.builder
    if builder.finished:
        return None
    end = _perf()
    start_ms = (state.start - builder.t0) * 1000.0
    queries = http = 0
    with builder._lock:
        for s in builder.spans[state.index :]:
            if s.kind == "sql":
                if s.data.get("db_alias", db_alias) == db_alias:
                    s.data["in_transaction"] = True
                    if not _is_control(s.data.get("sql")):
                        queries += 1
            elif s.kind == "http_out" and s.start_ms >= start_ms:
                http += 1
    if not (queries or http or keep_empty):
        return None
    span = Span(
        id=new_id(),
        parent_id=state.parent_id,
        kind="transaction",
        name=f"transaction {db_alias}",
        start_ms=round(max(0.0, start_ms), 3),
        duration_ms=round((end - state.start) * 1000.0, 3),
        callsite=state.callsite,
        data={"db_alias": db_alias, "outcome": outcome, "queries": queries, "http_out": http},
    )
    builder.add(span)
    return span
