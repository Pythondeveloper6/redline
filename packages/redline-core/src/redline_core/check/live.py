"""Compare two running versions by alternating requests between them.

Separate record-then-compare runs differ by whatever else the machine did in between. Here
each request goes to the old and the new server back to back, and the order flips every
round (ABBA), so warm-up and drift hit both sides alike. Both servers must serve equivalent
data; suites only contain read-only methods, so nothing is written to either.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from time import perf_counter
from typing import Any

from .diff import capture, compare_captures
from .stats import median
from .suite import Suite, validate_base_url

__all__ = ["run_live_compare"]

MAX_ROUNDS = 1000


def _join(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def run_live_compare(
    suite: Suite,
    *,
    old_url: str,
    new_url: str,
    rounds: int = 5,
    transport_old: Any = None,
    transport_new: Any = None,
    stop: threading.Event | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Send each suite request to both servers ``rounds`` times, alternating order.

    Returns ``{"status", "rounds_done", "requests": [{name, method, path, old_ms, new_ms,
    median_old, median_new, median_delta_ms, responses_equal, errors}]}``. A server that
    fails is recorded in ``errors`` and the run continues; ``responses_equal`` is ``None``
    unless both sides answered. Raises ``ValueError``/``SuiteError`` for bad input.
    """
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= MAX_ROUNDS:
        raise ValueError(f"rounds must be a whole number from 1 to {MAX_ROUNDS}")
    urls = {
        "old": validate_base_url(old_url, "old_url"),
        "new": validate_base_url(new_url, "new_url"),
    }
    import httpx

    from .. import __version__

    headers = [suite.headers_for(req, environ) for req in suite.requests]
    rows = [
        {
            "name": req.name,
            "method": req.method,
            "path": req.path,
            "old_ms": [],
            "new_ms": [],
            "errors": {"old": {}, "new": {}},
            "captures": {},
        }
        for req in suite.requests
    ]
    transports = {"old": transport_old, "new": transport_new}
    status, done = "done", 0
    clients = {
        side: httpx.Client(
            transport=transports[side],
            timeout=suite.timeout_s,
            follow_redirects=False,
            headers={"User-Agent": f"redline-check/{__version__}"},
        )
        for side in urls
    }
    try:
        for round_no in range(rounds):
            if stop is not None and stop.is_set():
                status = "stopped"
                break
            order = ("old", "new") if round_no % 2 == 0 else ("new", "old")
            for i, req in enumerate(suite.requests):
                row = rows[i]
                # Flip the order per request too, so ABBA holds across the whole round.
                for side in order if i % 2 == 0 else order[::-1]:
                    start = perf_counter()
                    try:
                        resp = clients[side].request(
                            req.method, _join(urls[side], req.path), headers=headers[i]
                        )
                        content = resp.content
                    except httpx.HTTPError as exc:
                        key = type(exc).__name__
                        errors = row["errors"][side]
                        errors[key] = errors.get(key, 0) + 1
                        continue
                    row[f"{side}_ms"].append(round((perf_counter() - start) * 1000.0, 3))
                    if side not in row["captures"]:
                        row["captures"][side] = capture(
                            resp.status_code,
                            resp.headers.multi_items(),
                            content,
                            ignore_headers=req.ignore_headers,
                        )
            done += 1
    finally:
        for client in clients.values():
            client.close()
    for req, row in zip(suite.requests, rows, strict=True):
        caps = row.pop("captures")
        mo, mn = median(row["old_ms"]), median(row["new_ms"])
        row["median_old"], row["median_new"] = mo, mn
        row["median_delta_ms"] = round(mn - mo, 3) if mo is not None and mn is not None else None
        if "old" in caps and "new" in caps:
            same_status = caps["old"].get("status") == caps["new"].get("status")
            diff = compare_captures(
                caps["old"],
                caps["new"],
                ignore=req.ignore,
                array_keys=req.array_keys,
                tolerances=req.tolerances,
            )
            row["responses_equal"] = bool(same_status and not diff["diff_total"])
        else:
            row["responses_equal"] = None
    return {"status": status, "rounds_done": done, "requests": rows}
