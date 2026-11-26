"""What the database login may do for Redline's plan and index tools, found out once.

SQL Server plan and catalog features each need a specific permission. Without a check, the
first plan request fails with a raw driver error and nobody can tell a broken feature from a
missing grant. :func:`probe_mssql` asks the server (``HAS_PERMS_BY_NAME``; it reads nothing
from user tables and changes nothing) and returns one entry per capability:

``estimated_plan``      ``SET SHOWPLAN_XML ON`` (database permission ``SHOWPLAN``)
``actual_plan``         ``SET STATISTICS XML ON`` (``SHOWPLAN`` plus the right to run the query)
``index_usage``         ``sys.dm_db_index_usage_stats`` (``VIEW SERVER STATE``, or
                        ``VIEW DATABASE STATE`` on Azure SQL and SQL Server 2022+)
``object_definitions``  catalog visibility of tables and indexes (``VIEW DEFINITION``)

Each entry has a ``status`` of ``granted``, ``missing`` or ``unknown`` and, when missing, the
exact ``GRANT`` statement to ask the database administrator for. :func:`permission_hint` turns
a raw permission error into the same message.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

__all__ = [
    "CAPABILITIES",
    "capabilities_for",
    "partial_visibility",
    "permission_hint",
    "probe_mssql",
]

CAPABILITIES = ("estimated_plan", "actual_plan", "index_usage", "object_definitions")

#: ``sys.dm_db_index_usage_stats`` counters reset on restart and on a managed-instance failover.
USAGE_NOTE = (
    "Usage counters reset on a restart or failover, so 'unused' only means unused since then."
)

_PROBE_SQL = (
    "SELECT USER_NAME(), "
    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'SHOWPLAN'), "
    "HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE'), "
    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DATABASE STATE'), "
    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION'), "
    "CAST(SERVERPROPERTY('EngineEdition') AS int)"
)

#: Engine editions of the managed services (Azure SQL Database, Managed Instance, Edge).
_AZURE_EDITIONS = {5, 8, 9, 11}


def _quote(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def _flag(value: Any) -> bool | None:
    if value is None:
        return None
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return None


def probe_mssql(executor: Callable[[str, Any], Sequence[Sequence[Any]]]) -> dict[str, Any]:
    """Probe the four capabilities through ``executor(sql, params) -> rows`` (read only)."""
    try:
        rows = list(executor(_PROBE_SQL, None))
        row = rows[0]
        user = str(row[0] or "")
        showplan, server_state, db_state, definition = (_flag(v) for v in row[1:5])
        edition = int(row[5]) if row[5] is not None else 0
    except Exception as exc:
        return {
            "vendor": "mssql",
            "principal": None,
            "managed": None,
            "capabilities": {
                name: {
                    "status": "unknown",
                    "detail": f"The permission probe failed ({type(exc).__name__}).",
                    "grant": None,
                }
                for name in CAPABILITIES
            },
        }
    managed = edition in _AZURE_EDITIONS
    who = _quote(user or "<login>")
    state_ok = db_state if managed else (server_state or db_state)
    grant_state = (
        f"GRANT VIEW DATABASE STATE TO {who};"
        if managed
        else f"GRANT VIEW SERVER STATE TO {who};  -- or VIEW DATABASE STATE on SQL Server 2022+"
    )

    def entry(ok: bool | None, what: str, grant: str) -> dict[str, Any]:
        if ok is None:
            return {"status": "unknown", "detail": f"Could not tell whether {what}.", "grant": None}
        if ok:
            return {"status": "granted", "detail": what[0].upper() + what[1:] + ".", "grant": None}
        return {"status": "missing", "detail": f"This login cannot {what}.", "grant": grant}

    return {
        "vendor": "mssql",
        "principal": user or None,
        "managed": managed,
        "capabilities": {
            "estimated_plan": entry(
                showplan, "read estimated plans (SHOWPLAN)", f"GRANT SHOWPLAN TO {who};"
            ),
            "actual_plan": entry(
                showplan, "collect actual plans (SHOWPLAN)", f"GRANT SHOWPLAN TO {who};"
            ),
            "index_usage": entry(
                state_ok, "read index usage statistics (VIEW ... STATE)", grant_state
            ),
            "object_definitions": entry(
                definition,
                "see object definitions (VIEW DEFINITION)",
                f"GRANT VIEW DEFINITION TO {who};",
            ),
        },
    }


def partial_visibility(report: dict[str, Any] | None) -> bool:
    """True when the probe shows catalog visibility is incomplete (index advice is then less
    certain). Unknown capabilities do not count: only a confirmed missing grant does."""
    if not report:
        return False
    caps = report.get("capabilities", {})
    return any(
        caps.get(name, {}).get("status") == "missing"
        for name in ("object_definitions", "index_usage")
    )


# ── permission errors → a clear message ────────────────────────────────────────────────

_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"SHOWPLAN permission denied", re.I), "SHOWPLAN", "GRANT SHOWPLAN TO {who};"),
    (
        re.compile(r"VIEW SERVER STATE permission", re.I),
        "VIEW SERVER STATE",
        "GRANT VIEW SERVER STATE TO {who};",
    ),
    (
        re.compile(r"VIEW DATABASE STATE permission", re.I),
        "VIEW DATABASE STATE",
        "GRANT VIEW DATABASE STATE TO {who};",
    ),
    (
        re.compile(r"VIEW DEFINITION permission", re.I),
        "VIEW DEFINITION",
        "GRANT VIEW DEFINITION TO {who};",
    ),
)
#: Error numbers: 262 (permission denied in database), 297/300 (VIEW SERVER/DATABASE STATE).
_NUMBERS = {262: "SHOWPLAN", 297: "VIEW SERVER STATE", 300: "VIEW SERVER STATE"}


def permission_hint(exc: BaseException, principal: str | None = None) -> dict[str, str] | None:
    """``{permission, grant, message}`` when ``exc`` is a known SQL Server permission error."""
    text = str(exc)
    who = _quote(principal) if principal else "[<login>]"
    for pattern, permission, grant in _PATTERNS:
        if pattern.search(text):
            return _hint(permission, grant.format(who=who))
    for arg in getattr(exc, "args", ()) or ():
        number = getattr(arg, "number", None)
        if isinstance(arg, int):
            number = arg
        if number in _NUMBERS:
            permission = _NUMBERS[number]
            return _hint(permission, f"GRANT {permission} TO {who};")
    m = re.search(r"\((\d{3,5})\)", text)  # pyodbc: "... (262) (SQLExecDirectW)"
    if m and int(m.group(1)) in _NUMBERS:
        permission = _NUMBERS[int(m.group(1))]
        return _hint(permission, f"GRANT {permission} TO {who};")
    return None


def _hint(permission: str, grant: str) -> dict[str, str]:
    return {
        "permission": permission,
        "grant": grant,
        "message": (
            f"The database login lacks the {permission} permission. Ask the database "
            f"administrator to run: {grant}  You can also point Redline at a separate login "
            "for plans with the explain_url setting, or paste a plan as XML."
        ),
    }


# ── a small cache: the probe runs once per database, not per request ───────────────────

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()
TTL_S = 600.0
_MAX_CACHE = 64


def capabilities_for(
    key: str,
    executor: Callable[[str, Any], Sequence[Sequence[Any]]],
    vendor: str,
    *,
    refresh: bool = False,
) -> dict[str, Any] | None:
    """The cached probe for ``key`` (a database alias), or ``None`` for vendors without one."""
    if vendor != "mssql":
        return None
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and not refresh and now - hit[0] < TTL_S:
            return hit[1]
    report = probe_mssql(executor)
    with _LOCK:
        if len(_CACHE) >= _MAX_CACHE:  # bounded: aliases from callers cannot grow it
            _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]))
        _CACHE[key] = (now, report)
    return report


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
