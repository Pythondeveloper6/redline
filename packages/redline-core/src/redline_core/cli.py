"""The ``redline`` command line (console script ``redline = redline_core.cli:main``).

Subcommands are registered in :data:`COMMANDS`; each is a module-level ``(configure,
handler)`` pair so new ones (``scan``, …) plug in without touching the dispatcher. Heavy
imports happen inside the handlers, so ``redline --help`` stays instant.

Exit codes: 0 ok, 1 a ``--fail-*`` threshold was breached or the run failed, 2 usage or
scenario error, 130 interrupted (Ctrl-C).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, TextIO

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


def _ui_hint(base_url: str, run_id: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}/_redline/#/load/{run_id}"


def _cmd_load(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    if args.scenario == "init":  # pragma: no cover - routed before parsing
        return EXIT_USAGE
    from .load import control
    from .load.analysis import check_thresholds
    from .load.engine import run
    from .load.profile import ScenarioError
    from .load.scenario import load_scenario, validate_base_url
    from .load.sinks import JsonSink

    try:
        scenario = load_scenario(args.scenario)
        if args.base_url:
            scenario = scenario.replace(base_url=validate_base_url(args.base_url, "--base-url"))
        if not scenario.base_url:
            raise ScenarioError("is required (in the scenario or via --base-url)", "base_url")
        profile = _cli_profile(args, scenario)
    except ScenarioError as exc:
        err.write(f"redline load: {exc}\n")
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
    except (ScenarioError, RuntimeError) as exc:
        err.write(f"redline load: {exc}\n")
        return EXIT_USAGE if isinstance(exc, ScenarioError) else EXIT_FAIL
    finally:
        if store_path:
            control.cleanup(run_id, store_path)

    _print_final(printer, result)
    if store_path and sinks and not isinstance(sinks[0], JsonSink):
        printer.line(f"See bottlenecks in the Redline UI at {_ui_hint(scenario.base_url, run_id)}")
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


def _doctor_checks(resolved: Any) -> list[tuple[str, str, str]]:
    """``[(status, check, detail)]`` with status ``ok`` / ``warn`` / ``fail``."""
    import importlib.util

    from . import ui
    from .access import _parse_hash, _read_token
    from .conf import MIN_TOKEN_LENGTH, SettingsError

    s = resolved.settings
    checks: list[tuple[str, str, str]] = []

    # storage
    path = s.storage_path
    if path == ":memory:":
        checks.append(("ok", "storage", "in memory"))
    else:
        target = os.path.abspath(path)
        parent = os.path.dirname(target)
        probe = parent
        while probe and not os.path.exists(probe):
            probe = os.path.dirname(probe) if os.path.dirname(probe) != probe else ""
        if os.path.exists(target) and not os.access(target, os.W_OK):
            checks.append(("fail", "storage", f"{target} is not writable"))
        elif probe and os.access(probe, os.W_OK | os.X_OK):
            checks.append(("ok", "storage", f"{target} is writable"))
        else:
            checks.append(("fail", "storage", f"cannot create or write {target}"))

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
    if policy != "local" and not s.trusted_proxies and s.preset != "dev":
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


def _cmd_doctor(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    resolved = _resolve(args, err)
    if resolved is None:
        out.write("FAIL  config      the configuration does not load (see above)\n")
        return EXIT_FAIL
    checks = _doctor_checks(resolved)
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


# ============================================================================ dispatch
Handler = Callable[[argparse.Namespace, TextIO, TextIO], int]

#: name -> (help, configure(parser), handler, subcommands {name: (help, configure, handler)})
COMMANDS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None], Handler, dict]] = {
    "load": (
        "run a load test (open model) against a running app",
        _configure_load,
        _cmd_load,
        {"init": ("write a scenario file", _configure_init, _cmd_init)},
    ),
    "config": (
        "print the effective configuration, where each value comes from, and warnings",
        _configure_config,
        _cmd_config,
        {},
    ),
    "doctor": (
        "check storage, the UI bundle and the access settings",
        _configure_config,
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
