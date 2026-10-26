"""``DjangoFixSuggester``: turns an N+1 into the ``select_related``/``prefetch_related`` fix.

Two halves:

1. **Which relation?** The repeated SQL is mapped back to models through the app
   registry (``db_table`` → model):

   * ``FROM child WHERE child.parent_id = ?`` – a reverse foreign key, fixed with
     ``prefetch_related`` (a reverse one-to-one with ``select_related``);
   * ``FROM target JOIN through … WHERE through.x_id = ?`` – a many-to-many, from either
     side: ``prefetch_related``;
   * ``FROM parent WHERE parent.id = ?`` – a forward foreign key or one-to-one:
     ``select_related``;
   * ``FROM t WHERE content_type_id = ? AND object_id = ?`` – a ``GenericRelation``:
     ``prefetch_related``;
   * pk lookups reached through a ``GenericForeignKey`` – ``prefetch_related``, with low
     confidence.

   When several foreign keys point at the fetched model, the callsite's source line
   (``item.product.name``) picks the right one.

2. **Where is the queryset?** The callsite's project stack is walked innermost-first.
   In each frame the instance variable on the callsite line (``item`` in
   ``item.product``) is traced back through the enclosing ``for`` loops – across
   relation hops such as ``for item in order.items.all()``, which turn the lookup into
   ``items__product`` – to the assignment that builds the root queryset
   (``Order.objects…``). Failing that, any queryset of the parent model (or of a model
   one or two relation hops above it) in the frame's function or class is used – which
   covers loops in templates, generic class-based views (``queryset =``, ``model =``,
   ``get_queryset``) and DRF serializers, whose view the middleware adds to the stack.

   The fix is inserted into that expression – before any ordering/slicing, replacing a
   bare ``.all()``, or as an extra argument of an existing ``prefetch_related`` – and
   rendered as a real unified diff. Without a located queryset the result is a code hint
   with lower confidence.
"""

from __future__ import annotations

import ast
import dataclasses
import difflib
import os
import re
import textwrap
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.apps import apps
from django.db import models

from redline_core import sql as sqlmod
from redline_core.model import Callsite, Frame, Issue, Span, Suggestion

__all__ = ["DjangoFixSuggester", "Relation"]

DOCS = "https://docs.djangoproject.com/en/stable/ref/models/querysets/"
DOCS_PREFETCH = DOCS + "#prefetch-related"
DOCS_SELECT = DOCS + "#select-related"
DOCS_PREFETCH_OBJ = DOCS + "#prefetch-objects"
DOCS_GENERIC = "https://docs.djangoproject.com/en/stable/ref/contrib/contenttypes/#genericprefetch"

_IDENT = r'(?:"(?:[^"]|"")+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_EQ = re.compile(rf"(?P<col>{_IDENT}(?:\s*\.\s*{_IDENT})?)\s*=\s*\?")
_WHERE = re.compile(r"\bWHERE\b(?P<body>.*?)(?=\bORDER BY\b|\bGROUP BY\b|\bLIMIT\b|\bOFFSET\b|$)")
_MANY_KINDS = ("reverse_fk", "m2m", "reverse_m2m", "generic_relation", "gfk")
#: Queryset methods the fix is inserted *before* (ordering, slicing, terminal calls).
_STOP = frozenset(
    {
        "order_by",
        "reverse",
        "first",
        "last",
        "get",
        "iterator",
        "count",
        "exists",
        "aggregate",
        "in_bulk",
        "latest",
        "earliest",
        "union",
        "intersection",
        "difference",
    }
)
_NO_PREFETCH = frozenset({"values", "values_list"})
_MAX_DEPTH = 2  # relation hops above the parent model searched for a root queryset


def _unquote(ident: str) -> str:
    ident = ident.strip()
    if ident[:1] in ('"', "`", "[") and len(ident) >= 2:
        return ident[1:-1]
    return ident


# ── relations ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hop:
    """``model.<accessor>`` reaches ``target``."""

    model: type[models.Model]
    accessor: str
    target: type[models.Model]
    multi: bool


