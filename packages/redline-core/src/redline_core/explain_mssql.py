"""SQL Server estimated plan parsing. Actual execution is deliberately separate."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from .plan_analysis import Analysis, Finding, PlanNode

_NS = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _quote(value):
    return "[" + str(value).strip("[]").replace("]", "]]") + "]"


def analyze_mssql(plan, sql="", params=None):
    if not isinstance(plan, str) or len(plan.encode()) > 5_000_000 or "<!" in plan:
        raise ValueError("Invalid or oversized SQL Server XML plan")
    root = ET.fromstring(plan)
    elements = list(root.iter())
    if len(elements) > 20_000:
        raise ValueError("SQL Server plan exceeds element limit")
    findings, indexes = [], []

    def parse(element, depth=0):
        if depth > 80:
            raise ValueError("SQL Server plan exceeds nesting limit")
        node = PlanNode(
            element.get("PhysicalOp", "Query"),
            rows=_number(element.get("EstimateRows")),
            cost=_number(element.get("EstimatedTotalSubtreeCost")),
        )
        objects = element.findall(f".//{_NS}Object")
        if objects:
            obj = objects[0]
            node.detail = ".".join(
                obj.get(k, "") for k in ("Schema", "Table", "Index") if obj.get(k)
            )

        counters = element.findall(f"{_NS}RunTimeInformation/{_NS}RunTimeCountersPerThread")
        elapsed = [_number(c.get("ActualElapsedms")) for c in counters]
        elapsed = [n for n in elapsed if n is not None]
        node.actual_ms = max(elapsed) if elapsed else None

        # Stop at immediate RelOp descendants so children do not duplicate entire subtrees.
        def descendants(parent):
            for child in parent:
                if child.tag == _NS + "RelOp":
                    yield child
                else:
                    yield from descendants(child)

        node.children = [parse(child, depth + 1) for child in descendants(element)]
        if "Scan" in node.label and node.rows is not None and node.rows >= 1000:
            node.flags.append("scan")
        return node

    relops = root.findall(f".//{_NS}RelOp")
    if not relops:
        raise ValueError("SQL Server plan contains no operators")
    tree = PlanNode("SQL Server estimated plan")
    statements = root.findall(f".//{_NS}StmtSimple")
    for statement in statements:
        operators = statement.findall(f".//{_NS}RelOp")
        if operators:
            node = parse(operators[0])
            node.cost = _number(statement.get("StatementSubTreeCost")) or node.cost
            tree.children.append(node)
    if not tree.children:
        tree.children = [parse(relops[0])]
    for element in elements:
        tag = element.tag.removeprefix(_NS)
        if tag == "PlanAffectingConvert":
            tree.flags.append("implicit_conversion")
            findings.append(
                Finding(
                    "warning",
                    "Plan-affecting implicit conversion",
                    (
                        "SQL Server reports a conversion that can affect estimates or "
                        "access paths. Compare parameter and column types: "
                    )
                    + element.get("Expression", "")[:500],
                )
            )
        elif tag == "SpillToTempDb":
            tree.flags.append("sort_spill")
            findings.append(
                Finding(
                    "warning",
                    "Operator spilled to tempdb",
                    "Review actual row counts, memory grants and access paths "
                    "before adding memory or indexes.",
                )
            )
        elif tag == "Warnings" and element.get("NoJoinPredicate") in {"1", "true"}:
            tree.flags.append("no_join_predicate")
            findings.append(
                Finding(
                    "warning", "Join has no predicate", "Check whether a cross join is intentional."
                )
            )
    for missing in root.findall(f".//{_NS}MissingIndex"):
        schema, table = missing.get("Schema"), missing.get("Table")
        if not schema or not table:
            continue
        keys, included = [], []
        for group in missing.findall(_NS + "ColumnGroup"):
            columns = [c.get("Name") for c in group.findall(_NS + "Column") if c.get("Name")][:32]
            (included if group.get("Usage") == "INCLUDE" else keys).extend(columns)
        if keys:
            columns_sql = ", ".join(_quote(c) for c in keys)
            statement = (
                f"CREATE INDEX [redline_candidate] ON {_quote(schema)}."
                f"{_quote(table)} ({columns_sql})"
            )
            if included:
                statement += f" INCLUDE ({', '.join(_quote(c) for c in included)})"
            indexes.append(statement + ";")
    if indexes:
        findings.append(
            Finding(
                "info",
                "SQL Server missing-index candidates",
                (
                    "Planner candidates require review of existing indexes, write "
                    "costs and workload. No index is created."
                ),
            )
        )
    return Analysis(tree, findings, list(dict.fromkeys(indexes)))
