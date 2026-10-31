"""Explicit development commands; never run during request recording."""

from __future__ import annotations

import json
import subprocess
import sys


def configure_export(parser):
    parser.add_argument(
        "target",
        nargs="?",
        help="recording id or load run id to summarise (with --html)",
    )
    parser.add_argument(
        "--html",
        metavar="OUT",
        help="write one self-contained, redacted HTML summary of TARGET instead of OTLP",
    )
    parser.add_argument("--store", required=True)
    parser.add_argument("--endpoint")
    parser.add_argument(
        "--logs-endpoint",
        help="optional OTLP logs endpoint for recorded issues",
    )
    parser.add_argument(
        "--include-sql",
        action="store_true",
        help="include raw SQL text instead of the normalized fingerprint",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="send the displayed spans and logs to the explicit collectors",
    )
    parser.add_argument("--limit", type=int, default=100)


def _export_html(args, out, err):
    from . import report_html
    from .storage.multiple import TraceReader

    if args.send or args.endpoint or args.logs_endpoint:
        print("--html cannot be combined with --send or the OTLP endpoints", file=err)
        return 2
    if not args.target:
        print("--html needs a recording id or load run id", file=err)
        return 2
    try:
        reader = TraceReader(args.store)
    except (OSError, ValueError):
        print("Cannot open the store read-only", file=err)
        return 1
    try:
        redactor = report_html.project_redactor()
        trace = reader.get(args.target, include_profile=False)
        if trace is not None:
            page = report_html.render_trace(trace, redactor, include_sql=args.include_sql)
        else:
            run = report_html.load_run(reader, args.target)
            if run is None:
                print("No recording or load run with that id", file=err)
                return 1
            page = report_html.render_run(run, redactor)
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(page)
        print(f"wrote {args.html}", file=out)
        return 0
    except OSError as exc:
        print(f"Cannot write {args.html}: {exc.strerror or exc}", file=err)
        return 1
    finally:
        reader.close()


def export_command(args, out, err):
    from .otlp import export
    from .storage.multiple import TraceReader

    if args.html:
        return _export_html(args, out, err)
    if args.target:
        print("A target id needs --html", file=err)
        return 2

    if not 1 <= args.limit <= 500 or (args.send and not args.endpoint):
        print("Use limit 1..500 and an explicit traces endpoint for --send", file=err)
        return 2
    if args.logs_endpoint and not args.send:
        print("--logs-endpoint requires --send", file=err)
        return 2
    reader = TraceReader(args.store)
    try:
        traces = [reader.get(i["id"]) for i in reader.list(limit=args.limit).items]
        print(
            json.dumps(
                export(
                    traces,
                    args.endpoint,
                    dry_run=not args.send,
                    logs_endpoint=args.logs_endpoint,
                    include_sql=args.include_sql,
                )
            ),
            file=out,
        )
        return 0
    except Exception:
        print("Export failed; verify store, collector and protocol support", file=err)
        return 1
    finally:
        reader.close()


def configure_audit(parser):
    parser.add_argument("--store", required=True)
    parser.add_argument("--limit", type=int, default=200)


def audit_command(args, out, err):
    from .conf import Settings
    from .privacy import PROFILES
    from .redact import Redactor
    from .storage.multiple import TraceReader

    if not 1 <= args.limit <= 1000:
        print("Use limit 1..1000", file=err)
        return 2
    reader = TraceReader(args.store)
    counts = dict.fromkeys(PROFILES, 0)
    redactors = {
        name: Redactor(Settings(redact_keys=(), redact_profiles=(name,))) for name in PROFILES
    }

    def inspect(value):
        if isinstance(value, str) and value[:1] in {"{", "["}:
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return
        if isinstance(value, dict):
            for key, item in value.items():
                for name, redactor in redactors.items():
                    if redactor.is_secret_key(str(key)) and item not in (None, "[redacted]"):
                        counts[name] += 1
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    try:
        page = reader.list(limit=min(args.limit, 500))
        for item in page.items:
            inspect(reader.get(item["id"], include_profile=False).to_dict())
        print(
            json.dumps(
                {
                    "recordings_checked": len(page.items),
                    "possible_unmasked_keys": counts,
                    "limited": page.next_cursor is not None,
                    "note": "Counts only. Key-pattern matches are candidates, "
                    "not a privacy certification.",
                }
            ),
            file=out,
        )
        return 1 if any(counts.values()) else 0
    finally:
        reader.close()


def configure_imports(parser):
    parser.add_argument(
        "target", help="module or module:attribute to import in a development subprocess"
    )
    parser.add_argument("--timeout", type=int, default=15)


def imports_command(args, out, err):
    import re

    if (
        not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?::[A-Za-z_]\w*)?", args.target)
        or not 1 <= args.timeout <= 60
    ):
        print("Use module[:attribute] and timeout 1..60", file=err)
        return 2
    code = """import ast, contextlib, importlib, importlib.util, inspect, json, os, sys
original = sys.stdout
with (open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink),
      contextlib.redirect_stderr(sink)):
    lazy_errors, lazy_modules, source_errors = [], [], []
    def lazy_imports(tree):
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            parent = parents.get(node)
            inside_function = False
            while parent is not None:
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    inside_function = True
                    break
                parent = parents.get(parent)
            if inside_function:
                found.append(node)
        return found
    try:
        module, _, attr = sys.argv[1].partition(':')
        value = importlib.import_module(module)
        if attr: value = getattr(value, attr)
        tasks = getattr(value, 'tasks', {})
        modules = []
        for task in tasks.values():
            name = getattr(task, '__module__', None)
            if name and name not in modules: modules.append(name)
        for name in modules:
            try:
                task_module = importlib.import_module(name)
                source = inspect.getsource(task_module)
                tree = ast.parse(source)
            except BaseException as exc:
                lazy_errors.append({'module': name, 'error_type': type(exc).__name__})
                continue
            package = getattr(task_module, '__package__', None) or name.rpartition('.')[0]
            for node in lazy_imports(tree):
                targets = []
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif node.module != '__future__':
                    target = node.module or ''
                    if node.level:
                        try:
                            target = importlib.util.resolve_name('.' * node.level + target, package)
                        except (ImportError, ValueError) as exc:
                            lazy_errors.append({
                                'module': target or '.', 'error_type': type(exc).__name__
                            })
                            continue
                    if target: targets = [target]
                for target in targets:
                    if target in lazy_modules: continue
                    lazy_modules.append(target)
                    try:
                        importlib.import_module(target)
                    except BaseException as exc:
                        lazy_errors.append({'module': target, 'error_type': type(exc).__name__})
        result = {'imported': True, 'registered_tasks': len(tasks),
                  'lazy_modules': lazy_modules, 'lazy_errors': lazy_errors[:50],
                  'source_errors': source_errors[:50],
                  'limitation': 'Only statically visible imports inside task functions are run; '
                                'runtime branches, dynamic imports and task execution are not.'}
    except BaseException as exc:
        result = {'imported': False, 'error_type': type(exc).__name__, 'lazy_modules': [],
                  'lazy_errors': [], 'source_errors': []}
print(json.dumps(result), file=original)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, args.target],
            capture_output=True,
            timeout=args.timeout,
            text=True,
        )
        data = json.loads(result.stdout)
        print(json.dumps(data), file=out)
        return 0 if data.get("imported") and not data.get("lazy_errors") else 1
    except (subprocess.TimeoutExpired, ValueError):
        print("Import check failed or timed out; application import output was withheld", file=err)
        return 1
