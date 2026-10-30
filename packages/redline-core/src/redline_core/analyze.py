"""Bounded syntax-only performance analysis. Application code is never executed."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

MAX_SOURCE_BYTES = 200_000
MAX_FILES = 200
MAX_PROJECT_BYTES = 2_000_000
MAX_SCAN_FILES = 10_000
MAX_SCAN_BYTES = 64_000_000


def validate_scan_limits(max_files, max_project_bytes):
    for name, value, ceiling in (
        ("max_files", max_files, MAX_SCAN_FILES),
        ("max_project_bytes", max_project_bytes, MAX_SCAN_BYTES),
    ):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"{name} must be an integer between 1 and {ceiling}")


EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", "migrations", "site", "static"}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _name(node.func)
    return ""


def analyze_source(
    source: str, file: str = "input.py", *, _imported_helpers=(), _summary=None
) -> dict:
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
    try:
        tree = ast.parse(source, filename=file)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return {
            "findings": [],
            "errors": [
                {
                    "file": file,
                    "line": getattr(exc, "lineno", None),
                    "message": "Python source could not be parsed",
                }
            ],
        }
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def resolved(node):
        name = _name(node)
        head, dot, tail = name.partition(".")
        return aliases.get(head, head) + (dot + tail if dot else "")

    def ancestors(node):
        while node in parents:
            node = parents[node]
            yield node

    def scope(node):
        return next(
            (p for p in ancestors(node) if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef))),
            tree,
        )

    def assigned(value):
        return [
            n.id
            for n in ast.walk(value)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        ]

    sessions = set()
    session_types = {"sqlalchemy.orm.Session", "sqlalchemy.ext.asyncio.AsyncSession"}
    typing_annotated = {"typing.Annotated", "typing_extensions.Annotated"}

    def annotation_is_session(annotation, depth=0):
        if depth > 6:
            return False
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            text = annotation.value
            if len(text) > 1024:
                return False
            try:
                parsed = ast.parse(text, mode="eval").body
            except (SyntaxError, ValueError, RecursionError):
                return False
            return annotation_is_session(parsed, depth + 1)
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return annotation_is_session(annotation.left, depth + 1) or annotation_is_session(
                annotation.right, depth + 1
            )
        if isinstance(annotation, ast.Subscript):
            arguments = (
                annotation.slice.elts
                if isinstance(annotation.slice, ast.Tuple)
                else [annotation.slice]
            )
            if resolved(annotation.value) in typing_annotated:
                arguments = arguments[:1]
            return any(annotation_is_session(arg, depth + 1) for arg in arguments)
        return resolved(annotation) in session_types

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.arg)
            and node.annotation is not None
            and annotation_is_session(node.annotation)
        ):
            sessions.add((scope(node), node.arg))
    bindings = {}
    for assignment in ast.walk(tree):
        if isinstance(assignment, (ast.Assign, ast.AnnAssign)) and assignment.value is not None:
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault((scope(assignment), target.id), []).append(assignment)

    def bound_value(node):
        candidates = bindings.get((scope(node), node.id), [])
        enclosing = set(ancestors(node))
        candidates = [
            b
            for b in candidates
            if b not in enclosing and (b.lineno, b.col_offset) < (node.lineno, node.col_offset)
        ]
        latest = max(candidates, key=lambda b: (b.lineno, b.col_offset), default=None)
        return latest.value if latest is not None else None

    session_factories = {
        "sqlalchemy.orm.sessionmaker",
        "sqlalchemy.ext.asyncio.async_sessionmaker",
    }

    def safe_resolved(node):
        """Resolve imported names only when the relevant scope has not rebound them."""
        name = _name(node)
        head = name.split(".", 1)[0]
        owner = scope(node)
        if owner is not tree:
            if any(
                arg.arg == head
                for arg in [
                    *owner.args.posonlyargs,
                    *owner.args.args,
                    *owner.args.kwonlyargs,
                ]
            ):
                return name
            if bindings.get((owner, head)):
                return name
        if bindings.get((tree, head)):
            return name
        return resolved(node)

    def factory_call(node):
        return isinstance(node, ast.Call) and safe_resolved(node.func) in session_factories

    def factory_name(name, node):
        """Whether a name is bound to a known factory in this lexical scope."""
        owner = scope(node)
        if owner is not tree and any(
            arg.arg == name
            for arg in [*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs]
        ):
            return False
        local = bindings.get((owner, name), []) if owner is not tree else []
        if local:
            # Any local assignment makes this name local throughout the function.
            candidates = [
                item
                for item in local
                if (item.lineno, item.col_offset) < (node.lineno, node.col_offset)
            ]
            latest = max(candidates, key=lambda item: (item.lineno, item.col_offset), default=None)
            return latest is not None and factory_call(latest.value)
        module_bindings = bindings.get((tree, name), [])
        if not module_bindings:
            return False
        latest = max(module_bindings, key=lambda item: (item.lineno, item.col_offset))
        return factory_call(latest.value)

    def is_session_factory_call(node):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and factory_name(node.func.id, node)
        )

    def bind_session(target, node):
        for name in assigned(target):
            sessions.add((scope(node), name))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            value = node.value
            if isinstance(value, ast.Call) and safe_resolved(value.func) in session_types:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    bind_session(target, node)
            elif is_session_factory_call(value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    bind_session(target, node)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if is_session_factory_call(item.context_expr) and item.optional_vars is not None:
                    bind_session(item.optional_vars, node)

    def list_input(node):
        value = bound_value(node)
        return isinstance(value, (ast.List, ast.ListComp)) or (
            isinstance(value, ast.Call) and resolved(value.func) in {"list", "builtins.list"}
        )

    query_methods = {
        "all",
        "filter",
        "exclude",
        "order_by",
        "select_related",
        "prefetch_related",
        "values",
        "values_list",
        "annotate",
        "scalars",
    }

    def queryset(node):
        if isinstance(node, ast.Await):
            return queryset(node.value)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            return queryset(node.value)
        if isinstance(node, ast.Name):
            value = bound_value(node)
            return value is not None and queryset(value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and (scope(node), node.func.value.id) in sessions
                and node.func.attr in {"query", "execute", "scalars", "scalar", "get"}
            ):
                return True
            return node.func.attr in query_methods and (
                _name(node.func.value).endswith(".objects") or queryset(node.func.value)
            )
        return False

    # Summaries only follow unshadowed module-level helpers, with bounded depth.
    functions = {
        n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    shadowed_helpers = set(bindings)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names = (
                    functions if alias.name == "*" else [alias.asname or alias.name.split(".")[0]]
                )
                shadowed_helpers.update((scope(node), name) for name in names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if scope(node) is not tree:
                shadowed_helpers.add((scope(node), node.name))

    def local_helper(call):
        if not isinstance(call.func, ast.Name) or call.func.id not in functions:
            return None
        owner = scope(call)
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs]
            if any(a.arg == call.func.id for a in args):
                return None
        if (owner, call.func.id) in shadowed_helpers or (tree, call.func.id) in shadowed_helpers:
            return None
        return functions[call.func.id]

    def imported_helper(call):
        name = _name(call.func)
        if name not in _imported_helpers:
            return False
        head = name.split(".")[0]
        owner = scope(call)
        if owner is not tree and (owner, head) in shadowed_helpers:
            return False
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs]
            if any(arg.arg == head for arg in args):
                return False
        return True

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    query_helpers = {
        scope(n)
        for n in calls
        if isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and (scope(n), n.func.value.id) in sessions
        and n.func.attr in {"query", "execute", "scalars", "scalar", "get"}
    }
    for _ in range(8):
        expanded = query_helpers | {
            scope(n) for n in calls if local_helper(n) in query_helpers or imported_helper(n)
        }
        if expanded == query_helpers:
            break
        query_helpers = expanded

    if _summary is not None:
        _summary.update(name for name, function in functions.items() if function in query_helpers)

    findings = []
    seen = set()

    def emit(kind, node, message, advice):
        key = (kind, node.lineno)
        if key in seen:
            return
        seen.add(key)
        identity = f"{file}:{node.lineno}:{kind}"
        findings.append(
            {
                "id": hashlib.sha256(identity.encode()).hexdigest()[:16],
                "kind": kind,
                "file": file,
                "line": node.lineno,
                "column": node.col_offset + 1,
                "confidence": "medium",
                "detail": message,
                "suggestion": advice,
                "limitation": "Syntax candidate; verify behavior with a refactor check.",
            }
        )

    for node in ast.walk(tree):
        outer = list(ancestors(node))
        loops = []
        for parent in outer:
            if isinstance(
                parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                break
            if isinstance(
                parent,
                (
                    ast.For,
                    ast.AsyncFor,
                    ast.While,
                    ast.ListComp,
                    ast.GeneratorExp,
                    ast.SetComp,
                    ast.DictComp,
                ),
            ):
                # A loop's first iterable is evaluated once, before its body.
                # Later comprehension iterables repeat for preceding generators.
                iterable = None
                if isinstance(parent, (ast.For, ast.AsyncFor)):
                    iterable = parent.iter
                elif isinstance(
                    parent, (ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.DictComp)
                ):
                    iterable = parent.generators[0].iter
                if iterable is node or iterable in outer:
                    continue
                loops.append(parent)
        if isinstance(node, ast.Call):
            name = resolved(node.func)
            query_call = (
                isinstance(node.func, ast.Attribute)
                and (
                    _name(node.func.value).endswith(".objects")
                    or queryset(node.func.value)
                    or queryset(node)
                )
                and node.func.attr
                in {
                    *query_methods,
                    "get",
                    "scalar",
                    "count",
                    "exists",
                    "aggregate",
                    "first",
                    "execute",
                    "query",
                }
            )
            helper_call = local_helper(node) in query_helpers or imported_helper(node)
            if loops and (query_call or helper_call):
                emit(
                    "query_in_loop",
                    node,
                    "A query-capable helper is called inside a loop."
                    if helper_call
                    else "A queryset operation appears inside a loop.",
                    "Batch reads with IN queries or eager loading (SQLAlchemy: "
                    "selectinload/joinedload; Django: prefetch_related/select_related). "
                    "Check identity-map hits, conditional queries and intentional batching.",
                )
            if (
                name == "sum"
                and node.args
                and isinstance(node.args[0], (ast.GeneratorExp, ast.ListComp))
            ):
                expr = node.args[0]
                if any(queryset(g.iter) for g in expr.generators):
                    emit(
                        "python_aggregation",
                        node,
                        "Python aggregates values fetched from a queryset.",
                        "Review SQL aggregation; verify nulls, precision and duration rounding.",
                    )
            if (
                loops
                and name in {"sorted", "builtins.sorted"}
                and node.args
                and isinstance(node.args[0], ast.Name)
            ):
                collection = node.args[0].id
                loop = loops[0]
                bound = set(assigned(loop))
                mutated = any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and _name(n.func.value) == collection
                    and n.func.attr
                    in {"append", "extend", "insert", "pop", "remove", "clear", "sort", "reverse"}
                    for n in ast.walk(loop)
                )
                if collection not in bound and not mutated:
                    emit(
                        "sorting_in_loop",
                        node,
                        "A loop repeatedly sorts the same named input.",
                        "If the input and sort key remain invariant, sort once outside the loop.",
                    )
            if (
                loops
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sort"
                and isinstance(node.func.value, ast.Name)
                and list_input(node.func.value)
            ):
                collection = node.func.value.id
                loop = loops[0]
                other_mutation = any(
                    isinstance(n, ast.Call)
                    and n is not node
                    and isinstance(n.func, ast.Attribute)
                    and _name(n.func.value) == collection
                    and n.func.attr
                    in {"append", "extend", "insert", "pop", "remove", "clear", "sort", "reverse"}
                    for n in ast.walk(loop)
                )
                if collection not in assigned(loop) and not other_mutation:
                    emit(
                        "sorting_in_loop",
                        node,
                        "A loop repeatedly sorts the same list.",
                        "If values and sort options stay invariant, sort once outside the loop.",
                    )
            function = next(
                (p for p in outer if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef))), None
            )
            if isinstance(function, ast.AsyncFunctionDef) and name in {
                "time.sleep",
                "requests.get",
                "requests.post",
                "requests.put",
                "requests.delete",
            }:
                emit(
                    "blocking_async_call",
                    node,
                    "A known synchronous blocking call appears in an async function.",
                    "Use an async client or thread offloading; verify event-loop behavior.",
                )
        if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.DictComp)):
            if any(g.ifs and queryset(g.iter) for g in node.generators):
                emit(
                    "python_filtering",
                    node,
                    "A comprehension filters rows from a queryset in Python.",
                    "Consider SQL filtering where comparison and null semantics match.",
                )
        if isinstance(node, ast.If) and loops:
            loop = next((p for p in loops if isinstance(p, (ast.For, ast.AsyncFor))), None)
            if loop is not None and queryset(loop.iter):
                names = set(assigned(loop.target))
                checks_row = any(
                    isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.value.id in names
                    for n in ast.walk(node.test)
                )
                filters_row = any(
                    isinstance(n, ast.Continue)
                    or (
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "append"
                        and any(
                            isinstance(v, ast.Name) and v.id in names
                            for arg in n.args
                            for v in ast.walk(arg)
                        )
                    )
                    for statement in node.body
                    for n in ast.walk(statement)
                )
                if checks_row and filters_row:
                    emit(
                        "python_filtering",
                        node,
                        "A loop condition selects fetched queryset rows.",
                        "Review SQL filtering where nulls, comparisons and side effects match.",
                    )
        if isinstance(node, ast.Compare) and loops:
            for op, right in zip(node.ops, node.comparators, strict=True):
                if (
                    isinstance(op, (ast.In, ast.NotIn))
                    and isinstance(right, ast.Name)
                    and list_input(right)
                ):
                    emit(
                        "list_membership_in_loop",
                        node,
                        "Repeated membership tests scan a list inside a loop.",
                        "Consider a set; preserve hashing, equality and mutation semantics.",
                    )
        if isinstance(node, (ast.For, ast.AsyncFor)) and loops and isinstance(node.iter, ast.Name):
            parent = loops[0]
            if (
                isinstance(parent, ast.For)
                and isinstance(parent.iter, ast.Call)
                and _name(parent.iter.func) == "range"
            ):
                if (
                    all(
                        isinstance(a, ast.Constant) and isinstance(a.value, int)
                        for a in parent.iter.args
                    )
                    and parent.iter.args
                ):
                    if node.iter.id not in assigned(parent.target):
                        emit(
                            "repeated_collection_scan",
                            node,
                            "A fixed-range outer loop repeatedly traverses the same named input.",
                            "Review one-pass bucketing; preserve dependencies and output ordering.",
                        )
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add) and loops:
            loop = loops[0]
            if isinstance(loop, ast.For) and queryset(loop.iter):
                emit(
                    "python_aggregation",
                    node,
                    "A Python loop accumulates values over a queryset.",
                    "Review SQL aggregation; verify nulls, duration units, precision and rounding.",
                )
    return {"findings": sorted(findings, key=lambda f: (f["line"], f["kind"])), "errors": []}


def analyze_project(
    root: str | Path, *, max_files: int = MAX_FILES, max_project_bytes: int = MAX_PROJECT_BYTES
) -> dict:
    validate_scan_limits(max_files, max_project_bytes)
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("project root must be an existing directory")
    result = {"findings": [], "errors": [], "files_analyzed": 0, "skipped": 0, "limited": False}
    total = 0
    visited = 0
    sources = {}

    def finish():
        from .project_analysis import analyze_imports

        reports, inference = analyze_imports(sources, root)
        result["findings"] = [
            finding for report in reports.values() for finding in report["findings"]
        ]
        result["project_inference"] = inference
        result["limited"] = result["limited"] or inference["limited"]
        return result

    # os.walk prunes directories before traversal; never follow links outside the project.
    import os

    for directory, dirs, files in os.walk(root, followlinks=False):
        visited += len(dirs) + len(files)
        if visited > 20000:
            result["limited"] = True
            return finish()
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in EXCLUDED and not d.startswith(".") and not Path(directory, d).is_symlink()
        )
        for name in sorted(files):
            path = Path(directory, name)
            if path.suffix != ".py" or path.is_symlink():
                continue
            if result["files_analyzed"] >= max_files or total >= max_project_bytes:
                result["limited"] = True
                return finish()
            try:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    result["skipped"] += 1
                    continue
                with path.open(encoding="utf-8") as stream:
                    source = stream.read(MAX_SOURCE_BYTES + 1)
                if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
                    result["skipped"] += 1
                    continue
                total += len(source.encode("utf-8"))
                if total > max_project_bytes:
                    result["limited"] = True
                    return finish()
                relative = str(path.relative_to(root))
                sources[relative] = source
                report = analyze_source(source, relative)
                result["files_analyzed"] += 1
                result["findings"].extend(report["findings"])
                result["errors"].extend(report["errors"])
            except (OSError, UnicodeError):
                result["errors"].append(
                    {"file": str(path.relative_to(root)), "message": "Source could not be read"}
                )
    return finish()
