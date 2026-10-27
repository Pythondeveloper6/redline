"""The check runner (``CONTRACT.md`` section 19.2).

Sequential, one ``httpx.Client`` with a single keep-alive connection, no concurrency: the
goal is a stable comparison, not load. The schedule is::

    warmup × (r0, r1, …, rN)     untimed, ``n=0``
    repeat × (r0, r1, …, rN)     timed, ``n=1…repeat``

so drift (CPU throttling, caches warming up) spreads evenly over every request. A request
with ``perf = false`` only takes part in the first and last timed rounds. Every call
carries ``X-Redline-Check: <run_id>;req=<index>;n=<call#>`` so the app's traces of the
check can be found again (:class:`ServerMetrics`).

Per call: status, wall time (``perf_counter``, body read included), response size. The
first and last successful timed responses are captured (:func:`~redline_core.check.diff.
capture`); their differences are the request's volatile fields. Connection errors and
timeouts are failed calls (status ``None``); ≥ 50 % failed calls make a request
``broken``. When every call of the first round fails to connect the run stops with
:class:`CheckUnreachable`.

``stop`` (a ``threading.Event``) or ``KeyboardInterrupt`` (Ctrl-C; the CLI maps SIGTERM to
it) stop the run between two calls: the sink gets ``finish(..., "stopped", ...)`` and
the partial result is returned with ``status == "stopped"``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal, Protocol, Union, runtime_checkable

from .baseline import Baseline, git_info
from .diff import capture, detect_volatile
from .report import Report, build_report, compare_request
from .stats import summarize
from .suite import CheckRequest, Suite, redact_suite

__all__ = [
    "CHECK_HEADER",
    "CheckResult",
    "CheckSink",
    "CheckUnreachable",
    "ServerMetrics",
    "check_header",
    "run_suite",
    "schedule",
]

log = logging.getLogger("redline.check")

CHECK_HEADER = "X-Redline-Check"
SERVER_METRICS_TIMEOUT_S = 5.0

CheckResult = Union[Baseline, Report]  # noqa: UP007 - a runtime alias, py3.10 friendly


class CheckUnreachable(RuntimeError):
    """No call of the first round reached the application."""


@runtime_checkable
class CheckSink(Protocol):
    def start(self, run: dict) -> None: ...

    def result(self, run_id: str, idx: int, name: str, data: dict) -> None: ...

    def finish(self, run_id: str, status: str, summary: dict | None, error: str | None) -> None: ...


@runtime_checkable
class ServerMetrics(Protocol):
    def for_check(self, run_id: str, timeout_s: float = 5.0) -> dict[int, dict]: ...


def check_header(run_id: str, idx: int, n: int) -> str:
    """The ``X-Redline-Check`` value of call ``n`` (0 = warmup) of request ``idx``."""
    return f"{run_id};req={idx};n={n}"


def schedule(suite: Suite) -> list[tuple[int, int]]:
    """``[(request index, call#)]`` in send order; call# 0 is a warmup call."""
    out = [(i, 0) for _ in range(suite.warmup) for i in range(len(suite.requests))]
    for k in range(1, suite.repeat + 1):
        for i, req in enumerate(suite.requests):
            if req.perf or k in (1, suite.repeat):
                out.append((i, k))
    return out


@dataclass
class _Track:
    req: CheckRequest
    idx: int
    samples: list[float] = field(default_factory=list)
    calls: int = 0
    failed: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    first: dict[str, Any] | None = None
    last_raw: tuple[int, Any, bytes] | None = None
    last_is_first: bool = False

    def entry(self, redactor: Any) -> dict[str, Any]:
        last = None
        if self.last_raw is not None and not self.last_is_first:
            status, headers, content = self.last_raw
            last = capture(
                status, headers, content, redactor=redactor, ignore_headers=self.req.ignore_headers
            )
        vol = detect_volatile(self.first, last)
        broken = self.calls > 0 and self.failed * 2 >= self.calls
        perf = None
        if self.req.perf:
            stats = summarize(self.samples)
            perf = {"samples_ms": [round(s, 3) for s in self.samples], **stats}
            perf["failed"] = self.failed
        return {
            "idx": self.idx,
            "name": self.req.name,
            "method": self.req.method,
            "path": self.req.path,
            "calls": self.calls,
            "failed": self.failed,
            "broken": broken,
            "statuses": dict(self.statuses),
            "errors": dict(self.errors),
            "perf": perf,
            "server": None,
            "response": self.first,
            "volatile": vol["volatile"],
            "volatile_headers": vol["volatile_headers"],
        }


def _safe(sink: CheckSink | None, method: str, *args: Any) -> None:
    if sink is None:
        return
    try:
        getattr(sink, method)(*args)
    except Exception:
        log.exception("redline check: sink %s() failed", method)


def _join(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def run_suite(
    suite: Suite,
    *,
    base_url: str,
    kind: Literal["baseline", "compare"],
    baseline: Baseline | None = None,
    run_id: str | None = None,
    suite_id: str | None = None,
    label: str | None = None,
    sink: CheckSink | None = None,
    server_metrics: ServerMetrics | None = None,
    on_progress: Callable[[dict], None] | None = None,
    stop: threading.Event | None = None,
    transport: Any = None,
    redactor: Any = None,
    environ: Mapping[str, str] | None = None,
    git: Mapping[str, Any] | None = None,
) -> CheckResult:
    """Run ``suite`` against ``base_url``.

    ``kind="baseline"`` returns a :class:`Baseline`; ``kind="compare"`` needs ``baseline``
    and returns a :class:`Report`. ``transport`` is handed to ``httpx.Client`` (tests:
    ``httpx.MockTransport`` / ``httpx.WSGITransport``). ``redactor`` (default
    ``Redactor()``) masks captured bodies, headers and the stored suite; ``environ``
    (default ``os.environ``) expands ``${env:NAME}``; ``git`` overrides the
    :func:`~redline_core.check.baseline.git_info` lookup.

    Raises :class:`~redline_core.check.suite.SuiteError` (a missing environment variable),
    :class:`CheckUnreachable`, or ``RuntimeError`` when httpx is missing.
    """
    if kind not in ("baseline", "compare"):
        raise ValueError(f"kind must be 'baseline' or 'compare' (got {kind!r})")
    if kind == "compare" and baseline is None:
        raise ValueError("a compare run needs a baseline")
    try:
        import httpx
    except ImportError:
        raise RuntimeError(
            "the refactor check needs httpx: pip install 'redline-core[load]'"
        ) from None
    from .. import __version__
    from ..conf import resolve_release
    from ..redact import Redactor

    r = redactor if redactor is not None else Redactor()
    # Expand placeholders first: a missing variable fails before anything is sent.
    headers = [suite.headers_for(req, environ) for req in suite.requests]
    run_id = run_id or uuid.uuid4().hex
    created_at = time.time()
    git_meta = dict(git) if git is not None else git_info()
    suite_dict = redact_suite(suite, r)
    meta = {
        "id": run_id,
        "suite_id": suite_id,
        "kind": kind,
        "baseline_id": baseline.id if baseline is not None else None,
        "label": label,
        "git": git_meta,
        "created_at": created_at,
        "suite": suite_dict,
    }
    _safe(sink, "start", meta)

    tracks = [_Track(req, i) for i, req in enumerate(suite.requests)]
    plan = schedule(suite)
    first_round = min(len(suite.requests), len(plan))
    status = "done"
    connect_failures = 0
    successes = 0
    stopped_reason: str | None = None
    try:
        with httpx.Client(
            transport=transport,
            timeout=suite.timeout_s,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"User-Agent": f"redline-check/{__version__}"},
        ) as client:
            for done, (i, n) in enumerate(plan, start=1):
                if stop is not None and stop.is_set():
                    status, stopped_reason = "stopped", "stop requested"
                    break
                t = tracks[i]
                req = t.req
                h = dict(headers[i])
                h[CHECK_HEADER] = check_header(run_id, i, n)
                url = _join(base_url, req.path)
                code: int | None = None
                ms: float | None = None
                start = perf_counter()
                try:
                    resp = client.request(req.method, url, headers=h)
                    content = resp.content
                    ms = (perf_counter() - start) * 1000.0
                    code = resp.status_code
                except httpx.HTTPError as exc:  # connect errors, timeouts, protocol errors
                    if n > 0:
                        t.calls += 1
                        t.failed += 1
                        key = type(exc).__name__
                        t.errors[key] = t.errors.get(key, 0) + 1
                    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                        connect_failures += 1
                else:
                    successes += 1
                    if n > 0:
                        t.calls += 1
                        t.statuses[str(code)] = t.statuses.get(str(code), 0) + 1
                        if req.perf:
                            t.samples.append(ms)
                        if t.first is None:
                            t.first = capture(
                                code,
                                resp.headers.multi_items(),
                                content,
                                redactor=r,
                                ignore_headers=req.ignore_headers,
                            )
                            t.last_raw = (code, resp.headers.multi_items(), content)
                            t.last_is_first = True
                        else:
                            t.last_raw = (code, resp.headers.multi_items(), content)
                            t.last_is_first = False
                if done == first_round and successes == 0 and connect_failures == done:
                    raise CheckUnreachable(
                        f"could not reach {base_url}: every call failed to connect"
                    )
                if on_progress is not None:
                    try:
                        on_progress(
                            {
                                "run_id": run_id,
                                "phase": "warmup" if n == 0 else "timed",
                                "done": done,
                                "total": len(plan),
                                "current": req.name,
                                "idx": i,
                                "name": req.name,
                                "n": n,
                                "status": code,
                                "ms": ms,
                            }
                        )
                    except Exception:
                        log.exception("redline check: progress callback failed")
    except KeyboardInterrupt:
        status, stopped_reason = "stopped", "interrupted (Ctrl-C)"
    except CheckUnreachable as exc:
        _safe(sink, "finish", run_id, "failed", None, str(exc))
        raise
    except BaseException as exc:
        _safe(sink, "finish", run_id, "failed", None, f"{type(exc).__name__}: {exc}")
        raise

    try:
        entries = [t.entry(r) for t in tracks]
        if server_metrics is not None and status == "done":
            try:
                metrics = server_metrics.for_check(run_id, timeout_s=SERVER_METRICS_TIMEOUT_S)
            except Exception:
                log.exception("redline check: server-side metrics failed")
                metrics = {}
            for e in entries:
                m = (metrics or {}).get(e["idx"])
                e["server"] = dict(m) if m else None
        finished_at = time.time()
        result: CheckResult
        if kind == "baseline":
            result = Baseline(
                id=run_id,
                suite=suite_dict,
                requests=entries,
                created_at=created_at,
                label=label,
                git=git_meta,
                release=resolve_release(r.settings),
                base_url=r.url(base_url),
                status=status,
            )
            summary = result.summary()
            data = entries
        else:
            assert baseline is not None
            names = {req.name for req in suite.requests}
            report_entries = [
                compare_request(t.req, baseline.request(t.req.name), e, suite.thresholds)
                for t, e in zip(tracks, entries, strict=True)
            ]
            result = build_report(
                suite,
                report_entries,
                run={
                    "id": run_id,
                    "label": label,
                    "git": git_meta,
                    "created_at": created_at,
                    "finished_at": finished_at,
                    "base_url": r.url(base_url),
                },
                baseline=baseline,
                suite_dict=suite_dict,
                status=status,
                not_in_suite=[
                    str(b.get("name")) for b in baseline.requests if b.get("name") not in names
                ],
            )
            summary = result.summary
            data = report_entries
        if stopped_reason:
            summary = {**summary, "stopped_reason": stopped_reason}
        for e in data:
            _safe(sink, "result", run_id, e["idx"], e["name"], e)
        _safe(sink, "finish", run_id, status, summary, None)
    except BaseException as exc:
        _safe(sink, "finish", run_id, "failed", None, f"{type(exc).__name__}: {exc}")
        raise
    return result
