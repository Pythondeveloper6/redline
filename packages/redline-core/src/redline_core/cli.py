"""The ``redline`` command line (console script ``redline = redline_core.cli:main``).

Subcommands are registered in :data:`COMMANDS`; each is a module-level ``(configure,
handler)`` pair so new ones (``scan``, …) plug in without touching the dispatcher. Heavy
imports happen inside the handlers, so ``redline --help`` stays instant.

Exit codes: 0 ok, 1 a ``--fail-*`` threshold was breached, a ``redline check`` verdict
matched ``--fail-on``, or the run failed, 2 usage or scenario/suite error, 3 the app could
not be reached (``redline check``), 130 interrupted (Ctrl-C).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from . import digest, operations, project_init
from .check_sql import command as _cmd_sql
from .check_sql import configure_cli as _configure_sql
from .schema_watch import command as _cmd_schema_watch
from .schema_watch import configure_cli as _configure_schema_watch

__all__ = ["COMMANDS", "main"]

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130
DEFAULT_STORE = ".redline/redline.db"


# ============================================================================ helpers
def _duration(value: str) -> float:
    from .load.profile import ScenarioError, parse_duration

    try:
        return parse_duration(value)
    except ScenarioError as exc:
        raise argparse.ArgumentTypeError(exc.message) from None


def _numbers(value: str, count: int, label: str) -> list[float]:
    parts = value.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        nums = []
    if len(nums) != count or any(n < 0 or not math.isfinite(n) for n in nums):
        raise argparse.ArgumentTypeError(f"expected {label}, got {value!r}")
    return nums


def _fraction(value: str) -> float:
    text = value.strip()
    try:
        num = float(text[:-1]) / 100.0 if text.endswith("%") else float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a fraction like 0.01 or 1%, got {value!r}"
        ) from None
    if not 0 <= num <= 1:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1 (or 0%–100%), got {value!r}")
    return num


def fmt_ms(value: float) -> str:
    if value >= 10_000:
        return f"{value / 1000:.1f}s"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    if value >= 100:
        return f"{value:.0f}ms"
    if value >= 10:
        return f"{value:.1f}ms"
    return f"{value:.2f}ms"


# ============================================================================ load
def _configure_load(p: argparse.ArgumentParser) -> None:
    p.add_argument("scenario", help="scenario file (.toml, .json or .py defining SCENARIO)")
    shape = p.add_mutually_exclusive_group()
    shape.add_argument("--rps", type=float, metavar="N", help="constant rate (requests/s)")
    shape.add_argument("--ramp", metavar="A:B", help="ramp linearly from A to B rps")
    shape.add_argument("--steps", metavar="A:B:STEP", help="steps from A to B rps, +STEP each")
    p.add_argument("--duration", type=_duration, metavar="60s", help="total duration")
    p.add_argument("--step-duration", type=_duration, metavar="30s", help="duration of each step")
    p.add_argument("--poisson", action="store_true", help="Poisson arrivals (default fixed)")
    p.add_argument("--workers", type=int, metavar="N", help="worker processes (default ≤ 8)")
    p.add_argument("--base-url", metavar="URL", help="override the scenario's base_url")
    p.add_argument(
        "--ui-url",
        metavar="PATH|URL",
        help="where the Redline UI is mounted, for the printed link (default: the scenario's "
        "ui_url, else mount_path from the project's Redline config, else /_redline)",
    )
    p.add_argument("--name", help="run name (default: the scenario's)")
    p.add_argument(
        "--store",
        default=DEFAULT_STORE,
        metavar="PATH",
        help=f"Redline store to write the run to (default {DEFAULT_STORE})",
    )
    p.add_argument("--no-store", action="store_true", help="do not write to the store")
    p.add_argument("--json", metavar="OUT", help="also write the results as JSON")
    p.add_argument("--fail-p95-ms", type=float, metavar="X", help="exit 1 if p95 > X ms")
    p.add_argument(
        "--fail-error-rate", type=_fraction, metavar="Y", help="exit 1 if error rate > Y"
    )
    p.add_argument("--quiet", "-q", action="store_true", help="only print the final table")
    p.add_argument("--run-id", help=argparse.SUPPRESS)


def _cli_profile(args: argparse.Namespace, scenario: Any) -> Any:
    from .load.profile import parse_profile

    base = scenario.profile.to_dict()
    stop_on = base.get("stop_on")
    poisson = args.poisson or base.get("poisson", False)
    if args.rps is not None:
        data: dict[str, Any] = {"mode": "constant", "rps": args.rps}
        data["duration_s"] = args.duration or base.get("duration_s") or 30
    elif args.ramp:
        a, b = _numbers(args.ramp, 2, "A:B")
        data = {"mode": "ramp", "start_rps": a, "end_rps": b}
        data["duration_s"] = args.duration or base.get("duration_s") or 30
        if args.step_duration:
            data["step_duration_s"] = args.step_duration
    elif args.steps:
        a, b, step = _numbers(args.steps, 3, "A:B:STEP")
        data = {"mode": "steps", "start_rps": a, "end_rps": b, "step_rps": step}
        data["step_duration_s"] = args.step_duration or 30
        if args.duration:
            data["duration_s"] = args.duration
    else:
        data = dict(base)
        if args.duration:
            data["duration_s"] = args.duration
        if args.step_duration:
            data["step_duration_s"] = args.step_duration
    if stop_on:
        data["stop_on"] = stop_on
    if poisson:
        data["poisson"] = True
    return parse_profile(data, "profile")


class _Printer:
    """Live terminal output: plain text; per-second status only on a TTY."""

    HEADER = (
        f"{'step':>4} {'target':>8} {'achieved':>9} {'sent':>7} {'ok':>7} {'err':>6} "
        f"{'drop':>6} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8} {'err%':>6}"
    )

    def __init__(self, out: TextIO, *, quiet: bool) -> None:
        self.out = out
        self.quiet = quiet
        self.tty = bool(getattr(out, "isatty", lambda: False)())
        self.header_done = False
        self.status_len = 0

    def _clear(self) -> None:
        if self.status_len:
            self.out.write("\r" + " " * self.status_len + "\r")
            self.status_len = 0

    def line(self, text: str = "") -> None:
        self._clear()
        self.out.write(text + "\n")
        self.out.flush()

    def row(self, s: Any) -> str:
        d = s.to_dict()
        flag = " (partial)" if s.partial else ""
        return (
            f"{d['step']:>4} {d['target_rps']:>8.1f} {d['achieved_rps']:>9.1f} {d['sent']:>7} "
            f"{d['ok']:>7} {d['errors']:>6} {d['dropped']:>6} {fmt_ms(d['p50_ms']):>8} "
            f"{fmt_ms(d['p95_ms']):>8} {fmt_ms(d['p99_ms']):>8} {fmt_ms(d['max_ms']):>8} "
            f"{d['error_rate'] * 100:>5.1f}%{flag}"
        )

    def __call__(self, kind: str, data: Any) -> None:
        if self.quiet:
            return
        if kind == "step":
            if not self.header_done:
                self.line(self.HEADER)
                self.header_done = True
            self.line(self.row(data))
        elif kind == "second" and self.tty:
            text = (
                f"  {data['t']:6.1f}s  step {data['step']}  sent {data['sent']}/s  "
                f"ok {data['ok']}  err {data['errors']}  drop {data['dropped']}  "
                f"p95 {fmt_ms(data['p95_ms'])}"
            )
            self._clear()
            self.out.write(text)
            self.out.flush()
            self.status_len = len(text)
        elif kind == "stopping":
            self.line(f"stopping: {data}")


def _open_store_sink(path: str, err: TextIO) -> Any:
    """``StoreSink`` over ``SQLiteStore(path)``, or ``None`` (with a warning)."""
    from .load.sinks import StoreSink, store_supports_runs
    from .storage import SQLiteStore

    try:
        store = SQLiteStore(path, background=False)
    except Exception as exc:
        err.write(f"warning: cannot open the store {path}: {exc}; writing JSON only\n")
        return None
    if not store_supports_runs(store):
        store.close()
        err.write(
            f"warning: this store ({type(store).__name__}) cannot hold load runs yet; "
            "writing JSON only\n"
        )
        return None
    return StoreSink(store, close=True)


def _graceful_sigterm() -> Any:
    """Make SIGTERM stop the run like Ctrl-C (partial results kept). Returns the previous
    handler, or ``None`` when it cannot be installed (not the main thread)."""

    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        return signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):  # not the main thread
        return None


def _fail_run(store_path: str, run_id: str, message: str) -> None:
    """Close a still-``running`` row (pre-inserted by the UI) as ``failed``, so a run that
    could not start never stays "running" forever."""
    from .storage import SQLiteStore

    try:
        store = SQLiteStore(store_path, background=False)
    except Exception:
        return
    try:
        run = store.get_run(run_id)
        if run is not None and run.status == "running":
            store.finish_run(run_id, "failed", {"message": message})
    except Exception:  # pragma: no cover - best effort
        pass
    finally:
        store.close()


def _configured_mount() -> str | None:
    """``mount_path`` from the Redline config of the project in the working directory."""
    from .config import resolve_settings

    try:
        return resolve_settings(None, project_root=os.getcwd()).settings.mount_path
    except Exception:  # an invalid or unreadable config must not break the run's summary
        return None


def _ui_hint(base_url: str, run_id: str, ui_url: str | None = None) -> str:
    """Link to the run in the UI: ``ui_url`` (a path on ``base_url``'s origin or an
    absolute URL; default ``/_redline``) + ``/#/load/<id>``."""
    from urllib.parse import urlsplit

    ui = (ui_url or "/_redline").strip()
    if not ui.startswith(("http://", "https://")):
        parts = urlsplit(base_url)
        ui = f"{parts.scheme}://{parts.netloc}/" + ui.lstrip("/")
    return f"{ui.rstrip('/')}/#/load/{run_id}"


