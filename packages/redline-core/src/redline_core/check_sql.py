"""Bounded SQL extraction and linting. Application modules are never imported."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from .analyze import MAX_FILES, MAX_PROJECT_BYTES, MAX_SOURCE_BYTES, validate_scan_limits
from .sql import _TOKEN, comment_placeholders, effective_kinds, fingerprint
from .sql_literals import SQLLiteral, literal_value

RULES = {
    "bind-in-comment": (
        "Bind-like text in a SQL comment can cause compiler/driver parameter mismatches."
    ),
    "output-without-into": (
        "Bare OUTPUT is incompatible with enabled triggers on the target. "
        "Verify metadata before treating this as an error."
    ),
    "session-option-without-reset": (
        "An observed session option is enabled without an OFF in this "
        "statement. Check a guaranteed finally/reset path."
    ),
    "select-star-in-insert-select": (
        "INSERT SELECT * depends on column order; prefer explicit source and destination columns."
    ),
    "parameter-not-bound": "A named SQL parameter is absent from the literal parameter mapping.",
}


_ROW_METHODS = {
    "fetchone",
    "fetchall",
    "fetchmany",
    "first",
    "one",
    "one_or_none",
    "scalar",
    "scalar_one",
    "scalar_one_or_none",
    "mappings",
}
_RESULT_METHODS = {"execute", "exec_driver_sql", "executemany"}
_ROW_ATTRIBUTES = {
    "append",
    "count",
    "extend",
    "get",
    "index",
    "items",
    "keys",
    "pop",
    "values",
}


def _call_name(node):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _targets(node):
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {
        part.id
        for target in targets
        for part in ast.walk(target)
        if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store)
    }


def _result_reads(scope, limit=100):
    """Best-effort literal columns read from row objects in this scope.

    A row object is a value assigned from ``fetchone()``/``first()``/``mappings()`` and
    similar, or a loop variable over an ``execute()`` result (or a list of rows). Only
    literal string subscripts (``row["col"]``) and plain attributes (``row.col``) are
    reported; dynamic keys and tuple indexes are ignored. This is a candidate list for
    online result-set comparison, never a proof that the column is read.
    """
    rows: set[str] = set()
    results: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            name = _call_name(node.value)
            if name in _ROW_METHODS:
                rows.update(_targets(node))
            elif name in _RESULT_METHODS:
                results.update(_targets(node))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            iterator = node.iter
            source = iterator.id if isinstance(iterator, ast.Name) else _call_name(iterator)
            if source in results or source in rows or source in _ROW_METHODS | _RESULT_METHODS:
                rows.update(_targets(node))
    found = {}
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in rows
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and 0 < len(node.slice.value) <= 128
        ):
            found.setdefault(node.slice.value, node.lineno)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in rows
            and node.attr
            and not node.attr.startswith("_")
            and node.attr not in _ROW_ATTRIBUTES
            and node.attr not in _ROW_METHODS
        ):
            found.setdefault(node.attr, node.lineno)
    return [
        {"name": name, "line": line}
        for name, line in sorted(found.items(), key=lambda item: (item[1], item[0]))[:limit]
    ]


def _read_scope(scopes, tree):
    for scope in scopes:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return scope
    return tree


def _constant(node, constants, helpers, aliases):
    return literal_value(node, constants, helpers, aliases)


def _stored_names(node):
    return {
        part.id
        for part in ast.walk(node)
        if isinstance(part, ast.Name) and isinstance(part.ctx, (ast.Store, ast.Del))
    }


def _local_constants(function, call, wanted, module_constants, helpers, aliases):
    """Straight-line constants for ``wanted`` names just before ``call`` in ``function``.

    Only top-level statements of the function body that precede the statement holding the
    call are evaluated, in order. Any name stored inside a compound statement (branches,
    loops, ``try``) becomes unknown, as do the function's own parameters until assigned.
    """
    parameters = {
        a.arg
        for a in [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
            *([function.args.vararg] if function.args.vararg else []),
            *([function.args.kwarg] if function.args.kwarg else []),
        ]
    }
    env = {k: v for k, v in module_constants.items() if k.split(".", 1)[0] not in parameters}
    known = {}
    holder = next(
        (stmt for stmt in function.body if any(part is call for part in ast.walk(stmt))),
        None,
    )
    for stmt in function.body:
        if stmt is holder:
            for name in _stored_names(stmt):
                known.pop(name, None)
                env.pop(name, None)
            break
        target = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, expr = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, expr = stmt.target, stmt.value
        elif isinstance(stmt, ast.AugAssign):
            target = stmt.target
            expr = (
                ast.BinOp(left=ast.Name(id=target.id, ctx=ast.Load()), op=stmt.op, right=stmt.value)
                if isinstance(target, ast.Name)
                else None
            )
        if isinstance(target, ast.Name) and expr is not None:
            try:
                value = literal_value(expr, env, helpers, aliases)
            except (ValueError, TypeError, RecursionError):
                known.pop(target.id, None)
                env.pop(target.id, None)
            else:
                known[target.id] = env[target.id] = value
            continue
        for name in _stored_names(stmt):
            known.pop(name, None)
            env.pop(name, None)
    return {name: value for name, value in known.items() if name in wanted}


def extract(source, file="<source>", *, imported_constants=None, _exports=None):
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return [], [{"file": file, "line": 1, "reason": "source limit exceeded"}]
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as exc:
        return [], [
            {"file": file, "line": getattr(exc, "lineno", 1), "reason": "invalid Python source"}
        ]
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    aliases = {"text"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in {
            "sqlalchemy",
            "sqlalchemy.sql",
            "sqlalchemy.sql.expression",
        }:
            aliases.update(n.asname or n.name for n in node.names if n.name == "text")
    helper_definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    orm_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module in {"sqlalchemy", "sqlalchemy.sql", "sqlalchemy.sql.expression"}
        for alias in statement.names
        if alias.name in {"select", "insert", "update", "delete"}
    }
    helpers = {}
    unknown = object()
    constants, history = {}, {}
    imported_constants = imported_constants or {}
    module_positions = {node: index for index, node in enumerate(tree.body)}
    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {
                part.id
                for target in targets
                for part in ast.walk(target)
                if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store)
            }
            try:
                if not all(isinstance(target, ast.Name) for target in targets):
                    raise ValueError("unsupported assignment target")
                value = _constant(node.value, constants, helpers, aliases)
            except (ValueError, TypeError):
                value = unknown
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            names = {node.target.id}
            try:
                value = _constant(
                    ast.BinOp(
                        left=ast.Name(id=node.target.id, ctx=ast.Load()),
                        op=node.op,
                        right=node.value,
                    ),
                    constants,
                    helpers,
                    aliases,
                )
            except (ValueError, TypeError):
                value = unknown
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names, value = {node.name}, unknown
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                helpers[node.name] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {alias.asname or alias.name.split(".")[0] for alias in node.names}
            value = unknown
            import_values = {}
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name != "*":
                        imported = imported_constants.get(local, unknown)
                        import_values[local] = imported
                        if imported is not unknown:
                            constants[local] = imported
            else:
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    prefix = local + "."
                    for imported_name, imported in imported_constants.items():
                        if imported_name.startswith(prefix):
                            constants[imported_name] = imported
        else:
            names = {
                part.id
                for part in ast.walk(node)
                if isinstance(part, ast.Name) and isinstance(part.ctx, (ast.Store, ast.Del))
            }
            value = unknown
        for name in names:
            item_value = (
                import_values.get(name, unknown) if isinstance(node, ast.ImportFrom) else value
            )
            history.setdefault(name, []).append((index, item_value))
            if item_value is unknown:
                constants.pop(name, None)
            else:
                constants[name] = item_value
                helpers.pop(name, None)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)):
            for name in names:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    helpers.pop(name, None)
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                for imported_name, imported in imported_constants.items():
                    if imported_name.startswith(local + "."):
                        history.setdefault(imported_name, []).append((index, imported))
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)):
            for name in names:
                if not any(
                    isinstance(node, ast.Import) and imported_name.startswith(name + ".")
                    for imported_name in imported_constants
                ):
                    for dotted in [key for key in constants if key.startswith(name + ".")]:
                        constants.pop(dotted, None)
    dynamic_globals = {
        name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names
    }
    stable_constants = {
        name: changes[0][1]
        for name, changes in history.items()
        if len(changes) == 1 and changes[0][1] is not unknown and name not in dynamic_globals
    }
    stable_helpers = {
        name: definition
        for name, definition in helper_definitions.items()
        if len(history.get(name, ())) == 1
        and history[name][0][1] is unknown
        and name not in dynamic_globals
    }
    if _exports is not None:
        _exports.update(
            {name: value for name, value in stable_constants.items() if "." not in name}
        )
    statements, skipped, seen = [], [], set()
    read_cache = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        call_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if call_name not in aliases | {"text", "execute", "executemany", "exec_driver_sql"}:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Call):
            inner_name = (
                arg.func.id
                if isinstance(arg.func, ast.Name)
                else arg.func.attr
                if isinstance(arg.func, ast.Attribute)
                else ""
            )
            if inner_name in aliases:
                continue  # inner text() is extracted once
        try:
            top, ancestor, scopes = node, parents.get(node), []
            while ancestor is not None and ancestor is not tree:
                top = ancestor
                if isinstance(
                    ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
                ):
                    scopes.append(ancestor)
                ancestor = parents.get(ancestor)
            names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
            position = module_positions.get(top, -1)
            available_helpers = {
                helper_name: helper
                for helper_name, helper in stable_helpers.items()
                if history[helper_name][0][0] < position
            }
            if any(
                isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                for scope in scopes
            ):
                active_constants = {
                    name: stable_constants[name] for name in names if name in stable_constants
                }
                active_constants.update(
                    {
                        key: stable_constants[key]
                        for key in stable_constants
                        if "." in key and key.split(".", 1)[0] in names
                    }
                )
            else:
                active_constants = {}
                for name in names:
                    value = next(
                        (
                            value
                            for index, value in reversed(history.get(name, ()))
                            if index < position
                        ),
                        unknown,
                    )
                    if value is not unknown and name not in dynamic_globals:
                        active_constants[name] = value
                for dotted in (key for key in history if "." in key):
                    prefix = dotted.split(".", 1)[0]
                    if prefix in names:
                        value = next(
                            (v for i, v in reversed(history[dotted]) if i < position),
                            unknown,
                        )
                        if value is not unknown:
                            active_constants[dotted] = value
            pending_helpers = [name for name in names if name in available_helpers]
            visited_helpers = set()
            helper_globals = set()
            while pending_helpers:
                helper_name = pending_helpers.pop()
                if helper_name in visited_helpers:
                    continue
                visited_helpers.add(helper_name)
                helper = available_helpers[helper_name]
                helper_globals.update(n.id for n in ast.walk(helper) if isinstance(n, ast.Name))
                pending_helpers.extend(
                    n.id
                    for n in ast.walk(helper)
                    if isinstance(n, ast.Name)
                    and n.id in available_helpers
                    and n.id not in visited_helpers
                )
            helper_parameters = {
                arg.arg
                for name in visited_helpers
                for arg in ast.walk(available_helpers[name].args)
                if isinstance(arg, ast.arg)
            }
            for helper_global in helper_globals - helper_parameters:
                if helper_global in stable_constants:
                    active_constants.setdefault(helper_global, stable_constants[helper_global])
            bound_names = set()
            for scope in scopes:
                bound = {
                    n.id
                    for n in ast.walk(scope)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
                }
                if hasattr(scope, "args"):
                    bound.update(n.arg for n in ast.walk(scope.args) if isinstance(n, ast.arg))
                bound_names.update(bound)
                active_constants = {
                    k: v for k, v in active_constants.items() if k.split(".", 1)[0] not in bound
                }
            active_helpers = {
                helper_name: helper
                for helper_name, helper in available_helpers.items()
                if helper_name not in bound_names
            }
            if scopes and isinstance(scopes[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
                active_constants.update(
                    _local_constants(
                        scopes[0], node, names, stable_constants, active_helpers, aliases
                    )
                )
            if (
                call_name in _RESULT_METHODS
                and isinstance(arg, ast.Name)
                and isinstance(active_constants.get(arg.id), SQLLiteral)
            ):
                # Its text() declaration already reports the static SQL at its source line.
                continue
            value = _constant(arg, active_constants, active_helpers, aliases)
            if isinstance(value, SQLLiteral):
                value = value.sql
            if not isinstance(value, str) or not effective_kinds(value):
                continue
        except (ValueError, TypeError):
            contains_orm = any(
                isinstance(part, ast.Call)
                and isinstance(part.func, ast.Name)
                and part.func.id in orm_names
                for part in ast.walk(arg)
            )
            reason = (
                "SQLAlchemy expression; use runtime capture or explicit compilation"
                if contains_orm
                else "dynamic SQL"
            )
            category = (
                "sqlalchemy_expression"
                if contains_orm
                else getattr(sys.exc_info()[1], "category", "dynamic")
            )
            skipped.append(
                {"file": file, "line": node.lineno, "reason": reason, "category": category}
            )
            continue
        key = (arg.lineno, value)
        if key in seen:
            continue
        seen.add(key)
        bindings = None
        binding_call = node
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and parent.args and parent.args[0] is node:
            binding_call = parent
        if len(binding_call.args) > 1 and isinstance(binding_call.args[1], ast.Dict):
            keys = binding_call.args[1].keys
            if all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in keys):
                bindings = [k.value for k in keys]
        scope = _read_scope(scopes, tree)
        statements.append(
            {
                "file": file,
                "line": arg.lineno,
                "sql": value,
                "bindings": bindings,
                "result_reads": read_cache.setdefault(id(scope), _result_reads(scope)),
            }
        )
    return statements, skipped


def unresolved_by_reason(skipped):
    """Count unresolved inputs by a stable category (``dynamic``, ``unknown_name``, ...)."""
    counts = {}
    for item in skipped:
        category = item.get("category")
        if category is None:
            reason = item.get("reason", "")
            category = (
                "limit"
                if "limit" in reason
                else "invalid_source"
                if "invalid" in reason
                else "unreadable"
                if "unreadable" in reason
                else "compilation"
                if reason.startswith("Online compilation")
                else "dynamic"
            )
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


def lint(sql, dialect="other", bindings=None):
    inert = {"lcomment", "bcomment", "string", "dollar", "dident", "bident", "sqident"}
    clean = _TOKEN.sub(
        lambda match: " " * len(match.group()) if match.lastgroup in inert else match.group(), sql
    )
    rules = []
    if comment_placeholders(sql):
        rules.append("bind-in-comment")
    if dialect == "mssql":
        for part in clean.split(";"):
            if re.search(
                r"\b(?:INSERT|UPDATE|DELETE|MERGE)\b.*?\bOUTPUT\b", part, re.I | re.S
            ) and not re.search(r"\bOUTPUT\b.*\bINTO\b", part, re.I | re.S):
                rules.append("output-without-into")
        for option in ("NOCOUNT", "XACT_ABORT", "ANSI_NULLS", "ANSI_WARNINGS", "ARITHABORT"):
            changes = re.findall(rf"\bSET\s+{option}\s+(ON|OFF)\b", clean, re.I)
            if changes and changes[-1].upper() == "ON":
                rules.append("session-option-without-reset")
    if any(re.search(r"\bINSERT\b.*\bSELECT\s+\*", part, re.I | re.S) for part in clean.split(";")):
        rules.append("select-star-in-insert-select")
    if bindings is not None:
        names = set(re.findall(r"(?<![:\w]):([A-Za-z_]\w*)", clean))
        if names - set(bindings):
            rules.append("parameter-not-bound")
    ignored = {
        rule
        for token in _TOKEN.finditer(sql)
        if token.lastgroup in {"lcomment", "bcomment"}
        for rule in re.findall(r"redline:\s*ignore\[([a-z-]+)\]", token.group())
    }
    return [rule for rule in dict.fromkeys(rules) if rule not in ignored]


def analyze_sql_source(source, *, file="<source>", dialect="other"):
    statements, skipped = extract(source, file)
    findings = []
    for statement in statements:
        sql = statement["sql"]
        for rule in lint(sql, dialect, statement["bindings"]):
            identity = f"{file}:{statement['line']}:{rule}:{fingerprint(sql)}"
            findings.append(
                {
                    "id": hashlib.sha256(identity.encode()).hexdigest()[:20],
                    "kind": rule,
                    "file": file,
                    "line": statement["line"],
                    "detail": RULES[rule],
                    "confidence": "medium",
                    "fingerprint": fingerprint(sql),
                }
            )
    return {
        "findings": findings,
        "skipped": skipped,
        "unresolved_by_reason": unresolved_by_reason(skipped),
        "statements": len(statements),
        "note": "Offline candidates only; schema and triggers are not checked.",
    }


def _module_names(records):
    modules, trees, bindings, dynamic = {}, {}, {}, {}
    for file, source, root in records:
        path = Path(file).resolve()
        root_path = Path(root)
        if root_path.is_dir():
            try:
                relative = path.relative_to(root_path)
            except ValueError:
                continue
            parts = list(relative.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if (root_path / "__init__.py").is_file():
                parts.insert(0, root_path.name)
        else:
            parts = [path.stem]
        module = ".".join(parts)
        if not module:
            continue
        modules[module] = file
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue
        trees[file] = tree
        counts = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.ImportFrom):
                names = [alias.asname or alias.name for alias in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
            else:
                names = [
                    item.id
                    for item in ast.walk(node)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del))
                ]
            for name in names:
                counts[name] = counts.get(name, 0) + 1
        bindings[file] = counts
        dynamic[file] = {
            name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names
        }
    return modules, trees, bindings, dynamic


def _imports_for_file(file, module, tree, modules, bindings, dynamic, exports):
    imported = {}
    if tree is None:
        return imported
    parts = module.split(".") if module else []
    # A module's package is its containing package; __init__ modules were already normalized.
    package = parts if Path(file).name == "__init__.py" else parts[:-1]
    counts = bindings.get(file, {})
    mutated = dynamic.get(file, set())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.level > len(package):
                continue
            base = package[: len(package) - node.level + 1] if node.level else []
            if node.module:
                base.extend(node.module.split("."))
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == "*" or counts.get(local) != 1 or local in mutated or not base:
                    continue
                canonical = ".".join([*base, alias.name])
                if canonical in modules:
                    for exported, value in exports.items():
                        prefix = canonical + "."
                        if exported.startswith(prefix):
                            imported[f"{local}.{exported[len(prefix) :]}"] = value
                elif canonical in exports:
                    imported[local] = exports[canonical]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                if counts.get(local) != 1 or local in mutated:
                    continue
                canonical = alias.name if alias.asname else local
                prefix = canonical + "."
                for exported, value in exports.items():
                    if exported.startswith(prefix):
                        imported[f"{local}.{exported[len(prefix) :]}"] = value
    return imported


def _analyze_project_sql(records, dialect):
    modules, trees, bindings, dynamic = _module_names(records)
    file_modules = {file: module for module, file in modules.items()}
    exports = {}
    reports = {}
    changed = False
    for _ in range(8):
        next_exports = {}
        reports = {}
        for file, source, _ in records:
            module = file_modules.get(file, "")
            imports = _imports_for_file(
                file,
                module,
                trees.get(file),
                modules,
                bindings,
                dynamic,
                exports,
            )
            local_exports = {}
            statements, skipped = extract(
                source, file, imported_constants=imports, _exports=local_exports
            )
            findings = []
            for statement in statements:
                sql = statement["sql"]
                for rule in lint(sql, dialect, statement["bindings"]):
                    identity = f"{file}:{statement['line']}:{rule}:{fingerprint(sql)}"
                    findings.append(
                        {
                            "id": hashlib.sha256(identity.encode()).hexdigest()[:20],
                            "kind": rule,
                            "file": file,
                            "line": statement["line"],
                            "detail": RULES[rule],
                            "confidence": "medium",
                            "fingerprint": fingerprint(sql),
                        }
                    )
            reports[file] = {
                "findings": findings,
                "skipped": skipped,
                "statements": len(statements),
                "note": "Offline candidates only; schema and triggers are not checked.",
            }
            module = file_modules.get(file)
            if module:
                next_exports.update(
                    {f"{module}.{name}": value for name, value in local_exports.items()}
                )
        changed = next_exports != exports
        exports = next_exports
        if not changed:
            break
    if changed:
        # Avoid presenting partially propagated values from a graph that exceeded the pass cap.
        reports = {}
        for file, source, _ in records:
            local_exports = {}
            statements, skipped = extract(source, file, _exports=local_exports)
            report = analyze_sql_source(source, file=file, dialect=dialect)
            report["statements"] = len(statements)
            reports[file] = report
            report["skipped"].append(
                {"file": file, "line": 1, "reason": "project import resolution limit exceeded"}
            )
    return reports


def scan(paths, *, dialect="other", max_files=MAX_FILES, max_project_bytes=MAX_PROJECT_BYTES):
    validate_scan_limits(max_files, max_project_bytes)
    result = {"findings": [], "skipped": [], "statements": 0, "files": 0}
    visited, byte_count, entries = set(), 0, 0
    records = []
    excluded = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "dist", "build"}

    def visit(path):
        nonlocal entries
        entries += 1
        if entries > 20000:
            raise ValueError("project entry limit")
        if path.is_symlink():
            return
        if path.is_dir():
            for child in path.iterdir():
                if child.name in excluded:
                    entries += 1
                    if entries > 20000:
                        raise ValueError("project entry limit")
                    continue
                yield from visit(child)
        elif path.suffix == ".py":
            yield path

    for root in paths:
        try:
            if not Path(root).exists():
                raise OSError("path not found")
            for path in visit(Path(root)):
                resolved = path.resolve()
                root_path = Path(root).resolve()
                containment_root = root_path if root_path.is_dir() else root_path.parent
                if not resolved.is_relative_to(containment_root):
                    continue
                if resolved in visited:
                    continue
                visited.add(resolved)
                if len(visited) > max_files or byte_count >= max_project_bytes:
                    result["skipped"].append(
                        {"file": str(root), "line": 1, "reason": "project limit exceeded"}
                    )
                    break
                with path.open("rb") as stream:
                    raw = stream.read(MAX_SOURCE_BYTES + 1)
                byte_count += len(raw)
                if byte_count > max_project_bytes:
                    result["skipped"].append(
                        {"file": str(root), "line": 1, "reason": "project limit exceeded"}
                    )
                    break
                source = raw.decode("utf-8")
                records.append((str(path), source, str(containment_root)))
        except (OSError, UnicodeError, RecursionError, ValueError):
            result["skipped"].append(
                {"file": str(root), "line": 1, "reason": "unreadable or oversized project"}
            )
    reports = _analyze_project_sql(records, dialect)
    for file, _, _ in records:
        report = reports.get(file)
        if report is None:
            continue
        result["findings"].extend(report["findings"])
        result["skipped"].extend(report["skipped"])
        result["statements"] += report["statements"]
        result["files"] += 1
    result["findings"].sort(key=lambda f: (f["file"], f["line"], f["kind"]))
    result["unresolved_by_reason"] = unresolved_by_reason(result["skipped"])
    return result


def configure_cli(parser):
    parser.add_argument("paths", nargs="+")
    parser.add_argument(
        "--dialect", choices=("other", "mssql", "postgresql", "mysql", "sqlite"), default="other"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--junit")
    parser.add_argument(
        "--dsn",
        help=(
            "compile explicit files against an isolated development database "
            "(optional SQLAlchemy required)"
        ),
    )
    parser.add_argument(
        "--schema-snapshot", help="compare/update default-schema metadata with --dsn"
    )
    parser.add_argument("--baseline")
    parser.add_argument("--write-baseline")
    parser.add_argument(
        "--max-files", type=int, default=MAX_FILES, help="Python file budget (1–10000)"
    )
    parser.add_argument(
        "--max-project-bytes",
        type=int,
        default=MAX_PROJECT_BYTES,
        help="source byte budget (1–64000000)",
    )


def command(args, out, err):
    try:
        report = scan(
            args.paths,
            dialect=args.dialect,
            max_files=getattr(args, "max_files", MAX_FILES),
            max_project_bytes=getattr(args, "max_project_bytes", MAX_PROJECT_BYTES),
        )
    except ValueError as exc:
        print(f"redline check-sql: {exc}", file=err)
        return 2
    if args.dsn:
        from .sql_compile import compile_paths

        try:
            report["compilation"] = compile_paths(
                args.paths, args.dsn, snapshot=args.schema_snapshot
            )
        except Exception:
            print(
                "Online compilation failed; verify the driver, development DSN and permissions",
                file=err,
            )
            return 2
        for item in report["compilation"]:
            report["findings"].extend(item.get("findings", ()))
            if item["status"] != "compiled":
                report["skipped"].append(
                    {
                        "file": item["file"],
                        "line": item["line"],
                        "reason": "Online compilation " + item["status"],
                    }
                )
        report["findings"].sort(key=lambda f: (f["file"], f["line"], f["kind"]))
    report["unresolved_by_reason"] = unresolved_by_reason(report["skipped"])
    baseline = set()
    if args.baseline:
        try:
            data = json.loads(Path(args.baseline).read_text())
            if not isinstance(data, list) or not all(isinstance(v, str) for v in data):
                raise ValueError
            baseline = set(data)
        except (OSError, ValueError):
            print("Invalid SQL baseline", file=err)
            return 2
    report["baseline_findings"] = sum(f["id"] in baseline for f in report["findings"])
    fresh = [f for f in report["findings"] if f["id"] not in baseline]
    if args.write_baseline:
        Path(args.write_baseline).write_text(
            json.dumps([f["id"] for f in report["findings"]], indent=2) + "\n"
        )
    if args.junit:
        suite = ET.Element(
            "testsuite",
            name="redline-check-sql",
            tests="0",
            failures=str(len(fresh)),
            errors=str(len(report["skipped"])),
        )
        for f in fresh:
            case = ET.SubElement(
                suite, "testcase", classname=f["file"], name=f"{f['line']}:{f['kind']}"
            )
            ET.SubElement(case, "failure", message=f["detail"])
        for f in report["skipped"]:
            case = ET.SubElement(suite, "testcase", classname=f["file"], name=str(f["line"]))
            ET.SubElement(case, "error", message=f["reason"])
        suite.set("tests", str(len(suite.findall("testcase"))))
        Path(args.junit).write_bytes(ET.tostring(suite, encoding="utf-8", xml_declaration=True))
    if args.json:
        print(json.dumps(report), file=out)
    else:
        for f in fresh:
            print(f"{f['file']}:{f['line']}: {f['kind']}: {f['detail']}", file=out)
        print(
            f"{report['statements']} statements; {len(fresh)} new findings; "
            f"{len(report['skipped'])} unresolved inputs",
            file=out,
        )
        for item in report.get("compilation", ()):
            selective = item.get("selective")
            if selective:
                print(
                    f"online: {selective['compiled']} compiled, "
                    f"{selective['skipped_unchanged']} skipped "
                    f"({selective['reason']})",
                    file=out,
                )
                break
    return 2 if report["skipped"] else 1 if fresh else 0
