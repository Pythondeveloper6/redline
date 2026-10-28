"""Coverage of observed routes and inputs, not a claim about unobserved behavior."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from urllib.parse import urlsplit

from .suite import Suite


@lru_cache(maxsize=512)
def _route_pattern(route: str):
    pieces = []
    previous = 0
    for match in re.finditer(r"<([^<>]+)>|\{([^{}]+)\}", route):
        pieces.append(re.escape(route[previous : match.start()]))
        declaration = match[1] or match[2]
        parts = declaration.split(":")
        converter = (
            parts[0] if match[1] and len(parts) > 1 else parts[-1] if len(parts) > 1 else "str"
        )
        pieces.append(
            {"int": r"\d+", "path": r".+", "slug": r"[A-Za-z0-9_-]+"}.get(converter, r"[^/]+")
        )
        previous = match.end()
    pieces.append(re.escape(route[previous:]))
    return re.compile("^" + "".join(pieces) + "$")


def coverage(suite: Suite, traces: list[dict]) -> dict:
    inputs: dict[tuple[str, str], set[str]] = defaultdict(set)
    lookup: dict[tuple[str, str], tuple[str, str]] = {}
    for trace in traces:
        method = trace["method"]
        path = urlsplit(trace["path"]).path
        route = trace.get("route") or path
        inputs[(method, route)].add(path)
        lookup[(method, path)] = (method, route)
    tested: dict[tuple[str, str], set[str]] = defaultdict(set)
    unmatched = []
    for req in suite.requests:
        key = lookup.get((req.method, urlsplit(req.path).path))
        if key is None:
            matches = [
                key
                for key in inputs
                if key[0] == req.method
                and _route_pattern(key[1]).fullmatch(urlsplit(req.path).path)
            ]
            key = matches[0] if len(matches) == 1 else None
        if key is None:
            unmatched.append(req.name)
        else:
            tested[key].add(req.path + json.dumps(req.headers, sort_keys=True))
    rows = [
        {
            "method": method,
            "route": route,
            "observed_paths": len(paths),
            "suite_inputs": len(tested[(method, route)]),
            "status": "missing"
            if not tested[(method, route)]
            else "single_input"
            if len(tested[(method, route)]) == 1
            else "covered",
        }
        for (method, route), paths in sorted(inputs.items())
    ]
    return {
        "routes": rows,
        "unmatched_suite_requests": unmatched,
        "observed_traces": len(traces),
        "note": "Based on the latest retained trace sample. Unobserved routes and parameter "
        "values are unknown; one input does not establish behavioral coverage.",
    }
