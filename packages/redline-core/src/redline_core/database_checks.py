"""On-demand catalog checks. Never creates indexes or executes application queries."""

from __future__ import annotations

import hashlib
import re

from . import sql as sqlmod
from .index_advisor import load_catalog, query_requirements

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def inspect_query(sql: str, vendor: str, executor) -> dict:
    table = sqlmod.primary_table(sql)
    tables = sqlmod.tables(sql)
    qualified = any(
        len(sqlmod._split_qualified(m.group("table"))) > 1
        for m in sqlmod._TABLE_REF.finditer(sqlmod.fingerprint(sql))
    )
    if qualified or not table or not _IDENTIFIER.fullmatch(table) or len(tables) != 1:
        return {
            "findings": [],
            "limitations": ["Catalog checks currently require one unqualified table."],
        }
    columns = []
    if vendor == "sqlite":
        for row in executor(f'PRAGMA table_xinfo("{table}")', None):
            columns.append({"name": str(row[1]), "type": str(row[2]), "expression": None})
    elif vendor in {"postgresql", "mysql"}:
        scope = (
            "table_schema = current_schema()"
            if vendor == "postgresql"
            else "table_schema = DATABASE()"
        )
        rows = executor(
            "SELECT column_name, data_type, generation_expression FROM information_schema.columns "
            f"WHERE {scope} AND table_name = '{table}' ORDER BY ordinal_position LIMIT 500",
            None,
        )
        columns = [{"name": str(row[0]), "type": str(row[1]), "expression": row[2]} for row in rows]
    else:
        return {"findings": [], "limitations": ["Unsupported database vendor."]}
    catalog = load_catalog(vendor, executor)
    indexes = catalog.indexes.get(table.lower(), [])
    findings = []
    selected = {column.lower() for _, column in sqlmod.select_columns(sql)}
    large_types = {
        "text",
        "json",
        "jsonb",
        "blob",
        "bytea",
        "longtext",
        "mediumtext",
        "longblob",
        "mediumblob",
    }
    wide = [
        c["name"]
        for c in columns
        if c["type"].lower() in large_types and ("*" in selected or c["name"].lower() in selected)
    ]
    if wide:
        findings.append(
            {
                "kind": "wide_columns",
                "title": "Large column types selected",
                "detail": "Review these TEXT/JSON/binary columns. Types do not prove size or use.",
                "columns": wide,
                "suggestion": "Review only()/defer()/load_only() or explicit projections.",
            }
        )

    # Preserve JSON path literals: fingerprinting them would incorrectly equate different paths.
    def compact(text):
        pieces = []
        previous = 0
        for token in sqlmod._TOKEN.finditer(text):
            pieces.append(re.sub(r"\s+", "", text[previous : token.start()]))
            if token.lastgroup not in {"lcomment", "bcomment"}:
                pieces.append(
                    token.group()
                    if token.lastgroup in {"string", "dollar", "dident", "bident"}
                    else token.group().lower()
                )
            previous = token.end()
        pieces.append(re.sub(r"\s+", "", text[previous:]))
        return "".join(pieces)

    expression_starts = [
        token.start()
        for token in sqlmod._TOKEN.finditer(sql)
        if token.lastgroup in {"word", "dident", "bident"}
    ][:256]
    for column in columns:
        expression = column["expression"]
        if not expression or not any(op in str(expression).lower() for op in ("json", "->", "#>")):
            continue
        indexed = any(column["name"].lower() in cols for _, cols, _ in indexes)
        if indexed and any(
            compact(sql[start:]).startswith(compact(str(expression))) for start in expression_starts
        ):
            findings.append(
                {
                    "kind": "generated_json_column",
                    "title": "Indexed generated JSON column exists",
                    "detail": "Review using the matching generated column. "
                    "Verify types, nulls and collation.",
                    "column": column["name"],
                    "expression": str(expression),
                }
            )
    # Only bounded single-table aggregate projections with simple column operands.
    projection = re.split(r"\bFROM\b", sqlmod.structural_sql(sql), maxsplit=1, flags=re.I)[0]
    operands = re.findall(
        r"\b(?:SUM|AVG|MIN|MAX|COUNT)\s*\(\s*([A-Za-z_]\w*)\s*\)", projection, re.I
    )
    if operands and not re.search(r"\b(DISTINCT|OVER|UNION|JOIN)\b", sql, re.I):
        known = {c["name"].lower() for c in columns}
        for req in query_requirements(sql, vendor=vendor):
            if req.table.lower() != table.lower() or req.kind != "btree":
                continue
            keys = [*sorted(req.eq), *req.tail]
            include = sorted({*operands, *selected} - set(keys))
            if (
                not keys
                or not include
                or len(keys) + len(include) > 6
                or not set(keys + include) <= known
            ):
                continue
            if any(set(keys + include) <= set(cols) for _, cols, _ in indexes):
                continue

            def q(text):
                quote = "`" if vendor == "mysql" else '"'
                return quote + text.replace(quote, quote * 2) + quote

            name = (
                "ix_cover_"
                + hashlib.sha256((table + repr(keys + include)).encode()).hexdigest()[:10]
            )
            statement = f"CREATE INDEX {q(name)} ON {q(table)} ({', '.join(q(k) for k in keys)})"
            if vendor == "postgresql":
                statement += f" INCLUDE ({', '.join(q(k) for k in include)})"
            else:
                all_columns = ", ".join(q(k) for k in keys + include)
                statement = f"CREATE INDEX {q(name)} ON {q(table)} ({all_columns})"
            findings.append(
                {
                    "kind": "covering_index",
                    "title": "Covering index candidate",
                    "detail": "Potentially cover filter/group/projection columns. "
                    "Validate with a plan; account for write overhead and size. "
                    "Index-only execution is not guaranteed.",
                    "suggestion": statement + ";",
                    "key_columns": keys,
                    "include_columns": include,
                }
            )
    return {
        "table": table,
        "columns": columns,
        "indexes": [
            {"name": name, "columns": list(cols), "kind": kind} for name, cols, kind in indexes
        ],
        "findings": findings,
        "limitations": ["Catalog-based candidates, not measured gains. No DDL was executed."],
    }
