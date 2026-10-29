"""On-demand EXPLAIN for a recorded query – without the core touching a database driver.

The adapter supplies an **executor**: a callable ``executor(sql, params) -> rows`` bound to
the right connection (Django ``connections[alias]``, a SQLAlchemy engine) that runs the
statement **inside a transaction it always rolls back**, and returns the fetched rows as
sequences. The core builds the EXPLAIN statement, calls it once, and analyses the result::

    def executor(sql, params):
        with transaction.atomic(using=alias):
            with connections[alias].cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            transaction.set_rollback(True, using=alias)
        return rows

    result = run_explain(span.data["sql"], span.data["params"], "postgresql", executor)

Safety rules enforced here, whatever the executor does:

* only statements :func:`redline_core.sql.is_explainable` accepts (one read-only
  ``SELECT``/``WITH``) are explained;
* ``ANALYZE`` (which *executes* the query) needs ``allow_analyze=True`` – the API passes
  ``Settings.explain_analyze``; otherwise :class:`ExplainError` ``analyze_disabled`` (403).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import sql as sqlmod
from .plan_analysis import Finding, PlanNode, analyze_plan, render_text

__all__ = [
    "ExplainError",
    "ExplainExecutor",
    "ExplainResult",
    "explain_statement",
    "run_explain",
]

#: ``executor(sql, params) -> rows``; must run in a rolled-back transaction.
ExplainExecutor = Callable[[str, Any], Sequence[Sequence[Any]]]

VENDORS = ("postgresql", "mysql", "sqlite", "mssql")


class ExplainError(Exception):
    """EXPLAIN could not run. ``code`` and ``status`` map onto the API error shape."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class ExplainResult:
    vendor: str
    analyzed: bool
    plan: Any
    text: str
    tree: PlanNode
    findings: list[Finding] = field(default_factory=list)
    suggested_indexes: list[str] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **({"statistics": self.statistics} if self.statistics else {}),
            "vendor": self.vendor,
            "analyzed": self.analyzed,
            "plan": self.plan,
            "text": self.text,
            "tree": self.tree.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
            "suggested_indexes": list(self.suggested_indexes),
        }


def explain_statement(sql: str, vendor: str, *, analyze: bool = False) -> str:
    """The EXPLAIN statement for ``sql`` on ``vendor``."""
    body = sql.strip().rstrip(";")
    if vendor == "mssql":
        return body
    if vendor == "postgresql":
        opts = "ANALYZE, BUFFERS, FORMAT JSON" if analyze else "FORMAT JSON"
        return f"EXPLAIN ({opts}) {body}"
    if vendor == "mysql":
        if analyze:
            raise ExplainError(
                "analyze_unsupported", "EXPLAIN ANALYZE is not supported for MySQL yet.", 400
            )
        return f"EXPLAIN FORMAT=JSON {body}"
    if vendor == "sqlite":
        return f"EXPLAIN QUERY PLAN {body}"
    raise ExplainError("unsupported_vendor", f"EXPLAIN is not supported for {vendor!r}.", 400)


def run_explain(
    sql: str,
    params: Any,
    vendor: str,
    executor: ExplainExecutor,
    *,
    analyze: bool = False,
    allow_analyze: bool = False,
) -> ExplainResult:
    """Run EXPLAIN through ``executor`` and analyse the plan.

    Raises :class:`ExplainError` (``not_explainable``, ``analyze_disabled``,
    ``unsupported_vendor``, ``explain_failed``).
    """
    if vendor not in VENDORS:
        raise ExplainError("unsupported_vendor", f"EXPLAIN is not supported for {vendor!r}.")
    if not sqlmod.is_explainable(sql):
        raise ExplainError(
            "not_explainable", "Only single read-only SELECT statements can be explained."
        )
    if analyze and not allow_analyze:
        raise ExplainError(
            "analyze_disabled",
            "EXPLAIN ANALYZE executes the query; enable it with the explain_analyze setting.",
            403,
        )
    if vendor == "sqlite":
        analyze = False  # EXPLAIN QUERY PLAN has no ANALYZE form
    statement = explain_statement(sql, vendor, analyze=analyze)
    try:
        if vendor == "mssql":
            plan_executor = getattr(
                executor, "mssql_actual_plan" if analyze else "mssql_estimated_plan", None
            )
            if plan_executor is None:
                raise ExplainError(
                    "explain_unavailable",
                    "This executor cannot safely toggle SHOWPLAN on a dedicated connection.",
                    501,
                )
            rows = list(plan_executor(statement, params))
        else:
            rows = list(executor(statement, params))
    except ExplainError:
        raise
    except Exception as exc:
        raise ExplainError("explain_failed", f"{type(exc).__name__}: {exc}", 422) from exc
    try:
        plan = _raw_plan(vendor, rows)
        analysis = analyze_plan(vendor, plan, sql, params)
    except ExplainError:
        raise
    except Exception as exc:
        raise ExplainError("explain_failed", "Returned plan could not be parsed", 422) from exc
    return ExplainResult(
        vendor=vendor,
        analyzed=analyze,
        plan=plan,
        text=render_text(analysis.tree),
        tree=analysis.tree,
        findings=analysis.findings,
        suggested_indexes=analysis.suggested_indexes,
        statistics=rows[0][1]
        if vendor == "mssql" and rows and len(rows[0]) > 1 and isinstance(rows[0][1], dict)
        else {},
    )


def _raw_plan(vendor: str, rows: list[Sequence[Any]]) -> Any:
    if vendor == "sqlite":
        return [list(r) if not isinstance(r, dict) else r for r in rows]
    if not rows:
        raise ExplainError("explain_failed", "EXPLAIN returned no rows.", 422)
    first = rows[0]
    value = first[0] if isinstance(first, (list, tuple)) else first
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if vendor == "mssql":
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ExplainError("explain_failed", f"Unparseable plan: {exc}", 422) from exc
    return value