@dataclass(frozen=True)
class Relation:
    """The relation an N+1 query loads, from the model whose instances trigger it."""

    parent: type[models.Model]
    accessor: str
    target: type[models.Model] | None
    kind: str  # fk, o2o, reverse_fk, reverse_o2o, m2m, reverse_m2m, generic_relation, gfk
    filtered: bool = False  # the repeated query filters on more than the relation

    @property
    def multi(self) -> bool:
        return self.kind in _MANY_KINDS


class _Registry:
    """``db_table`` → model, and every relation hop, built once from the app registry."""

    def __init__(self) -> None:
        self.by_table: dict[str, type[models.Model]] = {}
        self.models: list[type[models.Model]] = []
        for model in apps.get_models(include_auto_created=True):
            self.by_table.setdefault(model._meta.db_table.lower(), model)
            if not model._meta.auto_created:
                self.models.append(model)
        self.hops: list[Hop] = []
        for model in self.models:
            self.hops.extend(_hops_of(model))

    def model(self, table: str | None) -> type[models.Model] | None:
        return self.by_table.get(table.lower()) if table else None

    def into(self, target: type[models.Model]) -> list[Hop]:
        return [h for h in self.hops if h.target is target]

    def named(self, accessor: str, target: type[models.Model]) -> list[Hop]:
        return [h for h in self.hops if h.accessor == accessor and h.target is target]


def _hops_of(model: type[models.Model]) -> Iterator[Hop]:
    for f in model._meta.get_fields():
        if not f.is_relation or f.related_model is None or isinstance(f.related_model, str):
            continue
        if f.concrete or not f.auto_created:
            name = f.name
        else:
            name = f.get_accessor_name()
        if not name or name.endswith("+"):
            continue
        multi = bool(f.one_to_many or f.many_to_many)
        yield Hop(model=model, accessor=name, target=f.related_model, multi=multi)


def _where_columns(sql: str) -> list[tuple[str, str]]:
    """``[(table, column)]`` compared with ``= ?`` in the WHERE clause."""
    fp = sqlmod.fingerprint(sql)
    match = _WHERE.search(fp)
    if not match:
        return []
    aliases = sqlmod.table_aliases(sql)
    primary = sqlmod.primary_table(sql)
    out: list[tuple[str, str]] = []
    for m in _EQ.finditer(match.group("body")):
        parts = [_unquote(p) for p in re.findall(_IDENT, m.group("col"))]
        if not parts or parts[-1].upper() in sqlmod.KEYWORDS:
            continue
        if len(parts) >= 2:
            table = aliases.get(parts[-2], parts[-2])
        elif primary:
            table = primary
        else:
            continue
        out.append((table, parts[-1]))
    return out


