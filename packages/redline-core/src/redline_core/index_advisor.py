"""Workload-level index advisor (``GET api/indexes``, ``CONTRACT.md`` §12).

Reads the SQL workload of a window (:meth:`ExtendedStore.sql_workload`) and proposes the
smallest set of indexes that covers the most query time:

1. **Requirements.** Every explainable ``SELECT`` fingerprint (the top
   :data:`MAX_QUERIES` by total time) is parsed with :mod:`redline_core.predicates` – using
   the sample trace's raw SQL and parameters when available, so ``LIKE`` patterns are known.
   Per table it needs one B-tree *requirement* per ``OR`` branch cluster: the equality
   columns (``=``, ``IN``, ``IS NULL``, in any order) followed by one range column
   (``<``, ``BETWEEN``, prefix ``LIKE 'abc%'``) or, without a range, the ``ORDER BY``
   columns of that table. A join condition ``a.x = b.y`` gives a single-column requirement
   on each side. A requirement that is just ``(id)`` is skipped (assumed primary key).
   Predicates no B-tree can serve are left out (functions of the column, negations,
   ``IS NOT NULL``, a ``LIKE`` whose pattern is unknown); a leading-wildcard ``LIKE`` or an
   ``ILIKE`` becomes a *text* requirement instead: ``pg_trgm`` (PostgreSQL), FTS5 (SQLite)
   or ``FULLTEXT`` (MySQL). A query's ``total_ms`` is split evenly over its requirements.
2. **Existing indexes.** With an executor for the database, the catalog is read
   (``pg_tables``/``pg_indexes``, ``sqlite_master`` + ``pragma_index_list``/
   ``pragma_index_info``/``pragma_table_info``, ``information_schema``) through the same
   always-rolled-back executor. A requirement already served by an existing index prefix
   is dropped, and so is one on a name that is not a table (a CTE, a view).
3. **EXPLAIN.** The top :data:`EXPLAIN_TOP` remaining fingerprints are explained (plain
   ``EXPLAIN``, never ``ANALYZE``) with their sample's raw SQL; the plan tells which
   tables are read by a sequential/full scan. Catalog and EXPLAIN share a time budget
   (:data:`BUDGET_S`); whatever does not fit is simply not verified.
4. **Set cover.** Candidates are the requirements' own column lists (equality columns
   ordered by how much workload time uses them, so shared leading columns merge). An index
   ``(a, b)`` covers a requirement ``(a)`` or ``(a, b)`` (equality columns as a set, then
   the tail in order). Greedy weighted set cover picks the candidate covering the most
   still-uncovered time, up to :data:`MAX_SUGGESTIONS`.

``est_saving_ms`` is a heuristic: :data:`SCAN_SAVING` (80 %) of the time credited to the
candidate for queries whose EXPLAIN showed a scan of that table, :data:`DEFAULT_SAVING`
(40 %) for the rest. ``confidence`` is ``high`` when such a scan was seen, ``medium`` for an
unverified B-tree, ``low`` for text-search alternatives (not drop-in) and for B-trees whose
queries already avoid a full scan.

Unused indexes come from ``pg_stat_user_indexes`` (PostgreSQL only).

Nothing here ever runs DDL: every statement sent to the executor is a read-only ``SELECT``
(catalog) or a plain ``EXPLAIN`` of an explainable statement.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import predicates as predmod
from . import sql as sqlmod
from .explain import ExplainExecutor, run_explain
from .plan_analysis import _quote
from .redact import MASK
from .storage.derive import fingerprint_key

__all__ = [
    "BUDGET_S",
    "DEFAULT_SAVING",
    "EXPLAIN_TOP",
    "MAX_QUERIES",
    "MAX_SUGGESTIONS",
    "SCAN_SAVING",
    "Catalog",
    "Requirement",
    "advise",
    "covers",
    "create_sql",
    "load_catalog",
    "query_requirements",
    "select_indexes",
    "unused_indexes",
]

logger = logging.getLogger("redline")

MAX_QUERIES = 100
MAX_SUGGESTIONS = 10
MAX_COLUMNS = 4
EXPLAIN_TOP = 20
BUDGET_S = 3.0
SCAN_SAVING = 0.8
DEFAULT_SAVING = 0.4

_EQ_OPS = ("eq", "in", "is_null")
_TEXT_KIND = {"postgresql": "trigram", "sqlite": "fts", "mysql": "fulltext"}
_SCAN_FLAGS = {"seq_scan", "full_scan"}

ExecutorFactory = Callable[[str, str], "ExplainExecutor | None"]


@dataclass(frozen=True)
class Requirement:
    """What one query needs on one table: ``eq`` columns (any order) then ``tail``."""

    table: str
    eq: frozenset[str] = frozenset()
    tail: tuple[str, ...] = ()
    kind: str = "btree"  # btree | trigram | fts | fulltext

    @property
    def size(self) -> int:
        return len(self.eq) + len(self.tail)


def covers(columns: Sequence[str], req: Requirement) -> bool:
    """Whether a B-tree on ``columns`` serves ``req`` (case-insensitive).

    The first ``len(req.eq)`` columns must be exactly the equality columns (in any order),
    followed by ``req.tail`` in order; extra trailing columns are fine.
    """
    cols = [c.lower() for c in columns]
    n = len(req.eq)
    if len(cols) < req.size:
        return False
    if set(cols[:n]) != {c.lower() for c in req.eq}:
        return False
    return cols[n : req.size] == [c.lower() for c in req.tail]


# ── static analysis ─────────────────────────────────────────────────────────────────────

_ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_ORDER_END = re.compile(r"\b(?:LIMIT|OFFSET|FETCH|FOR)\b|\)|$", re.IGNORECASE)
_ORDER_ITEM = re.compile(
    rf"^(?P<col>{sqlmod._QUALIFIED})(?:\s+(?P<dir>ASC|DESC))?(?:\s+NULLS\s+(?:FIRST|LAST))?$",
    re.IGNORECASE,
)


def _order_by(fp: str, aliases: dict[str, str], first: str | None) -> tuple[str, list[str]] | None:
    """``(table, columns)`` of a top-level ``ORDER BY`` on plain columns of one table."""
    found = [m for m in _ORDER_BY.finditer(fp) if sqlmod._depth_at(fp, m.start()) == 0]
    if not found:
        return None
    rest = fp[found[-1].end() :]
    body = rest[: _ORDER_END.search(rest).start()]  # type: ignore[union-attr]
    table: str | None = None
    cols: list[str] = []
    directions = set()
    for item in body.split(","):
        m = _ORDER_ITEM.match(item.strip())
        if not m:
            return None
        parts = sqlmod._split_qualified(m.group("col"))
        if not parts or parts[-1].upper() in sqlmod.KEYWORDS:
            return None
        owner = aliases.get(parts[-2], parts[-2]) if len(parts) >= 2 else first
        if owner is None or (table is not None and owner != table):
            return None
        table = owner
        directions.add((m.group("dir") or "ASC").upper())
        if parts[-1] not in cols:
            cols.append(parts[-1])
    if table is None or len(directions) > 1 or not cols:
        return None
    return table, cols


def _like_known(p: predmod.Predicate) -> bool:
    return p.pattern is not None and p.pattern != MASK


def _btree_usable(p: predmod.Predicate) -> bool:
    if predmod.blocked_reason(p) is not None:
        return False
    if p.op == "like":  # usable only as a known prefix pattern
        return _like_known(p)
    return p.op in _EQ_OPS or p.op == "range"


def _text_search(p: predmod.Predicate) -> bool:
    if p.wrapped:
        return p.op == "ilike" or (p.op == "like" and _like_known(p) and _leading(p))
    if p.op == "ilike":
        return True
    return p.op == "like" and _like_known(p) and _leading(p)


def _leading(p: predmod.Predicate) -> bool:
    return predmod.leading_wildcard(p.pattern or "")


def _clusters(preds: list[predmod.Predicate]) -> list[list[predmod.Predicate]]:
    """Groups of predicates that hold together (one per combination of ``OR`` branches)."""
    clusters: list[list[predmod.Predicate]] = []
    seen: list[predmod.Predicate] = []
    for p in preds:
        placed = False
        for cl in clusters:
            if all(predmod.compatible(p, q) for q in cl):
                cl.append(p)
                placed = True
        if not placed:
            clusters.append([q for q in seen if predmod.compatible(p, q)] + [p])
        seen.append(p)
    sets = [frozenset(id(p) for p in cl) for cl in clusters]
    return [cl for i, cl in enumerate(clusters) if not any(sets[i] < s for s in sets)]


def query_requirements(
    sql: str, params: Any = None, vendor: str = "postgresql"
) -> list[Requirement]:
    """The index requirements of one statement (``[]`` for non-``SELECT`` statements).

    ``sql`` may be raw (with ``params``) or normalised (``?`` placeholders); with the
    normalised form ``LIKE ?`` patterns are unknown and produce no requirement.
    """
    if not sql or not sqlmod.is_explainable(sql):
        return []
    if vendor == "mssql" and any(
        len(parts := sqlmod._split_qualified(m.group("table"))) > 1 and parts[-2].lower() != "dbo"
        for m in sqlmod._TABLE_REF.finditer(sql)
    ):
        return []
    try:
        preds = predmod.analyze_predicates(sql, params)
        fp = sqlmod.fingerprint(sql)
        order = _order_by(fp, sqlmod._aliases(fp), sqlmod.primary_table(sql))
        limited = sqlmod.has_limit(sql)
    except Exception:  # pragma: no cover - regex safety net
        return []
    out: list[Requirement] = []

    def add(req: Requirement) -> None:
        if req.size == 0 or req in out:
            return
        if req.kind == "btree" and not req.tail and {c.lower() for c in req.eq} == {"id"}:
            return  # the primary key
        out.append(req)

    tables = list(preds)
    if order and order[0] not in tables:
        tables.append(order[0])
    text_kind = _TEXT_KIND.get(vendor)
    for table in tables:
        mine = preds.get(table, [])
        filters = [p for p in mine if p.join_column is None]
        for p in mine:
            if p.join_column is not None and p.join_table is not None and not p.wrapped:
                add(Requirement(table, frozenset([p.column])))
                add(Requirement(p.join_table, frozenset([p.join_column])))
        if text_kind:
            for p in filters:
                if _text_search(p):
                    add(Requirement(table, frozenset(), (p.column,), text_kind))
        clusters = _clusters([p for p in filters if _btree_usable(p)])
        ocols = order[1] if order and order[0] == table else None
        for cl in clusters:
            eq = list(dict.fromkeys(p.column for p in cl if p.op in _EQ_OPS))
            ranges = [p.column for p in cl if p.op not in _EQ_OPS and p.column not in eq]
            tail: tuple[str, ...] = ()
            usable_order = (
                [c for c in ocols if c not in eq] if ocols and len(clusters) == 1 else None
            )
            if ranges:
                if usable_order and usable_order[0] == ranges[0]:
                    tail = tuple(usable_order)
                else:
                    tail = (ranges[0],)
            elif usable_order:
                tail = tuple(usable_order)
            if len(eq) >= MAX_COLUMNS:
                eq, tail = sorted(eq)[:MAX_COLUMNS], ()
            tail = tail[: MAX_COLUMNS - len(eq)]
            add(Requirement(table, frozenset(eq), tail))
        if not clusters and ocols and limited:
            add(Requirement(table, frozenset(), tuple(ocols[:MAX_COLUMNS])))
    return out


# ── DDL text ──────────────────────────────────────────────────────────────────────────


def _index_name(*parts: str) -> str:
    name = re.sub(r"\W+", "_", "_".join(parts)).strip("_").lower()
    if len(name) > 63:
        digest = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        name = f"{name[:54]}_{digest}"
    return name


def create_sql(table: str, columns: Sequence[str], vendor: str, kind: str = "btree") -> str:
    """The ``CREATE INDEX`` (or text-search equivalent) statement, vendor-quoted."""
    q = lambda name: _quote(name, vendor)  # noqa: E731
    cols = ", ".join(q(c) for c in columns)
    if kind == "trigram":
        name = _index_name("ix", table, *columns, "trgm")
        return (
            "CREATE EXTENSION IF NOT EXISTS pg_trgm; "
            f"CREATE INDEX {q(name)} ON {q(table)} USING gin ({cols} gin_trgm_ops);"
        )
    if kind == "fts":
        name = _index_name(table, *columns, "fts")
        content = table.replace("'", "''")
        return (
            f"CREATE VIRTUAL TABLE {q(name)} USING fts5({cols}, content='{content}', "
            "tokenize='trigram');"
        )
    if kind == "fulltext":
        name = _index_name("ix", table, *columns, "ft")
        return f"ALTER TABLE {q(table)} ADD FULLTEXT INDEX {q(name)} ({cols});"
    name = _index_name("ix", table, *columns)
    return f"CREATE INDEX {q(name)} ON {q(table)} ({cols});"


_TEXT_NOTE = {
    "trigram": "Needs the pg_trgm extension; serves LIKE/ILIKE '%…%'. Adds write overhead.",
    "fts": "Not a drop-in: an FTS5 table queried with MATCH (trigram tokenizer, SQLite 3.34+); "
    "it must be kept in sync with the content table.",
    "fulltext": "Not a drop-in: word-based, query with MATCH(col) AGAINST (… IN BOOLEAN MODE).",
}


# ── catalog ───────────────────────────────────────────────────────────────────────────


@dataclass
class Catalog:
    """Existing indexes: ``{table_lower: [(index_name, (columns_lower…), kind)]}``."""

    tables: set[str] = field(default_factory=set)
    indexes: dict[str, list[tuple[str, tuple[str, ...], str]]] = field(default_factory=dict)

    def add(self, table: str, name: str, columns: Iterable[str], kind: str) -> None:
        cols = tuple(c.lower() for c in columns)
        if cols:
            self.indexes.setdefault(table.lower(), []).append((name, cols, kind))

    def satisfied_by(self, req: Requirement) -> str | None:
        """The name of an existing index that already serves ``req``."""
        for name, cols, kind in self.indexes.get(req.table.lower(), ()):
            if req.kind == "btree":
                if kind == "btree" and covers(cols, req):
                    return name
            elif kind == req.kind and req.tail and req.tail[0].lower() in cols:
                if kind != "trigram" or cols[0] == req.tail[0].lower():
                    return name
        return None

    def has_table(self, table: str) -> bool:
        return not self.tables or table.lower() in self.tables


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


_PG_INDEXDEF = re.compile(r"\bUSING\s+(?P<method>\w+)\s*\(", re.IGNORECASE)
_PG_ITEM = re.compile(
    r'^(?P<col>"(?:[^"]|"")+"|[A-Za-z_][\w$]*)(?:\s+COLLATE\s+\S+)?(?:\s+(?P<opc>[\w.]+))?'
    r"(?:\s+(?:ASC|DESC))?(?:\s+NULLS\s+(?:FIRST|LAST))?$",
    re.IGNORECASE,
)


def _split_top(text: str) -> list[str]:
    items, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(text[start:i])
            start = i + 1
    items.append(text[start:])
    return [s.strip() for s in items]


def parse_pg_indexdef(indexdef: str) -> tuple[list[str], str] | None:
    """``(leading plain columns, kind)`` of a ``pg_indexes.indexdef``; ``None`` if partial."""
    m = _PG_INDEXDEF.search(indexdef)
    if not m:
        return None
    depth, end = 1, len(indexdef)
    for i in range(m.end(), len(indexdef)):
        if indexdef[i] == "(":
            depth += 1
        elif indexdef[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if re.search(r"\bWHERE\b", indexdef[end:], re.IGNORECASE):
        return None  # partial: serves only its own predicate
    method = m.group("method").lower()
    cols: list[str] = []
    opclasses: list[str] = []
    for item in _split_top(indexdef[m.end() : end]):
        im = _PG_ITEM.match(item)
        if not im:
            break  # an expression: only the plain prefix counts
        cols.append(sqlmod._unquote(im.group("col")))
        opclasses.append((im.group("opc") or "").lower())
    if method == "btree":
        kind = "btree"
    elif method in ("gin", "gist") and any("trgm" in o for o in opclasses):
        kind = "trigram"
    else:
        kind = method
    return cols, kind


_SQLITE_FTS = re.compile(r"\bfts5\s*\((?P<args>.*)\)", re.IGNORECASE | re.DOTALL)
_SQLITE_CONTENT = re.compile(r"^content\s*=\s*['\"]?(?P<t>[^'\"]+)['\"]?$", re.IGNORECASE)


def load_catalog(vendor: str, executor: ExplainExecutor) -> Catalog:
    """Existing indexes, read with catalog ``SELECT``\\ s through ``executor``.

    Raises whatever the executor raises; :func:`advise` treats that as "not verified".
    """
    cat = Catalog()
    if vendor == "mssql":
        rows = executor(
            "SELECT TOP (5000) o.name, i.name, c.name, i.has_filter "
            "FROM sys.tables o JOIN sys.schemas s ON s.schema_id=o.schema_id "
            "JOIN sys.indexes i ON i.object_id=o.object_id "
            "JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id "
            "JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id "
            "WHERE s.name='dbo' AND i.is_disabled=0 AND i.is_hypothetical=0 "
            "AND ic.is_included_column=0 AND ic.key_ordinal>0 "
            "ORDER BY o.name,i.name,ic.key_ordinal",
            None,
        )
        cat.tables.update(_text(r[0]).lower() for r in rows)
        _add_grouped(cat, ((r[0], r[1], r[2], "btree", bool(r[3])) for r in rows))
    elif vendor == "postgresql":
        scope = "schemaname = ANY (current_schemas(false))"
        for row in executor(f"SELECT tablename FROM pg_tables WHERE {scope}", None):
            cat.tables.add(_text(row[0]).lower())
        for row in executor(
            f"SELECT tablename, indexname, indexdef FROM pg_indexes WHERE {scope}", None
        ):
            parsed = parse_pg_indexdef(_text(row[2]))
            if parsed:
                cat.add(_text(row[0]), _text(row[1]), parsed[0], parsed[1])
    elif vendor == "sqlite":
        for row in executor("SELECT name, sql FROM sqlite_master WHERE type = 'table'", None):
            name, ddl = _text(row[0]), _text(row[1])
            cat.tables.add(name.lower())
            fts = _SQLITE_FTS.search(ddl)
            if fts:
                args = _split_top(fts.group("args"))
                content = next(
                    (m.group("t") for a in args if (m := _SQLITE_CONTENT.match(a))), name
                )
                cols = [sqlmod._unquote(a) for a in args if "=" not in a]
                for col in cols:  # every column is searchable on its own
                    cat.add(content, name, [col], "fts")
        rows = executor(
            "SELECT m.name, il.name, il.partial, ii.seqno, ii.name FROM sqlite_master AS m "
            "JOIN pragma_index_list(m.name) AS il JOIN pragma_index_info(il.name) AS ii "
            "WHERE m.type = 'table' ORDER BY m.name, il.name, ii.seqno",
            None,
        )
        _add_grouped(cat, ((r[0], r[1], r[4], "btree", bool(r[2])) for r in rows))
        rows = executor(
            "SELECT m.name, p.name, p.pk FROM sqlite_master AS m "
            "JOIN pragma_table_info(m.name) AS p WHERE m.type = 'table' AND p.pk > 0 "
            "ORDER BY m.name, p.pk",
            None,
        )
        _add_grouped(cat, ((r[0], "(primary key)", r[1], "btree", False) for r in rows))
    elif vendor == "mysql":
        for row in executor(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()",
            None,
        ):
            cat.tables.add(_text(row[0]).lower())
        rows = executor(
            "SELECT TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
            "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
            None,
        )
        kinds = {"BTREE": "btree", "FULLTEXT": "fulltext"}
        _add_grouped(
            cat,
            (
                (r[0], r[1], r[3], kinds.get(_text(r[4]).upper(), _text(r[4]).lower()), False)
                for r in rows
            ),
        )
    else:
        raise ValueError(f"No catalog reader for {vendor!r}")
    return cat


def _add_grouped(cat: Catalog, rows: Iterable[tuple[Any, Any, Any, str, bool]]) -> None:
    """Add ``(table, index, column, kind, partial)`` rows (ordered by position)."""
    groups: dict[tuple[str, str], tuple[list[str | None], str, bool]] = {}
    for table, index, column, kind, partial in rows:
        key = (_text(table), _text(index))
        if key not in groups:
            groups[key] = ([], kind, partial)
        groups[key][0].append(_text(column) if column is not None else None)
    for (table, index), (cols, kind, partial) in groups.items():
        if partial:
            continue
        prefix: list[str] = []
        for c in cols:
            if c is None:
                break  # an expression column ends the usable prefix
            prefix.append(c)
        if kind == "fulltext":
            for c in prefix:
                cat.add(table, index, [c], kind)
        else:
            cat.add(table, index, prefix, kind)


_PG_UNUSED = (
    "SELECT s.schemaname, s.relname, s.indexrelname, s.idx_scan, "
    "pg_relation_size(s.indexrelid) FROM pg_stat_user_indexes AS s "
    "JOIN pg_index AS i ON i.indexrelid = s.indexrelid "
    "WHERE s.idx_scan = 0 AND NOT i.indisprimary AND NOT i.indisunique "
    "AND NOT EXISTS (SELECT 1 FROM pg_constraint AS c WHERE c.conindid = s.indexrelid) "
    "ORDER BY pg_relation_size(s.indexrelid) DESC, s.relname, s.indexrelname LIMIT 100"
)


_MSSQL_UNUSED = (
    "SELECT TOP (100) s.name, o.name, i.name, "
    "COALESCE(us.user_seeks, 0) + COALESCE(us.user_scans, 0) "
    "+ COALESCE(us.user_lookups, 0) AS scans, "
    "COALESCE((SELECT SUM(ps.used_page_count) FROM sys.dm_db_partition_stats AS ps "
    "WHERE ps.object_id = o.object_id AND ps.index_id = i.index_id), 0) * 8192 "
    "FROM sys.indexes AS i "
    "JOIN sys.tables AS o ON o.object_id = i.object_id "
    "JOIN sys.schemas AS s ON s.schema_id = o.schema_id "
    "LEFT JOIN sys.dm_db_index_usage_stats AS us "
    "ON us.database_id = DB_ID() AND us.object_id = i.object_id AND us.index_id = i.index_id "
    "WHERE s.name = 'dbo' AND i.index_id > 1 AND i.is_primary_key = 0 "
    "AND i.is_unique_constraint = 0 AND i.is_unique = 0 AND i.is_hypothetical = 0 "
    "AND i.is_disabled = 0 "
    "AND COALESCE(us.user_seeks, 0) + COALESCE(us.user_scans, 0) "
    "+ COALESCE(us.user_lookups, 0) = 0 "
    "ORDER BY 5 DESC, s.name, o.name, i.name"
)


def unused_indexes(executor: ExplainExecutor, vendor: str = "postgresql") -> list[dict[str, Any]]:
    """Never-read indexes for PostgreSQL or SQL Server.

    Primary keys, unique indexes and indexes backing a constraint are excluded. SQL Server
    counters come from ``sys.dm_db_index_usage_stats`` and reset when the instance restarts.
    """
    if vendor == "mssql":
        query = _MSSQL_UNUSED
    elif vendor == "postgresql":
        query = _PG_UNUSED
    else:
        raise ValueError(f"Unused-index analysis is not available for {vendor!r}")
    out = []
    for row in executor(query, None):
        schema, table, index = _text(row[0]), _text(row[1]), _text(row[2])
        if vendor == "mssql":
            drop_sql = (
                f"DROP INDEX {_quote(index, 'mssql')} ON "
                f"{_quote(schema, 'mssql')}.{_quote(table, 'mssql')};"
            )
        else:
            drop_sql = f"DROP INDEX {_quote(schema, 'postgresql')}."
            drop_sql += f"{_quote(index, 'postgresql')};"
        out.append(
            {
                "schema": schema,
                "table": table,
                "index": index,
                "scans": int(row[3] or 0),
                "size_bytes": int(row[4] or 0),
                "drop_sql": drop_sql,
            }
        )
    return out


# ── set cover ─────────────────────────────────────────────────────────────────────────


@dataclass
class _Query:
    fingerprint: str
    sql: str
    count: int
    total_ms: float
    vendor: str
    requirements: list[Requirement]
    raw_sql: str | None = None
    params: Any = None
    #: lower-cased tables a full/seq scan was seen on; ``None`` when not explained.
    scanned: set[str] | None = None

    @property
    def weight(self) -> float:
        return self.total_ms / len(self.requirements) if self.requirements else 0.0


def _ordered_columns(req: Requirement, col_weight: Mapping[tuple[str, str], float]) -> list[str]:
    t = req.table.lower()
    eq = sorted(req.eq, key=lambda c: (-col_weight.get((t, c.lower()), 0.0), c))
    return [*eq, *req.tail]


def select_indexes(
    queries: Sequence[_Query], *, limit: int = MAX_SUGGESTIONS, verified: Mapping[str, bool]
) -> list[dict[str, Any]]:
    """Greedy weighted set cover over the queries' requirements."""
    col_weight: dict[tuple[str, str], float] = {}
    for q in queries:
        for r in q.requirements:
            for c in r.eq:
                key = (r.table.lower(), c.lower())
                col_weight[key] = col_weight.get(key, 0.0) + q.weight
    # (table, columns, kind, vendor) → candidate
    pool: dict[tuple[str, tuple[str, ...], str, str], tuple[str, list[str], str, str]] = {}
    for q in queries:
        for r in q.requirements:
            cols = _ordered_columns(r, col_weight)
            key = (r.table.lower(), tuple(c.lower() for c in cols), r.kind, q.vendor)
            pool.setdefault(key, (r.table, cols, r.kind, q.vendor))
    units = [(qi, ri) for qi, q in enumerate(queries) for ri in range(len(q.requirements))]

    def serves(cand: tuple[str, list[str], str, str], qi: int, ri: int) -> bool:
        table, cols, kind, vendor = cand
        q = queries[qi]
        r = q.requirements[ri]
        if q.vendor != vendor or r.table.lower() != table.lower() or r.kind != kind:
            return False
        if kind == "btree":
            return covers(cols, r)
        return [c.lower() for c in cols] == [c.lower() for c in r.tail]

    uncovered = set(units)
    chosen: list[dict[str, Any]] = []
    while uncovered and len(chosen) < limit:
        best: tuple[float, int, str] | None = None
        best_cand = None
        best_units: list[tuple[int, int]] = []
        for key, cand in pool.items():
            hit = [u for u in uncovered if serves(cand, *u)]
            gain = sum(queries[qi].weight for qi, _ in hit)
            if gain <= 0:
                continue
            rank = (-gain, len(cand[1]), "/".join(key[1]) + key[0])
            if best is None or rank < best:
                best, best_cand, best_units = rank, cand, hit
        if best_cand is None:
            break
        uncovered -= set(best_units)
        chosen.append(_candidate(best_cand, best_units, queries, verified))
    return chosen


