"""Result-size detectors: ``large_result``, ``unbounded_query``, ``inefficient_count``.

They read ``sql`` span data that the adapters fill in: ``rows`` (rows the query returned,
``None`` when the driver/ORM could not tell), ``has_limit`` (core computes it from the SQL
text) and, when the adapter's suggester can work it out, ``wide_columns``.

=====================  ==============================================================
``large_result``       a SELECT returned ``>= large_result_rows`` rows (default 1000);
                       *critical* at ``>= 5x``
``unbounded_query``    a SELECT without ``LIMIT``/``FETCH FIRST``/``TOP`` returned
                       ``>= unbounded_rows`` rows (default 200); *critical* at ``>= 5x``
``inefficient_count``  the callsite line is ``len(<qs>)``, ``bool(<qs>)``, ``if <qs>:`` or
                       ``<qs>.count() > 0`` – the first three loaded every row to answer a
                       count/existence question, the last counts when ``exists`` would stop
                       at one row
=====================  ==============================================================

``inefficient_count`` only looks at the span's *own* callsite line, so ``len([1, 2])`` on a
line that does not issue a query never fires. The facts a suggester needs to rewrite the
line are in ``Issue.extra``: ``pattern``, ``expr``, ``old`` (the text to replace), ``new``
(the Django-style ``.count()``/``.exists()`` replacement), ``fix_line``.
"""

from __future__ import annotations

import re
from typing import Any

from .. import sql as sqlmod
from ..conf import Settings
from ..model import Issue, Severity, Span, Trace
from ._common import issue_id, plural, where

__all__ = ["CountPattern", "count_pattern", "detect"]

_READS = ("SELECT", "WITH")


def _select_spans(trace: Trace) -> list[Span]:
    out = []
    for s in trace.spans:
        if s.kind != "sql" or s.data.get("many"):
            continue
        if sqlmod.statement_kind(str(s.data.get("sql", ""))) in _READS:
            out.append(s)
    return out


def _fp(span: Span) -> str:
    return span.data.get("fingerprint") or sqlmod.fingerprint(span.data.get("sql", ""))


def _rows(span: Span) -> int | None:
    rows = span.data.get("rows")
    return rows if isinstance(rows, int) and not isinstance(rows, bool) and rows >= 0 else None


def _severity(value: float, threshold: float) -> Severity:
    return "critical" if value >= 5 * threshold else "warning"


def _group(spans: list[Span]) -> dict[tuple[str, str | None], list[Span]]:
    groups: dict[tuple[str, str | None], list[Span]] = {}
    for s in spans:
        loc = s.callsite.location if s.callsite else None
        groups.setdefault((_fp(s), loc), []).append(s)
    return groups


def _on(fp: str) -> str:
    table = sqlmod.primary_table(fp)
    return f" from {table}" if table else ""


def _facts(group: list[Span], fp: str) -> dict[str, Any]:
    biggest = max(group, key=lambda s: _rows(s) or 0)
    wide = next((s.data["wide_columns"] for s in group if s.data.get("wide_columns")), [])
    has_limit = biggest.data.get("has_limit")
    return {
        "rows": _rows(biggest),
        "has_limit": has_limit if isinstance(has_limit, bool) else None,
        "wide_columns": list(wide),
        "table": sqlmod.primary_table(fp),
    }


def detect(trace: Trace, settings: Settings) -> list[Issue]:
    selects = _select_spans(trace)
    if not selects:
        return []
    issues = _large(selects, settings)
    issues.extend(_unbounded(selects, settings))
    issues.extend(_counts(selects, settings))
    return issues


