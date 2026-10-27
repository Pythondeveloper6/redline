"""SQLAlchemy fix suggester: turn an N+1 into a concrete eager-loading diff.

The core detector tells us *which* query repeated and *where*. This module works out what
to change:

1. parse the repeated statement into ``(table, column)`` equality filters (``WHERE
   order_items.order_id = ?`` – SQLAlchemy puts the bound parameter on either side);
2. map those onto the ORM relationships of the mapped classes (``registry.mappers``):

   ===========================================  =====================================
   repeated query                               suggestion
   ===========================================  =====================================
   child table filtered by its FK column        ``selectinload(Parent.children)``
   parent table filtered by its PK/target col   ``joinedload(Child.parent)``
   association table filtered by a parent col   ``selectinload(Parent.tags)``
   ===========================================  =====================================

3. find the ``select(Parent)`` / ``session.query(Parent)`` statement in the source, walking
   the callsite's project frames, and emit a unified diff that appends
   ``.options(...)`` (plus the import when missing). When the statement cannot be found
   the suggestion is a code hint with lower confidence.

SQLModel works unchanged: its models live in a normal SQLAlchemy registry.
"""

from __future__ import annotations

import ast
import difflib
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import JSON, LargeBinary, Text

from redline_core import sql as sqlmod
from redline_core.linefix import line_diff
from redline_core.model import Issue, Span, Suggestion

logger = logging.getLogger("redline")

__all__ = ["SQLAlchemyFixSuggester"]

DOCS_URL = "https://docs.sqlalchemy.org/en/20/orm/queryguide/relationships.html"
DOCS_LOAD_ONLY = "https://docs.sqlalchemy.org/en/20/orm/queryguide/columns.html"
DOCS_QUERYING = "https://docs.sqlalchemy.org/en/20/orm/queryguide/select.html"

_WIDE_TYPES = (Text, LargeBinary, JSON)

#: Methods that end a query chain; ``.options()`` must be inserted before them.
_TERMINAL = frozenset(
    {
        "all", "first", "one", "one_or_none", "scalar", "scalar_one", "scalar_one_or_none",
        "count", "get", "scalars", "execute", "exists", "delete", "update", "yield_per",
        "fetchall", "fetchone", "unique", "mappings",
    }
)  # fmt: skip


@dataclass
class _Candidate:
    owner: Any  # mapper that loads the relationship (the class to select)
    rel: Any  # RelationshipProperty
    loader: str  # "selectinload" | "joinedload"

    @property
    def owner_name(self) -> str:
        return str(self.owner.class_.__name__)

    @property
    def expr(self) -> str:
        return f"{self.loader}({self.owner_name}.{self.rel.key})"