def _candidate(
    cand: tuple[str, list[str], str, str],
    units: list[tuple[int, int]],
    queries: Sequence[_Query],
    verified: Mapping[str, bool],
) -> dict[str, Any]:
    table, cols, kind, vendor = cand
    fps: list[str] = []
    total_ms = 0.0
    executions = 0
    saving = 0.0
    states: set[str] = set()
    for qi, _ in sorted(units):
        q = queries[qi]
        if q.scanned is None:
            state = "unknown"
        elif table.lower() in q.scanned:
            state = "scan"
        else:
            state = "indexed"
        states.add(state)
        saving += q.weight * (SCAN_SAVING if state == "scan" else DEFAULT_SAVING)
        if q.fingerprint not in fps:
            fps.append(q.fingerprint)
            total_ms += q.total_ms
            executions += q.count
    if kind != "btree":
        confidence = "low"
    elif "scan" in states:
        confidence = "high"
    elif "unknown" in states:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "table": table,
        "columns": list(cols),
        "create_sql": create_sql(table, cols, vendor, kind),
        "covers": fps,
        "queries": executions,
        "total_ms": round(total_ms, 3),
        "est_saving_ms": round(saving, 3),
        "vendor": vendor,
        "kind": kind,
        "confidence": confidence,
        "verified": bool(verified.get(vendor)),
        "explained": "scan" in states or "indexed" in states,
        "note": _TEXT_NOTE.get(kind),
    }


