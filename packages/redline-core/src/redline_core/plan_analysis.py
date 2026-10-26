"""Turn raw EXPLAIN output into a :class:`PlanNode` tree, findings and index suggestions.

Three input formats are understood:

* **PostgreSQL** ``EXPLAIN (FORMAT JSON)`` – ``[{"Plan": {...}}]``;
* **MySQL** ``EXPLAIN FORMAT=JSON`` – ``{"query_block": {...}}``;
* **SQLite** ``EXPLAIN QUERY PLAN`` rows – ``(id, parent, notused, detail)``.

Findings raised (each a :class:`Finding` and a flag on the offending node):

==========================  ===========================================================
flag                        meaning
==========================  ===========================================================
``seq_scan``                PostgreSQL sequential scan of a large table (≥ 1 000 rows
                            estimated, or cost ≥ 1 000) – *warning* when it filters rows
                            (an index would help), *info* otherwise
``full_scan``               MySQL ``access_type: ALL`` on ≥ 1 000 rows, or SQLite
                            ``SCAN <table>`` whose table is filtered/joined in the query
``sort_spill``              a sort that went to disk (``external merge`` / ``Disk``)
``hash_spill``              a hash join that needed more than one batch
``nested_loop_high_rows``   a nested loop driving ≥ 10 000 rows, or whose inner side
                            ran ≥ 1 000 times
``temp_sort``               SQLite ``USE TEMP B-TREE`` / MySQL ``using_filesort``
``automatic_index``         SQLite built a throw-away index for this query
==========================  ===========================================================

Index suggestions (``CREATE INDEX …``) are derived from the plan's filter/join conditions
when the plan exposes them (PostgreSQL ``Filter``/``Hash Cond``, MySQL
``attached_condition``) and otherwise from the SQL's ``WHERE``/``ON`` columns. They are
suggestions: nothing here ever runs DDL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import sql as sqlmod

__all__ = [
    "LARGE_TABLE_ROWS",
    "NESTED_LOOP_LOOPS",
    "NESTED_LOOP_ROWS",
    "SEQ_SCAN_COST",
    "Analysis",
    "Finding",
    "PlanNode",
    "analyze_mysql",
    "analyze_plan",
    "analyze_postgres",
    "analyze_sqlite",
    "create_index_sql",
    "render_text",
]

LARGE_TABLE_ROWS = 1_000
SEQ_SCAN_COST = 1_000.0
NESTED_LOOP_ROWS = 10_000
NESTED_LOOP_LOOPS = 1_000


@dataclass
class PlanNode:
    label: str
    detail: str = ""
    cost: float | None = None
    rows: float | None = None
    actual_ms: float | None = None
    children: list[PlanNode] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "detail": self.detail,
            "cost": self.cost,
            "rows": self.rows,
            "actual_ms": self.actual_ms,
            "children": [c.to_dict() for c in self.children],
            "flags": list(self.flags),
        }

    def walk(self) -> Iterable[PlanNode]:
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class Finding:
    severity: str
    title: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "title": self.title, "detail": self.detail}


@dataclass
class Analysis:
    tree: PlanNode
    findings: list[Finding]
    suggested_indexes: list[str]


class _Collector:
    def __init__(self, vendor: str, sql: str) -> None:
        self.vendor = vendor
        self.sql = sql
        self.findings: list[Finding] = []
        self.indexes: list[str] = []
        self._sql_cols: dict[str, list[str]] | None = None

    def finding(self, node: PlanNode, flag: str, severity: str, title: str, detail: str) -> None:
        if flag not in node.flags:
            node.flags.append(flag)
        self.findings.append(Finding(severity, title, detail))

    def sql_columns(self, table: str) -> list[str]:
        if self._sql_cols is None:
            try:
                self._sql_cols = sqlmod.filter_columns(self.sql) if self.sql else {}
            except Exception:  # pragma: no cover - regex safety net
                self._sql_cols = {}
        return list(self._sql_cols.get(table, []))

    def index(self, table: str, columns: Sequence[str]) -> str | None:
        cols = [c for c in dict.fromkeys(columns) if c]
        if not table or not cols:
            return None
        stmt = create_index_sql(table, cols[:3], self.vendor)
        if stmt not in self.indexes:
            self.indexes.append(stmt)
        return stmt

    def result(self, tree: PlanNode) -> Analysis:
        return Analysis(tree, self.findings, self.indexes)


_SIMPLE_IDENT = re.compile(r"[a-z_][a-z0-9_]*")


def _quote(name: str, vendor: str) -> str:
    if _SIMPLE_IDENT.fullmatch(name):
        return name
    if vendor == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def create_index_sql(table: str, columns: Sequence[str], vendor: str = "postgresql") -> str:
    """``CREATE INDEX <table>_<cols>_idx ON <table> (<cols>);`` with safe quoting."""
    name = re.sub(r"\W+", "_", f"{table}_{'_'.join(columns)}_idx").strip("_")[:63]
    cols = ", ".join(_quote(c, vendor) for c in columns)
    return f"CREATE INDEX {_quote(name, vendor)} ON {_quote(table, vendor)} ({cols});"


def render_text(node: PlanNode, indent: int = 0) -> str:
    """An indented, human-readable rendering of the plan tree."""
    bits = [node.label]
    if node.cost is not None:
        bits.append(f"cost={node.cost:g}")
    if node.rows is not None:
        bits.append(f"rows={node.rows:g}")
    if node.actual_ms is not None:
        bits.append(f"actual={node.actual_ms:.3f}ms")
    line = "  " * indent + ("-> " if indent else "") + "  ".join(bits)
    if node.detail:
        line += f"\n{'  ' * indent}   {node.detail}"
    if node.flags:
        line += f"  [{', '.join(node.flags)}]"
    return "\n".join([line, *(render_text(c, indent + 1) for c in node.children)])


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── PostgreSQL ───────────────────────────────────────────────────────────────────────

_PG_COND_COL = re.compile(
    r"(?<![:\w.'])(?:(\w+)\.)?\(?(\w+)\)?(?:::\w+(?:\s\w+)*)?\)?\s*(?:=|<>|<=|>=|<|>|~~\*?|!~~)"
)
_PG_QUALIFIED = re.compile(r"(?<![:\w])(\w+)\.(\w+)")
_PG_DETAIL_KEYS = (
    "Index Name",
    "Index Cond",
    "Recheck Cond",
    "Filter",
    "Hash Cond",
    "Merge Cond",
    "Join Filter",
    "Sort Key",
    "Group Key",
    "Sort Method",
)


def _pg_label(p: dict[str, Any]) -> str:
    label = str(p.get("Node Type", "?"))
    if p.get("Join Type") and p["Join Type"] != "Inner":
        label += f" ({p['Join Type']})"
    rel = p.get("Relation Name")
    if rel:
        label += f" on {rel}"
        alias = p.get("Alias")
        if alias and alias != rel:
            label += f" {alias}"
    elif p.get("CTE Name"):
        label += f" on {p['CTE Name']}"
    return label


def _pg_detail(p: dict[str, Any]) -> str:
    parts = []
    for key in _PG_DETAIL_KEYS:
        value = p.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            value = ", ".join(map(str, value))
        parts.append(f"{key}: {value}")
    if p.get("Rows Removed by Filter"):
        parts.append(f"Rows Removed by Filter: {p['Rows Removed by Filter']}")
    return "; ".join(parts)


def _pg_columns(cond: str) -> list[str]:
    return [m.group(2) for m in _PG_COND_COL.finditer(cond or "") if not m.group(2).isdigit()]


def analyze_postgres(plan: Any, sql: str = "") -> Analysis:
    """Analyse PostgreSQL ``EXPLAIN (FORMAT JSON)`` output (str, list or dict)."""
    if isinstance(plan, (bytes, str)):
        plan = json.loads(plan)
    if isinstance(plan, list):
        plan = plan[0] if plan else {}
    if isinstance(plan, list):  # some drivers wrap twice: [[{...}]]
        plan = plan[0] if plan else {}
    root = plan.get("Plan", plan) if isinstance(plan, dict) else {}
    col = _Collector("postgresql", sql)
    aliases: dict[str, str] = {}
    scanned: dict[str, tuple[PlanNode, str]] = {}  # alias -> (node, relation) for seq scans

    def build(p: dict[str, Any]) -> PlanNode:
        loops = _num(p.get("Actual Loops")) or 1.0
        actual = _num(p.get("Actual Total Time"))
        node = PlanNode(
            label=_pg_label(p),
            detail=_pg_detail(p),
            cost=_num(p.get("Total Cost")),
            rows=_num(p.get("Actual Rows") if "Actual Rows" in p else p.get("Plan Rows")),
            actual_ms=round(actual * loops, 3) if actual is not None else None,
        )
        rel = p.get("Relation Name")
        if rel:
            aliases[str(p.get("Alias") or rel)] = rel
        node.children = [build(c) for c in p.get("Plans") or ()]
        check(p, node)
        return node

    def check(p: dict[str, Any], node: PlanNode) -> None:
        ntype = p.get("Node Type", "")
        rel = p.get("Relation Name")
        if ntype == "Seq Scan" and rel:
            plan_rows = _num(p.get("Plan Rows")) or 0.0
            actual_rows = (_num(p.get("Actual Rows")) or 0.0) * (_num(p.get("Actual Loops")) or 1.0)
            removed = _num(p.get("Rows Removed by Filter")) or 0.0
            examined = max(plan_rows, actual_rows + removed)
            cost = _num(p.get("Total Cost")) or 0.0
            scanned[str(p.get("Alias") or rel)] = (node, rel)
            if examined >= LARGE_TABLE_ROWS or cost >= SEQ_SCAN_COST:
                cols = _pg_columns(p.get("Filter", "")) or col.sql_columns(rel)
                if p.get("Filter") or cols:
                    stmt = col.index(rel, cols)
                    col.finding(
                        node,
                        "seq_scan",
                        "warning",
                        f"Sequential scan on {rel}",
                        f"PostgreSQL reads the whole of {rel} (~{examined:,.0f} rows) and "
                        "discards non-matching rows."
                        + (f" An index would let it seek directly: {stmt}" if stmt else ""),
                    )
                else:
                    col.finding(
                        node,
                        "seq_scan",
                        "info",
                        f"Sequential scan on {rel}",
                        f"The query reads all of {rel} (~{examined:,.0f} rows) without a "
                        "filter; fine for small tables, consider LIMIT or pagination.",
                    )
        if p.get("Sort Space Type") == "Disk" or "external" in str(p.get("Sort Method", "")):
            col.finding(
                node,
                "sort_spill",
                "warning",
                "Sort spilled to disk",
                f"The sort ({p.get('Sort Method', 'external')}) did not fit in work_mem. An "
                "index matching the ORDER BY, a LIMIT, or a larger work_mem avoids it.",
            )
        if ntype == "Hash" and (_num(p.get("Hash Batches")) or 1) > 1:
            col.finding(
                node,
                "hash_spill",
                "info",
                "Hash join used multiple batches",
                f"The hash table needed {p.get('Hash Batches')} batches (spilled to disk).",
            )
        if ntype == "Nested Loop":
            children = p.get("Plans") or []
            outer_rows = _num(children[0].get("Plan Rows")) if children else None
            inner_loops = _num(children[1].get("Actual Loops")) if len(children) > 1 else None
            rows = max(outer_rows or 0.0, _num(p.get("Plan Rows")) or 0.0)
            if rows >= NESTED_LOOP_ROWS or (inner_loops or 0) >= NESTED_LOOP_LOOPS:
                col.finding(
                    node,
                    "nested_loop_high_rows",
                    "warning",
                    "Nested loop over many rows",
                    f"A nested loop drives ~{max(rows, inner_loops or 0):,.0f} iterations of its "
                    "inner side; an index on the join column (or a hash join) is usually "
                    "much cheaper.",
                )

    tree = build(root) if root else PlanNode(label="(empty plan)")
    # Join conditions: suggest an index on the join column of a seq-scanned relation.
    for p in _pg_walk(root):
        for key in ("Hash Cond", "Merge Cond", "Join Filter"):
            for alias, column in _PG_QUALIFIED.findall(str(p.get(key) or "")):
                if alias in scanned:
                    node, rel = scanned[alias]
                    if node.flags:  # only where the scan was already deemed costly
                        col.index(rel, [column])
    if isinstance(plan, dict):
        for key in ("Planning Time", "Execution Time"):
            if key in plan:
                tree.detail = "; ".join(filter(None, [tree.detail, f"{key}: {plan[key]} ms"]))
    return col.result(tree)


def _pg_walk(p: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if not p:
        return
    yield p
    for c in p.get("Plans") or ():
        yield from _pg_walk(c)


# ── MySQL ────────────────────────────────────────────────────────────────────────────

_MYSQL_ACCESS = {
    "ALL": "Full table scan",
    "index": "Full index scan",
    "range": "Index range scan",
    "ref": "Index lookup",
    "eq_ref": "Unique index lookup",
    "const": "Constant lookup",
    "system": "Constant lookup",
    "ref_or_null": "Index lookup",
    "fulltext": "Full-text lookup",
    "index_merge": "Index merge",
}
_MYSQL_COL = re.compile(
    r"`(?:\w+)`\.`(?P<table>\w+)`\.`(?P<col>\w+)`\s*(?:=|<>|<=|>=|<|>|\blike\b|\bin\b)",
    re.IGNORECASE,
)
_MYSQL_COL_RHS = re.compile(
    r"(?:=|<>|<=|>=|<|>)\s*`(?:\w+)`\.`(?P<table>\w+)`\.`(?P<col>\w+)`", re.IGNORECASE
)


def analyze_mysql(plan: Any, sql: str = "") -> Analysis:
    """Analyse MySQL ``EXPLAIN FORMAT=JSON`` output (str or dict)."""
    if isinstance(plan, (bytes, str)):
        plan = json.loads(plan)
    if isinstance(plan, list):
        plan = plan[0] if plan else {}
    col = _Collector("mysql", sql)
    block = plan.get("query_block", plan) if isinstance(plan, dict) else {}

    def table_node(t: dict[str, Any], *, joined: bool) -> PlanNode:
        name = str(t.get("table_name", "?"))
        access = str(t.get("access_type", ""))
        cost_info = t.get("cost_info") or {}
        rows = _num(t.get("rows_examined_per_scan"))
        detail = []
        for key in ("key", "possible_keys", "ref", "attached_condition"):
            if t.get(key):
                v = t[key]
                detail.append(f"{key}: {', '.join(v) if isinstance(v, list) else v}")
        node = PlanNode(
            label=f"{_MYSQL_ACCESS.get(access, access or 'Table')} on {name}",
            detail="; ".join(detail),
            cost=_num(cost_info.get("prefix_cost") or cost_info.get("read_cost")),
            rows=rows,
        )
        real_table = name
        cond = str(t.get("attached_condition") or "")
        if access == "ALL" and (rows or 0) >= LARGE_TABLE_ROWS:
            cols = [m.group("col") for m in _MYSQL_COL.finditer(cond) if m.group("table") == name]
            cols += [
                m.group("col") for m in _MYSQL_COL_RHS.finditer(cond) if m.group("table") == name
            ]
            cols = cols or col.sql_columns(real_table)
            stmt = col.index(real_table, cols) if cols else None
            what = "joins" if joined else "reads"
            col.finding(
                node,
                "full_scan",
                "warning" if cols or joined else "info",
                f"Full table scan on {name}",
                f"MySQL {what} every row of {name} (~{rows:,.0f})"
                + (" and no index is usable" if not t.get("possible_keys") else "")
                + "."
                + (f" Suggested: {stmt}" if stmt else ""),
            )
        if t.get("using_filesort"):
            col.finding(
                node,
                "temp_sort",
                "info",
                f"Filesort on {name}",
                "Rows are sorted without an index.",
            )
        if t.get("using_temporary_table"):
            col.finding(
                node,
                "temp_sort",
                "info",
                f"Temporary table for {name}",
                "MySQL materialises an internal temporary table.",
            )
        children = []
        for key in ("materialized_from_subquery", "attached_subqueries"):
            sub = t.get(key)
            for s in sub if isinstance(sub, list) else [sub] if sub else []:
                children.append(walk(s.get("query_block", s), joined=False))
        node.children = children
        return node

    def walk(obj: dict[str, Any], *, joined: bool) -> PlanNode:
        if "table" in obj and len([k for k in obj if k in _CONTAINERS]) == 0:
            node = table_node(obj["table"], joined=joined)
            return _wrap_block(obj, node)
        for key, label in _CONTAINERS.items():
            if key not in obj:
                continue
            inner = obj[key]
            if key == "nested_loop":
                node = PlanNode(label="Nested loop")
                node.children = [walk(item, joined=i > 0) for i, item in enumerate(inner)]
            else:
                node = PlanNode(label=label)
                if inner.get("using_filesort"):
                    col.finding(
                        node,
                        "temp_sort",
                        "info",
                        "Filesort",
                        "The result is sorted without an index.",
                    )
                if inner.get("using_temporary_table"):
                    col.finding(
                        node,
                        "temp_sort",
                        "info",
                        "Temporary table",
                        "MySQL materialises an internal temporary table.",
                    )
                node.children = [walk(inner, joined=joined)]
            return _wrap_block(obj, node)
        if "table" in obj:
            return _wrap_block(obj, table_node(obj["table"], joined=joined))
        return PlanNode(label=str(obj.get("message", "Query block")))

    def _wrap_block(obj: dict[str, Any], node: PlanNode) -> PlanNode:
        if "select_id" in obj:
            cost = _num((obj.get("cost_info") or {}).get("query_cost"))
            return PlanNode(label=f"Query block #{obj['select_id']}", cost=cost, children=[node])
        return node

    tree = walk(block, joined=False) if block else PlanNode(label="(empty plan)")
    return col.result(tree)


_CONTAINERS = {
    "nested_loop": "Nested loop",
    "ordering_operation": "Sort",
    "grouping_operation": "Group",
    "duplicates_removal": "Distinct",
    "windowing": "Window",
    "union_result": "Union",
}


# ── SQLite ───────────────────────────────────────────────────────────────────────────

_SQLITE_SCAN = re.compile(
    r"^SCAN (?:TABLE )?(?P<table>[\w$]+)(?: AS (?P<alias>[\w$]+))?(?P<rest>.*)$"
)
_SQLITE_SEARCH = re.compile(
    r"^SEARCH (?:TABLE )?(?P<table>[\w$]+)(?: AS (?P<alias>[\w$]+))?(?P<rest>.*)$"
)
_SQLITE_NOT_TABLES = {"CONSTANT", "SUBQUERY"}


def analyze_sqlite(rows: Sequence[Any], sql: str = "") -> Analysis:
    """Analyse ``EXPLAIN QUERY PLAN`` rows ``(id, parent, notused, detail)``."""
    col = _Collector("sqlite", sql)
    aliases = sqlmod.table_aliases(sql) if sql else {}
    root = PlanNode(label="QUERY PLAN")
    nodes: dict[int, PlanNode] = {0: root}
    for row in rows:
        if isinstance(row, dict):
            node_id, parent, detail = row["id"], row["parent"], row["detail"]
        else:
            node_id, parent, detail = row[0], row[1], row[-1]
        detail = str(detail)
        node = PlanNode(label=detail)
        nodes.setdefault(int(parent), root).children.append(node)
        nodes[int(node_id)] = node
        _sqlite_check(node, detail, aliases, col)
    return col.result(root)


def _sqlite_check(node: PlanNode, detail: str, aliases: dict[str, str], col: _Collector) -> None:
    scan = _SQLITE_SCAN.match(detail)
    if scan and scan.group("table") not in _SQLITE_NOT_TABLES:
        name = scan.group("alias") or scan.group("table")
        table = aliases.get(name, scan.group("table"))
        if "INDEX" in scan.group("rest"):
            return  # a full scan of an index – usually a covering index, fine
        cols = col.sql_columns(table)
        if cols:
            stmt = col.index(table, cols)
            col.finding(
                node,
                "full_scan",
                "warning",
                f"Full table scan on {table}",
                f"SQLite reads every row of {table} to apply the filter/join on "
                f"{', '.join(cols)}. Suggested: {stmt}",
            )
        else:
            col.finding(
                node,
                "full_scan",
                "info",
                f"Full table scan on {table}",
                f"SQLite reads every row of {table}; fine for small tables or when the "
                "query genuinely needs all rows.",
            )
        return
    search = _SQLITE_SEARCH.match(detail)
    if search and "AUTOMATIC" in search.group("rest"):
        table = aliases.get(search.group("alias") or search.group("table"), search.group("table"))
        cols = re.findall(r"(\w+)\s*[=<>]", search.group("rest")) or col.sql_columns(table)
        stmt = col.index(table, cols)
        col.finding(
            node,
            "automatic_index",
            "warning",
            f"Automatic index on {table}",
            "SQLite had to build a temporary index for this query, which means a permanent "
            f"one is missing. Suggested: {stmt}",
        )
        return
    if detail.startswith("USE TEMP B-TREE"):
        col.finding(
            node,
            "temp_sort",
            "info",
            "Sort without an index",
            f"SQLite sorts in a temporary b-tree ({detail[len('USE TEMP B-TREE ') :].lower()}).",
        )


def analyze_plan(vendor: str, plan: Any, sql: str = "") -> Analysis:
    """Dispatch on ``vendor`` (``postgresql`` / ``mysql`` / ``sqlite``)."""
    if vendor == "postgresql":
        return analyze_postgres(plan, sql)
    if vendor == "mysql":
        return analyze_mysql(plan, sql)
    if vendor == "sqlite":
        return analyze_sqlite(plan, sql)
    raise ValueError(f"Unsupported vendor for plan analysis: {vendor!r}")
