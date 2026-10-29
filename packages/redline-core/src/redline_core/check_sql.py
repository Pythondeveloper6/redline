"""Bounded SQL extraction and linting. Application modules are never imported."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from .analyze import MAX_SOURCE_BYTES
from .sql import _TOKEN, comment_placeholders, effective_kinds, fingerprint

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


def _constant(node, constants, depth=0):
    if depth > 12:
        raise ValueError("constant expression too deep")
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
        if isinstance(node.value, str) and len(node.value.encode()) > MAX_SOURCE_BYTES:
            raise ValueError("constant SQL exceeds source limit")
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        value = _constant(node.left, constants, depth + 1) + _constant(
            node.right, constants, depth + 1
        )
        if isinstance(value, str) and len(value.encode()) <= MAX_SOURCE_BYTES:
            return value
    if isinstance(node, ast.JoinedStr):
        parts = []
        size = 0
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                if value.format_spec is not None or value.conversion != -1:
                    raise ValueError("formatted dynamic SQL")
                value = value.value
            part = str(_constant(value, constants, depth + 1))
            size += len(part.encode())
            if size > MAX_SOURCE_BYTES:
                raise ValueError("constant SQL exceeds source limit")
            parts.append(part)
        return "".join(parts)
    raise ValueError("dynamic SQL")


def extract(source, file="<source>"):
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return [], [{"file": file, "line": 1, "reason": "source limit exceeded"}]
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as exc:
        return [], [
            {"file": file, "line": getattr(exc, "lineno", 1), "reason": "invalid Python source"}
        ]
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    unknown = object()
    constants, history = {}, {}
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
                value = _constant(node.value, constants)
            except (ValueError, TypeError):
                value = unknown
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names, value = {node.name}, unknown
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {alias.asname or alias.name.split(".")[0] for alias in node.names}
            value = unknown
        else:
            names = {
                part.id
                for part in ast.walk(node)
                if isinstance(part, ast.Name) and isinstance(part.ctx, (ast.Store, ast.Del))
            }
            value = unknown
        for name in names:
            history.setdefault(name, []).append((index, value))
            if value is unknown:
                constants.pop(name, None)
            else:
                constants[name] = value
    dynamic_globals = {
        name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names
    }
    stable_constants = {
        name: changes[0][1]
        for name, changes in history.items()
        if len(changes) == 1 and changes[0][1] is not unknown and name not in dynamic_globals
    }
    aliases = {"text"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in {
            "sqlalchemy",
            "sqlalchemy.sql",
            "sqlalchemy.sql.expression",
        }:
            aliases.update(n.asname or n.name for n in node.names if n.name == "text")
    statements, skipped, seen = [], [], set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if name not in aliases | {"text", "execute", "executemany", "exec_driver_sql"}:
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
            if any(
                isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                for scope in scopes
            ):
                active_constants = {
                    name: stable_constants[name] for name in names if name in stable_constants
                }
            else:
                active_constants = {}
                position = module_positions.get(top, -1)
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
            for scope in scopes:
                bound = {
                    n.id
                    for n in ast.walk(scope)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
                }
                if hasattr(scope, "args"):
                    bound.update(n.arg for n in ast.walk(scope.args) if isinstance(n, ast.arg))
                active_constants = {k: v for k, v in active_constants.items() if k not in bound}
            value = _constant(arg, active_constants)
            if not isinstance(value, str) or not effective_kinds(value):
                continue
        except (ValueError, TypeError):
            skipped.append({"file": file, "line": node.lineno, "reason": "dynamic SQL"})
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
        statements.append({"file": file, "line": arg.lineno, "sql": value, "bindings": bindings})
    return statements, skipped


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
        "statements": len(statements),
        "note": "Offline candidates only; schema and triggers are not checked.",
    }


def scan(paths, *, dialect="other"):
    result = {"findings": [], "skipped": [], "statements": 0, "files": 0}
    visited, byte_count, entries = set(), 0, 0
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
                if resolved in visited:
                    continue
                visited.add(resolved)
                if len(visited) > 200 or byte_count >= 2_000_000:
                    result["skipped"].append(
                        {"file": str(root), "line": 1, "reason": "project limit exceeded"}
                    )
                    return result
                with path.open("rb") as stream:
                    raw = stream.read(MAX_SOURCE_BYTES + 1)
                byte_count += len(raw)
                source = raw.decode("utf-8")
                report = analyze_sql_source(source, file=str(path), dialect=dialect)
                result["findings"].extend(report["findings"])
                result["skipped"].extend(report["skipped"])
                result["statements"] += report["statements"]
                result["files"] += 1
        except (OSError, UnicodeError, RecursionError, ValueError):
            result["skipped"].append(
                {"file": str(root), "line": 1, "reason": "unreadable or oversized project"}
            )
    result["findings"].sort(key=lambda f: (f["file"], f["line"], f["kind"]))
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


def command(args, out, err):
    report = scan(args.paths, dialect=args.dialect)
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
            if item["status"] != "compiled":
                report["skipped"].append(
                    {
                        "file": item["file"],
                        "line": item["line"],
                        "reason": "Online compilation " + item["status"],
                    }
                )
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
    return 2 if report["skipped"] else 1 if fresh else 0