def relations_for(sql: str, registry: _Registry) -> list[Relation]:
    """Candidate relations whose lazy loading issues ``sql`` (best first)."""
    primary = sqlmod.primary_table(sql)
    target = registry.model(primary)
    if target is None:
        return []
    where = _where_columns(sql)
    if not where:
        return []
    cols_on_primary = {c for t, c in where if t.lower() == primary.lower()}
    compared = {
        c
        for t, cs in sqlmod.filter_columns(sql).items()
        if t.lower() == primary.lower()
        for c in cs
    }
    out: list[Relation] = []

    # many-to-many: the filter is on the through table, not the selected one
    for table, col in where:
        if table.lower() == primary.lower():
            continue
        through = registry.model(table)
        if through is None:
            continue
        for model in registry.models:
            for f in model._meta.local_many_to_many:
                if f.remote_field.through is not through:
                    continue
                filtered = len(where) > 1
                if col == f.m2m_column_name():
                    out.append(Relation(model, f.name, target, "m2m", filtered))
                elif col == f.m2m_reverse_name():
                    accessor = f.remote_field.get_accessor_name()
                    if accessor and not accessor.endswith("+"):
                        out.append(
                            Relation(f.related_model, accessor, model, "reverse_m2m", filtered)
                        )
    if out:
        return out

    opts = target._meta
    # generic relation: content_type_id = ? AND object_id = ?
    for gfk in _generic_fks(target):
        try:
            ct_col = opts.get_field(gfk.ct_field).column
            fk_col = opts.get_field(gfk.fk_field).column
        except Exception:
            continue
        if ct_col in cols_on_primary and fk_col in cols_on_primary:
            for model in registry.models:
                for f in model._meta.private_fields:
                    if _is_generic_relation(f) and f.related_model is target:
                        out.append(Relation(model, f.name, target, "generic_relation"))
            if out:
                return out
            return [Relation(target, gfk.name, None, "generic_relation")]

    pk_col = opts.pk.column
    for f in opts.concrete_fields:
        if not f.is_relation or f.column not in cols_on_primary:
            continue
        if f.column == pk_col and not f.one_to_one:
            continue
        accessor = f.remote_field.get_accessor_name()
        if not accessor or accessor.endswith("+"):
            continue
        kind = "reverse_o2o" if f.one_to_one else "reverse_fk"
        filtered = len(compared | cols_on_primary) > 1
        out.append(Relation(f.related_model, accessor, target, kind, filtered))
    if out and not (pk_col in cols_on_primary and not opts.pk.is_relation):
        return out

    # forward FK / one-to-one: a pk (or to_field) lookup on the target
    looked_up = cols_on_primary
    for model in registry.models:
        for f in model._meta.concrete_fields:
            if not (f.many_to_one or f.one_to_one) or f.related_model is not target:
                continue
            if f.target_field.column in looked_up and len(looked_up) == 1:
                kind = "o2o" if f.one_to_one else "fk"
                out.append(Relation(model, f.name, target, kind))
        for f in model._meta.private_fields:
            if _is_generic_fk(f) and pk_col in looked_up:
                out.append(Relation(model, f.name, target, "gfk"))
    return out


def _generic_fks(model: type[models.Model]) -> list[Any]:
    return [f for f in model._meta.private_fields if _is_generic_fk(f)]


def _is_generic_fk(f: Any) -> bool:
    return type(f).__name__ == "GenericForeignKey"


def _is_generic_relation(f: Any) -> bool:
    return type(f).__name__ == "GenericRelation"


# ── source ──────────────────────────────────────────────────────────────────────────


@dataclass
class _Source:
    path: str
    display: str
    text: str
    tree: ast.Module
    line_starts: list[int]
    parents: dict[ast.AST, ast.AST]

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines(keepends=True)

    def offset(self, lineno: int, col: int) -> int:
        """Character offset of an ast (lineno, utf-8 byte col) position."""
        start = self.line_starts[lineno - 1]
        end = self.line_starts[lineno] if lineno < len(self.line_starts) else len(self.text)
        raw = self.text[start:end].encode("utf-8")[:col]
        return start + len(raw.decode("utf-8", errors="ignore"))

    def segment(self, node: ast.AST) -> str:
        return self.text[self.start(node) : self.end(node)]

    def start(self, node: ast.AST) -> int:
        return self.offset(node.lineno, node.col_offset)  # type: ignore[attr-defined]

    def end(self, node: ast.AST) -> int:
        return self.offset(node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]

    def scopes(self, line: int) -> list[ast.AST]:
        """Enclosing function/class nodes of ``line``, innermost first."""
        found = [
            n
            for n in ast.walk(self.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.lineno <= line <= (n.end_lineno or n.lineno)
        ]
        found.sort(key=lambda n: n.lineno, reverse=True)
        return found


class _SourceCache:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, _Source | None]] = {}
        self._lock = threading.Lock()

    def get(self, path: str, display: str) -> _Source | None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        with self._lock:
            hit = self._cache.get(path)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        src = self._load(path, display)
        with self._lock:
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[path] = (mtime, src)
        return src

    @staticmethod
    def _load(path: str, display: str) -> _Source | None:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            tree = ast.parse(text)
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
            return None
        starts = [0]
        for line in text.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        return _Source(path, display, text, tree, starts[:-1] or [0], parents)


# ── queryset expressions ────────────────────────────────────────────────────────────