def _large(selects: list[Span], settings: Settings) -> list[Issue]:
    limit = settings.large_result_rows
    issues = []
    big = [s for s in selects if (_rows(s) or 0) >= limit]
    for (fp, loc), group in _group(big).items():
        facts = _facts(group, fp)
        rows = facts["rows"]
        callsite = group[0].callsite
        issues.append(
            Issue(
                id=issue_id("large_result", fp, loc),
                kind="large_result",
                severity=_severity(rows, limit),
                title=f"Large result: {rows:,} rows{_on(fp)}{where(callsite)}",
                detail=(
                    f"A query returned {rows:,} rows into memory, over the {limit:,}-row "
                    "threshold. Every row costs fetch, hydrate and serialize time; fetch "
                    "only what the response needs: paginate, stream in chunks, or select "
                    "fewer columns."
                ),
                span_ids=[s.id for s in group],
                fingerprint=fp,
                callsite=callsite,
                count=len(group),
                total_ms=round(sum(s.duration_ms for s in group), 3),
                extra={**facts, "threshold": limit},
            )
        )
    return issues


def _unbounded(selects: list[Span], settings: Settings) -> list[Issue]:
    limit = settings.unbounded_rows
    issues = []
    big = [s for s in selects if s.data.get("has_limit") is False and (_rows(s) or 0) >= limit]
    for (fp, loc), group in _group(big).items():
        facts = _facts(group, fp)
        rows = facts["rows"]
        callsite = group[0].callsite
        issues.append(
            Issue(
                id=issue_id("unbounded_query", fp, loc),
                kind="unbounded_query",
                severity=_severity(rows, limit),
                title=f"Unbounded query: {rows:,} rows, no LIMIT{_on(fp)}{where(callsite)}",
                detail=(
                    f"This query has no LIMIT and returned {rows:,} rows. It keeps getting "
                    "slower and heavier as the table grows. Add a LIMIT, paginate, or "
                    "iterate in chunks."
                ),
                span_ids=[s.id for s in group],
                fingerprint=fp,
                callsite=callsite,
                count=len(group),
                total_ms=round(sum(s.duration_ms for s in group), 3),
                extra={**facts, "threshold": limit},
            )
        )
    return issues


# ── inefficient count / exists ───────────────────────────────────────────────────────


class CountPattern(dict):
    """``{"pattern", "expr", "old", "new", "fix_line", "method"}`` for one callsite line."""


_CMP = re.compile(r"\.count\(\)\s*(?P<op>>=\s*1|>\s*0|!=\s*0|==\s*0|<\s*1|<=\s*0)(?![\w.])")
_IF = re.compile(r"^(?P<kw>(?:el)?if)\s+(?P<not>not\s+)?(?P<expr>.+?)\s*:\s*(?:#.*)?$")
_CALL = re.compile(r"(?<![\w.])(?P<fn>len|bool)\(")
_NEVER = frozenset({"True", "False", "None", "self", "cls"})


def _expr_start(code: str, end: int) -> int | None:
    """Start of the name/attribute/call/subscript chain that ends just before ``end``."""
    i = end
    while i > 0:
        ch = code[i - 1]
        if ch in ")]":
            depth, j = 0, i - 1
            while j >= 0:
                if code[j] in ")]":
                    depth += 1
                elif code[j] in "([":
                    depth -= 1
                if depth == 0:
                    break
                j -= 1
            else:
                return None
            i = j
        elif ch.isalnum() or ch in "_.":
            i -= 1
        else:
            break
    return i


def _is_chain(expr: str) -> bool:
    expr = expr.strip()
    if not expr or expr in _NEVER or not (expr[0].isalpha() or expr[0] in "_("):
        return False
    return _expr_start(expr, len(expr)) == 0


