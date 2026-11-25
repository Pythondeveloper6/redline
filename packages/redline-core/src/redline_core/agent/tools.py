"""The tools an agent may call.

Most tools only read. ``call_tool`` never raises: bad input and missing data become
``{"error": ...}`` so a model can read the message and correct itself. Output is bounded in
item count and string length, and secrets (credentials in URLs, tokens, header and identity
values) are masked on the way out.

The few tools that change anything (start or stop a load run, delete a run) are *gated*: they
are neither listed nor callable unless the operator switched them on when starting
``redline mcp`` (see :class:`~redline_core.agent.base.ServerOptions`).
"""

from __future__ import annotations

import json
import time
from typing import Any

from .audit import AuditLog
from .base import (
    MAX_ITEMS,
    OPTIONS,
    ServerOptions,
    Tool,
    clip,
    obj,
    validate,
)

__all__ = ["TOOLS", "ServerOptions", "Tool", "call_tool", "visible_tools"]

MAX_SPANS = 50
MAX_OUTPUT = 400_000


def _api(store: Any) -> Any:
    from .base import _api as make

    return make(store)


def _issues(store: Any, window: str, kind: str | None, sort: str | None) -> list[dict]:
    params: dict[str, str] = {"window": window}
    if kind:
        params["kind"] = kind
    if sort:
        params["sort"] = sort
    status, data = _api(store).dispatch("GET", "api/issues", params)
    if status != 200:
        raise ValueError(data.get("error", {}).get("message", "could not list issues"))
    return data["items"]


def _list_issues(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    limit = args.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"limit must be a whole number from 1 to {MAX_ITEMS}")
    items = _issues(store, str(args.get("window", "24h")), args.get("kind"), "impact")
    return {"items": items[:limit], "total": len(items), "sampled": True}


def _explain_issue(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    wanted = args.get("issue_id")
    for item in _issues(store, str(args.get("window", "7d")), None, "impact"):
        if item["id"] == wanted:
            return item
    raise ValueError(f"no issue {wanted!r} in the window; list issues first")


def _get_trace(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    status, data = _api(store).dispatch("GET", f"api/traces/{args.get('trace_id')}")
    if status != 200:
        raise ValueError(data.get("error", {}).get("message", "trace not found"))
    spans = []
    for s in (data.get("spans") or [])[:MAX_SPANS]:
        spans.append(
            {
                "kind": s.get("kind"),
                "name": s.get("name"),
                "duration_ms": s.get("duration_ms"),
                "callsite": s.get("callsite"),
            }
        )
    keys = ("id", "method", "path", "route", "status", "duration_ms", "query_count", "query_ms")
    out = {k: data.get(k) for k in keys}
    out["issues"] = [
        {k: i.get(k) for k in ("id", "kind", "severity", "title", "count")}
        for i in data.get("issues") or []
    ]
    out["spans"] = spans
    out["spans_total"] = len(data.get("spans") or [])
    return out


def _instrumentation(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..instrumentation_report import build_report

    return build_report(store)


def _verify_fix(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..check.report import Report, ReportError
    from ..check.verify import verify_report

    path = args.get("report_path")
    if not isinstance(path, str) or not path.endswith(".json"):
        raise ValueError("report_path must be the path of a compare report (.json)")
    try:
        with open(path, encoding="utf-8") as fh:
            report = Report.from_dict(json.load(fh))
    except (OSError, ValueError, ReportError) as exc:
        raise ValueError(f"could not read report: {exc}") from None
    min_samples = args.get("min_samples", 5)
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 0:
        raise ValueError("min_samples must be a whole number of 0 or more")
    return verify_report(
        report.requests, str(args["kind"]), args.get("fingerprint"), min_samples=min_samples
    )


def _llm_summary(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    status, data = _api(store).dispatch(
        "GET", "api/llm", {"window": str(args.get("window", "24h"))}
    )
    if status != 200:
        raise ValueError(data.get("error", {}).get("message", "could not summarise LLM calls"))
    return data


_WINDOW = {"type": "string", "description": "Look-back window such as 1h, 24h or 7d"}
BASE_TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_issues",
        "List performance findings recorded by redline, ranked by sampled impact. "
        "Each has why-it-fired evidence, callsite and a suggestion.",
        obj(
            {
                "window": _WINDOW,
                "kind": {"type": "string", "description": "Only this issue kind"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ITEMS},
            },
        ),
        _list_issues,
    ),
    Tool(
        "explain_issue",
        "Full detail for one finding id from list_issues.",
        obj({"issue_id": {"type": "string"}, "window": _WINDOW}, ("issue_id",)),
        _explain_issue,
    ),
    Tool(
        "get_trace",
        "Bounded summary of one recorded request: timings, issues and its slowest spans "
        "(no bodies, headers or logs).",
        obj({"trace_id": {"type": "string"}}, ("trace_id",)),
        _get_trace,
    ),
    Tool(
        "instrumentation_report",
        "What redline cannot see: inactive integrations, truncated traces, dropped writes.",
        obj({}),
        _instrumentation,
    ),
    Tool(
        "verify_fix",
        "After a change, read a saved `redline check compare --json` report and say whether "
        "a finding is gone, still_present or inconclusive, and whether responses stayed equal.",
        obj(
            {
                "report_path": {"type": "string"},
                "kind": {"type": "string"},
                "fingerprint": {"type": "string"},
                "min_samples": {"type": "integer", "minimum": 0},
            },
            ("report_path", "kind"),
        ),
        _verify_fix,
    ),
    Tool(
        "llm_summary",
        "Sampled LLM-call tokens and counts per route and model.",
        obj({"window": _WINDOW}),
        _llm_summary,
    ),
)


def _register() -> dict[str, Tool]:
    from .tools_analysis import ANALYSIS_TOOLS
    from .tools_runs import RUN_TOOLS

    tools: dict[str, Tool] = {t.name: t for t in BASE_TOOLS}
    for tool in (*RUN_TOOLS, *ANALYSIS_TOOLS):
        if tool.name in tools:
            raise RuntimeError(f"duplicate tool {tool.name}")
        tools[tool.name] = tool
    return tools


TOOLS: dict[str, Tool] = {}


def visible_tools(options: ServerOptions | None = None) -> list[Tool]:
    """The tools a server started with ``options`` offers (gated ones only when enabled)."""
    options = options or ServerOptions()
    return [t for t in TOOLS.values() if options.allows(t.gate)]


def call_tool(
    name: str,
    args: Any,
    *,
    store: Any,
    audit: AuditLog | None = None,
    options: ServerOptions | None = None,
) -> dict[str, Any]:
    """Run one tool; always returns a JSON-safe dict, ``{"error": message}`` on any failure."""
    start = time.perf_counter()
    ok = True
    options = options or ServerOptions()
    token = OPTIONS.set(options)
    try:
        tool = TOOLS.get(name)
        if tool is None or not options.allows(tool.gate):
            available = ", ".join(t.name for t in visible_tools(options))
            raise ValueError(f"unknown tool {str(name)[:60]!r}; available: {available}")
        validate(tool, args)
        result = clip(tool.run(store, args), max_str=tool.max_str)
        if len(json.dumps(result, default=str)) > MAX_OUTPUT:
            raise ValueError("the result is too large; narrow the request (window, limit, ids)")
    except Exception as exc:  # a tool must never take the server down
        ok = False
        result = {"error": str(exc)[:500] or type(exc).__name__}
    finally:
        OPTIONS.reset(token)
    if audit is not None:
        try:
            audit.record(str(name), args, ok, (time.perf_counter() - start) * 1000.0)
        except OSError:
            pass
    return result


TOOLS.update(_register())