def _cmd_load(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    if args.scenario == "init":  # pragma: no cover - routed before parsing
        return EXIT_USAGE
    from .load import control
    from .load.analysis import check_thresholds
    from .load.engine import run
    from .load.profile import ScenarioError
    from .load.scenario import load_scenario, validate_base_url, validate_ui_url
    from .load.sinks import JsonSink

    try:
        scenario = load_scenario(args.scenario)
        if args.base_url:
            scenario = scenario.replace(base_url=validate_base_url(args.base_url, "--base-url"))
        if not scenario.base_url:
            raise ScenarioError("is required (in the scenario or via --base-url)", "base_url")
        if args.ui_url:
            scenario = scenario.replace(ui_url=validate_ui_url(args.ui_url, "--ui-url"))
        profile = _cli_profile(args, scenario)
    except ScenarioError as exc:
        err.write(f"redline load: {exc}\n")
        if args.run_id and not args.no_store:  # started by the UI: its row must not stay "running"
            _fail_run(args.store, args.run_id, str(exc))
        return EXIT_USAGE
    except argparse.ArgumentTypeError as exc:
        err.write(f"redline load: {exc}\n")
        return EXIT_USAGE
    if args.workers is not None and args.workers < 1:
        err.write("redline load: --workers must be ≥ 1\n")
        return EXIT_USAGE

    import uuid

    run_id = args.run_id or uuid.uuid4().hex
    sinks: list[Any] = []
    store_path = None if args.no_store else args.store
    if store_path:
        sink = _open_store_sink(store_path, err)
        if sink is not None:
            sinks.append(sink)
        elif not args.json:
            fallback = control.runs_dir(store_path) / f"{run_id}.json"
            err.write(f"results: {fallback}\n")
            sinks.append(JsonSink(fallback))
    if args.json:
        sinks.append(JsonSink(args.json))

    stop = control.StopFile(run_id, store_path)
    if store_path:
        stop.clear()
        control.write_pid(run_id, store_path)
    previous_term = _graceful_sigterm()
    printer = _Printer(out, quiet=args.quiet)
    if not args.quiet:
        printer.line(f"redline load: {args.name or scenario.name} → {scenario.base_url}")
        printer.line(f"run {run_id}: {profile.describe()}")
    try:
        result = run(
            scenario,
            profile=profile,
            sink=sinks,
            stop_event=stop if store_path else None,
            workers=args.workers,
            run_id=run_id,
            name=args.name,
            on_event=printer,
        )
    except Exception as exc:
        err.write(f"redline load: {exc}\n")
        if store_path:
            _fail_run(store_path, run_id, f"{type(exc).__name__}: {exc}")
        return EXIT_USAGE if isinstance(exc, ScenarioError) else EXIT_FAIL
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        if store_path:
            control.cleanup(run_id, store_path)

    _print_final(printer, result)
    if store_path and sinks and not isinstance(sinks[0], JsonSink):
        ui_url = scenario.ui_url or _configured_mount()
        printer.line(f"Open the run in Redline: {_ui_hint(scenario.base_url, run_id, ui_url)}")
    if result.status == "failed":
        err.write(f"redline load: run failed: {result.message}\n")
        return EXIT_FAIL
    breaches = check_thresholds(
        result.summary, fail_p95_ms=args.fail_p95_ms, fail_error_rate=args.fail_error_rate
    )
    for b in breaches:
        printer.line(f"FAIL: {b}")
    if breaches:
        return EXIT_FAIL
    if result.status == "stopped" and "Ctrl-C" in (result.summary.get("stopped_reason") or ""):
        return EXIT_INTERRUPTED
    return EXIT_OK


def _print_final(printer: _Printer, result: Any) -> None:
    s = result.summary
    printer.line("")
    printer.line(
        f"run {result.run_id} {result.status}" + (f": {result.message}" if result.message else "")
    )
    if result.steps and not printer.header_done:  # live rows were not shown
        printer.line(printer.HEADER)
        for step in result.steps:
            printer.line(printer.row(step))
    if s.get("stopped_reason"):
        printer.line(f"stopped: {s['stopped_reason']}")
    printer.line(
        f"total: sent {s['sent']}  ok {s['ok']}  errors {s['errors']}  dropped {s['dropped']}  "
        f"p50 {fmt_ms(s['p50_ms'])}  p95 {fmt_ms(s['p95_ms'])}  p99 {fmt_ms(s['p99_ms'])}"
    )
    breakdown: dict[str, int] = {}
    for step in result.steps:
        for key, n in step.error_breakdown.items():
            breakdown[key] = breakdown.get(key, 0) + n
    if breakdown:
        printer.line("errors: " + ", ".join(f"{k} ×{v}" for k, v in sorted(breakdown.items())))
    if s.get("knee_step") is not None:
        printer.line(f"knee: step {s['knee_step']} – {s.get('knee_reason')}")
    else:
        printer.line("knee: none")
    max_ok = s.get("max_ok_rps")
    printer.line(f"max ok rps: {max_ok:g}" if max_ok is not None else "max ok rps: -")
    printer.line(f"verdict: {s['verdict']} ({s.get('verdict_detail')})")


# ============================================================================ load init
def _configure_init(p: argparse.ArgumentParser) -> None:
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-openapi", metavar="URL|FILE", help="OpenAPI JSON (URL or file)")
    src.add_argument("--from-traces", action="store_true", help="captured traffic in the store")
    p.add_argument("--store", default=DEFAULT_STORE, metavar="PATH")
    p.add_argument("--window", default="24h", help="traffic window for --from-traces (24h)")
    p.add_argument("--top", type=int, default=10, help="endpoints to keep (--from-traces)")
    p.add_argument("--include-writes", action="store_true", help="also POST/PUT/PATCH/DELETE")
    p.add_argument("--base-url", metavar="URL")
    p.add_argument("-o", "--out", default="scenario.toml", help="output (.toml/.json, - = stdout)")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")


def _cmd_init(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from .load import generate
    from .load.scenario import parse_scenario

    try:
        if args.from_openapi:
            doc = generate.load_openapi(args.from_openapi)
            data = generate.from_openapi(
                doc, include_writes=args.include_writes, base_url=args.base_url
            )
        else:
            from .storage import SQLiteStore, parse_window

            if not os.path.exists(args.store):
                err.write(f"redline load init: no store at {args.store}\n")
                return EXIT_USAGE
            store = SQLiteStore(args.store, background=False)
            try:
                data = generate.from_traces(
                    store,
                    window_s=parse_window(args.window),
                    top=args.top,
                    include_writes=args.include_writes,
                    base_url=args.base_url,
                )
            finally:
                store.close()
        parse_scenario(data)  # never write a scenario we could not run
    except Exception as exc:
        err.write(f"redline load init: {exc}\n")
        return EXIT_USAGE
    text = generate.to_json(data) if args.out.endswith(".json") else generate.to_toml(data)
    if args.out == "-":
        out.write(text)
        return EXIT_OK
    if os.path.exists(args.out) and not args.force:
        err.write(f"redline load init: {args.out} exists (use --force to overwrite)\n")
        return EXIT_USAGE
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    enabled = sum(1 for r in data["requests"] if r["weight"] > 0)
    out.write(
        f"wrote {args.out}: {len(data['requests'])} requests ({enabled} enabled), "
        f"base_url {data['base_url']}\n"
    )
    return EXIT_OK


# ============================================================================ config
def _configure_config(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument(
        "--project-root",
        metavar="DIR",
        help="where to look for redline.toml / pyproject.toml (default: the current directory)",
    )


def _resolve(args: argparse.Namespace, err: TextIO) -> Any:
    from .conf import SettingsError
    from .config import resolve_settings

    try:
        return resolve_settings(None, project_root=args.project_root or os.getcwd())
    except SettingsError as exc:
        err.write(f"redline: invalid configuration: {exc}\n")
        return None


def _show(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    return json.dumps(value)


def _cmd_config(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    resolved = _resolve(args, err)
    if resolved is None:
        return EXIT_USAGE
    data = resolved.public()
    if args.json:
        import json

        out.write(json.dumps(data, indent=2) + "\n")
        return EXIT_OK
    out.write(f"preset: {data['preset']}\n")
    out.write(f"config file: {data['file'] or '(none)'}\n")
    out.write(
        "note: settings given in code (REDLINE = {...}, install(settings=...)) are not "
        "visible here; GET <mount>/api/config shows the running app's.\n"
    )
    for group, names in data["groups"].items():
        if not names:
            continue
        out.write(f"\n[{group}]\n")
        width = max(len(n) for n in names)
        for name in names:
            source = data["sources"].get(name, "default")
            env = resolved.env_names.get(name)
            where = f"env {env}" if env and source == "env" else source
            out.write(f"  {name.ljust(width)}  {_show(data['settings'][name])}  ({where})\n")
    out.write("\nwarnings:\n" if data["warnings"] else "\nno warnings\n")
    for warning in data["warnings"]:
        out.write(f"  - {warning}\n")
    return EXIT_OK


def _doctor_checks(
    resolved: Any,
    *,
    strict: bool = False,
    workers: int | None = None,
    environ: Any = None,
) -> list[tuple[str, str, str]]:
    """``[(status, check, detail)]`` with status ``ok`` / ``warn`` / ``fail``.

    ``strict`` turns the deployment-shape warnings about a SQLite store (a network
    filesystem, several worker processes) into failures. ``workers`` is the worker count
    the deployment runs; without it the environment is read (``WEB_CONCURRENCY``,
    ``GUNICORN_CMD_ARGS``, ``UVICORN_WORKERS``; ``environ`` defaults to ``os.environ``).
    """
    import importlib.util

    from . import ui
    from .access import _parse_hash, _read_token
    from .conf import MIN_TOKEN_LENGTH, SettingsError

    s = resolved.settings
    checks: list[tuple[str, str, str]] = []

    # storage
    from .storage.safety import (
        UNRELIABLE_WAL_FILESYSTEMS,
        detect_workers,
        filesystem_type,
        prepare_path,
        probe_wal,
        probe_writable,
    )

    shape = "fail" if strict else "warn"  # deployment-shape problems of a SQLite store
    path: str | None = None
    if s.storage_url:
        checks += _postgres_checks(s.storage_url)
    else:
        try:
            path = prepare_path(s)
        except SettingsError as exc:
            checks.append(("fail", "storage", str(exc)))
    if path == ":memory:":
        checks.append(("ok", "storage", "in memory"))
    elif path is not None:
        target = os.path.abspath(path)
        kind = filesystem_type(target)
        try:
            probe_writable(target)
        except OSError:
            checks.append(("fail", "storage", f"cannot create or write {target}"))
        else:
            checks.append(("ok", "storage", f"{target} is writable"))
        if kind in UNRELIABLE_WAL_FILESYSTEMS:
            checks.append(
                (
                    shape,
                    "filesystem",
                    f"{kind} may not support SQLite WAL reliably; use a Docker named "
                    "volume or a local disk, or a shared PostgreSQL store (storage_url)",
                )
            )
        try:
            wal = probe_wal(target)
        except OSError:
            checks.append(("fail", "storage WAL", f"cannot test WAL beside {target}"))
        else:
            if wal:
                suffix = f" on {kind}" if kind else ""
                checks.append(("ok", "storage WAL", f"journal_mode=wal works{suffix}"))
            else:
                checks.append(
                    (
                        "fail",
                        "storage WAL",
                        "journal_mode=wal was refused; move storage to a supported local "
                        "filesystem",
                    )
                )

    if path is not None and path != ":memory:":
        declared = (workers, "--workers") if workers else detect_workers(environ)
        if declared is not None and declared[0] > 1:
            checks.append(
                (
                    shape,
                    "workers",
                    f"{declared[0]} worker processes ({declared[1]}) share one SQLite file: "
                    "their writes queue on its lock, and every host or container keeps its "
                    "own file. Set storage_url to a PostgreSQL database for one shared store",
                )
            )

    # UI bundle
    if ui.is_built():
        checks.append(("ok", "ui bundle", str(ui.static_dir())))
    else:
        checks.append(("fail", "ui bundle", "missing: build it with scripts/build_ui.sh"))

    # access
    policy = s.access
    if policy == "open":
        allowed = os.environ.get("REDLINE_ALLOW_OPEN", "").strip() == "1"
        checks.append(
            (
                "warn" if allowed else "fail",
                "access",
                "access='open' exposes every captured request to anyone"
                + ("" if allowed else " (and is refused without REDLINE_ALLOW_OPEN=1)"),
            )
        )
    elif policy == "local":
        if not resolved.access_explicit:
            checks.append(
                (
                    "warn",
                    "access",
                    "access is not set (in the config file or REDLINE_ACCESS): unless the "
                    "application sets it in code, the UI and API answer 404 outside framework "
                    "debug mode. Set access='token'/'basic', or access='local' to accept "
                    "that a reverse proxy on this machine without X-Forwarded-For makes every "
                    "visitor local",
                )
            )
        if s.trust_unix_socket:
            checks.append(
                (
                    "warn",
                    "access",
                    "trust_unix_socket: a reverse proxy reaching the app over a unix socket "
                    "makes every visitor local",
                )
            )
        if s.preset == "dev":
            checks.append(("ok", "access", "local: loopback clients only"))
        else:
            checks.append(
                (
                    "warn",
                    "access",
                    f"access='local' with the {s.preset!r} preset: behind a reverse proxy "
                    "nobody can reach Redline (proxied requests are refused); use 'token' or "
                    "'basic'",
                )
            )
    elif policy == "token":
        try:
            token = _read_token(s) or ""
        except SettingsError as exc:
            checks.append(("fail", "token", str(exc)))
        else:
            if len(token) < MIN_TOKEN_LENGTH:
                checks.append(
                    ("warn", "token", f"only {len(token)} characters (use {MIN_TOKEN_LENGTH}+)")
                )
            else:
                checks.append(("ok", "token", f"{len(token)} characters"))
            if s.token_file and os.name == "posix":
                try:
                    mode = os.stat(os.path.expanduser(s.token_file)).st_mode
                except OSError:
                    mode = 0
                if mode & 0o077:
                    checks.append(
                        ("warn", "token", f"{s.token_file} is readable by other users (chmod 600)")
                    )
    elif policy == "basic":
        for user, encoded in s.basic_users.items():
            try:
                iterations, _, _ = _parse_hash(encoded)
            except SettingsError as exc:
                checks.append(("fail", "basic", f"{user}: {exc}"))
                continue
            if iterations < 100_000:
                checks.append(
                    ("warn", "basic", f"{user}: weak hash ({iterations} PBKDF2 iterations)")
                )
        if not any(c[1] == "basic" for c in checks):
            checks.append(("ok", "basic", f"{len(s.basic_users)} user(s)"))
    else:
        checks.append(("ok", "access", f"access={policy!r}"))
    if policy in ("token", "basic") and not s.secret_key:
        checks.append(
            (
                "warn",
                "sessions",
                "secret_key unset: UI logins are per worker process (set REDLINE_SECRET_KEY)",
            )
        )
    if policy in ("token", "basic") and not s.trusted_proxies:
        checks.append(
            (
                "warn",
                "proxies",
                "trusted_proxies is empty: behind a reverse proxy all clients share its "
                "address – one login rate-limit bucket for everyone, and no Secure cookie "
                "from X-Forwarded-Proto",
            )
        )
    elif policy != "local" and not s.trusted_proxies and s.preset != "dev":
        checks.append(
            (
                "warn",
                "proxies",
                "trusted_proxies is empty: behind a proxy all clients share its address",
            )
        )

    # capture
    if s.preset == "production":
        captured = [
            n
            for n in ("capture_request_body", "capture_response_body", "capture_locals")
            if getattr(s, n)
        ]
        if captured:
            checks.append(("warn", "capture", "production preset captures " + ", ".join(captured)))
        else:
            checks.append(("ok", "capture", "no bodies or locals captured"))

    # retention
    if not s.history_retention_days:
        checks.append(("warn", "retention", "history_retention_days off: history grows forever"))
    elif not s.max_rows and not s.max_age_days:
        checks.append(("warn", "retention", "max_rows and max_age_days off: traces never pruned"))
    else:
        checks.append(
            (
                "ok",
                "retention",
                f"traces: {s.max_rows or '∞'} rows / {s.max_age_days or '∞'} days; "
                f"history: {s.history_retention_days} days",
            )
        )

    # load extra
    if s.load_from_ui and s.feature("load"):
        if importlib.util.find_spec("httpx") is None:
            checks.append(
                ("warn", "load", "load_from_ui is on but httpx is missing: redline-core[load]")
            )
        else:
            checks.append(("ok", "load", "load tester available"))
    return checks


def _postgres_checks(url: str) -> list[tuple[str, str, str]]:
    """The doctor's checks of a ``storage_url`` store: the driver, then a connection."""
    import importlib.util

    from .storage.postgres import mask_url, ping

    where = mask_url(url)
    if importlib.util.find_spec("psycopg") is None:
        return [
            (
                "fail",
                "storage",
                'storage_url needs the PostgreSQL driver: pip install "redline-core[postgres]"',
            )
        ]
    try:
        version = ping(url)
    except Exception as exc:
        return [("fail", "storage", f"cannot connect to {where} ({type(exc).__name__})")]
    return [("ok", "storage", f"PostgreSQL {version} at {where}, shared by every worker")]


def _configure_doctor(p: argparse.ArgumentParser) -> None:
    _configure_config(p)
    p.add_argument(
        "--strict",
        action="store_true",
        help="fail (not warn) when a SQLite store sits on a network filesystem or is shared "
        "by several worker processes",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="the number of worker processes the deployment runs (default: read "
        "WEB_CONCURRENCY, GUNICORN_CMD_ARGS and UVICORN_WORKERS)",
    )


def _cmd_doctor(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    resolved = _resolve(args, err)
    if resolved is None:
        out.write("FAIL  config      the configuration does not load (see above)\n")
        return EXIT_FAIL
    checks = _doctor_checks(
        resolved, strict=getattr(args, "strict", False), workers=getattr(args, "workers", None)
    )
    if args.json:
        import json

        out.write(
            json.dumps([{"status": st, "check": c, "detail": d} for st, c, d in checks], indent=2)
            + "\n"
        )
    else:
        out.write(f"preset {resolved.settings.preset}, config file {resolved.file or '(none)'}\n")
        for status, check, detail in checks:
            out.write(f"{status.upper():<5} {check:<11} {detail}\n")
    return EXIT_FAIL if any(c[0] == "fail" for c in checks) else EXIT_OK


def _configure_hash(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--iterations",
        type=int,
        default=None,
        metavar="N",
        help="PBKDF2-SHA256 iterations (default 600000)",
    )


def _cmd_hash_password(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    import getpass

    from .access import PBKDF2_ITERATIONS, hash_password

    iterations = args.iterations or PBKDF2_ITERATIONS
    if iterations < 1_000:
        err.write("redline hash-password: --iterations must be at least 1000\n")
        return EXIT_USAGE
    if sys.stdin is not None and not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\r\n")  # scripted: one line on stdin
    else:
        password = getpass.getpass("Password: ")
        again = getpass.getpass("Password (again): ")
        if password != again:
            err.write("redline hash-password: the passwords do not match\n")
            return EXIT_USAGE
    if not password:
        err.write("redline hash-password: the password must not be empty\n")
        return EXIT_USAGE
    out.write(hash_password(password, iterations=iterations) + "\n")
    return EXIT_OK


# ============================================================================ check
EXIT_UNREACHABLE = 3


def _fail_on_arg(value: str) -> tuple[str, ...]:
    from .check.report import parse_fail_on

    try:
        return parse_fail_on(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _check_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("suite", help="suite file (.toml or .json)")
    p.add_argument("--base-url", metavar="URL", help="override the suite's base_url")
    p.add_argument(
        "--store",
        metavar="PATH",
        help="the app's Redline store, for server-side metrics (default: the configured "
        "storage_path when that file exists)",
    )
    p.add_argument("--no-store", action="store_true", help="client-side metrics only")
    p.add_argument("--label", metavar="TEXT", help="a label for this run (e.g. 'before')")
    p.add_argument("--quiet", "-q", action="store_true", help="no progress line")


def _check_compare_args(p: argparse.ArgumentParser) -> None:
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--baseline", metavar="FILE", help="baseline JSON (default <suite>.baseline.json)"
    )
    src.add_argument("--baseline-id", metavar="ID", help="a baseline stored in --store")
    p.add_argument("--json", metavar="OUT", help="write the full report as JSON")
    p.add_argument("--markdown", metavar="OUT", help="write a Markdown PR comment")
    p.add_argument("--junit", metavar="OUT", help="write JUnit XML (one testcase per request)")
    p.add_argument(
        "--fail-on",
        type=_fail_on_arg,
        default=None,
        metavar="LIST",
        help="verdicts that make the exit code 1 (default slower,changed,broken; 'none')",
    )
    p.add_argument("--slower-pct", type=float, metavar="X", help="override thresholds.slower_pct")
    p.add_argument("--slower-ms", type=float, metavar="Y", help="override thresholds.slower_ms")
    p.add_argument("--no-color", action="store_true", help="no ANSI colours")
    p.add_argument(
        "--ruff",
        nargs="?",
        const="",
        metavar="PATH",
        help="include Ruff JSON from PATH, or run `ruff check --select F` and include it",
    )


def _configure_check(p: argparse.ArgumentParser) -> None:
    p.add_argument("rest", nargs="*", help=argparse.SUPPRESS)


def _configure_check_record(p: argparse.ArgumentParser) -> None:
    _check_common(p)
    p.add_argument("--out", metavar="FILE", help="baseline file (default <suite>.baseline.json)")


def _configure_check_compare(p: argparse.ArgumentParser) -> None:
    _check_common(p)
    _check_compare_args(p)


def _configure_check_run(p: argparse.ArgumentParser) -> None:
    _check_common(p)
    _check_compare_args(p)
    p.add_argument("--out", metavar="FILE", help="baseline file to write when recording")


def _configure_check_init(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--from-traces", action="store_true", required=True, help="recorded traffic in the store"
    )
    p.add_argument("--store", metavar="PATH", help="Redline store (default: configured path)")
    p.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="PATTERN",
        help="only routes matching this pattern (fnmatch; repeatable)",
    )
    p.add_argument("--limit", type=int, default=20, metavar="N", help="at most N requests (20)")
    p.add_argument("--base-url", metavar="URL")
    p.add_argument("-o", "--out", default="check.toml", help="output (.toml/.json, - = stdout)")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")


def _configure_check_show(p: argparse.ArgumentParser) -> None:
    p.add_argument("report", help="report JSON written by `redline check compare --json`")
    p.add_argument("--fail-on", type=_fail_on_arg, default=None, metavar="LIST")
    p.add_argument("--markdown", metavar="OUT", help="also write the Markdown PR comment")
    p.add_argument("--junit", metavar="OUT", help="also write JUnit XML")
    p.add_argument("--no-color", action="store_true")
    p.add_argument(
        "--ruff",
        nargs="?",
        const="",
        metavar="PATH",
        help="include Ruff JSON from PATH, or run `ruff check --select F` and include it",
    )


def _cmd_check(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    err.write(
        "usage: redline check {record,compare,run,init,show,ratchet,comment} …\n\n"
        + "".join(f"  redline check {n:<8} {h}\n" for n, (h, _, _) in CHECK_COMMANDS.items())
    )
    return EXIT_USAGE


def _configured_store_path() -> str:
    from .config import resolve_settings

    try:
        return resolve_settings(None, project_root=os.getcwd()).settings.storage_path
    except Exception:
        return DEFAULT_STORE


def _project_redactor() -> Any:
    """A ``Redactor`` with the project's redaction settings (defaults when unreadable)."""
    from .config import resolve_settings
    from .redact import Redactor

    try:
        return Redactor(resolve_settings(None, project_root=os.getcwd()).settings)
    except Exception:
        return Redactor()


class _CheckStore:
    """The store, its check sink and server metrics; all ``None`` when unavailable."""

    def __init__(self, args: argparse.Namespace, err: TextIO, *, needed: bool = False) -> None:
        self.store: Any = None
        self.sink: Any = None
        self.metrics: Any = None
        self.module: Any = None
        if getattr(args, "no_store", False):
            return
        path = args.store or _configured_store_path()
        if path == ":memory:" or not os.path.exists(path):
            if args.store or needed:
                err.write(f"redline check: no store at {path}; server-side metrics off\n")
            return
        try:
            from .check import sink as module
        except ImportError:
            err.write(
                "redline check: server-side metrics need redline-core with check storage; "
                "continuing client-side only\n"
            )
            return
        from .storage import SQLiteStore

        try:
            self.store = SQLiteStore(path, background=False)
            self.sink = module.StoreCheckSink(self.store)
            self.metrics = module.StoreServerMetrics(self.store)
            self.module = module
        except Exception as exc:
            err.write(f"redline check: cannot use the store {path}: {exc}; client-side only\n")
            self.close()

    def close(self) -> None:
        if self.store is not None:
            try:
                self.store.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self.store = self.sink = self.metrics = None


class _CheckProgress:
    """``[done/total] request`` on one line, TTY only."""

    def __init__(self, out: TextIO, *, quiet: bool) -> None:
        self.out = out
        self.enabled = not quiet and bool(getattr(out, "isatty", lambda: False)())
        self.width = 0

    def __call__(self, event: dict) -> None:
        if not self.enabled:
            return
        text = f"  [{event['done']}/{event['total']}] {event['phase']:<6} {event['name']}"[:100]
        self.out.write("\r" + text.ljust(self.width))
        self.out.flush()
        self.width = len(text)

    def clear(self) -> None:
        if self.enabled and self.width:
            self.out.write("\r" + " " * self.width + "\r")
            self.out.flush()
            self.width = 0


def _load_suite_for(args: argparse.Namespace, err: TextIO) -> tuple[Any, str] | None:
    from dataclasses import replace

    from .check.suite import Suite, SuiteError, validate_base_url

    try:
        suite = Suite.from_file(args.suite)
        base_url = args.base_url or suite.base_url
        if not base_url:
            raise SuiteError("is required (in the suite or via --base-url)", "base_url")
        base_url = validate_base_url(base_url, "--base-url" if args.base_url else "base_url")
        th = suite.thresholds
        for name in ("slower_pct", "slower_ms"):
            value = getattr(args, name, None)
            if value is not None:
                if not math.isfinite(value) or value < 0:
                    raise SuiteError(f"must be a number ≥ 0 (got {value!r})", f"--{name}")
                th = replace(th, **{name: float(value)})
        suite = suite.replace(thresholds=th)
    except SuiteError as exc:
        err.write(f"redline check: {exc}\n")
        return None
    return suite, base_url


def _run_check(
    args: argparse.Namespace,
    out: TextIO,
    *,
    err: TextIO,
    suite: Any,
    base_url: str,
    kind: str,
    baseline: Any,
    store: _CheckStore,
) -> Any:
    """``run_suite`` with progress and SIGTERM → Ctrl-C; the result, or an exit code."""
    from .check.baseline import BaselineError
    from .check.runner import CheckUnreachable, run_suite
    from .check.suite import SuiteError

    progress = _CheckProgress(out, quiet=args.quiet)
    previous_term = _graceful_sigterm()
    if not args.quiet:
        what = "recording a baseline" if kind == "baseline" else "comparing with the baseline"
        out.write(
            f"redline check: {suite.name} → {base_url} ({what}; {len(suite.requests)} requests "
            f"× {suite.repeat}, warmup {suite.warmup})\n"
        )
        out.flush()
    try:
        return run_suite(
            suite,
            base_url=base_url,
            kind=kind,  # type: ignore[arg-type]
            baseline=baseline,
            label=args.label,
            sink=store.sink,
            server_metrics=store.metrics,
            on_progress=progress,
            redactor=_project_redactor(),
        )
    except (SuiteError, BaselineError) as exc:
        progress.clear()
        err.write(f"redline check: {exc}\n")
        return EXIT_USAGE
    except CheckUnreachable as exc:
        progress.clear()
        err.write(f"redline check: {exc}\n")
        return EXIT_UNREACHABLE
    except Exception as exc:
        progress.clear()
        err.write(f"redline check: {type(exc).__name__}: {exc}\n")
        return EXIT_FAIL
    finally:
        progress.clear()
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


def _print_baseline(out: TextIO, baseline: Any) -> None:
    rows = []
    for r in baseline.requests:
        perf = r.get("perf") or {}
        srv = r.get("server") or {}
        status = ",".join(f"{k}×{v}" for k, v in sorted((r.get("statuses") or {}).items()))
        rows.append(
            (
                r["name"][:40],
                status or "-",
                f"{r.get('failed', 0)}/{r.get('calls', 0)}",
                fmt_ms(perf["median"]) if perf.get("median") is not None else "-",
                fmt_ms(perf["p95"]) if perf.get("p95") is not None else "-",
                f"{perf['cv']:.2f}" if perf.get("cv") is not None else "-",
                f"{srv['queries_median']:g}" if srv.get("queries_median") is not None else "-",
                str(len(r.get("volatile") or []) + len(r.get("volatile_headers") or [])),
                "  BROKEN" if r.get("broken") else "",
            )
        )
    w = max([7, *(len(r[0]) for r in rows)])
    out.write(
        f"\n{'request'.ljust(w)}  {'status':<10} {'failed':>7} {'median':>8} {'p95':>8} "
        f"{'cv':>5} {'queries':>7} {'volatile':>8}\n"
    )
    for name, st, failed, med, p95, cv, q, vol, broken in rows:
        out.write(
            f"{name.ljust(w)}  {st:<10} {failed:>7} {med:>8} {p95:>8} {cv:>5} {q:>7} "
            f"{vol:>8}{broken}\n"
        )


def _cmd_check_record(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from .check.baseline import default_baseline_path

    loaded = _load_suite_for(args, err)
    if loaded is None:
        return EXIT_USAGE
    suite, base_url = loaded
    target = args.out or str(default_baseline_path(args.suite))
    store = _CheckStore(args, err)
    try:
        result = _run_check(
            args,
            out,
            err=err,
            suite=suite,
            base_url=base_url,
            kind="baseline",
            baseline=None,
            store=store,
        )
    finally:
        store.close()
    if isinstance(result, int):
        return result
    if result.status == "stopped":
        err.write("redline check: stopped – baseline not written\n")
        return EXIT_INTERRUPTED
    result.write(target, redactor=_project_redactor())
    _print_baseline(out, result)
    s = result.summary()
    git = result.git or {}
    sha = (git.get("sha") or "")[:8]
    where = f", git {sha}{' (dirty)' if git.get('dirty') else ''}" if sha else ""
    out.write(
        f"\nwrote baseline {target}: {s['requests']} requests, {s['calls']} timed calls{where}"
        + ("" if s["server_side"] else "; server-side metrics: unavailable")
        + "\n"
    )
    if s["broken"]:
        err.write(
            f"warning: {s['broken']} request(s) are broken in the baseline (≥ 50% failed calls)\n"
        )
    return EXIT_OK


def _load_baseline(args: argparse.Namespace, err: TextIO, store: _CheckStore) -> Any:
    from .check.baseline import Baseline, BaselineError, default_baseline_path

    if args.baseline_id:
        loader = getattr(store.module, "load_baseline", None) if store.module else None
        if loader is None:
            err.write("redline check: --baseline-id needs --store with check storage\n")
            return None
        try:
            data = loader(store.store, args.baseline_id)
        except Exception as exc:
            err.write(f"redline check: cannot load baseline {args.baseline_id}: {exc}\n")
            return None
        if data is None:
            err.write(f"redline check: no baseline {args.baseline_id} in the store\n")
            return None
        try:
            return data if isinstance(data, Baseline) else Baseline.from_dict(data)
        except BaselineError as exc:
            err.write(f"redline check: {exc}\n")
            return None
    path = args.baseline or str(default_baseline_path(args.suite))
    if not os.path.exists(path):
        err.write(
            f"redline check: no baseline at {path}; record one first: redline check record "
            f"{args.suite}\n"
        )
        return None
    try:
        return Baseline.from_file(path)
    except BaselineError as exc:
        err.write(f"redline check: {exc}\n")
        return None


def _attach_ruff(args: argparse.Namespace, report: Any, err: TextIO) -> None:
    """Add Ruff's JSON output without making Redline a linter or changing its exit model."""
    import json
    import shutil
    import subprocess

    spec = getattr(args, "ruff", None)
    if spec is None:
        return
    try:
        if spec:
            with open(spec, encoding="utf-8") as fh:
                found = json.load(fh)
        else:
            executable = shutil.which("ruff")
            if executable is None:
                raise FileNotFoundError("ruff is not installed or not on PATH")
            result = subprocess.run(
                [executable, "check", "--select", "F", "--output-format", "json"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode not in (0, 1):
                raise ValueError(f"ruff exited {result.returncode}")
            found = json.loads(result.stdout or "[]")
        if not isinstance(found, list):
            raise ValueError("Ruff JSON must be a list")
        items = []
        for item in found[:200]:
            if not isinstance(item, dict):
                continue
            location = item.get("location") or {}
            filename = str(item.get("filename") or "")
            items.append(
                {
                    "code": str(item.get("code") or "?"),
                    "filename": os.path.relpath(filename, os.getcwd()) if filename else "?",
                    "row": int(location.get("row") or 0),
                    "column": int(location.get("column") or 0),
                    "message": str(item.get("message") or "")[:500],
                }
            )
        items.sort(key=lambda i: (i["filename"], i["row"], i["column"], i["code"]))
        report.extra["ruff"] = items
        report.extra["ruff_total"] = len(found)
    except Exception as exc:
        message = type(exc).__name__
        if isinstance(exc, FileNotFoundError) and exc.filename is None:
            message = str(exc)
        report.extra["ruff_error"] = message
        err.write(f"redline check: Ruff result unavailable ({message})\n")


def _write_report_outputs(args: argparse.Namespace, report: Any, out: TextIO) -> None:
    for attr, render in (
        ("json", report.to_json),
        ("markdown", report.to_markdown),
        ("junit", report.to_junit),
    ):
        target = getattr(args, attr, None)
        if target:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(render())
            out.write(f"wrote {attr}: {target}\n")


def _use_color(args: argparse.Namespace, out: TextIO) -> bool:
    return (
        not getattr(args, "no_color", False)
        and not os.environ.get("NO_COLOR")
        and bool(getattr(out, "isatty", lambda: False)())
    )


def _cmd_check_compare(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from .check.report import DEFAULT_FAIL_ON

    loaded = _load_suite_for(args, err)
    if loaded is None:
        return EXIT_USAGE
    suite, base_url = loaded
    store = _CheckStore(args, err, needed=bool(args.baseline_id))
    try:
        baseline = _load_baseline(args, err, store)
        if baseline is None:
            return EXIT_USAGE
        result = _run_check(
            args,
            out,
            err=err,
            suite=suite,
            base_url=base_url,
            kind="compare",
            baseline=baseline,
            store=store,
        )
    finally:
        store.close()
    if isinstance(result, int):
        return result
    report = result.with_fail_on(args.fail_on if args.fail_on is not None else DEFAULT_FAIL_ON)
    _attach_ruff(args, report, err)
    out.write("\n" + report.to_terminal(color=_use_color(args, out)))
    _write_report_outputs(args, report, out)
    if report.status == "stopped":
        err.write("redline check: stopped – partial report\n")
        return EXIT_INTERRUPTED
    return report.exit_code()


def _cmd_check_run(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from .check.baseline import default_baseline_path

    if args.baseline_id:
        return _cmd_check_compare(args, out, err)
    path = args.baseline or args.out or str(default_baseline_path(args.suite))
    if os.path.exists(path):
        args.baseline = path
        return _cmd_check_compare(args, out, err)
    args.out = path
    out.write(f"no baseline at {path}: recording one\n")
    return _cmd_check_record(args, out, err)


def _configure_check_comment(p: argparse.ArgumentParser) -> None:
    p.add_argument("report", help="report JSON (`--json`) or a Markdown file (`--markdown`)")
    p.add_argument(
        "--marker-id",
        default="redline-check",
        metavar="ID",
        help="identifies the sticky comment (default redline-check); use one per suite",
    )
    p.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        metavar="VAR",
        help="environment variable holding the token (default GITHUB_TOKEN)",
    )
    p.add_argument("--no-comment", action="store_true", help="only write the job summary")
    p.add_argument("--dry-run", action="store_true", help="print the comment body, send nothing")


def _cmd_check_comment(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from .check import github
    from .check.report import Report, ReportError

    try:
        with open(args.report, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        err.write(f"redline check comment: {args.report}: {exc}\n")
        return EXIT_USAGE
    if args.report.lower().endswith(".md"):
        markdown = raw
    else:
        try:
            markdown = Report.from_dict(json.loads(raw)).to_markdown()
        except (ValueError, ReportError) as exc:
            err.write(f"redline check comment: {args.report}: {exc}\n")
            return EXIT_USAGE
    body = github.sticky_body(markdown, args.marker_id)
    if args.dry_run:
        out.write(body)
        return EXIT_OK
    if github.write_job_summary(markdown):
        out.write("wrote the job summary\n")
    if args.no_comment:
        return EXIT_OK
    number = github.pull_request_number()
    token = os.environ.get(args.token_env, "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if number is None:
        out.write("not a pull_request event: no comment posted\n")
        return EXIT_OK
    if not token or not repository:
        out.write(f"no {args.token_env} or GITHUB_REPOSITORY: no comment posted\n")
        return EXIT_OK
    try:
        action, comment_id = github.upsert_comment(
            body,
            token=token,
            repository=repository,
            pr_number=number,
            marker_id=args.marker_id,
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
    except github.CommentError as exc:
        # fork PRs get a read-only token; a missing comment must not fail the check itself
        err.write(f"redline check comment: {exc} (comment skipped)\n")
        return EXIT_OK
    out.write(f"{action} PR comment {comment_id}\n")
    return EXIT_OK


def _configure_check_ratchet(p: argparse.ArgumentParser) -> None:
    _check_common(p)
    p.add_argument(
        "--ratchet-file",
        metavar="FILE",
        help="committed ratchet JSON (default <suite>.ratchet.json)",
    )
    p.add_argument(
        "--update",
        action="store_true",
        help="when nothing regressed, write the file: improvements tighten it, new endpoints "
        "are added (required to create the file)",
    )
    p.add_argument("--no-latency", action="store_true", help="ratchet query counts only")
    p.add_argument("--latency-pct", type=float, default=30.0, metavar="X")
    p.add_argument("--latency-ms", type=float, default=20.0, metavar="Y")
    p.add_argument("--json", metavar="OUT", help="write the verdict as JSON")
    p.add_argument("--markdown", metavar="OUT", help="write a Markdown summary")


def _cmd_check_ratchet(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from .check import ratchet

    loaded = _load_suite_for(args, err)
    if loaded is None:
        return EXIT_USAGE
    suite, base_url = loaded
    path = args.ratchet_file or os.path.splitext(args.suite)[0] + ".ratchet.json"
    try:
        recorded = ratchet.load(path)
    except FileNotFoundError:
        if not args.update:
            err.write(f"redline check ratchet: {path}: no such file (create it with --update)\n")
            return EXIT_USAGE
        recorded = {}
    except ratchet.RatchetError as exc:
        err.write(f"redline check ratchet: {exc}\n")
        return EXIT_USAGE
    store = _CheckStore(args, err)
    try:
        result = _run_check(
            args,
            out,
            err=err,
            suite=suite,
            base_url=base_url,
            kind="baseline",
            baseline=None,
            store=store,
        )
    finally:
        store.close()
    if isinstance(result, int):
        return result
    if result.status == "stopped":
        err.write("redline check ratchet: stopped – ratchet file not touched\n")
        return EXIT_INTERRUPTED
    observed = ratchet.observe(result.requests, latency=not args.no_latency)
    if recorded and not any(o.get("queries") is not None for o in observed.values()):
        err.write("redline check ratchet: no server-side query counts (use --store)\n")
    verdict = ratchet.compare(
        recorded, observed, latency_pct=args.latency_pct, latency_ms=args.latency_ms
    )
    out.write("\n" + verdict.to_markdown("Redline ratchet"))
    for attr, text in (
        ("json", json.dumps(verdict.to_dict(), indent=2) + "\n"),
        ("markdown", verdict.to_markdown()),
    ):
        if getattr(args, attr):
            with open(getattr(args, attr), "w", encoding="utf-8") as fh:
                fh.write(text)
    if not verdict.ok:
        return EXIT_FAIL
    if args.update and verdict.updated != recorded:
        ratchet.write(path, verdict.updated)
        out.write(f"wrote {path}\n")
    return EXIT_OK


def _cmd_check_init(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from .check.generate import from_traces
    from .check.suite import Suite
    from .storage import SQLiteStore

    path = args.store or _configured_store_path()
    if path == ":memory:" or not os.path.exists(path):
        err.write(f"redline check init: no store at {path}\n")
        return EXIT_USAGE
    if args.limit < 1:
        err.write("redline check init: --limit must be ≥ 1\n")
        return EXIT_USAGE
    try:
        store = SQLiteStore(path, background=False)
        try:
            data = from_traces(store, routes=args.route, limit=args.limit, base_url=args.base_url)
        finally:
            store.close()
        suite = Suite.from_dict(data)
    except Exception as exc:
        err.write(f"redline check init: {exc}\n")
        return EXIT_USAGE
    text = suite.to_json() if args.out.endswith(".json") else suite.to_toml()
    if suite.base_url is None and not args.out.endswith(".json"):
        # never guess the address: leave a commented example to fill in
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("name = "):
                lines.insert(i + 1, '# base_url = "http://127.0.0.1:8000"')
                break
        text = "\n".join(lines)
    if args.out == "-":
        out.write(text)
        return EXIT_OK
    if os.path.exists(args.out) and not args.force:
        err.write(f"redline check init: {args.out} exists (use --force to overwrite)\n")
        return EXIT_USAGE
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    env = suite.env_names()
    where = (
        f"base_url {suite.base_url}"
        if suite.base_url
        else "no base_url (set it in the suite or pass --base-url when running)"
    )
    out.write(
        f"wrote {args.out}: {len(suite.requests)} read-only requests, {where}\n"
        + (f"set {', '.join(env)} in the environment before running it\n" if env else "")
    )
    return EXIT_OK


def _cmd_check_show(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    import json

    from .check.report import Report, ReportError

    try:
        with open(args.report, encoding="utf-8") as fh:
            report = Report.from_dict(json.load(fh))
    except FileNotFoundError:
        err.write(f"redline check show: {args.report}: file not found\n")
        return EXIT_USAGE
    except (OSError, ValueError, ReportError) as exc:
        err.write(f"redline check show: {args.report}: {exc}\n")
        return EXIT_USAGE
    if args.fail_on is not None:
        report = report.with_fail_on(args.fail_on)
    _attach_ruff(args, report, err)
    out.write(report.to_terminal(color=_use_color(args, out)))
    _write_report_outputs(args, report, out)
    return report.exit_code() if args.fail_on is not None else EXIT_OK


CHECK_COMMANDS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None], Any]] = {
    "record": ("run a suite and write a baseline", _configure_check_record, _cmd_check_record),
    "compare": (
        "run a suite again and compare it with a baseline (exit 1 on --fail-on verdicts, "
        "2 usage/suite error, 3 app unreachable)",
        _configure_check_compare,
        _cmd_check_compare,
    ),
    "run": (
        "record when there is no baseline file yet, else compare",
        _configure_check_run,
        _cmd_check_run,
    ),
    "init": (
        "write a suite from recorded read-only traffic",
        _configure_check_init,
        _cmd_check_init,
    ),
    "show": ("print a saved report (JSON) again", _configure_check_show, _cmd_check_show),
    "ratchet": (
        "fail on new query-count/latency regressions against a committed file; "
        "--update tightens it",
        _configure_check_ratchet,
        _cmd_check_ratchet,
    ),
    "comment": (
        "post a saved report as the sticky PR comment and job summary (GitHub Actions)",
        _configure_check_comment,
        _cmd_check_comment,
    ),
}


def _configure_analyze(parser):
    parser.add_argument(
        "path", nargs="?", default=".", help="Python source file or project directory"
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument("--fail", action="store_true", help="exit 1 when findings are present")
    parser.add_argument("--max-files", type=int, default=200, help="Python file budget (1–10000)")
    parser.add_argument(
        "--max-project-bytes", type=int, default=2_000_000, help="source byte budget (1–64000000)"
    )


def _cmd_analyze(args, out, err):
    import json
    from pathlib import Path

    from .analyze import analyze_project, analyze_source, validate_scan_limits

    try:
        path = Path(args.path)
        validate_scan_limits(args.max_files, args.max_project_bytes)
        report = (
            analyze_project(
                path, max_files=args.max_files, max_project_bytes=args.max_project_bytes
            )
            if path.is_dir()
            else analyze_source(path.read_text(encoding="utf-8"), str(path))
        )
    except (OSError, UnicodeError, ValueError) as exc:
        err.write(f"redline analyze: {exc}\n")
        return 2
    if args.json:
        out.write(json.dumps(report, indent=2) + "\n")
    else:
        for item in report["findings"]:
            out.write(
                f"{item['file']}:{item['line']}: {item['kind']}: {item['detail']}\n"
                f"  {item['suggestion']}\n"
            )
        out.write(f"{len(report['findings'])} findings; {len(report['errors'])} source errors\n")
        if report.get("limited"):
            out.write("Scan stopped at its file/byte limit; coverage is incomplete.\n")
    if report["errors"] or report.get("limited"):
        return 2
    return 1 if args.fail and report["findings"] else 0


# ============================================================================ dispatch
Handler = Callable[[argparse.Namespace, TextIO, TextIO], int]

#: name -> (help, configure(parser), handler, subcommands {name: (help, configure, handler)})
COMMANDS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None], Handler, dict]] = {
    "export": (
        "preview or explicitly send sanitized OTLP recording summaries",
        operations.configure_export,
        operations.export_command,
        {},
    ),
    "init": (
        "detect the framework and write a starter redline.toml (never overwrites without --force)",
        project_init.configure,
        project_init.command,
        {},
    ),
    "digest": (
        "post newly seen issues to a chat webhook (deduplicated, with a state file)",
        digest.configure,
        digest.command,
        {},
    ),
    "audit-store": (
        "count possible unmasked profile keys without displaying values",
        operations.configure_audit,
        operations.audit_command,
        {},
    ),
    "check-imports": (
        "explicitly import a development target in an isolated subprocess",
        operations.configure_imports,
        operations.imports_command,
        {},
    ),
    "check-sql": ("lint SQL in Python without importing the app", _configure_sql, _cmd_sql, {}),
    "schema-watch": (
        "recompile SQL after schema drift and optionally raise a UI issue",
        _configure_schema_watch,
        _cmd_schema_watch,
        {},
    ),
    "analyze": ("analyze Python source without executing it", _configure_analyze, _cmd_analyze, {}),
    "load": (
        "run a load test (open model) against a running app",
        _configure_load,
        _cmd_load,
        {"init": ("write a scenario file", _configure_init, _cmd_init)},
    ),
    "check": (
        "refactor check: record a baseline of read-only requests, then compare a new version",
        _configure_check,
        _cmd_check,
        CHECK_COMMANDS,
    ),
    "config": (
        "print the effective configuration, where each value comes from, and warnings",
        _configure_config,
        _cmd_config,
        {},
    ),
    "doctor": (
        "check storage, the UI bundle and the access settings",
        _configure_doctor,
        _cmd_doctor,
        {},
    ),
    "hash-password": (
        "hash a password for basic_users (asks twice; reads one line when piped)",
        _configure_hash,
        _cmd_hash_password,
        {},
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="redline", description="Redline command line tools.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (help_text, configure, _, subs) in COMMANDS.items():
        epilog = "".join(f"\n  redline {name} {s} …   {h}" for s, (h, _, _) in subs.items())
        p = sub.add_parser(
            name,
            help=help_text,
            description=help_text,
            epilog=("subcommands:" + epilog) if epilog else None,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        configure(p)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out, err = sys.stdout, sys.stderr
    # "redline <cmd> <sub> ..." for nested subcommands (e.g. "redline load init")
    if len(argv) >= 2 and argv[0] in COMMANDS and argv[1] in COMMANDS[argv[0]][3]:
        help_text, configure, handler = COMMANDS[argv[0]][3][argv[1]]
        p = argparse.ArgumentParser(prog=f"redline {argv[0]} {argv[1]}", description=help_text)
        configure(p)
        args = p.parse_args(argv[2:])
        return handler(args, out, err)
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(out)
        return EXIT_USAGE
    return COMMANDS[args.command][2](args, out, err)


if __name__ == "__main__":  # python -m redline_core.cli
    sys.exit(main())
