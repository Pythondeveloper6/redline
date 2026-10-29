"""Bounded SQL Server catalog reads for explicitly requested query inspection."""

from __future__ import annotations

import re

from . import sql as sqlmod
from .check_sql import lint


def inspect_query(sql, executor):
    references = list(sqlmod._TABLE_REF.finditer(sqlmod.fingerprint(sql)))
    names = {tuple(sqlmod._split_qualified(m.group("table"))) for m in references}
    if len(names) != 1:
        return {"findings": [], "limitations": ["SQL Server inspection requires one table."]}
    parts = next(iter(names))
    if len(parts) not in (1, 2) or not all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in parts
    ):
        return {"findings": [], "limitations": ["Use a simple table or schema.table reference."]}
    schema, table = parts if len(parts) == 2 else ("dbo", parts[0])
    scope = f"s.name = N'{schema}' AND o.name = N'{table}'"
    try:
        columns = executor(
            "SELECT TOP (500) c.name, t.name, c.max_length, cc.definition "
            "FROM sys.tables o JOIN sys.schemas s ON s.schema_id=o.schema_id "
            "JOIN sys.columns c ON c.object_id=o.object_id "
            "JOIN sys.types t ON t.user_type_id=c.user_type_id "
            "LEFT JOIN sys.computed_columns cc ON cc.object_id=c.object_id "
            "AND cc.column_id=c.column_id "
            f"WHERE {scope} ORDER BY c.column_id",
            None,
        )
        indexes = executor(
            "SELECT TOP (1000) i.name, c.name, ic.key_ordinal, ic.is_included_column "
            "FROM sys.tables o JOIN sys.schemas s ON s.schema_id=o.schema_id "
            "JOIN sys.indexes i ON i.object_id=o.object_id "
            "JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id "
            "JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id "
            f"WHERE {scope} AND i.is_hypothetical=0 ORDER BY i.name, ic.key_ordinal",
            None,
        )
        triggers = executor(
            "SELECT TOP (100) name FROM sys.triggers "
            f"WHERE parent_id=OBJECT_ID(N'[{schema}].[{table}]') AND is_disabled=0",
            None,
        )
    except Exception:
        return {
            "findings": [],
            "limitations": [
                "SQL Server catalog metadata is unavailable; verify connectivity "
                "and metadata visibility permissions."
            ],
        }
    selected = {c.lower() for _, c in sqlmod.select_columns(sql)}
    wide = [
        str(c[0])
        for c in columns
        if (str(c[1]).lower() in {"text", "ntext", "image", "xml"} or c[2] == -1)
        and ("*" in selected or str(c[0]).lower() in selected)
    ]
    findings = []
    if wide:
        findings.append(
            {
                "kind": "wide_columns",
                "title": "Large column types selected",
                "columns": wide,
                "detail": "Type metadata alone does not establish actual size "
                "or whether values are used.",
            }
        )
    if "output-without-into" in lint(sql, "mssql"):
        findings.append(
            {
                "kind": "output_without_into",
                "title": "Bare OUTPUT with enabled triggers"
                if triggers
                else "Bare OUTPUT requires trigger review",
                "detail": (
                    f"{len(triggers)} enabled triggers visible on this target. "
                    "Review OUTPUT INTO and result semantics; visibility can be incomplete."
                ),
            }
        )
    grouped_indexes = {}
    for row in indexes:
        entry = grouped_indexes.setdefault(
            str(row[0]),
            {"name": str(row[0]), "columns": [], "included_columns": [], "kind": "btree"},
        )
        entry["included_columns" if row[3] else "columns"].append(str(row[1]))
    return {
        "table": f"{schema}.{table}",
        "findings": findings,
        "columns": [{"name": str(c[0]), "type": str(c[1]), "expression": c[3]} for c in columns],
        "indexes": list(grouped_indexes.values()),
        "limitations": [
            "Metadata visibility follows the current database principal. "
            "No application query or DDL was executed; unqualified tables use dbo."
        ],
    }
