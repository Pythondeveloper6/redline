"""Explicit online compilation on a dedicated connection; never a schema guarantee."""

from __future__ import annotations

import hashlib
import re

from .sql import effective_kinds, fingerprint
from .vendors import get_vendor


def _mssql_literal(sql):
    return "N'" + sql.replace("'", "''") + "'"


def _translate_named_binds(sql):
    from .sql import _TOKEN

    return _TOKEN.sub(
        lambda match: "@" + match.group()[1:] if match.lastgroup == "named" else match.group(),
        sql,
    )


def describe_mssql_parameters(conn, sql):
    """Infer parameter types without executing the statement.

    Returns ``(parameters, limitation)``. Driver or permission failures degrade to an
    untyped compile; they never turn into a passing schema guarantee.
    """
    try:
        rows = (
            conn.exec_driver_sql(
                "EXEC sys.sp_describe_undeclared_parameters @tsql = " + _mssql_literal(sql)
            )
            .mappings()
            .all()
        )
    except Exception:
        return [], "Parameter-type metadata was unavailable; compilation used declared names."
    parameters = []
    for row in rows[:64]:
        record = dict(row)
        name = record.get("name") or record.get("parameter_name")
        type_name = record.get("suggested_system_type_name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w{0,127}", name):
            continue
        if not isinstance(type_name, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_ ]*(?:\([0-9, ]+\))?", type_name.strip()
        ):
            type_name = None
        parameters.append({"name": name, "type": type_name})
    return parameters, None


def describe_mssql_result(conn, sql, kinds):
    """Describe a single statement's result columns without executing it."""
    if len(kinds) != 1 or kinds[0] not in {"SELECT", "WITH", "VALUES"}:
        return [], None
    try:
        rows = (
            conn.exec_driver_sql(
                "EXEC sys.sp_describe_first_result_set @tsql = " + _mssql_literal(sql)
            )
            .mappings()
            .all()
        )
    except Exception:
        return [], "Result-set metadata was unavailable for this statement."
    if rows and any(dict(row).get("error_number") is not None for row in rows):
        return [], "SQL Server could not describe this statement's result set."
    columns = []
    hidden = ("is_hidden", "is_hidden_column")
    for row in rows[:512]:
        record = dict(row)
        if any(record.get(key) for key in hidden):
            continue
        name = record.get("name")
        if isinstance(name, str):
            columns.append({"name": name, "type": str(record.get("system_type_name") or "")})
    return columns, None


def _mssql_compile_batch(sql, parameters):
    declares, seen = [], set()
    for parameter in parameters:
        name, type_name = parameter.get("name"), parameter.get("type")
        if not name or not type_name or name.lower() in seen:
            continue
        if re.search(rf"\bDECLARE\s+@{re.escape(name)}\b", sql, re.I):
            continue
        seen.add(name.lower())
        declares.append(f"DECLARE @{name} {type_name};")
    return "\n".join([*declares, _translate_named_binds(sql)])


def missing_result_columns(reads, columns):
    """Literal row reads absent from a described result set, sorted and deduplicated."""
    known = {str(column.get("name", "")).lower() for column in columns}
    return sorted(
        {
            str(read.get("name"))
            for read in reads or ()
            if str(read.get("name", "")).lower() not in known
        }
    )


def compile_statement(engine, sql, *, result_reads=None):
    """Compile a supported single statement without running its DML.

    No functions or application modules are imported. Server compilation can involve
    extensions and planner functions, so use an isolated development database/login.
    Dynamic/procedural batches and session-control statements are refused.
    """
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
        limitations = []
        extras = {}
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
                parameters, limitation = describe_mssql_parameters(conn, sql)
                if limitation:
                    limitations.append(limitation)
                columns, limitation = describe_mssql_result(conn, sql, kinds)
                if limitation:
                    limitations.append(limitation)
                extras["parameters"] = parameters
                extras["result_columns"] = columns
                missing = missing_result_columns(result_reads, columns) if columns else []
                if missing:
                    extras["result_reads_missing"] = missing
                cleanup = "SET NOEXEC OFF"
                conn.exec_driver_sql("SET NOEXEC ON")
                conn.exec_driver_sql(_mssql_compile_batch(sql, parameters))
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
                "limitations": limitations,
                **extras,
            }
        except Exception as exc:
            error = vendor.classify_error(getattr(exc, "orig", exc))
            # Do not export driver messages: they may echo parameters, schema literals or DSNs.
            return {"status": "error", "error": error, "limitations": limitations, **extras}
        finally:
            try:
                if cleanup:
                    conn.exec_driver_sql(cleanup)
            except BaseException:
                conn.invalidate()
                raise
            finally:
                conn.rollback()


def _missing_column_finding(file, statement, name):
    identity = (
        f"{file}:{statement['line']}:result-column-not-returned:"
        f"{fingerprint(statement['sql'])}:{name.lower()}"
    )
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest()[:20],
        "kind": "result-column-not-returned",
        "file": file,
        "line": statement["line"],
        "detail": (
            f"Row code reads column {name!r}, which the compiled SELECT does not return. "
            "This is a best-effort literal-read check."
        ),
        "confidence": "low",
        "fingerprint": fingerprint(statement["sql"]),
    }


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
                compiled = compile_statement(
                    engine,
                    statement["sql"],
                    result_reads=statement.get("result_reads"),
                )
                item = {"file": str(path), "line": statement["line"], **compiled}
                missing = compiled.get("result_reads_missing") or []
                if missing:
                    item["findings"] = [
                        _missing_column_finding(str(path), statement, name) for name in missing
                    ]
                reports.append(item)
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