def _chain(node: ast.AST) -> tuple[ast.AST, list[ast.AST]]:
    """``(root, [segments from the root outward])`` of an attribute/call/subscript chain."""
    segs: list[ast.AST] = []
    while True:
        if isinstance(node, ast.Call):
            segs.append(node)
            node = node.func
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            segs.append(node)
            node = node.value
        elif isinstance(node, ast.Await):
            node = node.value
        else:
            break
    segs.reverse()
    return node, segs


def _names_model(node: ast.AST, model: type[models.Model]) -> bool:
    name = model.__name__
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def _managers(model: type[models.Model]) -> set[str]:
    names = {"objects", "_default_manager", "_base_manager"}
    names.update(m.name for m in model._meta.managers if m.name)
    return names


def _manager_index(expr: ast.AST, model: type[models.Model]) -> int | None:
    """Index of the ``Model.<manager>`` segment in ``expr``'s chain, if it has one."""
    _, segs = _chain(expr)
    managers = _managers(model)
    for i, seg in enumerate(segs):
        if (
            isinstance(seg, ast.Attribute)
            and seg.attr in managers
            and _names_model(seg.value, model)
        ):
            return i
    return None


def _method_name(seg: ast.AST) -> str | None:
    if isinstance(seg, ast.Call) and isinstance(seg.func, ast.Attribute):
        return seg.func.attr
    return None


@dataclass
class _Target:
    """A located queryset expression of ``model`` in ``src`` (inside statement ``stmt``)."""

    src: _Source
    expr: ast.AST
    stmt: ast.AST
    model: type[models.Model]
    class_attr_after: ast.AST | None = None  # ``model = X`` line to add a queryset after


class _Locator:
    """Finds queryset expressions of a model in one source scope."""

    def __init__(self, src: _Source) -> None:
        self.src = src

    # assignments -------------------------------------------------------------
    def _assignments(self, scope: ast.AST, name: str, before: int) -> list[tuple[ast.AST, ast.AST]]:
        found = []
        for node in ast.walk(scope):
            value = None
            if isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    value = node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    value = node.value
            if value is not None and node.lineno <= before:
                found.append((node, value))
        found.sort(key=lambda p: p[0].lineno)
        return found

    def resolve_qs(
        self, expr: ast.AST, model: type[models.Model], scope: ast.AST, line: int, depth: int = 0
    ) -> tuple[ast.AST, ast.AST] | None:
        """``(defining expression, its statement)`` if ``expr`` is a queryset of ``model``."""
        if depth > 6:
            return None
        if _manager_index(expr, model) is not None:
            stmt = self._statement(expr)
            return expr, stmt
        root, segs = _chain(expr)
        if isinstance(root, ast.Name) and all(
            _method_name(s) not in _NO_PREFETCH for s in segs if isinstance(s, ast.Call)
        ):
            for node, value in reversed(self._assignments(scope, root.id, line)):
                hit = self.resolve_qs(value, model, scope, node.lineno - 1, depth + 1)
                if hit is not None:
                    return hit
                break
        return None

    def _statement(self, node: ast.AST) -> ast.AST:
        cur = node
        while cur in self.src.parents and not isinstance(cur, ast.stmt):
            cur = self.src.parents[cur]
        return cur

    # loops -------------------------------------------------------------------
    def loop_over(self, scope: ast.AST, var: str, line: int) -> ast.AST | None:
        """The iterable of the innermost loop/comprehension binding ``var`` around ``line``."""
        best: tuple[int, ast.AST] | None = None
        for node in ast.walk(scope):
            if isinstance(node, (ast.For, ast.AsyncFor)):
                if node.lineno <= line <= (node.end_lineno or node.lineno) and _binds(
                    node.target, var
                ):
                    if best is None or node.lineno >= best[0]:
                        best = (node.lineno, node.iter)
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                if node.lineno <= line <= (node.end_lineno or node.lineno):
                    for gen in node.generators:
                        if _binds(gen.target, var) and (best is None or node.lineno >= best[0]):
                            best = (node.lineno, gen.iter)
        if best is None:
            # ``for x in xs`` where the line is the loop header itself
            return None
        return best[1]

    def enclosing_loops(self, scope: ast.AST, line: int) -> list[ast.AST]:
        loops = [
            n
            for n in ast.walk(scope)
            if isinstance(n, (ast.For, ast.AsyncFor))
            and n.lineno <= line <= (n.end_lineno or n.lineno)
        ]
        loops.sort(key=lambda n: n.lineno, reverse=True)
        return [n.iter for n in loops]

    # whole-scope search --------------------------------------------------------
    def any_qs(self, scope: ast.AST, model: type[models.Model], line: int) -> _Target | None:
        """A queryset of ``model`` in ``scope``: loop iterables around ``line`` first, then
        the closest assignment/return before it, then class-level ``queryset``/``model``."""
        for it in self.enclosing_loops(scope, line):
            hit = self.resolve_qs(it, model, scope, line)
            if hit:
                return _Target(self.src, hit[0], hit[1], model)
        candidates: list[tuple[int, ast.AST]] = []
        for node in ast.walk(scope):
            value = None
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return)):
                value = node.value
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                value = node.iter
            if value is None:
                continue
            if isinstance(scope, ast.ClassDef) or node.lineno <= line:
                if _manager_index(value, model) is not None:
                    candidates.append((node.lineno, value))
        if candidates:
            candidates.sort(key=lambda p: (p[0] <= line, p[0]))
            value = candidates[-1][1]
            return _Target(self.src, value, self._statement(value), model)
        if isinstance(scope, ast.ClassDef):
            for node in scope.body:
                if (
                    isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "model" for t in node.targets)
                    and _names_model(node.value, model)
                ):
                    return _Target(self.src, node.value, node, model, class_attr_after=node)
        return None


