"""Agent tools for load testing: read runs, check and generate scenarios, and (only when the
operator allows it) start, stop and delete runs.

Reading a run never returns request bodies, header or cookie values, identity values or
credentials in URLs; scenarios are parsed and described, never run, by the read-only tools.
"""

from __future__ import annotations

import json
from typing import Any

from .base import (
    MAX_ITEMS,
    Tool,
    api_get,
    current_options,
    obj,
    read_local_file,
    safe_url,
)

__all__ = ["RUN_TOOLS"]

_RUN_ID = {"type": "string", "minLength": 1, "maxLength": 64, "description": "A load run id"}
_SCENARIO = {"type": "object", "description": "A scenario as a JSON object (see redline load)"}
_PATH = {
    "type": "string",
    "maxLength": 500,
    "description": "A file inside the working directory",
}
_MAX_REQUESTS = 100
#: scenario keys that make the load process read local files or import code
_LOCAL_ONLY_KEYS = ("metric_sources", "identities_file", "data")


# ------------------------------------------------------------------ reading runs
def _safe_request(req: Any) -> dict[str, Any]:
    if not isinstance(req, dict):
        return {}
    path = str(req.get("path") or "")
    path, _, query = path.partition("?")
    out: dict[str, Any] = {
        "name": req.get("name"),
        "method": req.get("method"),
        "path": path,
        "weight": req.get("weight"),
    }
    if query:
        out["query_names"] = sorted({q.split("=", 1)[0] for q in query.split("&") if q})[:20]
    if req.get("headers"):
        out["header_names"] = sorted(map(str, req["headers"]))[:50]
    if "json" in req or req.get("body") is not None:
        out["has_body"] = True
    for key in ("expect_status", "max_ms"):
        if req.get(key) is not None:
            out[key] = req[key]
    return {k: v for k, v in out.items() if v is not None}


def _safe_config(config: Any) -> dict[str, Any]:
    """A run's stored scenario with every value that could be a secret taken out."""
    if not isinstance(config, dict):
        return {}
    out: dict[str, Any] = {
        k: config[k]
        for k in (
            "name",
            "timeout_s",
            "connections",
            "max_inflight",
            "workers",
            "seed",
            "identity_mode",
            "profile",
        )
        if k in config
    }
    out["base_url"] = safe_url(config.get("base_url"))
    requests = [r for r in config.get("requests") or [] if isinstance(r, dict)]
    out["requests"] = [_safe_request(r) for r in requests[:_MAX_REQUESTS]]
    out["requests_total"] = len(requests)
    flows = [f for f in config.get("flows") or [] if isinstance(f, dict)]
    if flows:
        out["flows"] = [
            {
                "name": f.get("name"),
                "weight": f.get("weight"),
                "steps": [_safe_request(s) for s in (f.get("steps") or [])[:50]],
            }
            for f in flows[:50]
        ]
    if config.get("headers"):
        out["header_names"] = sorted(map(str, config["headers"]))[:50]
    identities = config.get("identities") or []
    if identities:
        out["identities"] = [
            str(i.get("name") or f"identity {n + 1}")
            if isinstance(i, dict)
            else f"identity {n + 1}"
            for n, i in enumerate(identities[:200])
        ]
    if config.get("auth"):
        auth = config["auth"]
        kind = auth.get("type") or auth.get("kind") if isinstance(auth, dict) else None
        out["auth"] = {"configured": True, "type": str(kind) if kind else None}
    return out


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    steps = run.get("steps") or []
    return {
        "id": run.get("id"),
        "name": run.get("name"),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "config": _safe_config(run.get("config")),
        "steps": steps[:MAX_ITEMS],
        "steps_total": len(steps),
        "summary": run.get("summary") or {},
    }


