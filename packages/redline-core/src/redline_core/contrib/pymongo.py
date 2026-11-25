"""pymongo collection calls as spans (document database).

Call :func:`instrument_pymongo` once at startup. Each ``Collection`` write, count and
``find_one`` call becomes one ``custom`` span with ``client="mongodb"``, the operation, the
``database.collection`` target and, on failure, the exception class. A ``find()`` cursor
fetches lazily, so its network batches (``Cursor._refresh``) are recorded as ``find``
spans instead of the cursor's construction. Filters, documents and results are never
captured. Built on :func:`redline_core.extending.instrument_client_method`; pymongo is
imported only when this function runs.
"""

from __future__ import annotations

from typing import Any

from redline_core.extending import instrument_client_method

__all__ = ["instrument_pymongo", "uninstrument_pymongo"]

_originals: dict[str, Any] = {}
_COLLECTION_OPS = (
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "replace_one",
    "delete_one",
    "delete_many",
    "bulk_write",
    "find_one",
    "find_one_and_update",
    "find_one_and_replace",
    "find_one_and_delete",
    "count_documents",
    "estimated_document_count",
    "distinct",
    "create_index",
)


def _collection_name(self: Any, args: Any, kwargs: Any) -> str | None:
    name = getattr(self, "full_name", None)
    return name if isinstance(name, str) else None


def _cursor_target(self: Any, args: Any, kwargs: Any) -> str | None:
    return _collection_name(getattr(self, "collection", None), args, kwargs)


def instrument_pymongo() -> bool:
    """Patch pymongo's ``Collection`` and ``Cursor``; ``False`` if pymongo is missing."""
    if _originals:
        return True
    try:
        from pymongo.collection import Collection
        from pymongo.cursor import Cursor
    except ImportError:
        return False
    for op in _COLLECTION_OPS:
        if hasattr(Collection, op):
            _originals[f"collection.{op}"] = instrument_client_method(
                Collection, op, client="mongodb", operation=op, target=_collection_name
            )
    if hasattr(Cursor, "_refresh"):
        _originals["cursor._refresh"] = instrument_client_method(
            Cursor, "_refresh", client="mongodb", operation="find", target=_cursor_target
        )
    return True


def uninstrument_pymongo() -> None:
    """Undo :func:`instrument_pymongo`."""
    for restore in list(_originals.values()):
        restore()
    _originals.clear()