def _binds(target: ast.AST, var: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == var
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_binds(t, var) for t in target.elts)
    return False


# ── edits ───────────────────────────────────────────────────────────────────────────


@dataclass
class _Edit:
    start: int
    end: int
    text: str
    already: bool = False  # the lookup is already there


def _plan_edit(target: _Target, method: str, lookup: str) -> _Edit | None:
    src = target.src
    quote = _quote_style(src.segment(target.stmt))
    arg = f"{quote}{lookup}{quote}"
    if target.class_attr_after is not None:
        node = target.class_attr_after
        indent = " " * node.col_offset  # type: ignore[attr-defined]
        pos = src.offset(node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]
        model = target.model.__name__
        return _Edit(pos, pos, f"\n{indent}queryset = {model}.objects.{method}({arg})")
    expr = target.expr
    index = _manager_index(expr, target.model)
    if index is None:
        return None
    _, segs = _chain(expr)
    after = segs[index + 1 :]
    if any(_method_name(s) in _NO_PREFETCH for s in after):
        return None
    for seg in after:
        if _method_name(seg) == method:
            assert isinstance(seg, ast.Call)
            existing = [
                a.value
                for a in seg.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            if lookup in existing:
                return _Edit(0, 0, "", already=True)
            if seg.args:
                pos = src.end(seg.args[-1])
                return _Edit(pos, pos, f", {arg}")
            pos = src.end(seg) - 1
            return _Edit(pos, pos, arg)
    # insert before the first ordering / slicing / terminal segment
    insert_at: int | None = None
    for seg in after:
        if isinstance(seg, ast.Subscript) or (isinstance(seg, ast.Attribute) and seg.attr in _STOP):
            insert_at = src.end(seg.value)
            break
    call = f".{method}({arg})"
    if insert_at is None:
        last = after[-1] if after else segs[index]
        if (
            isinstance(last, ast.Call)
            and _method_name(last) == "all"
            and not last.args
            and not last.keywords
        ):
            start = src.end(last.func.value)  # type: ignore[attr-defined]
            return _Edit(start, src.end(last), call)
        insert_at = src.end(last)
    else:
        # replace a bare ``.all()`` right before the insertion point
        before = [s for s in after if src.end(s) <= insert_at]
        last = before[-1] if before else None
        if (
            isinstance(last, ast.Call)
            and _method_name(last) == "all"
            and not last.args
            and src.end(last) == insert_at
        ):
            return _Edit(src.end(last.func.value), insert_at, call)  # type: ignore[attr-defined]
    return _Edit(insert_at, insert_at, call)


def _quote_style(text: str) -> str:
    return "'" if text.count("'") > text.count('"') else '"'


def _render(target: _Target, edit: _Edit) -> tuple[str, str]:
    """``(unified diff, the edited statement)`` for ``edit`` applied to ``target``."""
    src = target.src
    new_text = src.text[: edit.start] + edit.text + src.text[edit.end :]
    diff = "".join(
        difflib.unified_diff(
            src.text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{src.display}",
            tofile=f"b/{src.display}",
            n=2,
        )
    )
    if target.class_attr_after is not None:
        return diff, edit.text.strip()
    first = target.stmt.lineno  # type: ignore[attr-defined]
    last = target.expr.end_lineno + edit.text.count("\n")  # type: ignore[attr-defined]
    lines = new_text.splitlines(keepends=True)[first - 1 : last]
    code = textwrap.dedent("".join(lines)).strip()
    if isinstance(target.stmt, (ast.For, ast.AsyncFor)):
        code = code.split("\n")[0] if code.count("\n") == 0 else code
    return diff, code


# ── the suggester ───────────────────────────────────────────────────────────────────


@dataclass
class _Plan:
    relation: Relation
    root: type[models.Model]
    path: list[str]
    multi: bool
    target: _Target | None = None
    exact: bool = False  # found by tracing the instance variable, not by search
    serializer: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def lookup(self) -> str:
        return "__".join(self.path)

    @property
    def method(self) -> str:
        return "prefetch_related" if self.multi else "select_related"


class DjangoFixSuggester:
    """Suggests ``select_related``/``prefetch_related`` fixes (see the module docstring)."""

    def __init__(self, project_root: str | None = None) -> None:
        self.project_root = os.path.abspath(project_root or os.getcwd())
        self._sources = _SourceCache()
        self._registry: _Registry | None = None

    @property
    def registry(self) -> _Registry:
        if self._registry is None:
            self._registry = _Registry()
        return self._registry

    # protocol ----------------------------------------------------------------
    def suggest(self, issue: Issue, spans: list[Span]) -> Suggestion | None:
        if issue.kind not in ("n_plus_one", "duplicate_query") or not spans:
            return None
        if not apps.ready:
            return None
        sql = str(spans[0].data.get("sql", ""))
        candidates = relations_for(sql, self.registry)
        if not candidates:
            return None
        frames = self._frames(issue.callsite)
        code_line = issue.callsite.code if issue.callsite else None
        relation, matched_var = self._pick(candidates, code_line)
        if relation is None:
            return None
        if code_line and re.search(
            rf"\.\s*{re.escape(relation.accessor)}\s*\.\s*(filter|exclude|order_by|annotate)\(",
            code_line,
        ):
            relation = dataclasses.replace(relation, filtered=True)
        if issue.kind == "duplicate_query" and (relation.multi or matched_var is None):
            return None  # only "the same parent fetched again" has a Django-specific fix
        if relation.kind == "gfk" or (
            relation.kind == "generic_relation" and relation.target is None
        ):
            return self._generic(relation, frames, matched_var)
        plan = self._plan(relation, frames, matched_var)
        return self._suggestion(plan, ambiguous=len(candidates) > 1 and not matched_var)

    # relation choice -----------------------------------------------------------
    @staticmethod
    def _pick(
        candidates: Sequence[Relation], code: str | None
    ) -> tuple[Relation | None, str | None]:
        """The candidate the source line mentions (``var.accessor``), else the first."""
        if code:
            for rel in candidates:
                m = re.search(rf"\b([A-Za-z_]\w*)\s*\.\s*{re.escape(rel.accessor)}\b", code)
                if m and m.group(1) != "self":
                    return rel, m.group(1)
                if m:
                    return rel, None
        return candidates[0], None

    # frames ------------------------------------------------------------------
    def _frames(self, callsite: Callsite | None) -> list[tuple[_Source, Frame]]:
        if callsite is None:
            return []
        stack = list(callsite.stack) or [
            Frame(file=callsite.file, line=callsite.line, function=callsite.function)
        ]
        out = []
        for frame in reversed(stack):  # innermost first
            path = os.path.join(self.project_root, frame.file.replace("/", os.sep))
            src = self._sources.get(path, frame.file)
            if src is not None:
                out.append((src, frame))
        return out

    # planning ------------------------------------------------------------------
    def _plan(
        self,
        relation: Relation,
        frames: list[tuple[_Source, Frame]],
        var: str | None,
    ) -> _Plan:
        plan = _Plan(relation, relation.parent, [relation.accessor], relation.multi)
        if frames:
            src, frame = frames[0]
            plan.serializer = _serializer_name(src, frame.line)
        if var and frames:
            traced = self._trace(relation, frames[0], var)
            if traced is not None:
                return traced
        for src, frame in frames:
            hit = self._search(relation, src, frame)
            if hit is not None:
                hit.serializer = plan.serializer
                return hit
        return plan

    def _trace(self, relation: Relation, where: tuple[_Source, Frame], var: str) -> _Plan | None:
        """Follow ``var`` up through enclosing loops to the root queryset."""
        src, frame = where
        scopes = [s for s in src.scopes(frame.line) if not isinstance(s, ast.ClassDef)]
        if not scopes:
            return None
        scope, loc = scopes[0], _Locator(src)
        path, multi, model, line = [relation.accessor], relation.multi, relation.parent, frame.line
        for _ in range(_MAX_DEPTH + 2):
            it = loc.loop_over(scope, var, line)
            if it is None:
                for node, value in reversed(loc._assignments(scope, var, line)):
                    it = value
                    line = node.lineno
                    break
            if it is None:
                return None
            hit = loc.resolve_qs(it, model, scope, line)
            if hit is not None:
                plan = _Plan(relation, model, path, multi, _Target(src, hit[0], hit[1], model))
                plan.exact = True
                return plan
            root, segs = _chain(it)
            if not (isinstance(root, ast.Name) and segs and isinstance(segs[0], ast.Attribute)):
                return None
            accessor = segs[0].attr
            hops = self.registry.named(accessor, model)
            if len(hops) != 1:
                return None
            hop = hops[0]
            path.insert(0, accessor)
            multi = multi or hop.multi
            model, var = hop.model, root.id
            line = getattr(it, "lineno", line)
        return None

    def _search(self, relation: Relation, src: _Source, frame: Frame) -> _Plan | None:
        """Any queryset of the parent – or of a model up to two hops above it – in scope."""
        loc = _Locator(src)
        roots = self._roots(relation)
        for scope in src.scopes(frame.line):
            for model, path, multi in roots:
                target = loc.any_qs(scope, model, frame.line)
                if target is not None:
                    return _Plan(relation, model, path, multi, target)
        return None

    def _roots(self, relation: Relation) -> list[tuple[type[models.Model], list[str], bool]]:
        """``(root model, lookup path, multi)`` from the parent outward, nearest first."""
        roots = [(relation.parent, [relation.accessor], relation.multi)]
        frontier = list(roots)
        for _ in range(_MAX_DEPTH):
            nxt = []
            for model, path, multi in frontier:
                for hop in self.registry.into(model):
                    if hop.model is model or len(path) > _MAX_DEPTH:
                        continue
                    nxt.append((hop.model, [hop.accessor, *path], multi or hop.multi))
            roots.extend(nxt)
            frontier = nxt
        return roots

    # rendering -----------------------------------------------------------------
    def _suggestion(self, plan: _Plan, *, ambiguous: bool) -> Suggestion:
        rel = plan.relation
        method, lookup = plan.method, plan.lookup
        root = plan.root.__name__
        docs = DOCS_PREFETCH if plan.multi else DOCS_SELECT
        where = ""
        if plan.target is not None:
            line = plan.target.stmt.lineno  # type: ignore[attr-defined]
            where = f" ({plan.target.src.display}:{line})"
        if rel.filtered:
            child = rel.target.__name__ if rel.target else "Related"
            code = (
                f'{root}.objects.prefetch_related(Prefetch("{lookup}", '
                f'queryset={child}.objects.filter(...), to_attr="{rel.accessor}_list"))'
            )
            return Suggestion(
                summary=(
                    f"Each {rel.parent.__name__} runs a filtered query on .{rel.accessor}; "
                    f"prefetch it once with a Prefetch() object on the {root} queryset{where} "
                    f"and read the to_attr list in the loop."
                ),
                code=code,
                diff=None,
                confidence="medium",
                docs_url=DOCS_PREFETCH_OBJ,
            )
        edit = _plan_edit(plan.target, method, lookup) if plan.target is not None else None
        if edit is not None and edit.already:
            return Suggestion(
                summary=(
                    f"The {root} queryset{where} already has .{method}({lookup!r}), but the "
                    f"loop still queries .{rel.accessor} for every {rel.parent.__name__}. "
                    "Calling .filter()/.order_by()/.exclude() on a prefetched relation, or "
                    "iterating a different queryset, bypasses the prefetch cache – filter in "
                    "Python or use Prefetch(..., to_attr=...)."
                ),
                code=None,
                diff=None,
                confidence="medium",
                docs_url=DOCS_PREFETCH_OBJ if plan.multi else docs,
            )
        call = f'.{method}("{lookup}")'
        serializer = (
            f" {plan.serializer} reads .{rel.accessor} for every object it serializes, so "
            "optimise the view's get_queryset()."
            if plan.serializer
            else ""
        )
        why = (
            f"one extra query for all {rel.parent.__name__} rows instead of one per row"
            if plan.multi
            else "a JOIN in the same query instead of one query per row"
        )
        if edit is not None:
            diff, code = _render(plan.target, edit)  # type: ignore[arg-type]
            confidence = "medium" if ambiguous or len(plan.path) > 2 else "high"
            if not plan.exact and plan.target and plan.target.class_attr_after is not None:
                confidence = "medium"
            return Suggestion(
                summary=f"Add {call} to the {root} queryset{where}: {why}.{serializer}",
                code=code,
                diff=diff,
                confidence=confidence,
                docs_url=docs,
            )
        hint = f"{root}.objects{call}"
        place = (
            "the view's get_queryset()"
            if plan.serializer
            else "the queryset the loop iterates (it was not found near the callsite)"
        )
        return Suggestion(
            summary=f"Add {call} to {place}: {why}.{serializer}",
            code=hint,
            diff=None,
            confidence="low" if ambiguous else "medium",
            docs_url=docs,
        )

    def _generic(
        self, relation: Relation, frames: list[tuple[_Source, Frame]], var: str | None
    ) -> Suggestion:
        parent = relation.parent.__name__
        if relation.target is None:
            return Suggestion(
                summary=(
                    f"{parent} rows are fetched one object at a time by content type and "
                    f"object id. Add a GenericRelation({parent}) to the models they belong "
                    "to and prefetch_related() it on the queryset the loop iterates."
                ),
                code=None,
                diff=None,
                confidence="low",
                docs_url=DOCS_GENERIC,
            )
        plan = self._plan(relation, frames, var)
        plan.multi = True
        call = f'.prefetch_related("{plan.lookup}")'
        if relation.kind == "gfk":
            summary = (
                f"{parent}.{relation.accessor} is a GenericForeignKey, loaded one object at a "
                f"time. {call} on the {plan.root.__name__} queryset batches it into one query "
                "per content type (GenericPrefetch to customise each)."
            )
        else:  # pragma: no cover - generic relations with a target take the normal path
            summary = f"Prefetch the generic relation with {call}."
        diff = None
        code = f"{plan.root.__name__}.objects{call}"
        if plan.target is not None:
            edit = _plan_edit(plan.target, "prefetch_related", plan.lookup)
            if edit is not None and not edit.already:
                diff, code = _render(plan.target, edit)
        return Suggestion(
            summary=summary, code=code, diff=diff, confidence="low", docs_url=DOCS_GENERIC
        )


def _serializer_name(src: _Source, line: int) -> str | None:
    for scope in src.scopes(line):
        if isinstance(scope, ast.ClassDef):
            for base in scope.bases:
                name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                if name.endswith("Serializer"):
                    return scope.name
            return None
    return None
