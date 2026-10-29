"""Explicit development commands; never run during request recording."""

from __future__ import annotations

import json
import subprocess
import sys


def configure_export(parser):
    parser.add_argument("--store", required=True)
    parser.add_argument("--endpoint")
    parser.add_argument(
        "--send",
        action="store_true",
        help="send the displayed summary format to the explicit collector",
    )
    parser.add_argument("--limit", type=int, default=100)


def export_command(args, out, err):
    from .otlp import export
    from .storage.multiple import TraceReader

    if not 1 <= args.limit <= 500 or (args.send and not args.endpoint):
        print("Use limit 1..500 and an explicit endpoint for --send", file=err)
        return 2
    reader = TraceReader(args.store)
    try:
        traces = [reader.get(i["id"]) for i in reader.list(limit=args.limit).items]
        print(json.dumps(export(traces, args.endpoint, dry_run=not args.send)), file=out)
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
    code = """import contextlib, importlib, json, os, sys
original = sys.stdout
with (open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink),
      contextlib.redirect_stderr(sink)):
    try:
        module, _, attr = sys.argv[1].partition(':')
        value = importlib.import_module(module)
        if attr: value = getattr(value, attr)
        tasks = getattr(value, 'tasks', {})
        for task in tasks.values(): importlib.import_module(task.__module__)
        result = {'imported': True, 'registered_tasks': len(tasks),
                  'limitation': 'Lazy imports and execution branches were not run.'}
    except BaseException as exc:
        result = {'imported': False, 'error_type': type(exc).__name__}
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
        return 0 if data.get("imported") else 1
    except (subprocess.TimeoutExpired, ValueError):
        print("Import check failed or timed out; application import output was withheld", file=err)
        return 1
