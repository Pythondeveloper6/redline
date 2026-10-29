"""Database identity and driver-error classification without importing drivers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Vendor:
    name: str
    dialects: tuple[str, ...]

    def classify_error(self, exc: BaseException, *, disconnect: bool = False) -> dict[str, Any]:
        args = getattr(exc, "args", ())
        if type(args) is not tuple:
            args = ()
        state = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        if type(state) is not str or not re.fullmatch(r"[A-Z0-9]{5}", state):
            state = None
        if (
            state is None
            and args
            and type(args[0]) is str
            and re.fullmatch(r"[A-Z0-9]{5}", args[0])
        ):
            state = args[0]
        native = getattr(exc, "sqlite_errorcode", None)
        if native is None and args and type(args[0]) is int:
            native = args[0]
        if self.name == "mssql":
            # Driver messages are ordinary args; avoid application __str__ hooks.
            suffix = " ".join(arg[-4096:] for arg in args[:4] if type(arg) is str)
            matches = re.findall(r"\((\d{1,10})\)(?:\s*\(SQL\w+\))?", suffix)
            if matches:
                native = int(matches[-1])
        if type(native) is not int or native.bit_length() > 63:
            native = None
        category = "unknown"
        if disconnect or str(state).startswith("08"):
            category = "disconnect"
        elif state in ("40P01", "40001"):
            category = "deadlock" if state == "40P01" else "transient"
        elif state in ("HYT00", "HYT01", "57014"):
            category = "timeout"
        elif state in ("42703", "42P01", "42S22", "42S02"):
            category = "schema"
        elif state == "07002":
            category = "parameter"
        elif str(state).startswith("23"):
            category = "constraint"
        codes = {
            "mssql": {
                207: "schema",
                208: "schema",
                2705: "schema",
                334: "trigger_output",
                8178: "parameter",
                1205: "deadlock",
                229: "permission",
                262: "permission",
                40197: "transient",
                40501: "transient",
                40613: "transient",
                49918: "transient",
                49919: "transient",
                49920: "transient",
            },
            "mysql": {
                1054: "schema",
                1146: "schema",
                1213: "deadlock",
                1205: "timeout",
                2006: "disconnect",
                2013: "disconnect",
            },
            "sqlite": {19: "constraint", 5: "timeout", 6: "timeout"},
        }
        if category == "unknown":
            category = codes.get(self.name, {}).get(native, "unknown")
        return {
            "type": type(exc).__name__,
            "sqlstate": state,
            "native": native,
            "category": category,
            "is_disconnect": disconnect or category == "disconnect",
        }


_REGISTRY = {
    v.name: v
    for v in (
        Vendor("postgresql", ("postgresql", "postgres")),
        Vendor("mysql", ("mysql", "mariadb")),
        Vendor("sqlite", ("sqlite",)),
        Vendor("mssql", ("mssql", "microsoft", "sql_server", "sqlserver")),
        Vendor("other", ("other",)),
    )
}


def register(vendor: Vendor) -> None:
    """Register an explicitly supplied vendor; no plugins are imported automatically."""
    if not isinstance(vendor, Vendor) or not re.fullmatch(r"[a-z][a-z0-9_]*", vendor.name):
        raise ValueError("Expected a Vendor with a simple lowercase name")
    _REGISTRY[vendor.name] = vendor


def get_vendor(name: str) -> Vendor:
    for vendor in _REGISTRY.values():
        if name in vendor.dialects or name == vendor.name:
            return vendor
    return _REGISTRY["other"]