class SQLAlchemyFixSuggester:
    """Maps repeated SQL back to ORM relationships and proposes eager loading.

    ``models`` may be a declarative ``Base``, a ``registry``, a mapped class, or an iterable
    of those. Without it every registry known to SQLAlchemy is used, which is right for
    nearly all apps.
    """

    def __init__(self, models: Any = None, *, project_root: str | None = None) -> None:
        self._registries = list(_registries_of(models)) if models is not None else None
        self.project_root = os.path.abspath(project_root or os.getcwd())
        self._ast_cache: dict[str, tuple[float, str, ast.Module]] = {}

    # discovery -------------------------------------------------------------
    def _mappers(self) -> list[Any]:
        registries = self._registries
        if registries is None:
            try:
                from sqlalchemy.orm import mapperlib

                registries = list(mapperlib._all_registries())
            except Exception:  # pragma: no cover - private API moved
                registries = []
        mappers: list[Any] = []
        for reg in registries:
            try:
                reg.configure()
            except Exception:
                logger.debug("Redline: could not configure registry %r", reg, exc_info=True)
            mappers.extend(reg.mappers)
        return mappers

    # protocol ---------------------------------------------------------------
    def suggest(self, issue: Issue, spans: list[Span]) -> Suggestion | None:
        if issue.kind in ("large_result", "unbounded_query") and spans:
            return self._result_size(issue, spans)
        if issue.kind == "inefficient_count":
            return self._count(issue)
        if issue.kind != "n_plus_one" or not spans:
            return None
        sql = str(spans[0].data.get("sql", ""))
        filters, _tables = _param_filters(sql)
        if not filters:
            return None
        mappers = self._mappers()
        candidates = _candidates(mappers, filters)
        if not candidates:
            return None
        callsite = issue.callsite or spans[0].callsite
        ranked = self._rank(candidates, callsite)
        best = ranked[0]
        located = best[1]
        cand = best[2]
        unique = len(candidates) == 1
        what = f"{cand.owner_name}.{cand.rel.key} (" + (
            "collection)" if cand.rel.uselist else "many-to-one)"
        )
        loader_doc = "one extra IN query" if cand.loader == "selectinload" else "a JOIN"
        summary = (
            f"Eager-load {what} with {cand.loader}() when selecting {cand.owner_name} – "
            f"{loader_doc} instead of one query per row."
        )
        code = f"select({cand.owner_name}).options({cand.expr})"
        if located is not None:
            diff = self._diff(located, cand)
            return Suggestion(
                summary=summary,
                code=code,
                diff=diff,
                confidence="high" if diff else "medium",
                docs_url=DOCS_URL,
            )
        return Suggestion(
            summary=summary,
            code=code,
            diff=None,
            confidence="medium" if unique else "low",
            docs_url=DOCS_URL,
        )

    # ranking -----------------------------------------------------------------
    def _rank(
        self, candidates: list[_Candidate], callsite: Any
    ) -> list[tuple[int, Any, _Candidate]]:
        code = (callsite.code or "") if callsite else ""
        out = []
        for cand in candidates:
            located = self._locate(cand, callsite) if callsite else None
            score = (4 if located else 0) + (2 if f".{cand.rel.key}" in code else 0)
            score += 1 if cand.rel.uselist and cand.loader == "selectinload" else 0
            out.append((score, located, cand))
        out.sort(key=lambda t: -t[0])
        return out

    # source location -----------------------------------------------------------
    def _read(self, display: str) -> tuple[str, ast.Module, str] | None:
        for base in (self.project_root, os.getcwd(), ""):
            path = os.path.join(base, display) if base else display
            if os.path.isfile(path):
                break
        else:
            return None
        try:
            mtime = os.path.getmtime(path)
            cached = self._ast_cache.get(path)
            if cached and cached[0] == mtime:
                return path, cached[2], cached[1]
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            return None
        self._ast_cache[path] = (mtime, source, tree)
        return path, tree, source

    def _locate(self, cand: _Candidate, callsite: Any) -> _Located | None:
        frames = list(getattr(callsite, "stack", ()) or ())
        if not frames:
            frames = [callsite]
        for frame in reversed(frames):  # innermost first
            loaded = self._read(frame.file)
            if loaded is None:
                continue
            _path, tree, source = loaded
            func = _enclosing_function(tree, frame.function, frame.line)
            scope: ast.AST = func if func is not None else tree
            found = _find_statement(scope, cand.owner_name, frame.line)
            if found is None:
                continue
            node, lineno, col = found
            return _Located(frame.file, source, tree, lineno, col, node)
        return None

    # result size ----------------------------------------------------------------
    def wide_columns(self, sql: str) -> tuple[Any, list[str]]:
        """``(mapper, [attribute names])`` of the Text/LargeBinary/JSON columns ``sql`` selects.

        Computed here, off the hot path, from the SQL column list and the mappers.
        """
        table = sqlmod.primary_table(sql)
        if table is None:
            return None, []
        mapper = next(
            (m for m in self._mappers() if getattr(m.local_table, "name", None) == table), None
        )
        if mapper is None:
            return None, []
        aliases = sqlmod.table_aliases(sql)
        wide: list[str] = []
        for qualifier, column in sqlmod.select_columns(sql):
            if qualifier is not None and aliases.get(qualifier, qualifier) != table:
                continue
            col = mapper.local_table.columns.get(column)
            if col is None or not isinstance(col.type, _WIDE_TYPES):
                continue
            try:
                key = mapper.get_property_by_column(col).key
            except Exception:
                key = column
            if key not in wide:
                wide.append(key)
        return mapper, wide

    def _result_size(self, issue: Issue, spans: list[Span]) -> Suggestion | None:
        sql = str(spans[0].data.get("sql", ""))
        mapper, wide = self.wide_columns(sql)
        if mapper is None:
            return None
        if wide:
            issue.extra["wide_columns"] = wide
            for sp in spans:
                sp.data.setdefault("wide_columns", wide)
        owner = str(mapper.class_.__name__)
        rows = issue.extra.get("rows")
        sized = f"{rows:,} {owner} rows" if isinstance(rows, int) else f"{owner} rows"
        paginate = (
            "Paginate it (.limit()/.offset() or keyset pagination), or stream it with "
            ".execution_options(yield_per=...)."
        )
        callsite = issue.callsite or spans[0].callsite
        located = self._locate_owner(owner, callsite) if callsite else None
        if wide:
            expr = ", ".join(f"defer({owner}.{k})" for k in wide)
            summary = (
                f"The query loads {sized} including the large column(s) {', '.join(wide)}. "
                f"Defer them with defer() (or select only the needed columns with "
                f"load_only()) so they load on first access. {paginate}"
            )
            diff = self._options_diff(located, expr, "defer") if located else None
            return Suggestion(
                summary=summary,
                code=f"select({owner}).options({expr})",
                diff=diff,
                confidence="high" if diff else "medium",
                docs_url=DOCS_LOAD_ONLY,
            )
        if issue.kind == "unbounded_query":
            summary = f"This query has no limit and returned {sized}. Add .limit(N). {paginate}"
            code = f"select({owner}).limit(100)"
        else:
            summary = f"The query returned {sized}. {paginate}"
            code = f"select({owner}).execution_options(yield_per=1000)"
        return Suggestion(summary=summary, code=code, confidence="low", docs_url=DOCS_LOAD_ONLY)

    # inefficient count ------------------------------------------------------------
    def _count(self, issue: Issue) -> Suggestion | None:
        extra = issue.extra
        pattern, expr, old = extra.get("pattern"), str(extra.get("expr", "")), extra.get("old")
        if not (pattern and expr and old):
            return None
        legacy_all = expr.endswith(".all()") and "query(" in expr
        new: str | None = None
        if pattern == "count_cmp":
            negate = str(extra.get("new", "")).startswith("not ")
            new = f"{expr}.first() is {'' if negate else 'not '}None"
            summary = (
                f"`{old}` counts every matching row only to compare with zero. "
                f"`{new}` stops at the first row (LIMIT 1)."
            )
            hint = new
        elif pattern == "len":
            if legacy_all:
                new = expr[: -len(".all()")] + ".count()"
            summary = (
                "Let the database count instead of loading every row: "
                "select(func.count()).select_from(<statement>.subquery()) with session.scalar(), "
                "or Query.count()."
            )
            hint = new or "session.scalar(select(func.count()).select_from(stmt.subquery()))"
        else:
            if legacy_all:
                first = expr[: -len(".all()")] + ".first()"
                new = first if pattern == "truthy" else f"{first} is not None"
            summary = (
                f"`{old}` loads every row just to test for any. Fetch one row "
                "(.first() / LIMIT 1) or select an EXISTS."
            )
            hint = new or "session.scalar(select(exists().where(...)))"
        diff = line_diff(self.project_root, issue.callsite, str(old), new) if new else None
        code = None
        if new:
            code = str(extra.get("fix_line", "")).replace(str(extra.get("new", "")), new, 1)
        return Suggestion(
            summary=summary,
            code=code or hint,
            diff=diff,
            confidence="high" if diff else "low",
            docs_url=DOCS_QUERYING,
        )

    def _locate_owner(self, owner: str, callsite: Any) -> _Located | None:
        frames = list(getattr(callsite, "stack", ()) or ()) or [callsite]
        for frame in reversed(frames):
            loaded = self._read(frame.file)
            if loaded is None:
                continue
            _path, tree, source = loaded
            func = _enclosing_function(tree, frame.function, frame.line)
            found = _find_statement(func if func is not None else tree, owner, frame.line)
            if found is not None:
                node, lineno, col = found
                return _Located(frame.file, source, tree, lineno, col, node)
        return None

    def _diff(self, loc: _Located, cand: _Candidate) -> str | None:
        return self._options_diff(loc, cand.expr, cand.loader)

    def _options_diff(self, loc: _Located, expr: str, import_name: str) -> str | None:
        lines = loc.source.splitlines(keepends=True)
        i = loc.lineno - 1
        if not 0 <= i < len(lines):
            return None
        line = lines[i]
        # ast col offsets are UTF-8 byte offsets
        prefix = line.encode("utf-8")[: loc.col].decode("utf-8", errors="ignore")
        insert = f".options({expr})"
        new_lines = list(lines)
        new_lines[i] = prefix + insert + line[len(prefix) :]
        new_lines = _ensure_import(loc.tree, new_lines, import_name)
        if new_lines == lines:
            return None
        diff = difflib.unified_diff(
            lines,
            new_lines,
            fromfile=f"a/{loc.display}",
            tofile=f"b/{loc.display}",
            n=3,
        )
        text = "".join(d if d.endswith("\n") else d + "\n" for d in diff)
        return text or None


