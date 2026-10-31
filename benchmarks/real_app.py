"""Measure Redline's overhead on *your* running application over real HTTP.

``overhead.py`` measures the adapters in-process against trivial SQLite views. Before any
production-sampling decision you need the same numbers for your own app, database and
traffic shape. Start two copies of the app, identical except that one has Redline installed
(or one copy twice, toggling ``REDLINE_ENABLED``), then:

    uv run python benchmarks/real_app.py \
        --baseline http://127.0.0.1:8000 --candidate http://127.0.0.1:8001 \
        --path /api/orders --path /api/orders/42 --requests 500 --concurrency 4

Requests to the two targets are **interleaved** (A, B, A, B …) in rounds so drift on the
machine (thermal, other load, database cache) hits both equally. Each path is warmed up
first. The report gives p50/p95/p99 and the overhead per path and overall, as Markdown or
JSON (``--json``). Only status codes < 500 count; errors are reported separately.

Uses only the standard library (``urllib``), so it runs from any Python. Headers can be
added with ``--header 'Authorization: Bearer …'`` (sent to both targets).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field


@dataclass
class Samples:
    durations_ms: list[float] = field(default_factory=list)
    errors: int = 0

    def summary(self) -> dict[str, float | int]:
        values = sorted(self.durations_ms)
        return {
            "n": len(values),
            "errors": self.errors,
            "p50": quantile(values, 0.50),
            "p95": quantile(values, 0.95),
            "p99": quantile(values, 0.99),
            "mean": statistics.fmean(values) if values else 0.0,
        }


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an already sorted sequence (0 for empty)."""
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo))


def fetch(url: str, headers: dict[str, str], timeout: float) -> tuple[float, bool]:
    """One GET; returns (milliseconds, ok)."""
    request = urllib.request.Request(url, headers=headers)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            ok = response.status < 500
    except urllib.error.HTTPError as exc:
        exc.read()
        ok = exc.code < 500
    except (urllib.error.URLError, OSError):
        ok = False
    return (time.perf_counter() - start) * 1000.0, ok


def run(
    targets: dict[str, str],
    paths: Sequence[str],
    *,
    requests: int,
    concurrency: int,
    warmup: int,
    headers: dict[str, str],
    timeout: float,
    fetcher=fetch,
) -> dict[str, dict[str, Samples]]:
    """Interleave ``requests`` GETs per (target, path); returns samples by path then target."""
    results = {path: {name: Samples() for name in targets} for path in paths}
    for path in paths:
        for base in targets.values():
            for _ in range(warmup):
                fetcher(base.rstrip("/") + path, headers, timeout)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for _ in range(requests):
            for path in paths:
                for name, base in targets.items():
                    url = base.rstrip("/") + path
                    batch = [
                        pool.submit(fetcher, url, headers, timeout) for _ in range(concurrency)
                    ]
                    for future in batch:
                        ms, ok = future.result()
                        if ok:
                            results[path][name].durations_ms.append(ms)
                        else:
                            results[path][name].errors += 1
    return results


def report(results: dict[str, dict[str, Samples]]) -> dict[str, object]:
    rows = []
    total = {"baseline": Samples(), "candidate": Samples()}
    for path, by_target in results.items():
        base, cand = by_target["baseline"].summary(), by_target["candidate"].summary()
        for name in total:
            total[name].durations_ms += by_target[name].durations_ms
            total[name].errors += by_target[name].errors
        rows.append(_row(path, base, cand))
    rows.append(_row("(all)", total["baseline"].summary(), total["candidate"].summary()))
    return {"rows": rows}


def _row(path: str, base: dict, cand: dict) -> dict[str, object]:
    return {
        "path": path,
        "baseline": base,
        "candidate": cand,
        "overhead_p50_ms": cand["p50"] - base["p50"],
        "overhead_p95_ms": cand["p95"] - base["p95"],
    }


def to_markdown(data: dict[str, object]) -> str:
    lines = [
        "| path | n | baseline p50 / p95 ms | redline p50 / p95 ms | overhead p50 / p95 ms "
        "| errors (base/redline) |",
        "|---|---|---|---|---|---|",
    ]
    for row in data["rows"]:  # type: ignore[union-attr]
        b, c = row["baseline"], row["candidate"]
        lines.append(
            f"| {row['path']} | {c['n']} | {b['p50']:.2f} / {b['p95']:.2f} "
            f"| {c['p50']:.2f} / {c['p95']:.2f} "
            f"| {row['overhead_p50_ms']:+.2f} / {row['overhead_p95_ms']:+.2f} "
            f"| {b['errors']}/{c['errors']} |"
        )
    return "\n".join(lines) + "\n"


def _headers(values: Sequence[str]) -> dict[str, str]:
    headers = {}
    for value in values:
        name, sep, content = value.partition(":")
        if not sep or not name.strip():
            raise argparse.ArgumentTypeError(f"bad header {value!r}; use 'Name: value'")
        headers[name.strip()] = content.strip()
    return headers


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", required=True, help="base URL without Redline")
    parser.add_argument("--candidate", required=True, help="base URL with Redline")
    parser.add_argument("--path", action="append", required=True, help="GET path (repeat)")
    parser.add_argument("--requests", type=int, default=300, help="rounds per path (300)")
    parser.add_argument("--concurrency", type=int, default=1, help="parallel requests (1)")
    parser.add_argument("--warmup", type=int, default=20, help="warm-up GETs per path (20)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--header", action="append", default=[], metavar="'Name: value'")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    args = parser.parse_args(argv)
    try:
        headers = _headers(args.header)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    results = run(
        {"baseline": args.baseline, "candidate": args.candidate},
        args.path,
        requests=args.requests,
        concurrency=args.concurrency,
        warmup=args.warmup,
        headers=headers,
        timeout=args.timeout,
    )
    data = report(results)
    sys.stdout.write(json.dumps(data, indent=2) + "\n" if args.json else to_markdown(data))
    errors = sum(r["candidate"]["errors"] + r["baseline"]["errors"] for r in data["rows"][:-1])  # type: ignore[index]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
