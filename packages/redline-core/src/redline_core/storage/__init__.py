"""Trace storage: the :class:`Store` protocol, the default :class:`SQLiteStore` and the
shared :class:`PostgresStore`."""

from __future__ import annotations

from typing import Any

from .base import (
    WINDOWS,
    Bottlenecks,
    CacheStats,
    EndpointStats,
    ExtendedStore,
    ExternalStats,
    FilterError,
    Filters,
    History,
    IssueGroup,
    IssueIndexEntry,
    Page,
    Period,
    PeriodComparison,
    PeriodStats,
    ReleaseInfo,
    SqlStats,
    Store,
    parse_period,
    parse_window,
    percentile,
)
from .postgres import PostgresStore
from .sqlite import SQLiteStore, SQLStore


def store_from_settings(settings: Any, **kwargs: Any) -> Any:
    """The store the settings name: :class:`PostgresStore` when ``storage_url`` is set,
    else the SQLite file at ``storage_path`` (the default). ``extra_stores`` (read-only
    SQLite files browsed next to the primary store) apply to both."""
    url = getattr(settings, "storage_url", None)
    if not url:
        return SQLiteStore.from_settings(settings, **kwargs)
    primary = PostgresStore.from_settings(settings, **kwargs)
    if settings.extra_stores:
        from .multiple import MultipleStores

        return MultipleStores(primary, settings.extra_stores)
    return primary


__all__ = [
    "WINDOWS",
    "Bottlenecks",
    "CacheStats",
    "EndpointStats",
    "ExtendedStore",
    "ExternalStats",
    "FilterError",
    "Filters",
    "History",
    "IssueGroup",
    "IssueIndexEntry",
    "Page",
    "Period",
    "PeriodComparison",
    "PeriodStats",
    "PostgresStore",
    "ReleaseInfo",
    "SQLStore",
    "SQLiteStore",
    "SqlStats",
    "Store",
    "parse_period",
    "parse_window",
    "percentile",
    "store_from_settings",
]