@dataclass
class _Located:
    display: str
    source: str
    tree: ast.Module
    lineno: int  # 1-based line where `.options(...)` goes (end of the chain)
    col: int  # UTF-8 byte offset of the end of the chain
    node: ast.AST


# ── model side ─────────────────────────────────────────────────────────────────────────


def _registries_of(models: Any) -> Iterable[Any]:
    from sqlalchemy.orm import registry as registry_cls

    items = models if isinstance(models, (list, tuple, set, frozenset)) else [models]
    seen: set[int] = set()
    for item in items:
        reg = item if isinstance(item, registry_cls) else getattr(item, "registry", None)
        if reg is None:
            reg = getattr(item, "_sa_registry", None)  # SQLModel
        if reg is not None and id(reg) not in seen:
            seen.add(id(reg))
            yield reg


def _candidates(mappers: list[Any], filters: list[tuple[str, str]]) -> list[_Candidate]:
    from sqlalchemy.orm.interfaces import MANYTOMANY, MANYTOONE, ONETOMANY

    found: list[_Candidate] = []
    seen: set[tuple[int, str]] = set()
    for mapper in mappers:
        for rel in mapper.relationships:
            try:
                direction = rel.direction
                target = rel.mapper.local_table.name
                if rel.secondary is not None or direction is MANYTOMANY:
                    sec = getattr(rel.secondary, "name", None)
                    hit = any(
                        c.table.name == sec and c.name == col and sec == table
                        for _, c in rel.synchronize_pairs
                        for table, col in filters
                    )
                    loader = "selectinload"
                elif direction in (ONETOMANY, MANYTOONE):
                    hit = any(
                        c.table.name == table == target and c.name == col
                        for _, c in rel.local_remote_pairs
                        for table, col in filters
                    )
                    loader = (
                        "joinedload"
                        if direction is MANYTOONE or not rel.uselist
                        else "selectinload"
                    )
                else:
                    continue
            except Exception:
                continue
            key = (id(mapper), rel.key)
            if hit and key not in seen:
                seen.add(key)
                found.append(_Candidate(mapper, rel, loader))
    # prefer the relationship declared on the most "root" owner when subclasses repeat it
    return found


