"""SQL fingerprinting and light-weight SQL inspection.

A *fingerprint* is the query with every literal replaced by ``?`` so that the 24 queries an
N+1 loop issues collapse into one::

    SELECT "id", "qty" FROM "shop_orderitem" WHERE "order_id" = 17
    SELECT "id", "qty" FROM "shop_orderitem" WHERE "order_id" = %s
            ↓ fingerprint()
    SELECT "id", "qty" FROM "shop_orderitem" WHERE "order_id" = ?

Normalisation:

* comments are removed and whitespace is collapsed;
* string literals (including ``E'…'``, ``N'…'`` and ``$tag$…$tag$``), numbers, hex blobs
  and driver placeholders (``%s``, ``%(name)s``, ``?``, ``:name``, ``$1``) become ``?``;
* ``IN (?, ?, ?)`` becomes ``IN (...)`` whatever the list length, and a multi-row
  ``VALUES (…), (…)`` becomes ``VALUES (...)``;
* SQL keywords are upper-cased; identifiers (quoted or not) keep their spelling.

This is a tokenising regex pass, not a parser: it is fast (a few µs per query, and memoised)
and good enough for grouping, which is all it is used for. Nothing here ever executes SQL.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Any

__all__ = [
    "KEYWORDS",
    "filter_columns",
    "fingerprint",
    "fingerprint_hash",
    "has_limit",
    "is_explainable",
    "primary_table",
    "select_columns",
    "statement_kind",
    "table_aliases",
    "tables",
    "truncate_name",
]

KEYWORDS = frozenset(
    """
    select from where and or not in is null like ilike between exists as on join left right
    inner outer full cross natural using group by order having limit offset fetch first next
    rows only distinct all any some union intersect except insert into values update set
    delete returning with recursive case when then else end asc desc nulls last for share
    nowait skip locked create table index drop alter add primary key foreign references
    default constraint unique check view begin commit rollback savepoint release cast true
    false over partition window filter lateral escape collate interval explain analyze
    format json replace ignore duplicate conflict do nothing merge matched
    """.split()
)

# Order matters: comments and quoted things are consumed before anything inside them can
# be mistaken for a number, a placeholder or a keyword.
_TOKEN = re.compile(
    r"""
      (?P<lcomment>--[^\n]*)
    | (?P<bcomment>/\*.*?\*/)
    | (?P<dollar>\$(?P<tag>[A-Za-z_]\w*)?\$.*?\$(?P=tag)?\$)
    | (?P<string>(?:[EeNnBbXx])?'(?:[^'\\]|''|\\.)*')
    | (?P<dident>"(?:[^"]|"")*")
    | (?P<bident>`(?:[^`]|``)*`)
    | (?P<sqident>\[(?:[^\]]|\]\])*\])
    | (?P<pyformat>%\([^)]*\)s|%s|%d)
    | (?P<qmark>\?)
    | (?P<numbered>\$\d+)
    | (?P<named>(?<![:\w]):[A-Za-z_]\w*)
    | (?P<hex>\b0[xX][0-9A-Fa-f]+\b)
    | (?P<number>(?<![\w$.])\d+(?:\.\d*)?(?:[eE][+-]?\d+)?\b|(?<![\w$.])\.\d+(?:[eE][+-]?\d+)?\b)
    | (?P<word>[A-Za-z_][\w$]*)
    """,
    re.VERBOSE | re.DOTALL,
)
_NEG_NUMBER = re.compile(r"([=<>(,]|\bIN|\bAND|\bOR|\bBETWEEN|\bTHEN|\bELSE)(\s*)-\s*\?")
_WS = re.compile(r"\s+")
_OPERATOR = re.compile(r"(?<![<>!=:\-|])(<>|!=|<=|>=|=|<|>)(?![<>=|])")
_IN_LIST = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.IGNORECASE)
_PAREN_GROUP = r"\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)"
_VALUES_ROWS = re.compile(rf"\bVALUES\s*{_PAREN_GROUP}(?:\s*,\s*{_PAREN_GROUP})*", re.IGNORECASE)


def _sub_token(match: re.Match[str]) -> str:
    kind = match.lastgroup
    if kind in ("lcomment", "bcomment"):
        return " "
    if kind in ("dollar", "string", "pyformat", "qmark", "numbered", "named", "hex", "number"):
        return "?"
    text = match.group()
    if kind == "word" and text.lower() in KEYWORDS:
        return text.upper()
    return text


@lru_cache(maxsize=4096)
def fingerprint(sql: str) -> str:
    """The literal-free, whitespace-normalised form of ``sql`` (memoised)."""
    out = _TOKEN.sub(_sub_token, sql)
    out = _NEG_NUMBER.sub(r"\1\2?", out)
    out = _OPERATOR.sub(r" \1 ", out)
    out = _WS.sub(" ", out).strip()
    out = re.sub(r"\(\s+", "(", out)
    out = re.sub(r"\s+\)", ")", out)
    out = re.sub(r"\s*,\s*", ", ", out)
    out = _IN_LIST.sub("IN (...)", out)
    out = _VALUES_ROWS.sub("VALUES (...)", out)
    return out.rstrip(";").rstrip()


def fingerprint_hash(sql: str) -> str:
    """A short stable hash of :func:`fingerprint` (16 hex chars)."""
    return hashlib.sha1(fingerprint(sql).encode("utf-8")).hexdigest()[:16]


def truncate_name(text: str, limit: int = 120) -> str:
    """``text`` cut to ``limit`` characters with an ellipsis (used for span names)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


_FIRST_WORD = re.compile(r"[A-Za-z]+")
#: Words that make a statement write, lock or run something: DML/DDL (also inside a
#: data-modifying CTE), ``SELECT … INTO``, and every row-locking clause – ``FOR UPDATE``,
#: ``FOR NO KEY UPDATE``, ``FOR SHARE``, ``FOR KEY SHARE`` (PostgreSQL) and
#: ``LOCK IN SHARE MODE`` (MySQL).
_UNSAFE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|"
    r"CALL|COPY|INTO|EXEC|EXECUTE)\b"
    r"|\bFOR\s+(?:NO\s+KEY\s+)?UPDATE\b|\bFOR\s+(?:KEY\s+)?SHARE\b"
    r"|\bLOCK\s+IN\s+SHARE\s+MODE\b",
    re.IGNORECASE,
)
_QUOTED_IDENT = re.compile(r'"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[(?:[^\]]|\]\])*\]')
#: A second, *dialect-pessimistic* reading of the raw text. The fingerprint tokenizer
#: treats ``\'`` as an escaped quote and ``$tag$…$tag$`` as a string, and strips every
#: comment; a server may disagree (PostgreSQL's standard strings have no backslash
#: escapes, MySQL has no dollar quoting, runs ``/*! … */`` "comments" and needs a space
#: after ``--``). This pass only removes what *every* dialect agrees is inert, so a quoting
#: trick cannot hide a second statement or a write keyword from both passes.
_STRICT_INERT = re.compile(
    r"""
      --(?=\s|$)[^\n]*            # a comment in every dialect (MySQL needs the space)
    | /\*(?!!)(?:(?!\*/).)*\*/      # block comment, but not MySQL's executable /*! */
    | '(?:[^']|'')*'            # a standard string: a backslash escapes nothing
    | "(?:[^"]|"")*"            # a quoted identifier
    | `(?:[^`]|``)*`            # a MySQL identifier
    """,
    re.VERBOSE | re.DOTALL,
)
_READ_KINDS = ("SELECT", "WITH", "VALUES", "TABLE")


def statement_kind(sql: str) -> str:
    """The upper-cased leading keyword (``SELECT``, ``INSERT``…) or ``""``."""
    kinds = effective_kinds(sql)
    return next((k for k in kinds if k not in {"SET", "DECLARE"}), kinds[0] if kinds else "")


@lru_cache(maxsize=4096)
def is_explainable(sql: str) -> bool:
    """Whether ``sql`` is a single read-only ``SELECT`` (or ``WITH … SELECT``).

    Refused: anything that is not a ``SELECT``/``WITH``/``VALUES``/``TABLE``, a second
    statement, data-modifying CTEs (``WITH x AS (DELETE …)``), ``SELECT … INTO``, every
    row-locking clause (``FOR UPDATE``/``NO KEY UPDATE``/``SHARE``/``KEY SHARE``,
    ``LOCK IN SHARE MODE``) and MySQL executable comments. The text is checked twice –
    once literal-aware (the fingerprint) and once with a pessimistic reading of quoting
    and comments – and must pass both; ambiguous statements are refused. Keyword
    matching is case-insensitive.

    Functions with side effects (``SELECT nextval(…)``, ``pg_advisory_lock(…)``, a user
    function that writes) are **not** detected: a plain ``EXPLAIN`` never executes them,
    and ``EXPLAIN ANALYZE`` (off by default) runs inside a transaction that is always
    rolled back – read-only on PostgreSQL.
    """
    if not sql or "\x00" in sql:
        return False
    fp = fingerprint(sql)
    if ";" in fp:  # literals are already gone, so a ';' means a second statement
        return False
    if statement_kind(sql) not in _READ_KINDS:
        return False
    if _UNSAFE.search(_QUOTED_IDENT.sub(" ", fp)):
        return False
    strict = _STRICT_INERT.sub(" ", sql).strip().rstrip(";")
    if ";" in strict:
        return False
    return _UNSAFE.search(strict) is None


# ── table / column extraction (best-effort, for titles and index suggestions) ─────────

_IDENT = r'(?:"(?:[^"]|"")+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_QUALIFIED = rf"{_IDENT}(?:\s*\.\s*{_IDENT})*"
_TABLE_REF = re.compile(
    rf"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?P<table>{_QUALIFIED})"
    rf"(?:\s+(?:AS\s+)?(?P<alias>(?!(?:{'|'.join(sorted(KEYWORDS))})\b){_IDENT}))?",
    re.IGNORECASE,
)
_COMPARISON = re.compile(
    rf"(?<![\w:.$])(?P<col>{_QUALIFIED})(?:\s*::\s*\w+)?\s*(?:=|<>|!=|<=|>=|<|>|\bIN\b|\bLIKE\b|\bILIKE\b|\bIS\b|\bBETWEEN\b)",
    re.IGNORECASE,
)
_CLAUSE = re.compile(
    r"\b(WHERE|ON)\b(.*?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b"
    r"|\b(?:LEFT|RIGHT|INNER|OUTER|FULL|CROSS)?\s*JOIN\b|\bUNION\b|\bOFFSET\b|\bFOR\b|$)",
    re.IGNORECASE | re.DOTALL,
)


def _unquote(ident: str) -> str:
    ident = ident.strip()
    if ident[:1] in ('"', "`", "[") and len(ident) >= 2:
        return ident[1:-1].replace('""', '"')
    return ident


def _split_qualified(name: str) -> list[str]:
    return [_unquote(p) for p in re.findall(_IDENT, name)]


def tables(sql: str) -> list[str]:
    """Table names referenced by ``FROM``/``JOIN``/``UPDATE``/``INTO``, in order, deduped."""
    found: list[str] = []
    for m in _TABLE_REF.finditer(fingerprint(sql)):
        name = _split_qualified(m.group("table"))[-1]
        if name.upper() not in KEYWORDS and name != "?" and name not in found:
            found.append(name)
    return found


def primary_table(sql: str) -> str | None:
    """The first table of the statement, or ``None``."""
    found = tables(sql)
    return found[0] if found else None


def table_aliases(sql: str) -> dict[str, str]:
    """``{alias_or_table: table}`` for every table reference (tables map to themselves)."""
    return _aliases(fingerprint(sql))


def _aliases(fp: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for m in _TABLE_REF.finditer(fp):
        table = _split_qualified(m.group("table"))[-1]
        mapping[table] = table
        if m.group("alias"):
            mapping[_unquote(m.group("alias"))] = table
    return mapping


def filter_columns(sql: str) -> dict[str, list[str]]:
    """Columns compared in ``WHERE``/``ON`` clauses, keyed by table.

    ``SELECT … FROM shop_orderitem T0 WHERE T0.order_id = ?`` →
    ``{"shop_orderitem": ["order_id"]}``. Unqualified columns are attributed to the first
    table. Best-effort: used to propose ``CREATE INDEX`` statements, never to execute.
    """
    fp = fingerprint(sql)
    aliases = _aliases(fp)
    first = primary_table(sql)
    result: dict[str, list[str]] = {}
    for clause in _CLAUSE.finditer(fp):
        for m in _COMPARISON.finditer(clause.group(2)):
            parts = _split_qualified(m.group("col"))
            if not parts:
                continue
            column = parts[-1]
            if column.upper() in KEYWORDS or column == "?":
                continue
            table = aliases.get(parts[-2], parts[-2]) if len(parts) >= 2 else first
            if table is None:
                continue
            cols = result.setdefault(table, [])
            if column not in cols:
                cols.append(column)
    return result


# ── result-size inspection ──────────────────────────────────────────────────────────

_LIMIT = re.compile(
    r"\bLIMIT\b|\bFETCH\s+(?:FIRST|NEXT)\b|\bSELECT\s+(?:DISTINCT\s+)?TOP\b|\bROWNUM\b"
)


def _depth_at(text: str, pos: int) -> int:
    return text.count("(", 0, pos) - text.count(")", 0, pos)


def has_limit(sql: str) -> bool:
    """Whether the statement itself caps its row count (``LIMIT``, ``FETCH FIRST|NEXT``,
    ``SELECT TOP n``, ``ROWNUM``).

    Heuristic on the fingerprint: only a keyword outside every parenthesis counts, so the
    ``LIMIT`` of a subquery (``WHERE id IN (SELECT … LIMIT 5)``) does not make the outer
    query "bounded". A ``UNION`` whose last branch has a ``LIMIT`` counts as bounded.
    """
    fp = fingerprint(sql)
    return any(_depth_at(fp, m.start()) == 0 for m in _LIMIT.finditer(fp))


_SELECT_ITEM = re.compile(rf"^(?P<ref>{_IDENT}(?:\s*\.\s*{_IDENT})*(?:\s*\.\s*\*)?|\*)$")
_ALIAS = re.compile(r"\s+AS\s+\S+$", re.IGNORECASE)


def select_columns(sql: str) -> list[tuple[str | None, str]]:
    """``[(qualifier, column)]`` of the plain columns in a ``SELECT`` list.

    ``SELECT "t"."id", t.body AS t_body, count(*) FROM …`` → ``[("t", "id"), ("t", "body")]``.
    Expressions and function calls are skipped; ``*`` comes back as ``(qualifier, "*")``.
    Statements that do not start with ``SELECT`` (``WITH …``) give ``[]``. Best-effort.
    """
    fp = fingerprint(sql).lstrip("( ")
    if not fp.startswith("SELECT"):
        return []
    depth, start, end = 0, 6, len(fp)
    items: list[str] = []
    for i in range(6, len(fp)):
        ch = fp[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if ch == ",":
                items.append(fp[start:i])
                start = i + 1
            elif fp.startswith(" FROM ", i) or fp.startswith(" FROM(", i):
                end = i
                break
    items.append(fp[start:end])
    out: list[tuple[str | None, str]] = []
    for n, item in enumerate(items):
        item = item.strip()
        if n == 0:
            item = re.sub(r"^(?:DISTINCT|ALL)\s+", "", item)
            item = re.sub(r"^TOP\s+\?\s+", "", item)
        item = _ALIAS.sub("", item).strip()
        m = _SELECT_ITEM.match(item)
        if not m:
            continue
        ref = m.group("ref")
        parts = _split_qualified(ref) + (["*"] if ref.endswith("*") else [])
        out.append((parts[-2] if len(parts) > 1 else None, parts[-1]))
    return out


def structural_sql(sql: str) -> str:
    """SQL with literals and comments masked; numeric literals remain for pagination."""

    def replace(match: re.Match[str]) -> str:
        if match.lastgroup in ("lcomment", "bcomment", "dollar", "string"):
            return " "
        return match.group()

    return _TOKEN.sub(replace, sql)


def offset_value(sql: str, params: Any = None) -> int | None:
    """A terminal OFFSET literal or an observed integer bind; never guesses redacted binds."""
    if statement_kind(sql) != "SELECT":
        return None
    tokens = list(_TOKEN.finditer(sql))
    positional = 0
    for index, token in enumerate(tokens):
        if token.lastgroup in {"qmark", "pyformat"}:
            positional += 1
        if token.lastgroup != "word" or token.group().upper() != "OFFSET":
            continue
        if index + 1 >= len(tokens):
            continue
        value_token = tokens[index + 1]
        if sql[token.end() : value_token.start()].strip():
            continue
        if structural_sql(sql[value_token.end() :]).strip().strip(";").strip():
            continue
        value = None
        kind, text = value_token.lastgroup, value_token.group()
        if kind == "number" and text.isdigit() and len(text) <= 20:
            value = int(text)
        elif kind in {"qmark", "pyformat"} and isinstance(params, (list, tuple)):
            value = params[positional] if positional < len(params) else None
        elif kind == "numbered" and len(text) <= 20 and isinstance(params, (list, tuple)):
            position = int(text[1:]) - 1
            value = params[position] if 0 <= position < len(params) else None
        elif kind in {"named", "pyformat"} and isinstance(params, dict):
            key = text[1:] if kind == "named" else text[2:-2]
            value = params.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


@lru_cache(maxsize=4096)
def effective_kinds(sql: str) -> tuple[str, ...]:
    """Leading kinds in semicolon-delimited batches; not a procedural SQL parser."""
    clean = _TOKEN.sub(
        lambda m: (
            " "
            if m.lastgroup
            in {"lcomment", "bcomment", "string", "dollar", "dident", "bident", "sqident"}
            else m.group()
        ),
        sql,
    )
    kinds = []
    for part in clean.split(";"):
        match = _FIRST_WORD.match(part.lstrip("( \r\n\t"))
        if match:
            kinds.append(match.group().upper())
    return tuple(kinds)


@lru_cache(maxsize=4096)
def comment_placeholders(sql: str, paramstyle: str = "any") -> int:
    """Count bind markers inside comments, excluding strings and identifiers."""
    patterns = {
        "qmark": r"\?",
        "named": r"(?<![:\w]):[A-Za-z_]\w*",
        "numeric": r"(?<![:\w]):\d+",
        "numeric_dollar": r"\$\d+",
        "format": r"(?<!%)%[sd]",
        "pyformat": r"(?<!%)%(?:\([^)]*\))?[sd]",
    }
    pattern = patterns.get(paramstyle) or "|".join(patterns.values())
    return sum(
        len(re.findall(pattern, m.group()))
        for m in _TOKEN.finditer(sql)
        if m.lastgroup in {"lcomment", "bcomment"}
    )
