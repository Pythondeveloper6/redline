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

__all__ = [
    "KEYWORDS",
    "filter_columns",
    "fingerprint",
    "fingerprint_hash",
    "is_explainable",
    "primary_table",
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
_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CALL|COPY|INTO)\b"
)


def statement_kind(sql: str) -> str:
    """The upper-cased leading keyword (``SELECT``, ``INSERT``…) or ``""``."""
    fp = fingerprint(sql).lstrip("( ")
    match = _FIRST_WORD.match(fp)
    return match.group().upper() if match else ""


@lru_cache(maxsize=4096)
def is_explainable(sql: str) -> bool:
    """Whether ``sql`` is a single read-only ``SELECT`` (or ``WITH … SELECT``).

    Anything that writes or locks – data-modifying CTEs, ``SELECT … INTO``, ``FOR UPDATE``
    and multiple statements – is refused, so EXPLAIN can never run arbitrary statements.
    """
    fp = fingerprint(sql)
    if ";" in fp:  # literals are already gone, so a ';' means a second statement
        return False
    kind = statement_kind(sql)
    if kind not in ("SELECT", "WITH", "VALUES", "TABLE"):
        return False
    return _WRITE_WORDS.search(fp) is None


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
