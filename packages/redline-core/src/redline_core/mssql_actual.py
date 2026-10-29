"""Explicit actual-plan collection for synchronous SQL Server drivers supporting nextset."""

from __future__ import annotations

import re

from .explain import ExplainError
from .sql import is_explainable


def collect(engine, sql, params):
    if not is_explainable(sql):
        raise ExplainError("not_explainable", "Only a single read query can collect an actual plan")
    with engine.connect() as conn:
        result = None
        plans, reads = [], {}
        try:
            conn.exec_driver_sql("BEGIN TRANSACTION")
            conn.exec_driver_sql("SET STATISTICS XML ON")
            conn.exec_driver_sql("SET STATISTICS IO ON")
            result = conn.exec_driver_sql(
                sql, tuple(params) if isinstance(params, list) else params or ()
            )
            cursor = result.cursor
            if cursor is None or not hasattr(cursor, "nextset"):
                raise ExplainError(
                    "analyze_unsupported", "The driver does not expose multiple result sets"
                )
            rows_seen, bytes_seen = 0, 0
            while True:
                if cursor.description:
                    while rows := cursor.fetchmany(100):
                        rows_seen += len(rows)
                        if rows_seen > 100000:
                            raise ExplainError(
                                "plan_limit", "Actual-plan collection exceeded the row limit"
                            )
                        for row in rows:
                            if (
                                len(row) == 1
                                and isinstance(row[0], str)
                                and "<ShowPlanXML" in row[0]
                                and "http://schemas.microsoft.com/sqlserver/2004/07/showplan"
                                in row[0]
                            ):
                                bytes_seen += len(row[0].encode())
                                if bytes_seen > 5000000:
                                    raise ExplainError(
                                        "plan_limit", "Actual plan exceeded the byte limit"
                                    )
                                plans.append((row[0],))
                for message in (getattr(cursor, "messages", None) or [])[:100]:
                    for match in re.finditer(
                        r"Table '([^']+)'.*?logical reads (\d+)", str(message), re.I
                    ):
                        if len(reads) < 100:
                            reads[match[1]] = reads.get(match[1], 0) + int(match[2])
                if not cursor.nextset():
                    break
            if not plans:
                raise ExplainError("explain_failed", "The driver returned no actual XML plan")
            return [
                (
                    xml[0],
                    {
                        "logical_reads": reads,
                        "note": "STATISTICS IO parsing recognizes English driver messages only.",
                    },
                )
                for xml in plans
            ]
        finally:
            try:
                if result is not None:
                    result.close()
            finally:
                try:
                    conn.exec_driver_sql("SET STATISTICS XML OFF")
                    conn.exec_driver_sql("SET STATISTICS IO OFF")
                except BaseException:
                    conn.invalidate()
                    raise
                finally:
                    conn.rollback()
