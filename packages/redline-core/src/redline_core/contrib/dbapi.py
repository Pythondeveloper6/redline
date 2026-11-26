"""SQL sent through a raw DB-API driver, with no SQLAlchemy engine, as ``sql`` spans.

Call :func:`instrument_dbapi` once at startup with the driver module (``sqlite3``,
``psycopg2``, ``pymysql`` ...). It replaces the module's ``connect`` with a function that
returns a recording connection proxy; ``execute``, ``executemany`` and ``callproc`` become
spans with ``source="raw_cursor"``. Opt-in and reversible with :func:`uninstrument_dbapi`.
"""

from __future__ import annotations

from typing import Any

from ._rawcursor import wrap_connection

__all__ = ["instrument_dbapi", "uninstrument_dbapi"]

_ORIGINAL = "_redline_original_connect"

_VENDORS = (
    ("sqlite", "sqlite"),
    ("psycopg", "postgresql"),
    ("pg8000", "postgresql"),
    ("mysql", "mysql"),
    ("pymysql", "mysql"),
    ("MySQLdb", "mysql"),
    ("pyodbc", "mssql"),
    ("pymssql", "mssql"),
    ("oracledb", "oracle"),
    ("cx_Oracle", "oracle"),
)


def _vendor_of(module: Any) -> str:
    from redline_core.vendors import get_vendor

    name = str(getattr(module, "__name__", ""))
    for prefix, vendor in _VENDORS:
        if name.startswith(prefix):
            return get_vendor(vendor).name
    return "other"


def instrument_dbapi(module: Any, alias: str = "default") -> bool:
    """Wrap ``module.connect`` so connections record their cursor statements. Idempotent."""
    if getattr(module, _ORIGINAL, None) is not None:
        return True
    original = module.connect
    vendor = _vendor_of(module)

    def connect(*args: Any, **kwargs: Any) -> Any:
        return wrap_connection(original(*args, **kwargs), vendor, alias)

    try:
        setattr(module, _ORIGINAL, original)
        module.connect = connect
    except (AttributeError, TypeError):
        return False
    return True


def uninstrument_dbapi(module: Any) -> None:
    """Undo :func:`instrument_dbapi` for ``module``."""
    original = getattr(module, _ORIGINAL, None)
    if original is not None:
        module.connect = original
        delattr(module, _ORIGINAL)
