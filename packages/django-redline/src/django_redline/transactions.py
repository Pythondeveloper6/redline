"""``transaction.atomic`` → ``transaction`` spans.

``Atomic.__enter__``/``__exit__`` are wrapped (the decorator form and
``ATOMIC_REQUESTS`` go through them too). The first atomic block entered on a connection
while a trace is active opens the span and nested blocks (savepoints) only bump a depth
counter; the span closes when that block exits, with ``outcome`` ``commit`` or
``rollback``. "First while a trace is active" rather than "outermost": under a test
case's class-level atomic the request's own block is still reported. When no trace is
active each wrapper costs one ``ContextVar.get()``.
"""

from __future__ import annotations

import functools
from typing import Any

from django.db.transaction import Atomic, get_connection

from redline_core.recorder import current
from redline_core.transactions import begin_transaction, end_transaction

__all__ = ["install_atomic"]

_ATTR = "_redline_tx"


def install_atomic() -> None:
    """Wrap ``Atomic.__enter__``/``__exit__`` (idempotent)."""
    if getattr(Atomic.__enter__, "_redline", False):
        return
    enter, exit_ = Atomic.__enter__, Atomic.__exit__

    @functools.wraps(enter)
    def __enter__(self: Atomic) -> Any:
        builder = current()
        if builder is None or not builder.active:
            return enter(self)
        conn = get_connection(self.using)
        state = getattr(conn, _ATTR, None)
        if state is not None and state.builder is builder and not builder.finished:
            state.depth += 1
            try:
                return enter(self)
            except BaseException:
                state.depth -= 1
                raise
        state = begin_transaction(builder, builder.recorder.capturer.capture(skip=1))
        setattr(conn, _ATTR, state)
        try:
            return enter(self)
        except BaseException:
            setattr(conn, _ATTR, None)
            raise

    @functools.wraps(exit_)
    def __exit__(self: Atomic, exc_type: Any, exc: Any, tb: Any) -> Any:
        conn = get_connection(self.using)
        state = getattr(conn, _ATTR, None)
        if state is None:
            return exit_(self, exc_type, exc, tb)
        if state.depth > 0:
            state.depth -= 1
            return exit_(self, exc_type, exc, tb)
        setattr(conn, _ATTR, None)
        rollback = exc_type is not None or bool(conn.needs_rollback)
        try:
            return exit_(self, exc_type, exc, tb)
        except BaseException:
            rollback = True  # the COMMIT itself failed
            raise
        finally:
            try:
                end_transaction(
                    state, db_alias=conn.alias, outcome="rollback" if rollback else "commit"
                )
            except Exception:  # pragma: no cover - recording must never break the app
                pass

    __enter__._redline = True  # type: ignore[attr-defined]
    Atomic.__enter__ = __enter__  # type: ignore[method-assign]
    Atomic.__exit__ = __exit__  # type: ignore[method-assign]
