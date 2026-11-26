"""Agent tools for the rest of redline: recorded traffic, SQL and plan analysis, database
capabilities, configuration checks, refactor-check reports and fix briefs.

They reuse the code behind the CLI and the HTTP API. None of them executes SQL, sends a
request to an application or writes to the store; the plan tool analyses text it is given.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .base import (
    MAX_ITEMS,
    Tool,
    api_get,
    api_post,
    obj,
    resolve_local,
)

__all__ = ["ANALYSIS_TOOLS"]

_WINDOW = {"type": "string", "maxLength": 10, "description": "Look-back window such as 1h, 24h, 7d"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_ITEMS}
_DIALECT = {"type": "string", "enum": ["other", "mssql", "postgresql", "mysql", "sqlite"]}
_SHORT = {"type": "string", "maxLength": 200}
MAX_PLAN_CHARS = 800_000
MAX_SOURCE_CHARS = 200_000


def _params(args: dict[str, Any], *names: str) -> dict[str, str]:
    return {n: str(args[n]) for n in names if args.get(n) is not None}


def _list_of(store: Any, path: str, args: dict[str, Any], *names: str) -> dict[str, Any]:
    data = api_get(store, path, _params(args, *names))
    items = data.get("items")
    if isinstance(items, list):
        limit = args.get("limit", MAX_ITEMS)
        return {**data, "items": items[:limit], "items_total": len(items)}
    return data


# ------------------------------------------------------------------ recorded traffic
def _list_traces(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    params = _params(
        args, "q", "route", "method", "status", "min_ms", "min_queries", "has_issues", "limit"
    )
    params.setdefault("limit", "20")
    data = api_get(store, "api/traces", params)
    return {"items": data.get("items", []), "next_cursor": data.get("next_cursor")}


def _endpoints(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return _list_of(store, "api/endpoints", args, "window", "group_by")


def _compare_traces(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return api_get(store, "api/compare", _params(args, "a", "b"))


def _search_sql(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return _list_of(store, "api/sql", args, "q", "window", "limit")


def _index_advice(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return api_get(store, "api/indexes", _params(args, "window", "db"))


def _cache_summary(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return _list_of(store, "api/cache", args, "window")


def _external_summary(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return _list_of(store, "api/external", args, "window")


def _job_summary(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return api_get(store, "api/jobs/summary", _params(args, "window", "limit"))


def _history(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return api_get(store, "api/history", _params(args, "window", "bucket", "route", "method"))


def _compare_periods(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return api_get(store, "api/history/compare", _params(args, "a", "b"))


def _releases(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return _list_of(store, "api/releases", args)


# ------------------------------------------------------------------ database and plans
def _database_capabilities(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return api_get(store, "api/database/capabilities", _params(args, "db"))


def _analyze_sql_plan(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    body = {"vendor": args.get("vendor", "mssql"), "plan": args["plan"]}
    if args.get("sql"):
        body["sql"] = args["sql"]
    result = api_post(store, "api/explain/plan", body)
    result.pop("plan", None)  # the caller already has it; it may also hold literal values
    result["suggested_indexes"] = result.get("suggested_indexes", [])[:20]
    return result


_SQL_TEXT = {"type": "string", "minLength": 1, "maxLength": 100_000}


def _lint_sql(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..check_sql import RULES, lint

    dialect = args.get("dialect", "other")
    rules = lint(args["sql"], dialect)
    return {
        "dialect": dialect,
        "findings": [{"kind": r, "detail": RULES[r]} for r in rules],
        "note": "Offline candidates only; schema and triggers are not checked.",
    }


def _scan_sql_source(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..check_sql import analyze_sql_source

    return analyze_sql_source(args["source"], dialect=args.get("dialect", "other"))


def _scan_sql_project(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..check_sql import scan

    root = resolve_local(args["path"], what="project directory", directory=True)
    result = scan([root], dialect=args.get("dialect", "other"))
    cwd = str(Path(os.getcwd()).resolve()) + os.sep
    for item in (*result.get("findings", []), *result.get("skipped", [])):
        if isinstance(item.get("file"), str):
            item["file"] = item["file"].removeprefix(cwd)
    return result


def _analyze_source(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..analyze import analyze_source

    return analyze_source(args["source"], args.get("file", "input.py"))


def _analyze_project(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..analyze import analyze_project

    return analyze_project(resolve_local(args["path"], what="project directory", directory=True))


# ------------------------------------------------------------------ configuration
def _doctor(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    import os

    from .. import cli
    from ..conf import SettingsError
    from ..config import resolve_settings

    try:
        resolved = resolve_settings(None, project_root=os.getcwd())
    except SettingsError:
        return {
            "ok": False,
            "checks": [
                {"status": "fail", "check": "config", "detail": "configuration does not load"}
            ],
        }
    checks = cli._doctor_checks(resolved, strict=bool(args.get("strict", False)))
    if args.get("database", False):
        checks.extend(cli._database_checks(resolved))
    if args.get("production", False):
        from ..production_checks import production_checks

        checks.extend(production_checks(resolved))
    return {
        "preset": resolved.settings.preset,
        "ok": not any(c[0] == "fail" for c in checks),
        "checks": [{"status": s, "check": c, "detail": d} for s, c, d in checks],
    }


# ------------------------------------------------------------------ refactor-check reports
def _load_report(path: str) -> Any:
    from ..check.report import Report, ReportError
    from .base import read_local_file

    raw = read_local_file(path, (".json",), what="report")  # inside the working directory only
    try:
        return Report.from_dict(json.loads(raw))
    except (ValueError, ReportError):
        raise ValueError("the file is not a compare report") from None


def _check_report(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    report = _load_report(args["report_path"])
    failing = {e.get("name"): hits for e, hits in report.failures()}
    return {
        "id": report.id,
        "status": report.status,
        "headline": report.headline(),
        "exit_code": report.exit_code(),
        "fail_on": list(report.fail_on),
        "summary": report.summary,
        "requests": [
            {
                "name": e.get("name"),
                "method": e.get("method"),
                "path": e.get("path"),
                "perf": (e.get("perf") or {}).get("verdict"),
                "change_pct": (e.get("perf") or {}).get("change_pct"),
                "response": (e.get("response") or {}).get("verdict"),
                "failing": failing.get(e.get("name"), []),
            }
            for e in report.requests[:MAX_ITEMS]
        ],
        "requests_total": len(report.requests),
        "ruff": report.extra.get("ruff", [])[:20],
    }


def _check_findings(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from .fixprompt import new_findings

    findings = new_findings(_load_report(args["report_path"]).requests)
    return {"findings": findings[:MAX_ITEMS], "findings_total": len(findings)}


# ------------------------------------------------------------------ fix brief, diagnosis preview
def _issue(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from .tools import TOOLS

    return TOOLS["explain_issue"].run(store, args)


def _fix_prompt(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from .fixprompt import build_fix_prompt

    group = _issue(store, args)
    return {"issue_id": group["id"], "prompt": build_fix_prompt(group)}


def _diagnose_preview(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..conf import Settings
    from ..diagnose import build_payload, preview

    settings = Settings()
    payload = build_payload(_issue(store, args))
    return {
        "sent": False,
        "preview": preview(payload, settings.ai_provider, settings.ai_model),
        "note": "Nothing was sent. Only the owner may enable ai_diagnosis and set the key.",
    }


_ISSUE_ARGS = {"issue_id": {"type": "string", "minLength": 1, "maxLength": 100}, "window": _WINDOW}
_REPORT = {"type": "string", "minLength": 1, "maxLength": 500, "description": "A compare report"}

ANALYSIS_TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_traces",
        "Recorded requests, newest first, with duration, query count and issue count. "
        "Filter by route, method, status (2xx or 404), min_ms, min_queries, has_issues.",
        obj(
            {
                "q": _SHORT,
                "route": _SHORT,
                "method": {"type": "string", "maxLength": 10},
                "status": {"type": "string", "maxLength": 3},
                "min_ms": {"type": "number", "minimum": 0},
                "min_queries": {"type": "integer", "minimum": 0},
                "has_issues": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ITEMS},
            }
        ),
        _list_traces,
    ),
    Tool(
        "list_endpoints",
        "Per-route latency percentiles, error rate and query counts over a window.",
        obj({"window": _WINDOW, "group_by": _SHORT, "limit": _LIMIT}),
        _endpoints,
    ),
    Tool(
        "compare_traces",
        "Compare two recorded requests (trace ids a and b): time by kind and SQL that "
        "appeared, vanished or changed.",
        obj({"a": _SHORT, "b": _SHORT}, ("a", "b")),
        _compare_traces,
    ),
    Tool(
        "search_sql",
        "Normalised SQL statements seen in a window, with counts and timings; q filters text.",
        obj({"q": _SHORT, "window": _WINDOW, "limit": _LIMIT}),
        _search_sql,
    ),
    Tool(
        "index_advice",
        "Index suggestions from the recorded SQL workload. Offline: it does not connect to "
        "a database.",
        obj({"window": _WINDOW, "db": _SHORT}),
        _index_advice,
    ),
    Tool(
        "cache_summary",
        "Cache hit and miss counts per cache and operation over a window.",
        obj({"window": _WINDOW, "limit": _LIMIT}),
        _cache_summary,
    ),
    Tool(
        "external_summary",
        "Outbound HTTP call counts and latency per host over a window.",
        obj({"window": _WINDOW, "limit": _LIMIT}),
        _external_summary,
    ),
    Tool(
        "job_summary",
        "Background task outcomes and the first failing stage over a window.",
        obj({"window": _WINDOW, "limit": _LIMIT}),
        _job_summary,
    ),
    Tool(
        "history",
        "Latency and error history of a route (or all routes) in hourly or daily buckets.",
        obj(
            {
                "window": _WINDOW,
                "bucket": {"type": "string", "enum": ["1h", "1d"]},
                "route": _SHORT,
                "method": {"type": "string", "maxLength": 10},
            }
        ),
        _history,
    ),
    Tool(
        "compare_periods",
        "Compare two time periods (for example before and after a release): new, fixed and "
        "regressed findings and latency shifts. A period is release:NAME or "
        "range:START-END (epoch seconds).",
        obj({"a": _SHORT, "b": _SHORT}, ("a", "b")),
        _compare_periods,
    ),
    Tool(
        "list_releases",
        "Release markers recorded in the store.",
        obj({"limit": _LIMIT}),
        _releases,
    ),
    Tool(
        "database_capabilities",
        "What the database login used for plans and index data may do (SQL Server: plan cache, "
        "index usage, definitions), with the grant to ask for. This server holds no database "
        "connection, so it answers supported=false unless the store's process has one.",
        obj({"db": _SHORT}),
        _database_capabilities,
    ),
    Tool(
        "analyze_sql_plan",
        "Analyse a SQL Server XML execution plan you paste (nothing is executed or "
        "connected): operator tree as text, findings such as scans, key lookups and "
        "spills, and suggested indexes. Optionally pass the query text.",
        obj(
            {
                "plan": {"type": "string", "minLength": 1, "maxLength": MAX_PLAN_CHARS},
                "sql": {"type": "string", "maxLength": 100_000},
                "vendor": {"type": "string", "enum": ["mssql"]},
            },
            ("plan",),
        ),
        _analyze_sql_plan,
        max_str=8000,
    ),
    Tool(
        "lint_sql",
        "Offline lint of one SQL statement: bare OUTPUT, session options left on, "
        "INSERT SELECT *, bind markers in comments.",
        obj({"sql": _SQL_TEXT, "dialect": _DIALECT}, ("sql",)),
        _lint_sql,
    ),
    Tool(
        "scan_sql_source",
        "Find SQL statements in Python source text and lint them (offline, nothing runs).",
        obj(
            {
                "source": {"type": "string", "minLength": 1, "maxLength": MAX_SOURCE_CHARS},
                "dialect": _DIALECT,
            },
            ("source",),
        ),
        _scan_sql_source,
    ),
    Tool(
        "scan_sql_project",
        "Lint the SQL found in the Python files of a directory inside the working directory.",
        obj({"path": {"type": "string", "maxLength": 500}, "dialect": _DIALECT}, ("path",)),
        _scan_sql_project,
    ),
    Tool(
        "analyze_source",
        "Static performance review of Python source text: ORM and query patterns that tend "
        "to cause N+1 or large reads (offline, nothing runs).",
        obj(
            {
                "source": {"type": "string", "minLength": 1, "maxLength": MAX_SOURCE_CHARS},
                "file": {"type": "string", "maxLength": 200},
            },
            ("source",),
        ),
        _analyze_source,
    ),
    Tool(
        "analyze_project",
        "Static performance review of the Python files of a directory inside the working "
        "directory.",
        obj({"path": {"type": "string", "maxLength": 500}}, ("path",)),
        _analyze_project,
    ),
    Tool(
        "doctor",
        "Check redline's own setup in the working directory: storage, UI bundle, access, "
        "privacy and deployment shape. database=true also probes the configured plan login "
        "(read-only metadata); production=true adds the production checklist.",
        obj(
            {
                "strict": {"type": "boolean"},
                "database": {"type": "boolean"},
                "production": {"type": "boolean"},
            }
        ),
        _doctor,
    ),
    Tool(
        "check_report",
        "Summarise a saved `redline check compare --json` report: headline, verdict per "
        "request (faster/slower, changed/broken), failing requests and query counts.",
        obj({"report_path": _REPORT}, ("report_path",)),
        _check_report,
    ),
    Tool(
        "check_findings",
        "Findings that are new in a saved compare report, each with a fix brief.",
        obj({"report_path": _REPORT}, ("report_path",)),
        _check_findings,
    ),
    Tool(
        "fix_prompt",
        "A Markdown brief for fixing one finding: evidence, constraints and how to verify.",
        obj(_ISSUE_ARGS, ("issue_id",)),
        _fix_prompt,
        max_str=12_100,
    ),
    Tool(
        "diagnose_preview",
        "Show exactly what `redline diagnose --send` would transmit for a finding. It never "
        "sends anything.",
        obj(_ISSUE_ARGS, ("issue_id",)),
        _diagnose_preview,
        max_str=6000,
    ),
)
