"""Find the ``WHERE``/``ON`` predicates of a statement and judge whether a plain B-tree
index on their column could ever serve them.

Used by :mod:`redline_core.plan_analysis` to avoid clearly wrong ``CREATE INDEX`` advice:

* ``col LIKE '%x'`` / ``ILIKE`` with a leading wildcard (B-tree cannot seek on it);
* ``ILIKE`` and ``NOT LIKE`` in general; ``lower(col) = ?`` and other functions of the
  column (only an *expression* index matches); ``col != ?`` / ``NOT IN`` / ``IS NOT NULL``
  (matches most rows, the planner scans anyway);
* ``a = ? OR b = ?``: one composite index does not serve an OR across columns, so the
  columns are clustered per OR branch and each cluster gets its own index.

Best-effort and regex based, like the rest of :mod:`redline_core.sql`. Nothing is executed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import sql as sqlmod

__all__ = ["Predicate", "analyze_predicates", "blocked_reason", "leading_wildcard"]

_VALUE = "\x00"
_PLACEHOLDER = re.compile(
    r"'(?:[^']|'')*'"  # string literal
    r"|%\((?P<named>\w+)\)s|%s|\?|\$(?P<pos>\d+)|(?<![:\w]):(?P<colon>[A-Za-z_]\w*)"
)
_FUNC_KEYWORDS = {
    "AND", "OR", "NOT", "WHERE", "ON", "WHEN", "THEN", "ELSE", "CASE", "IN", "EXISTS",
    "SELECT", "BETWEEN", "ANY", "ALL", "AS", "FROM",
}  # fmt: skip

_PRED = re.compile(
    rf"(?<![\w.$:])(?P<lhs>(?:\w+\s*\(\s*)*(?P<col>{sqlmod._QUALIFIED})(?:\s*::\s*\w+)?[\s)]*?)"
    r"\s*(?P<op>NOT\s+ILIKE|NOT\s+LIKE|NOT\s+IN|ILIKE|LIKE|IS\s+NOT\s+NULL|IS\s+NULL"
    r"|!=|<>|<=|>=|=|<|>|\bIN\b)(?![\w])"
    rf"(?:\s*(?:\w+\s*\(\s*)?{_VALUE}(?P<val>\d+){_VALUE})?",
    re.IGNORECASE,
)
_OR = re.compile(r"\bOR\b", re.IGNORECASE)


@dataclass
class Predicate:
    table: str
    column: str
    op: str  # eq range in like ilike not_like ne not_in is_null is_not_null
    wrapped: bool = False  # the column is wrapped in a function (lower(col) …)
    pattern: str | None = None  # LIKE/ILIKE pattern when known
    expr: str = ""  # the left-hand expression as written
    path: tuple[tuple[int, int], ...] = ()  # (OR-group id, branch) of enclosing ORs


def leading_wildcard(pattern: str) -> bool:
    """Whether a LIKE ``pattern`` starts with an (unescaped) ``%`` or ``_``."""
    return pattern[:1] in ("%", "_")


def _tokenise(sql: str, params: Any) -> tuple[str, list[Any]]:
    """Replace literals/placeholders with ``\\0<n>\\0`` markers; return the values."""
    values: list[Any] = []
    seq = list(params) if isinstance(params, Sequence) and not isinstance(params, str) else []
    mapping = params if isinstance(params, Mapping) else {}
    counter = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal counter
        text = m.group(0)
        if text.startswith("'"):
            value: Any = text[1:-1].replace("''", "'")
        elif m.group("named") or m.group("colon"):
            value = mapping.get(m.group("named") or m.group("colon"))
        elif m.group("pos"):
            i = int(m.group("pos")) - 1
            value = seq[i] if 0 <= i < len(seq) else None
        else:
            value = seq[counter] if counter < len(seq) else None
            counter += 1
        values.append(value)
        return f"{_VALUE}{len(values) - 1}{_VALUE}"

    return _PLACEHOLDER.sub(sub, sql), values


def _or_spans(text: str) -> list[tuple[int, int, list[int]]]:
    """``(start, end, [positions of OR])`` for the text and each parenthesised group."""
    spans: list[list[Any]] = [[0, len(text), []]]
    stack = [0]
    for i, ch in enumerate(text):
        if ch == "(":
            spans.append([i, len(text), []])
            stack.append(len(spans) - 1)
        elif ch == ")" and len(stack) > 1:
            spans[stack.pop()][1] = i
    stack = []
    # attribute each OR to the innermost span containing it
    for m in _OR.finditer(text):
        best = 0
        for idx, (start, end, _) in enumerate(spans):
            if start <= m.start() < end and start >= spans[best][0]:
                best = idx
        spans[best][2].append(m.start())
    return [(s, e, ors) for s, e, ors in spans]


def analyze_predicates(sql: str, params: Any = None) -> dict[str, list[Predicate]]:
    """Predicates of the ``WHERE``/``ON`` clauses, keyed by table."""
    try:
        text, values = _tokenise(sql, params)
        fp = sqlmod.fingerprint(sql)
        aliases = sqlmod._aliases(fp)
        first = sqlmod.primary_table(sql)
        result: dict[str, list[Predicate]] = {}
        for clause in sqlmod._CLAUSE.finditer(text):
            body = clause.group(2)
            spans = [s for s in _or_spans(body) if s[2]]
            for m in _PRED.finditer(body):
                pred = _predicate(m, values, aliases, first)
                if pred is None:
                    continue
                path = []
                for sid, (start, end, ors) in enumerate(spans):
                    if start <= m.start() < end or (start == 0 and end == len(body)):
                        path.append((sid, sum(1 for o in ors if o < m.start())))
                pred.path = tuple(path)
                result.setdefault(pred.table, []).append(pred)
        return result
    except Exception:  # pragma: no cover - regex safety net
        return {}


def _predicate(
    m: re.Match[str], values: list[Any], aliases: dict[str, str], first: str | None
) -> Predicate | None:
    parts = sqlmod._split_qualified(m.group("col"))
    if not parts:
        return None
    column = parts[-1]
    if column.upper() in sqlmod.KEYWORDS or column == "?" or column.isdigit():
        return None
    table = aliases.get(parts[-2], parts[-2]) if len(parts) >= 2 else first
    if table is None:
        return None
    lhs = m.group("lhs")
    funcs = [f for f in re.findall(r"(\w+)\s*\(", lhs) if f.upper() not in _FUNC_KEYWORDS]
    op_raw = re.sub(r"\s+", " ", m.group("op").upper())
    op = {
        "=": "eq",
        "<": "range",
        ">": "range",
        "<=": "range",
        ">=": "range",
        "IN": "in",
        "LIKE": "like",
        "ILIKE": "ilike",
        "NOT LIKE": "not_like",
        "NOT ILIKE": "not_like",
        "NOT IN": "not_in",
        "!=": "ne",
        "<>": "ne",
        "IS NULL": "is_null",
        "IS NOT NULL": "is_not_null",
    }[op_raw]
    pattern = None
    if op in ("like", "ilike") and m.group("val") is not None:
        raw = values[int(m.group("val"))]
        pattern = raw if isinstance(raw, str) else None
    return Predicate(
        table=table,
        column=column,
        op=op,
        wrapped=bool(funcs),
        pattern=pattern,
        expr=re.sub(r"\s+", " ", lhs).strip(),
    )


def blocked_reason(p: Predicate) -> str | None:
    """Why a plain B-tree index on ``p.column`` cannot serve ``p`` (``None`` if it can)."""
    if p.wrapped:
        return "function"
    if p.op in ("like", "ilike") and p.pattern is not None and leading_wildcard(p.pattern):
        return "leading_wildcard"
    if p.op == "ilike":
        return "ilike"
    if p.op == "not_like":
        return "negated"
    if p.op in ("ne", "not_in"):
        return "negated"
    if p.op == "is_not_null":
        return "not_null"
    return None


def compatible(a: Predicate, b: Predicate) -> bool:
    """Can both predicates hold in the same AND-context (no different OR branches)?"""
    pa = dict(a.path)
    return all(pa.get(sid, branch) == branch for sid, branch in b.path)
