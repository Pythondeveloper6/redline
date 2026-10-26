"""The EXPLAIN executor the core API calls: Django ``connections[alias]``, always rolled back.

The statement runs through a normal Django cursor, so the captured ``%s`` placeholders work
on every backend (Django's cursor wrappers translate them to the driver's paramstyle). It
runs inside ``transaction.atomic`` with ``set_rollback(True)``, and on PostgreSQL – when it
owns the transaction – the transaction is also ``READ ONLY``. Nothing it executes is
recorded as a span.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from django.db import connections, transaction
from django.db.utils import ConnectionDoesNotExist

from .instrument import suppress

__all__ = ["executor_factory"]

Executor = Callable[[str, Any], Sequence[Sequence[Any]]]


def executor_factory(db_alias: str, vendor: str) -> Executor | None:
    """``(db_alias, vendor) -> executor`` for :class:`redline_core.api.RedlineAPI`."""
    try:
        connection = connections[db_alias]
    except ConnectionDoesNotExist:
        return None
    if vendor not in ("postgresql", "mysql", "sqlite"):
        return None
    if connection.vendor != vendor:
        return None

    def execute(sql: str, params: Any) -> list[Sequence[Any]]:
        if isinstance(params, list):
            params = tuple(params)
        with suppress():
            outermost = not connection.in_atomic_block
            with transaction.atomic(using=db_alias):
                try:
                    with connection.cursor() as cursor:
                        if outermost and connection.vendor == "postgresql":
                            cursor.execute("SET TRANSACTION READ ONLY")
                        cursor.execute(sql, params)
                        rows = list(cursor.fetchall())
                finally:
                    transaction.set_rollback(True, using=db_alias)
        return rows

    return execute
