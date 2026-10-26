"""Measure what Redline costs per request.

    uv run python benchmarks/overhead.py            # full run, prints a markdown report
    uv run python benchmarks/overhead.py --quick    # fewer iterations (smoke test)

Both adapters are driven in-process (no network, no server) against a view that runs N
trivial SQLite queries:

* **django**  - ``django.test.Client`` -> ``RedlineMiddleware`` -> view -> ``cursor.execute``.
* **fastapi** - raw ASGI calls on one event loop -> ``RedlineMiddleware`` -> ``async def``
  endpoint -> SQLAlchemy ``Session.execute`` (sync engine, in-memory SQLite).

Modes:

* **off**       - Redline is not installed at all (the baseline).
* **disabled**  - installed, ``enabled=False``.
* **sample0**   - recording happens but ``sample_rate=0`` discards every trace.
* **full**      - ``sample_rate=1``: every trace is analysed and stored (SQLite, written by a
  background thread, so some of that cost lands outside the request).
* **profile**   - full capture plus a pyinstrument flamegraph on every request.

Every (framework, mode, N) cell runs in a fresh subprocess so settings, patches and warm
caches cannot leak between modes. Each request is timed on its own; we report the median and
p95 over many iterations. Per-query overhead is derived from two query counts:
``(overhead(N=HIGH) - overhead(N=LOW)) / (HIGH - LOW)`` against the ``off`` baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time

MODES = ("off", "disabled", "sample0", "full", "profile")
FRAMEWORKS = ("django", "fastapi")
LOW, HIGH = 0, 20


def redline_settings(mode: str, tmp: str) -> dict:
    cfg = {"storage_path": os.path.join(tmp, "redline.db")}
    if mode == "disabled":
        cfg["enabled"] = False
    elif mode == "sample0":
        cfg.update(sample_rate=0.0, always_keep_slower_than_ms=1e9, always_keep_errors=False)
    elif mode == "full":
        cfg.update(sample_rate=1.0)
    elif mode == "profile":
        cfg.update(sample_rate=1.0, profile="all")
    return cfg


# --------------------------------------------------------------------------- workers
def django_worker(mode: str, n_queries: int, iterations: int, warmup: int) -> list[float]:
    import django
    from django.conf import settings

    tmp = tempfile.mkdtemp(prefix="redline-bench-")
    conf = {
        "DEBUG": False,
        "SECRET_KEY": "bench",
        "ALLOWED_HOSTS": ["*"],
        "USE_TZ": True,
        "ROOT_URLCONF": __name__,
        "INSTALLED_APPS": [],
        "MIDDLEWARE": [],
        "DATABASES": {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    }
    if mode != "off":
        conf["INSTALLED_APPS"] = ["django_redline"]
        conf["MIDDLEWARE"] = ["django_redline.middleware.RedlineMiddleware"]
        conf["REDLINE"] = {k.upper(): v for k, v in redline_settings(mode, tmp).items()}
    settings.configure(**conf)
    django.setup()

    from django.db import connection
    from django.http import HttpResponse
    from django.test import Client
    from django.urls import path

    with connection.cursor() as c:
        c.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT)")
        c.executemany(
            "INSERT INTO item (id, name) VALUES (%s, %s)", [(i, f"n{i}") for i in range(50)]
        )

    def view(request):
        with connection.cursor() as cur:
            for i in range(n_queries):
                cur.execute("SELECT id, name FROM item WHERE id = %s", [i % 50])
                cur.fetchall()
        return HttpResponse("ok")

    globals()["urlpatterns"] = [path("bench/", view)]
    client = Client()
    for _ in range(warmup):
        client.get("/bench/")
    out = []
    for _ in range(iterations):
        t = time.perf_counter_ns()
        resp = client.get("/bench/")
        out.append((time.perf_counter_ns() - t) / 1e3)
        assert resp.status_code == 200
    _flush_django()
    return out


def _flush_django() -> None:
    try:
        from django_redline.runtime import reset_runtime

        reset_runtime()
    except Exception:
        pass


def fastapi_worker(mode: str, n_queries: int, iterations: int, warmup: int) -> list[float]:
    import asyncio

    from fastapi import FastAPI
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import StaticPool

    tmp = tempfile.mkdtemp(prefix="redline-bench-")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT)"))
        for i in range(50):
            conn.execute(
                text("INSERT INTO item (id, name) VALUES (:i, :n)"), {"i": i, "n": f"n{i}"}
            )

    app = FastAPI()

    @app.get("/bench/")
    async def bench() -> dict:
        with engine.connect() as conn:
            for i in range(n_queries):
                conn.execute(text("SELECT id, name FROM item WHERE id = :i"), {"i": i % 50}).all()
        return {"ok": True}

    if mode != "off":
        from fastapi_redline import install

        install(app, engine=engine, settings=redline_settings(mode, tmp), dependencies=[])

    async def call() -> int:
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/bench/",
            "raw_path": b"/bench/",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"accept", b"*/*")],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        return sent[0]["status"]

    async def main() -> list[float]:
        async with app.router.lifespan_context(app):
            for _ in range(warmup):
                await call()
            out = []
            for _ in range(iterations):
                t = time.perf_counter_ns()
                status = await call()
                out.append((time.perf_counter_ns() - t) / 1e3)
                assert status == 200
            return out

    return asyncio.run(main())


# --------------------------------------------------------------------------- driver
def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


def run_cell(framework: str, mode: str, n: int, iterations: int, warmup: int) -> list[float]:
    proc = subprocess.run(
        [
            sys.executable,
            __file__,
            "--worker",
            framework,
            mode,
            str(n),
            str(iterations),
            str(warmup),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode:
        raise SystemExit(f"{framework}/{mode}/{n} failed:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def cpu_name() -> str:
    try:
        return subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip()
    except Exception:
        return platform.processor() or platform.machine()


def versions() -> str:
    from importlib.metadata import version

    names = ["django", "fastapi", "sqlalchemy", "pyinstrument", "redline-core"]
    return ", ".join(f"{n} {version(n)}" for n in names)


def report(iterations: int, rounds: int, results: dict) -> str:
    lines = [
        "# Redline overhead benchmarks",
        "",
        f"- Date: {time.strftime('%Y-%m-%d')}",
        f"- Python: {platform.python_version()} ({platform.python_implementation()}) on "
        f"{platform.system()} {platform.machine()}",
        f"- CPU: {cpu_name()}",
        f"- Packages: {versions()}",
        f"- Method: {iterations} timed requests per cell after warm-up, {rounds} interleaved "
        f"rounds pooled; each cell in a fresh subprocess; in-process client, in-memory SQLite.",
        f"- Queries per request: {LOW} (fixed cost) and {HIGH} (adds {HIGH - LOW} queries).",
        "",
    ]
    for fw in FRAMEWORKS:
        base = {n: results[(fw, "off", n)] for n in (LOW, HIGH)}
        lines += [
            f"## {fw}",
            "",
            "| mode | median ms (0 q) | p95 ms (0 q) | median ms (20 q) | p95 ms (20 q) "
            "| overhead / request (0 q) | overhead / query |",
            "|---|---|---|---|---|---|---|",
        ]
        for mode in MODES:
            lo, hi = results[(fw, mode, LOW)], results[(fw, mode, HIGH)]
            m_lo, m_hi = statistics.median(lo), statistics.median(hi)
            per_req = m_lo - statistics.median(base[LOW])
            per_q = ((m_hi - statistics.median(base[HIGH])) - per_req) / (HIGH - LOW)
            tag = "baseline" if mode == "off" else f"{per_req / 1000:+.3f} ms"
            tagq = "baseline" if mode == "off" else f"{per_q:+.1f} us"
            lines.append(
                f"| {mode} | {m_lo / 1000:.3f} | {pct(lo, 0.95) / 1000:.3f} "
                f"| {m_hi / 1000:.3f} | {pct(hi, 0.95) / 1000:.3f} | {tag} | {tagq} |"
            )
        lines.append("")
    lines += [
        "Overhead = median with Redline minus median of the `off` baseline, same query count.",
        "Per-query overhead = (overhead at 20 queries - overhead at 0 queries) / 20.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--iterations", type=int, default=1500)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--worker", nargs=5, metavar=("FW", "MODE", "N", "ITERS", "WARMUP"))
    args = ap.parse_args()

    if args.worker:
        fw, mode, n, iters, warm = args.worker
        fn = django_worker if fw == "django" else fastapi_worker
        print(json.dumps(fn(mode, int(n), int(iters), int(warm))))
        return

    iterations, rounds = (150, 1) if args.quick else (args.iterations, args.rounds)
    per_round = iterations // rounds
    results: dict[tuple, list[float]] = {}
    for r in range(rounds):  # interleave rounds so machine drift hits every mode equally
        for fw in FRAMEWORKS:
            for mode in MODES:
                for n in (LOW, HIGH):
                    print(f"round {r + 1}/{rounds} {fw} {mode} q={n}", file=sys.stderr)
                    cell = run_cell(fw, mode, n, per_round, max(50, per_round // 10))
                    results.setdefault((fw, mode, n), []).extend(cell)
    print(report(per_round * rounds, rounds, results))


if __name__ == "__main__":
    main()
