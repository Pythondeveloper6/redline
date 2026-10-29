"""Watch a development schema for drift and optionally persist a UI issue."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


def run_once(paths, dsn, snapshot, *, full=False):
    """Compile once and return a bounded report; never stores credentials or rows."""
    from .sql_compile import compile_paths

    existed = Path(snapshot).exists()
    reports = compile_paths(paths, dsn, snapshot=snapshot, selective=not full)
    snapshot_item = next((item for item in reports if "schema_drift" in item), None)
    if snapshot_item is None:
        raise RuntimeError("Schema snapshot was not produced")
    drift = snapshot_item["schema_drift"]
    errors = [item for item in reports if item.get("status") == "error"]
    changed = bool(
        drift["affected_tables"]
        or drift["added_routines"]
        or drift["removed_routines"]
        or drift["changed_routines"]
        or errors
    )
    return {
        "baseline": not existed,
        "clean": not changed,
        "alert": bool(existed and changed),
        "drift": drift,
        "compiled": snapshot_item.get("selective", {}).get("compiled", 0),
        "skipped_unchanged": snapshot_item.get("selective", {}).get("skipped_unchanged", 0),
        "errors": errors,
        "snapshot": str(snapshot),
        "limitation": (
            "Watches default-schema metadata only; it does not run migrations or prove "
            "that every application query was extracted."
        ),
    }


def _issue(report):
    from .model import Issue, Trace, new_id

    drift = report["drift"]
    affected = list(drift["affected_tables"])[:20]
    routines = sorted(
        set(drift["added_routines"])
        | set(drift["removed_routines"])
        | set(drift["changed_routines"])
    )[:20]
    identity = json.dumps(
        {
            "tables": affected,
            "triggers": drift["changed_triggers"],
            "routines": routines,
            "errors": len(report["errors"]),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    severity = "critical" if report["errors"] else "warning"
    title = (
        "Schema drift: " + ", ".join(affected[:3])
        if affected
        else "Schema drift in trigger or routine metadata"
    )
    detail = (
        f"Added tables: {', '.join(drift['added_tables']) or 'none'}; "
        f"removed: {', '.join(drift['removed_tables']) or 'none'}; "
        f"changed: {', '.join(drift['changed_tables']) or 'none'}; "
        f"trigger changes: {', '.join(drift['changed_triggers']) or 'none'}; "
        f"routine changes: {', '.join(routines) or 'none'}; "
        f"compile errors: {len(report['errors'])}."
    )
    issue = Issue(
        id="schema-watch:" + digest[:16],
        kind="schema_drift",
        severity=severity,
        title=title,
        detail=detail[:2000],
        fingerprint=digest,
        extra={
            "affected_tables": affected,
            "changed_triggers": list(drift["changed_triggers"])[:20],
            "changed_routines": routines,
            "compile_errors": len(report["errors"]),
        },
    )
    trace = Trace(
        id=new_id(),
        framework="other",
        method="TASK",
        path="redline.schema-watch",
        route="redline.schema-watch",
        status=500 if report["errors"] else 200,
        started_at=time.time(),
        tags={"source": "schema-watch", "snapshot": os.path.basename(report["snapshot"])},
        issues=[issue],
    )
    return trace


def persist(store_path, report):
    """Write one synthetic TASK trace carrying the drift issue; metadata only."""
    from .storage.sqlite import SQLiteStore

    store = SQLiteStore(store_path, background=False)
    try:
        store.save(_issue(report))
    finally:
        store.close()


def _print(report, out):
    drift = report["drift"]
    if report["baseline"]:
        print(f"schema baseline written to {report['snapshot']}", file=out)
        return
    if report["clean"]:
        print("schema unchanged", file=out)
        return
    for key in ("added_tables", "removed_tables", "changed_tables", "changed_triggers"):
        values = drift[key]
        if values:
            print(f"{key.replace('_', ' ')}: {', '.join(values[:20])}", file=out)
    for key in ("added_routines", "removed_routines", "changed_routines"):
        values = drift[key]
        if values:
            print(f"{key.replace('_', ' ')}: {', '.join(values[:20])}", file=out)
    print(
        f"{report['compiled']} statements compiled; {len(report['errors'])} compile errors",
        file=out,
    )


def configure_cli(parser):
    parser.add_argument("paths", nargs="+", help="explicit Python files to compile")
    parser.add_argument("--dsn", required=True, help="isolated development SQLAlchemy DSN")
    parser.add_argument("--snapshot", required=True, help="local schema snapshot path")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="seconds between checks; 0 (default) checks once",
    )
    parser.add_argument("--full", action="store_true", help="compile every statement")
    parser.add_argument(
        "--store",
        help="persist a schema_drift issue to this SQLite store for the UI",
    )
    parser.add_argument("--json", action="store_true")


def command(args, out, err):
    if args.interval < 0:
        print("--interval must be zero or positive", file=err)
        return 2
    while True:
        try:
            report = run_once(
                args.paths,
                args.dsn,
                args.snapshot,
                full=args.full,
            )
        except Exception:
            print(
                "Schema watch failed; verify the driver, development DSN and permissions",
                file=err,
            )
            return 2
        if args.json:
            print(json.dumps(report), file=out)
        else:
            _print(report, out)
        if args.store and report["alert"]:
            try:
                persist(args.store, report)
            except Exception:
                print("Could not persist the schema drift issue", file=err)
                return 2
        if args.interval <= 0:
            return 1 if report["alert"] else 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
