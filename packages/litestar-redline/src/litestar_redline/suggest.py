"""Compatibility shim: the implementation lives in :mod:`redline_core.contrib.sqlalchemy`."""

from redline_core.contrib.sqlalchemy.suggest import SQLAlchemyFixSuggester, _ensure_import

__all__ = ["SQLAlchemyFixSuggester", "_ensure_import"]