# ── SQL side ───────────────────────────────────────────────────────────────────────────

_EQ_RIGHT = r"(?:(?P<q1>\w+)\.)?(?P<c1>\w+)\s*=\s*\?"
_EQ_LEFT = r"\?\s*=\s*(?:(?P<q2>\w+)\.)?(?P<c2>\w+)"
_EQ = re.compile(f"{_EQ_RIGHT}|{_EQ_LEFT}")
_WHERE = re.compile(
    r"\bWHERE\b(.*?)(?=\bGROUP BY\b|\bORDER BY\b|\bLIMIT\b|$)", re.IGNORECASE | re.DOTALL
)


def _param_filters(sql: str) -> tuple[list[tuple[str, str]], list[str]]:
    """``[(table, column)]`` compared to a bound parameter in the WHERE clause."""
    fp = sqlmod.fingerprint(sql).replace('"', "").replace("`", "")
    tables = sqlmod.tables(sql)
    aliases = sqlmod.table_aliases(sql)
    where = _WHERE.search(fp)
    if not where or not tables:
        return [], tables
    out: list[tuple[str, str]] = []
    for m in _EQ.finditer(where.group(1)):
        qualifier = m.group("q1") or m.group("q2")
        column = m.group("c1") or m.group("c2")
        table = aliases.get(qualifier, qualifier) if qualifier else tables[0]
        if table and column and (table, column) not in out:
            out.append((table, column))
    return out, tables