# ── orchestration ─────────────────────────────────────────────────────────────────────


def _sample_span(trace: Any, fingerprint: str) -> Any:
    for span in getattr(trace, "spans", ()):
        if span.kind != "sql":
            continue
        data = span.data
        fp = data.get("fingerprint") or sqlmod.fingerprint(str(data.get("sql", "")))
        if fingerprint_key(fp) == fingerprint:
            return span
    return None


def _scanned_tables(tree: Any, tables: Iterable[str]) -> set[str]:
    out: set[str] = set()
    names = {t.lower() for t in tables}
    for node in tree.walk():
        if not _SCAN_FLAGS.intersection(node.flags):
            continue
        text = f"{node.label} {node.detail}".lower()
        for t in names:
            if re.search(rf"(?<![\w$]){re.escape(t)}(?![\w$])", text):
                out.add(t)
    return out


def advise(
    workload: Sequence[Any],
    *,
    get_trace: Callable[[str], Any] | None = None,
    db: str = "default",
    executor_factory: ExecutorFactory | None = None,
    budget_s: float = BUDGET_S,
    explain_top: int = EXPLAIN_TOP,
    max_queries: int = MAX_QUERIES,
    limit: int = MAX_SUGGESTIONS,
) -> dict[str, Any]:
    """The ``GET api/indexes`` payload for ``workload`` (``SqlStats``, by total time).

    ``get_trace(trace_id)`` loads a sample trace (raw SQL, parameters and ``db_alias``);
    fingerprints whose sample ran on another alias than ``db`` are left out (a missing
    sample counts as ``"default"``). ``executor_factory(db, vendor)`` enables the catalog
    check, EXPLAIN confirmation and the unused-index report.
    """
    started = time.monotonic()

    def in_budget() -> bool:
        return time.monotonic() - started < budget_s

    traces: dict[str, Any] = {}
    queries: list[_Query] = []
    vendors: dict[str, int] = {}
    analyzed = 0
    for stats in workload:
        if analyzed >= max_queries:
            break
        if not sqlmod.is_explainable(stats.sql):
            continue
        span = None
        if get_trace is not None and stats.sample_trace_id:
            if stats.sample_trace_id not in traces:
                try:
                    traces[stats.sample_trace_id] = get_trace(stats.sample_trace_id)
                except Exception:  # pragma: no cover - a broken sample must not fail the page
                    traces[stats.sample_trace_id] = None
            span = _sample_span(traces[stats.sample_trace_id], stats.fingerprint)
        alias = str(span.data.get("db_alias") or "default") if span is not None else "default"
        if alias != db:
            continue
        analyzed += 1
        vendor = str(stats.vendor or "other")
        vendors[vendor] = vendors.get(vendor, 0) + 1
        raw = params = None
        if span is not None and span.data.get("explainable") and span.data.get("sql"):
            raw, params = str(span.data["sql"]), span.data.get("params")
        reqs = query_requirements(raw, params, vendor) if raw else []
        if not reqs:
            reqs = query_requirements(stats.sql, None, vendor)
        if reqs:
            queries.append(
                _Query(stats.fingerprint, stats.sql, int(stats.count), float(stats.total_ms),
                       vendor, reqs, raw, params)
            )  # fmt: skip

    executors: dict[str, ExplainExecutor | None] = {}

    def executor_for(vendor: str) -> ExplainExecutor | None:
        if vendor not in executors:
            ex = None
            if executor_factory is not None and vendor in (*_TEXT_KIND, "mssql"):
                try:
                    ex = executor_factory(db, vendor)
                except Exception:
                    logger.debug("Redline: no executor for %s (%s)", db, vendor, exc_info=True)
            executors[vendor] = ex
        return executors[vendor]

    # existing indexes
    verified: dict[str, bool] = {}
    catalogs: dict[str, Catalog] = {}
    for vendor in sorted(vendors, key=lambda v: -vendors[v]):
        ex = executor_for(vendor)
        if ex is None or not in_budget():
            continue
        try:
            catalogs[vendor] = load_catalog(vendor, ex)
            verified[vendor] = True
        except Exception:
            logger.debug("Redline: catalog read failed for %s", vendor, exc_info=True)
    already = 0
    for q in queries:
        cat = catalogs.get(q.vendor)
        if cat is None:
            continue
        keep = []
        for r in q.requirements:
            if not cat.has_table(r.table):
                continue
            if cat.satisfied_by(r):
                already += 1
                continue
            keep.append(r)
        q.requirements = keep
    queries = [q for q in queries if q.requirements]

    # EXPLAIN the heaviest remaining queries
    explained = 0
    for q in sorted(queries, key=lambda q: -q.total_ms)[:explain_top]:
        ex = executor_for(q.vendor)
        if ex is None or q.raw_sql is None or not in_budget():
            continue
        try:
            result = run_explain(q.raw_sql, q.params, q.vendor, ex)
        except Exception:
            continue
        explained += 1
        q.scanned = _scanned_tables(result.tree, {r.table for r in q.requirements})

    candidates = select_indexes(queries, limit=limit, verified=verified)

    # unused indexes (PostgreSQL and SQL Server)
    unused: list[dict[str, Any]] = []
    unused_supported = False
    unused_error: dict[str, str] | None = None
    main_vendor = max(vendors, key=lambda v: vendors[v]) if vendors else None
    if main_vendor in (None, "postgresql", "mssql"):
        target = main_vendor or "postgresql"
        ex = executor_for(target)
        if ex is None:
            if main_vendor is not None:
                source = (
                    "sys.dm_db_index_usage_stats" if target == "mssql" else "pg_stat_user_indexes"
                )
                unused_error = {
                    "code": "explain_unavailable",
                    "message": f"No database connection for {db!r} ({target}); unused "
                    f"indexes need {source}.",
                }
        else:
            try:
                unused = unused_indexes(ex, target)
                unused_supported = True
            except Exception as exc:
                if main_vendor is not None:
                    unused_error = {
                        "code": "explain_failed",
                        "message": f"{type(exc).__name__}: {exc}",
                    }

    return {
        "db": db,
        "vendor": main_vendor,
        "candidates": candidates,
        "unused": unused,
        "unused_supported": unused_supported,
        "unused_error": unused_error,
        "verified": bool(verified) and all(verified.get(v) for v in vendors if v in _TEXT_KIND),
        "queries_analyzed": analyzed,
        "already_indexed": already,
        "explained": explained,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }
