"""Explicit online compilation on a dedicated connection; never a schema guarantee."""

from __future__ import annotations

from .sql import effective_kinds
from .vendors import get_vendor


def compile_statement(engine, sql):
    """Compile a supported single statement without running its DML.

    No functions or application modules are imported. Server compilation can involve
    extensions and planner functions, so use an isolated development database/login.
    Dynamic/procedural batches and session-control statements are refused.
    """
    import re

    from sqlalchemy import text

    from .sql import _STRICT_INERT

    strict = _STRICT_INERT.sub(" ", sql).strip().rstrip(";")
    if len(effective_kinds(strict)) != 1 or re.search(
        r"\b(?:NOEXEC|SHOWPLAN_XML|EXEC|EXECUTE|COMMIT|ROLLBACK|GO)\b", strict, re.I
    ):
        return {"status": "unresolved", "reason": "Ambiguous or control-flow SQL is not compiled"}
    kinds = effective_kinds(sql)
    if len(kinds) != 1 or kinds[0] not in {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE"}:
        return {
            "status": "unresolved",
            "reason": "Only a single supported query/DML statement can be compiled",
        }
    vendor = get_vendor(engine.dialect.name)
    statement = text(sql)
    values = dict.fromkeys(statement.compile().params, None)
    with engine.connect() as conn:
        cleanup = None
        try:
            if vendor.name == "sqlite":
                conn.execute(text("EXPLAIN " + sql), values).fetchall()
            elif vendor.name == "postgresql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                conn.execute(text("EXPLAIN (COSTS OFF) " + sql), values).fetchall()
            elif vendor.name == "mysql":
                from .sql import _TOKEN

                prepared = _TOKEN.sub(
                    lambda m: (
                        "?"
                        if m.lastgroup in {"named", "qmark", "pyformat", "numbered"}
                        else m.group()
                    ),
                    sql,
                )
                conn.execute(text("PREPARE redline_check FROM :statement"), {"statement": prepared})
                cleanup = "DEALLOCATE PREPARE redline_check"
            elif vendor.name == "mssql":
                cleanup = "SET NOEXEC OFF"
                conn.exec_driver_sql("SET NOEXEC ON")
                conn.execute(statement, values)
            else:
                return {
                    "status": "unresolved",
                    "reason": "Online compilation is not implemented for this dialect",
                }
            return {
                "status": "compiled",
                "limitation": (
                    "Compilation is not execution validation; deferred names, "
                    "parameter types, triggers and temporary objects may remain "
                    "unchecked."
                ),
            }
        except Exception as exc:
            error = vendor.classify_error(getattr(exc, "orig", exc))
            # Do not export driver messages: they may echo parameters, schema literals or DSNs.
            return {"status": "error", "error": error}
        finally:
            try:
                if cleanup:
                    conn.exec_driver_sql(cleanup)
            except BaseException:
                conn.invalidate()
                raise
            finally:
                conn.rollback()


def compile_paths(paths, dsn, *, snapshot=None):
    """Explicit CLI-only DSN; credentials never appear in the report."""
    from pathlib import Path

    from sqlalchemy import create_engine

    from .check_sql import extract

    engine = create_engine(dsn)
    reports = []
    try:
        if snapshot:
            import json

            destination = Path(snapshot)
            current_schema = schema_snapshot(engine)
            previous = (
                json.loads(destination.read_text()) if destination.exists() else {"tables": {}}
            )
            reports.append(
                {
                    "file": str(destination),
                    "line": 1,
                    "status": "compiled",
                    "schema_drift": compare_schema(previous, current_schema),
                }
            )
            destination.write_text(json.dumps(current_schema, indent=2) + "\n")
        for name in paths:
            path = Path(name)
            if path.is_symlink() or not path.is_file() or path.suffix != ".py":
                reports.append(
                    {
                        "file": str(path),
                        "line": 1,
                        "status": "unresolved",
                        "reason": "Online mode accepts explicit Python files",
                    }
                )
                continue
            with path.open("rb") as stream:
                raw = stream.read(200001)
            statements, skipped = extract(raw.decode("utf-8"), str(path))
            reports.extend({**s, "status": "unresolved"} for s in skipped)
            for statement in statements:
                reports.append(
                    {
                        "file": str(path),
                        "line": statement["line"],
                        **compile_statement(engine, statement["sql"]),
                    }
                )
    finally:
        engine.dispose()
    return reports


def schema_snapshot(engine):
    """Bounded metadata snapshot of the connection's default schema, without row data."""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    schema = inspector.default_schema_name
    tables = inspector.get_table_names(schema=schema)
    if len(tables) > 200:
        raise ValueError("Schema snapshot exceeds 200-table limit")
    result = {}
    for table in sorted(tables):
        columns = inspector.get_columns(table, schema=schema)
        if len(columns) > 500:
            raise ValueError("Schema snapshot exceeds 500-column limit")
        result[table] = [
            {"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable")}
            for c in columns
        ]
    return {
        "vendor": get_vendor(engine.dialect.name).name,
        "schema": schema,
        "tables": result,
        "limitation": (
            "Default schema column metadata only; no certification of stored "
            "procedures, triggers or application behavior."
        ),
    }


def compare_schema(before, after):
    """Name/type/nullability drift; callers still compile every extracted statement."""
    old, new = before.get("tables", {}), after.get("tables", {})
    return {
        "added_tables": sorted(set(new) - set(old)),
        "removed_tables": sorted(set(old) - set(new)),
        "changed_tables": sorted(k for k in old.keys() & new.keys() if old[k] != new[k]),
    }