# ── source side ────────────────────────────────────────────────────────────────────────


def _enclosing_function(tree: ast.Module, name: str, line: int) -> ast.AST | None:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            end = node.end_lineno or node.lineno
            if node.lineno <= line <= end and (best is None or node.lineno > best.lineno):
                best = node
    return best


def _is_query_call(call: ast.Call, owner: str) -> bool:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if name not in ("select", "query"):
        return False
    return any(isinstance(a, ast.Name) and a.id == owner for a in call.args)


def _find_statement(scope: ast.AST, owner: str, line: int) -> tuple[ast.AST, int, int] | None:
    parents: dict[ast.AST, ast.AST] = {}
    matches: list[ast.Call] = []
    for node in ast.walk(scope):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
        if isinstance(node, ast.Call) and _is_query_call(node, owner):
            matches.append(node)
    if not matches:
        return None
    before = [m for m in matches if m.lineno <= line]
    call = max(before, key=lambda m: m.lineno) if before else matches[0]
    node: ast.AST = call
    while True:
        attr = parents.get(node)
        if not (isinstance(attr, ast.Attribute) and attr.value is node):
            break
        outer = parents.get(attr)
        if not (isinstance(outer, ast.Call) and outer.func is attr) or attr.attr in _TERMINAL:
            break
        node = outer
    end_line = getattr(node, "end_lineno", None)
    end_col = getattr(node, "end_col_offset", None)
    if end_line is None or end_col is None:
        return None
    return node, end_line, end_col


def _ensure_import(tree: ast.Module, lines: list[str], name: str) -> list[str]:
    """``lines`` with ``name`` importable from ``sqlalchemy.orm`` (no-op when it is)."""
    last_import = last_absolute = after_sqlalchemy = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = node.end_lineno or node.lineno
            absolute = not (isinstance(node, ast.ImportFrom) and node.level)
            if absolute:
                last_absolute = last_import
                names = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                )
                if any(n == "sqlalchemy" or n.startswith("sqlalchemy.") for n in names):
                    after_sqlalchemy = last_import
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "sqlalchemy.orm"
                and not node.level
            ):
                if any(a.name == name and a.asname is None for a in node.names):
                    return lines
                single = node.lineno == node.end_lineno
                text = lines[node.lineno - 1]
                if single and "(" not in text and "#" not in text and "\\" not in text:
                    out = list(lines)
                    out[node.lineno - 1] = (
                        text.rstrip("\r\n") + f", {name}" + text[len(text.rstrip("\r\n")) :]
                    )
                    return out
                closing = lines[(node.end_lineno or node.lineno) - 1]
                if not single and closing.strip() == ")":
                    prev = lines[(node.end_lineno or node.lineno) - 2]
                    indent = prev[: len(prev) - len(prev.lstrip())]
                    out = list(lines)
                    out.insert((node.end_lineno or node.lineno) - 1, f"{indent}{name},\n")
                    return out
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            break
    new = f"from sqlalchemy.orm import {name}\n"
    out = list(lines)
    if last_import == 0:
        # after a module docstring when there are no imports at all
        idx = 0
        first = tree.body[0] if tree.body else None
        if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
            idx = first.end_lineno or 1
        out.insert(idx, new)
    else:
        out.insert(after_sqlalchemy or last_absolute or last_import, new)
    return out