def _list_load_runs(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    data = api_get(store, "api/runs", {"limit": str(args.get("limit", 20))})
    items = []
    for run in data["items"]:
        summary = run.get("summary") or {}
        items.append(
            {
                "id": run["id"],
                "name": run.get("name"),
                "status": run.get("status"),
                "created_at": run.get("created_at"),
                "finished_at": run.get("finished_at"),
                "steps": len(run.get("steps") or []),
                "knee_step": summary.get("knee_step"),
                "max_ok_rps": summary.get("max_ok_rps"),
                "verdict": summary.get("verdict"),
            }
        )
    return {"items": items}


def _get_load_run(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return _run_view(api_get(store, f"api/runs/{args['run_id']}"))


def _load_run_summary(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    run = api_get(store, f"api/runs/{args['run_id']}")
    summary = run.get("summary") or {}
    return {
        "id": run["id"],
        "name": run.get("name"),
        "status": run.get("status"),
        "summary": summary,
        "sections": sorted(
            k
            for k in ("flows", "recovery", "drift", "identities", "limiting_resource", "resources")
            if k in summary
        ),
    }


def _load_run_bottlenecks(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    data = api_get(store, f"api/runs/{args['run_id']}/bottlenecks")
    items = data.get("items") or []
    limit = args.get("limit", 20)
    return {**data, "items": items[:limit], "items_total": len(items)}


def _load_run_metrics(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return api_get(store, f"api/runs/{args['run_id']}/metrics")


# ------------------------------------------------------------------ scenarios
def _scenario_summary(sc: Any) -> dict[str, Any]:
    profile = sc.profile
    out: dict[str, Any] = {
        "name": sc.name,
        "base_url": safe_url(sc.base_url),
        "requests": [
            {
                "name": r.name,
                "method": r.method,
                "path": r.path.split("?", 1)[0],
                "weight": r.weight,
            }
            for r in sc.requests[:50]
        ],
        "requests_total": len(sc.requests),
        "flows": [{"name": f.name, "steps": len(f.steps), "weight": f.weight} for f in sc.flows],
        "profile": profile.describe(),
        "mode": profile.mode,
        "steps": profile.step_count(),
        "total_duration_s": profile.total_duration_s,
        "max_rps": profile.max_rps,
        "header_names": sorted(sc.headers)[:50],
        "identities": len(sc.identities),
        "workers": sc.workers,
    }
    reads_files = [
        label
        for label, present in (
            ("identities_file", bool(sc.identities_file)),
            ("data files", bool(sc.data)),
            ("metric_sources", bool(sc.metric_sources)),
        )
        if present
    ]
    if reads_files:
        out["warnings"] = [
            f"uses {', '.join(reads_files)}: only resolved when the scenario runs, so not checked"
        ]
    return out


def _validation(data: Any, default_name: str = "scenario") -> dict[str, Any]:
    from ..load.profile import ScenarioError
    from ..load.scenario import parse_scenario

    try:
        sc = parse_scenario(data, default_name=default_name)
    except ScenarioError as exc:
        return {"valid": False, "errors": [{"path": exc.path or "", "message": exc.message}]}
    return {"valid": True, "errors": [], "summary": _scenario_summary(sc)}


def _scenario_input(args: dict[str, Any]) -> tuple[Any, str]:
    given = [k for k in ("scenario", "path") if args.get(k) is not None]
    if len(given) != 1:
        raise ValueError("give exactly one of scenario (an object) or path (a .json/.toml file)")
    if given[0] == "scenario":
        return args["scenario"], "scenario"
    path = args["path"]
    text = read_local_file(path, (".json", ".toml"), what="scenario")
    try:
        if path.lower().endswith(".json"):
            return json.loads(text), path.rsplit("/", 1)[-1]
        from ..load.scenario import _toml_loads

        return _toml_loads(text), path.rsplit("/", 1)[-1]
    except ValueError as exc:
        raise ValueError(f"the scenario file is not valid: {str(exc)[:200]}") from None


def _validate_scenario(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    data, name = _scenario_input(args)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return {**_validation(data, stem), "ran": False}


def _mask_headers(scenario: dict[str, Any]) -> dict[str, Any]:
    """A generated scenario without header values (an example value could be a credential)."""
    out = dict(scenario)
    if isinstance(out.get("headers"), dict):
        out["headers"] = dict.fromkeys(out["headers"], "<set me>")
    out["requests"] = [
        {**r, "headers": dict.fromkeys(r["headers"], "<set me>")} if r.get("headers") else r
        for r in out.get("requests", [])
    ]
    return out


def _generated(scenario: dict[str, Any]) -> dict[str, Any]:
    from ..load.generate import to_toml

    total = len(scenario.get("requests", []))
    scenario = _mask_headers(scenario)
    scenario["requests"] = scenario["requests"][:_MAX_REQUESTS]
    return {
        "scenario": scenario,
        "requests_total": total,
        "toml": to_toml(scenario),
        "validation": _validation(scenario),
        "note": "Review before running: writes and requests needing input have weight 0.",
        "ran": False,
    }


def _check_base_url(value: Any) -> str | None:
    if value is None:
        return None
    from ..load.scenario import validate_base_url

    return validate_base_url(value, "base_url")


def _generate_openapi(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..load.generate import from_openapi

    given = [k for k in ("openapi", "path") if args.get(k) is not None]
    if len(given) != 1:
        raise ValueError("give exactly one of openapi (an object) or path (a .json file)")
    if "openapi" in args:
        doc = args["openapi"]
    else:
        try:
            doc = json.loads(read_local_file(args["path"], (".json",), what="OpenAPI"))
        except ValueError as exc:
            raise ValueError(f"not a JSON OpenAPI document: {str(exc)[:200]}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        raise ValueError("not an OpenAPI document (no 'paths')")
    return _generated(
        from_openapi(
            doc,
            include_writes=bool(args.get("include_writes", False)),
            base_url=_check_base_url(args.get("base_url")),
            name=args.get("name"),
        )
    )


def _generate_traces(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..load.generate import from_traces
    from ..storage.base import parse_window

    return _generated(
        from_traces(
            store,
            window_s=parse_window(str(args.get("window", "24h"))),
            top=args.get("top", 10),
            include_writes=bool(args.get("include_writes", False)),
            base_url=_check_base_url(args.get("base_url")),
        )
    )


def _generate_stages(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..load.generate import DEFAULT_BASE_URL

    stages = args["stages"]
    if isinstance(stages, str):
        from ..cli import _stages_arg

        try:
            stages = _stages_arg(stages)
        except Exception as exc:
            raise ValueError(f"stages: {str(exc)[:200]}") from None
    requests = args.get("requests") or [{"name": "home", "method": "GET", "path": "/"}]
    scenario = {
        "name": args.get("name") or "staged load",
        "base_url": _check_base_url(args.get("base_url")) or DEFAULT_BASE_URL,
        "timeout_s": 10,
        "requests": requests,
        "profile": {"mode": "stages", "stages": stages},
    }
    return _generated(scenario)


# ------------------------------------------------------------------ gated: change things
def _require_options() -> Any:
    options = current_options()
    if not options.store_path:
        raise ValueError("the server was started without a store path")
    return options


def _start_load_run(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..load.control import launch_background

    options = _require_options()
    if not options.load_target:
        raise ValueError("no load target configured (start the server with --load-target URL)")
    scenario = dict(args["scenario"])
    blocked = [k for k in _LOCAL_ONLY_KEYS if k in scenario]
    if blocked:
        raise ValueError(f"not allowed over MCP: {', '.join(blocked)} (reads local files or code)")
    if any(
        r.get("status") == "running" for r in api_get(store, "api/runs", {"limit": "20"})["items"]
    ):
        raise ValueError("a load run is already running; stop it or wait for it to finish")
    run_id = launch_background(scenario, options.load_target, options.store_path)
    return {
        "id": run_id,
        "status": "running",
        "target": safe_url(options.load_target),
        "note": "The target is fixed by the operator; base_url in the scenario is ignored.",
    }


def _known_run(store: Any, run_id: str) -> Any:
    run = api_get(store, f"api/runs/{run_id}")
    return run


def _stop_load_run(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..load.control import stop_run

    options = _require_options()
    _known_run(store, args["run_id"])
    return {"id": args["run_id"], "stopped": bool(stop_run(args["run_id"], options.store_path))}


def _delete_load_run(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    from ..storage.sqlite import SQLiteStore

    options = _require_options()
    run = _known_run(store, args["run_id"])
    if run.get("status") == "running":
        raise ValueError("stop the run before deleting it")
    writable = SQLiteStore(options.store_path, background=False)
    try:
        return {"id": args["run_id"], "deleted": writable.delete_run(args["run_id"])}
    finally:
        writable.close()


_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_ITEMS}
_WINDOW = {"type": "string", "maxLength": 10, "description": "Look-back window such as 1h, 24h, 7d"}
_BASE = {"type": "string", "maxLength": 300, "description": "http(s) URL of the target app"}

RUN_TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_load_runs",
        "List recent load runs, newest first: status, knee step, max OK rps and verdict.",
        obj({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
        _list_load_runs,
    ),
    Tool(
        "get_load_run",
        "One load run: its steps (latency percentiles, errors per step), the scenario with "
        "header, cookie and identity values removed, and the summary.",
        obj({"run_id": _RUN_ID}, ("run_id",)),
        _get_load_run,
    ),
    Tool(
        "load_run_summary",
        "A run's verdict and analysis: knee, flows, recovery after a spike, drift on long "
        "steps, identities (names and counts), limiting_resource and resources.",
        obj({"run_id": _RUN_ID}, ("run_id",)),
        _load_run_summary,
    ),
    Tool(
        "load_run_bottlenecks",
        "Per step, the queries and routes that slowed down most during a run, with callsites.",
        obj({"run_id": _RUN_ID, "limit": _LIMIT}, ("run_id",)),
        _load_run_bottlenecks,
    ),
    Tool(
        "load_run_metrics",
        "Application CPU, memory, GC, threads and connection-pool numbers per step of a run, "
        "and the likely limiting resource.",
        obj({"run_id": _RUN_ID}, ("run_id",)),
        _load_run_metrics,
    ),
    Tool(
        "validate_scenario",
        "Parse a load scenario (an object, or a .json/.toml file in the working directory) "
        "and return its errors or a summary. It is never run and nothing is sent.",
        obj({"scenario": _SCENARIO, "path": _PATH}),
        _validate_scenario,
    ),
    Tool(
        "generate_scenario_from_openapi",
        "Build a load scenario from an OpenAPI document (an object or a .json file). Reads "
        "only; writes get weight 0 unless include_writes. Header values are not returned.",
        obj(
            {
                "openapi": {"type": "object", "description": "The OpenAPI document"},
                "path": _PATH,
                "include_writes": {"type": "boolean"},
                "base_url": _BASE,
                "name": {"type": "string", "maxLength": 100},
            }
        ),
        _generate_openapi,
    ),
    Tool(
        "generate_scenario_from_traces",
        "Build a load scenario from the recorded traffic of the store (most-used endpoints, "
        "weighted by observed count).",
        obj(
            {
                "window": _WINDOW,
                "top": {"type": "integer", "minimum": 1, "maximum": 50},
                "include_writes": {"type": "boolean"},
                "base_url": _BASE,
            }
        ),
        _generate_traces,
    ),
    Tool(
        "generate_scenario_from_stages",
        "Build a scenario with a staged profile (spike, soak, recovery) from "
        "'RPS:DURATION,...' text such as '10:30s,200:20s,10:60s' or a list of "
        "{rps, duration_s}, over the given requests.",
        obj(
            {
                "stages": {
                    "anyOf": [
                        {"type": "string", "maxLength": 500},
                        {"type": "array", "maxItems": 50, "items": {"type": "object"}},
                    ],
                    "description": "'RPS:DURATION,...' or [{rps, duration_s}]",
                },
                "requests": {
                    "type": "array",
                    "maxItems": _MAX_REQUESTS,
                    "items": {"type": "object"},
                },
                "base_url": _BASE,
                "name": {"type": "string", "maxLength": 100},
            },
            ("stages",),
        ),
        _generate_stages,
    ),
    Tool(
        "start_load_run",
        "Start a load run in the background against the operator's fixed target. Sends real "
        "traffic. Needs the server flag --allow-load; one run at a time.",
        obj({"scenario": _SCENARIO}, ("scenario",)),
        _start_load_run,
        gate="load",
    ),
    Tool(
        "stop_load_run",
        "Ask a running load run to stop gracefully (results are kept). Needs --allow-load.",
        obj({"run_id": _RUN_ID}, ("run_id",)),
        _stop_load_run,
        gate="load",
    ),
    Tool(
        "delete_load_run",
        "Delete a finished load run and its steps (its traces stay). Needs --allow-delete-runs.",
        obj({"run_id": _RUN_ID}, ("run_id",)),
        _delete_load_run,
        gate="delete_runs",
    ),
)