def _matching_paren(code: str, open_at: int) -> int | None:
    depth, quote = 0, ""
    for i in range(open_at, len(code)):
        ch = code[i]
        if quote:
            if ch == quote and code[i - 1] != "\\":
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def count_pattern(code: str | None, *, count_query: bool) -> CountPattern | None:
    """Recognise an inefficient count/existence idiom on one source line.

    ``count_query`` says whether the span's SQL is a ``COUNT`` query: then only
    ``<qs>.count() > 0`` forms apply; otherwise (a full-row SELECT) ``len(<qs>)``,
    ``bool(<qs>)`` and ``if <qs>:``.
    """
    if not code:
        return None
    code = code.strip()
    if count_query:
        m = _CMP.search(code)
        if m is None:
            return None
        start = _expr_start(code, m.start())
        if start is None or start == m.start() or not _is_chain(code[start : m.start()]):
            return None
        expr = code[start : m.start()]
        negate = m.group("op").replace(" ", "") in ("==0", "<1", "<=0")
        new = f"{'not ' if negate else ''}{expr}.exists()"
        old = code[start : m.end()]
        return _pattern("count_cmp", expr, old, new, code, start, m.end(), "exists")
    for m in _CALL.finditer(code):
        close = _matching_paren(code, m.end() - 1)
        if close is None:
            continue
        arg = code[m.end() : close]
        if not _is_chain(arg) or arg.rstrip().endswith((".exists()", ".count()")):
            continue
        if m.group("fn") == "len":
            return _pattern("len", arg, code[m.start() : close + 1], f"{arg}.count()", code,
                            m.start(), close + 1, "count")  # fmt: skip
        return _pattern("bool", arg, code[m.start() : close + 1], f"{arg}.exists()", code,
                        m.start(), close + 1, "exists")  # fmt: skip
    m = _IF.match(code)
    if m and _is_chain(m.group("expr")):
        expr = m.group("expr")
        if expr.rstrip().endswith((".exists()", ".count()")):
            return None
        start = m.start("expr")
        return _pattern("truthy", expr, expr, f"{expr}.exists()", code, start, m.end("expr"),
                        "exists")  # fmt: skip
    return None


def _pattern(
    kind: str, expr: str, old: str, new: str, code: str, start: int, end: int, method: str
) -> CountPattern:
    return CountPattern(
        pattern=kind,
        expr=expr,
        old=old,
        new=new,
        fix_line=code[:start] + new + code[end:],
        method=method,
    )


def _is_count_query(span: Span) -> bool:
    return "COUNT(" in _fp(span).upper().replace(" (", "(")


def _counts(selects: list[Span], settings: Settings) -> list[Issue]:
    groups: dict[tuple[str, str | None], tuple[CountPattern, list[Span]]] = {}
    for s in selects:
        callsite = s.callsite
        if callsite is None or not callsite.code:
            continue
        count_query = _is_count_query(s)
        if not count_query and s.data.get("has_limit") is True:
            continue  # first()/get()/[:n] – already bounded
        found = count_pattern(callsite.code, count_query=count_query)
        if found is None:
            continue
        key = (_fp(s), callsite.location)
        groups.setdefault(key, (found, []))[1].append(s)
    issues = []
    for (fp, loc), (found, group) in groups.items():
        callsite = group[0].callsite
        rows = max((_rows(s) or 0 for s in group), default=0)
        total = sum(s.duration_ms for s in group)
        heavy = rows >= settings.unbounded_rows or total >= settings.slow_query_ms
        kind = found["pattern"]
        if kind == "count_cmp":
            title = f"Use exists() instead of count(){where(callsite)}"
            detail = (
                f"`{found['old']}` counts every matching row only to compare the count "
                "with zero. exists() stops at the first match."
            )
        else:
            what = {
                "len": f"`len({found['expr']})` loads",
                "bool": f"`bool({found['expr']})` loads",
                "truthy": f"`if {found['expr']}:` loads",
            }[kind]
            fix = "count()" if kind == "len" else "exists()"
            title = f"Loads every row just to {'count' if kind == 'len' else 'check'}" + (
                f"{where(callsite)}"
            )
            detail = (
                f"{what} all {plural(rows, 'row') if rows else 'rows'} into memory to "
                f"answer a {'count' if kind == 'len' else 'does-any-exist'} question"
                f"{_on(fp)}. Ask the database instead with .{fix}."
            )
        severity: Severity = "warning" if heavy else "info"
        issues.append(
            Issue(
                id=issue_id("inefficient_count", fp, loc),
                kind="inefficient_count",
                severity=severity,
                title=title,
                detail=detail,
                span_ids=[s.id for s in group],
                fingerprint=fp,
                callsite=callsite,
                count=len(group),
                total_ms=round(total, 3),
                extra={**found, "rows": rows or None, "table": sqlmod.primary_table(fp)},
            )
        )
    return issues
