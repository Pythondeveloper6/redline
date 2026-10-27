"""Verdicts and report rendering (``CONTRACT.md`` section 19.4).

:func:`compare_request` turns one suite request, its baseline entry and the new run's
observation (both in the baseline entry shape, see :mod:`redline_core.check.baseline`) into
a report entry; :func:`build_report` adds the summary. :class:`Report` renders as JSON,
a terminal table, a Markdown PR comment and JUnit XML, and gives the CLI's exit code.

Verdict rules, per request:

* **perf** (``perf = true``): ``noisy`` when either side's coefficient of variation is
  above 0.5; else ``slower`` when the two-sided Mann–Whitney p-value is below 0.01 and the
  median grew by more than ``slower_ms`` **and** ``slower_pct``; ``faster`` mirrored;
  otherwise ``same``. With server-side metrics on both sides, a median query count above
  the baseline's + ``max_query_increase`` makes it ``slower`` (reason ``queries``) whatever
  the timing said. ``n/a`` when off, missing from the baseline, or either side has ≥ 50 %
  failed calls.
* **response**: ``broken`` (≥ 50 % failed calls – also when ``response = false``; the
  status class went from 2xx/3xx to 4xx/5xx; ``expect_status`` not met), ``changed``
  (another status change, a content-type/header change, or a body difference outside the
  ignored and volatile paths), ``same``, or ``n/a``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .diff import compare_captures
from .stats import geometric_mean, mann_whitney_u, summarize
from .suite import CheckRequest, Thresholds

__all__ = [
    "DEFAULT_FAIL_ON",
    "FAIL_ON_CHOICES",
    "FORMAT",
    "NOISY_CV",
    "P_VALUE",
    "VERSION",
    "Report",
    "ReportError",
    "build_report",
    "compare_request",
    "parse_fail_on",
]

FORMAT = "redline-check-report"
VERSION = 1
P_VALUE = 0.01
NOISY_CV = 0.5
DEFAULT_FAIL_ON = ("slower", "changed", "broken")
#: Values ``--fail-on`` accepts (perf: slower/noisy/faster, response: changed/broken).
FAIL_ON_CHOICES = ("slower", "changed", "broken", "noisy", "faster")
PERF_VERDICTS = ("faster", "slower", "same", "noisy", "n/a")
RESPONSE_VERDICTS = ("same", "changed", "broken", "n/a")
TOP_N = 5


class ReportError(ValueError):
    """A report file is not a report this version understands."""


def parse_fail_on(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """``"slower,changed"`` → ``("slower", "changed")``; ``""``/``"none"`` → ``()``.
    Raises ``ValueError`` for an unknown verdict."""
    if value is None:
        return DEFAULT_FAIL_ON
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[str] = []
    for raw in items:
        item = str(raw).strip().lower()
        if not item or item == "none":
            continue
        if item not in FAIL_ON_CHOICES:
            raise ValueError(
                f"unknown verdict {item!r} in --fail-on; use {', '.join(FAIL_ON_CHOICES)} or none"
            )
        if item not in out:
            out.append(item)
    return tuple(out)


# ============================================================================ per request
def _perf_side(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not entry or not isinstance(entry.get("perf"), Mapping):
        return None
    p = entry["perf"]
    samples = [float(v) for v in p.get("samples_ms") or []]
    out = {k: p.get(k) for k in ("median", "p95", "mean", "stdev", "cv")}
    if out["cv"] is None and samples:
        out["cv"] = summarize(samples)["cv"]
    out["n"] = len(samples)
    out["failed"] = int(p.get("failed") or entry.get("failed") or 0)
    out["samples_ms"] = samples
    return out


def _queries(server: Mapping[str, Any] | None) -> float | None:
    if not isinstance(server, Mapping):
        return None
    q = server.get("queries_median")
    return float(q) if isinstance(q, (int, float)) and not isinstance(q, bool) else None


def _perf(
    req: CheckRequest,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    th: Thresholds,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "verdict": "n/a",
        "before": _perf_side(before),
        "after": _perf_side(after),
        "change_ms": None,
        "change_pct": None,
        "p_value": None,
        "reason": None,
        "note": None,
    }
    if not req.perf:
        out["note"] = "perf check off"
        return out
    if before is None:
        out["note"] = "not in the baseline"
        return out
    if before.get("broken") or after.get("broken"):
        out["note"] = "≥ 50% failed calls"
        return out
    b, a = out["before"], out["after"]
    if not b or not a or not b["samples_ms"] or not a["samples_ms"]:
        out["note"] = "no timed samples"
        return out
    bm, am = float(b["median"]), float(a["median"])
    change_ms = am - bm
    change_pct = change_ms / bm * 100.0 if bm > 0 else None
    _, p = mann_whitney_u(b["samples_ms"], a["samples_ms"])
    out["change_ms"] = round(change_ms, 3)
    out["change_pct"] = None if change_pct is None else round(change_pct, 2)
    out["p_value"] = p
    pct = change_pct if change_pct is not None else (float("inf") if change_ms else 0.0)
    significant = p < P_VALUE
    if (b["cv"] or 0) > NOISY_CV or (a["cv"] or 0) > NOISY_CV:
        out["verdict"] = "noisy"
        out["reason"] = "cv"
        out["note"] = "coefficient of variation > 0.5: raise repeat"
    elif significant and change_ms > th.slower_ms and pct > th.slower_pct:
        out["verdict"], out["reason"] = "slower", "time"
    elif significant and -change_ms > th.slower_ms and -pct > th.slower_pct:
        out["verdict"], out["reason"] = "faster", "time"
    else:
        out["verdict"] = "same"
    qb, qa = _queries(before.get("server")), _queries(after.get("server"))
    if qb is not None and qa is not None and qa > qb + th.max_query_increase:
        if out["verdict"] == "slower":
            out["note"] = "slower by time and by queries"
        out["reason"] = "queries"
        out["verdict"] = "slower"
    return out


def _issue_key(issue: Mapping[str, Any]) -> tuple[str, str]:
    return (str(issue.get("kind")), str(issue.get("fingerprint") or issue.get("title")))


def _server(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Any]:
    sb = before.get("server") if before else None
    sa = after.get("server")
    out: dict[str, Any] = {
        "before": sb,
        "after": sa,
        "queries_change": None,
        "new_issues": [],
        "fixed_issues": [],
        "new_sql": [],
        "gone_sql": [],
    }
    if not isinstance(sb, Mapping) or not isinstance(sa, Mapping):
        out["note"] = "server-side metrics unavailable" + (
            "" if sb or sa else " (no tagged traces: is the app instrumented with Redline?)"
        )
        return out
    qb, qa = _queries(sb), _queries(sa)
    if qb is not None and qa is not None:
        out["queries_change"] = round(qa - qb, 3)
    ib = {_issue_key(i): i for i in sb.get("issues") or []}
    ia = {_issue_key(i): i for i in sa.get("issues") or []}
    tb, ta = sb.get("sample_trace_id"), sa.get("sample_trace_id")

    def issue(i: Mapping[str, Any], trace: Any) -> dict[str, Any]:
        return {
            "kind": i.get("kind"),
            "title": i.get("title"),
            "fingerprint": i.get("fingerprint"),
            "sample_trace_id": i.get("sample_trace_id") or trace,
        }

    def sql(fp: str, v: Mapping[str, Any], trace: Any) -> dict[str, Any]:
        return {
            "fingerprint": fp,
            "sql": v.get("sql"),
            "count": v.get("count"),
            "sample_trace_id": v.get("sample_trace_id") or trace,
        }

    out["new_issues"] = [issue(ia[k], ta) for k in ia if k not in ib]
    out["fixed_issues"] = [issue(ib[k], tb) for k in ib if k not in ia]
    qsb, qsa = sb.get("sql") or {}, sa.get("sql") or {}
    out["new_sql"] = [sql(fp, v, ta) for fp, v in qsa.items() if fp not in qsb]
    out["gone_sql"] = [sql(fp, v, tb) for fp, v in qsb.items() if fp not in qsa]
    return out


def _status_class_bad(status: int | None) -> bool:
    return status is not None and status >= 400


def _response(
    req: CheckRequest,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    run_volatile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    rb = before.get("response") if before else None
    ra = after.get("response")
    sb = rb.get("status") if isinstance(rb, Mapping) else None
    sa = ra.get("status") if isinstance(ra, Mapping) else None
    out: dict[str, Any] = {
        "verdict": "n/a",
        "status": [sb, sa],
        "reason": None,
        "diff": [],
        "diff_total": 0,
        "text_diff": None,
        "ignored": [],
    }
    if before is None:
        out["reason"] = "not in the baseline"
        return out
    if after.get("broken"):
        out["verdict"] = "broken"
        out["reason"] = f"≥ 50% failed calls ({after.get('failed')}/{after.get('calls')})"
        return out
    if not req.response:
        out["reason"] = "response check off"
        return out
    if not isinstance(ra, Mapping):
        out["verdict"], out["reason"] = "broken", "no response captured"
        return out
    if req.expect_status is not None and sa not in req.expect_status:
        expected = "/".join(str(s) for s in req.expect_status)
        out["verdict"], out["reason"] = "broken", f"expected status {expected}, got {sa}"
        return out
    if not isinstance(rb, Mapping):
        out["verdict"] = "changed"
        out["reason"] = "the baseline had no response (≥ 50% failed calls)"
        return out
    if _status_class_bad(sa) and not _status_class_bad(sb):
        out["verdict"], out["reason"] = "broken", f"status {sb} → {sa}"
        return out
    volatile = list(before.get("volatile") or [])
    vol_headers = list(before.get("volatile_headers") or [])
    if run_volatile:
        volatile += [p for p in run_volatile.get("volatile") or [] if p not in volatile]
        vol_headers += run_volatile.get("volatile_headers") or []
    if rb.get("body_kind") != "json":  # JSON paths only apply to JSON bodies, lines to text
        volatile = [p for p in volatile if p == "$" or p.startswith("line:")]
    cmp = compare_captures(
        rb, ra, ignore=req.ignore, volatile=volatile, volatile_headers=vol_headers
    )
    out.update(cmp)
    if sb != sa:
        out["verdict"], out["reason"] = "changed", f"status {sb} → {sa}"
    elif cmp["diff_total"]:
        n = cmp["diff_total"]
        out["verdict"], out["reason"] = "changed", f"{n} difference{'s' if n != 1 else ''}"
    else:
        out["verdict"] = "same"
    return out


def compare_request(
    req: CheckRequest,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    thresholds: Thresholds,
) -> dict[str, Any]:
    """One report entry (``before`` is ``None`` when the baseline lacks the request)."""
    run_volatile = {
        "volatile": after.get("volatile") or [],
        "volatile_headers": after.get("volatile_headers") or [],
    }
    return {
        "idx": after.get("idx"),
        "name": req.name,
        "method": req.method,
        "path": req.path,
        "calls": [before.get("calls") if before else None, after.get("calls")],
        "failed": [before.get("failed") if before else None, after.get("failed")],
        "perf": _perf(req, before, after, thresholds),
        "server": _server(before, after),
        "response": _response(req, before, after, run_volatile),
    }


# ============================================================================ summary
def _failing(entry: Mapping[str, Any], fail_on: Sequence[str]) -> list[str]:
    hits = []
    pv = entry.get("perf", {}).get("verdict")
    rv = entry.get("response", {}).get("verdict")
    if pv in fail_on:
        hits.append(pv)
    if rv in fail_on:
        hits.append(rv)
    return hits


def _summary(entries: list[dict[str, Any]], fail_on: Sequence[str]) -> dict[str, Any]:
    perf = dict.fromkeys(PERF_VERDICTS, 0)
    resp = dict.fromkeys(RESPONSE_VERDICTS, 0)
    for e in entries:
        perf[e["perf"]["verdict"]] = perf.get(e["perf"]["verdict"], 0) + 1
        resp[e["response"]["verdict"]] = resp.get(e["response"]["verdict"], 0) + 1
    ratios: list[float] = []
    for e in entries:
        p = e["perf"]
        if p["verdict"] == "n/a" or not p["before"] or not p["after"]:
            continue
        if p["before"]["failed"] or p["after"]["failed"]:
            continue
        bm, am = p["before"]["median"], p["after"]["median"]
        if bm and am and bm > 0 and am > 0:
            ratios.append(am / bm)
    gm = geometric_mean(ratios)
    q_before = q_after = 0.0
    q_n = 0
    for e in entries:
        qb, qa = _queries(e["server"]["before"]), _queries(e["server"]["after"])
        if qb is not None and qa is not None:
            q_before += qb
            q_after += qa
            q_n += 1
    timed = [e for e in entries if e["perf"]["change_pct"] is not None]
    wins = sorted(
        (e for e in timed if e["perf"]["verdict"] == "faster"),
        key=lambda e: e["perf"]["change_pct"],
    )
    regressions = sorted(
        (e for e in entries if e["perf"]["verdict"] == "slower"),
        key=lambda e: -(e["perf"]["change_pct"] or 0.0),
    )

    def brief(e: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": e["name"],
            "change_pct": e["perf"]["change_pct"],
            "change_ms": e["perf"]["change_ms"],
            "reason": e["perf"]["reason"],
        }

    failing = [e["name"] for e in entries if _failing(e, fail_on)]
    return {
        "requests": len(entries),
        "perf": perf,
        "response": resp,
        "passed": len(entries) - len(failing),
        "failed": len(failing),
        "failing": failing,
        "fail_on": list(fail_on),
        "faster": perf["faster"],
        "slower": perf["slower"],
        "changed": resp["changed"],
        "broken": resp["broken"],
        "speed_ratio": None if gm is None else round(gm, 4),
        "speed_change_pct": None if gm is None else round((gm - 1.0) * 100.0, 2),
        "speed_requests": len(ratios),
        "queries_before": round(q_before, 3) if q_n else None,
        "queries_after": round(q_after, 3) if q_n else None,
        "queries_change": round(q_after - q_before, 3) if q_n else None,
        "server_side": q_n > 0 or any(e["server"]["after"] for e in entries),
        "wins": [brief(e) for e in wins[:TOP_N]],
        "regressions": [brief(e) for e in regressions[:TOP_N]],
    }


def build_report(
    suite: Any,
    entries: list[dict[str, Any]],
    *,
    run: Mapping[str, Any],
    baseline: Any,
    fail_on: Sequence[str] = DEFAULT_FAIL_ON,
    suite_dict: Mapping[str, Any] | None = None,
    status: str = "done",
    not_in_suite: Sequence[str] = (),
) -> Report:
    """Assemble a :class:`Report` from :func:`compare_request` entries."""
    summary = _summary(entries, fail_on)
    summary["not_in_baseline"] = [
        e["name"] for e in entries if e["perf"].get("note") == "not in the baseline"
    ]
    summary["not_in_suite"] = list(not_in_suite)
    return Report(
        id=str(run.get("id") or ""),
        suite=dict(suite_dict if suite_dict is not None else suite.to_dict()),
        baseline=baseline.ref() if hasattr(baseline, "ref") else dict(baseline or {}),
        run=dict(run),
        summary=summary,
        requests=entries,
        fail_on=tuple(fail_on),
        status=status,
    )


# ============================================================================ rendering
_COLORS = {
    "faster": "32",
    "same": "0",
    "slower": "31",
    "noisy": "33",
    "n/a": "2",
    "changed": "33",
    "broken": "1;31",
}


def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 10_000:
        return f"{value / 1000:.1f}s"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    if value >= 100:
        return f"{value:.0f}ms"
    if value >= 10:
        return f"{value:.1f}ms"
    return f"{value:.2f}ms"


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1f}%"


def _fmt_q(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g}" if float(value).is_integer() else f"{value:.1f}"


def _fmt_time(epoch: Any) -> str:
    if not isinstance(epoch, (int, float)):
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _overall(summary: Mapping[str, Any]) -> str:
    pct = summary.get("speed_change_pct")
    if pct is None:
        return "overall speed: n/a"
    if abs(pct) < 0.05:
        return "overall speed: unchanged"
    return f"overall {abs(pct):.1f}% {'faster' if pct < 0 else 'slower'}"


def _queries_line(summary: Mapping[str, Any]) -> str | None:
    if summary.get("queries_change") is None:
        return None
    return (
        f"queries {_fmt_q(summary['queries_before'])} → {_fmt_q(summary['queries_after'])} "
        f"({summary['queries_change']:+g})"
    )


def _counts(counts: Mapping[str, int], order: Sequence[str]) -> str:
    return ", ".join(f"{counts.get(k, 0)} {k}" for k in order if counts.get(k))


def _short_value(value: Any, quote: bool = False) -> str:
    text = value if isinstance(value, str) and not quote else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:59] + "…"


def _diff_lines(entry: Mapping[str, Any], limit: int) -> list[str]:
    r = entry["response"]
    lines = []
    for d in r.get("diff") or []:
        if d["path"] == "$" and r.get("text_diff"):
            continue
        if d["kind"] == "added":
            lines.append(f"+ {d['path']}: {_short_value(d['after'])}")
        elif d["kind"] == "removed":
            lines.append(f"- {d['path']}: {_short_value(d['before'])}")
        else:
            q = d["kind"] == "type"  # 200 → "200" must not read as unchanged
            lines.append(
                f"~ {d['path']}: {_short_value(d['before'], q)} → {_short_value(d['after'], q)}"
                + (" (type)" if q else "")
            )
    lines += list(r.get("text_diff") or [])
    extra = (r.get("diff_total") or 0) - len(r.get("diff") or [])
    if len(lines) > limit:
        extra += len(lines) - limit
        lines = lines[:limit]
    if extra > 0:
        lines.append(f"… {extra} more")
    return lines


@dataclass
class Report:
    id: str
    suite: dict[str, Any]
    baseline: dict[str, Any]
    run: dict[str, Any]
    summary: dict[str, Any]
    requests: list[dict[str, Any]]
    fail_on: tuple[str, ...] = DEFAULT_FAIL_ON
    status: str = "done"
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ data
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "id": self.id,
            "status": self.status,
            "suite": self.suite,
            "baseline": self.baseline,
            "run": self.run,
            "summary": self.summary,
            "requests": self.requests,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, data: Any) -> Report:
        if not isinstance(data, Mapping) or data.get("format") != FORMAT:
            raise ReportError(f"not a Redline check report (expected format {FORMAT!r})")
        if data.get("version") != VERSION:
            raise ReportError(
                f"unsupported report version {data.get('version')!r} (this Redline reads "
                f"version {VERSION})"
            )
        summary = dict(data.get("summary") or {})
        return cls(
            id=str(data.get("id") or ""),
            suite=dict(data.get("suite") or {}),
            baseline=dict(data.get("baseline") or {}),
            run=dict(data.get("run") or {}),
            summary=summary,
            requests=list(data.get("requests") or []),
            fail_on=tuple(summary.get("fail_on") or DEFAULT_FAIL_ON),
            status=str(data.get("status") or "done"),
        )

    def with_fail_on(self, fail_on: Sequence[str]) -> Report:
        """The same report judged against other ``--fail-on`` verdicts."""
        from dataclasses import replace

        summary = dict(self.summary)
        failing = [e["name"] for e in self.requests if _failing(e, fail_on)]
        summary.update(
            fail_on=list(fail_on),
            failing=failing,
            failed=len(failing),
            passed=len(self.requests) - len(failing),
        )
        return replace(self, fail_on=tuple(fail_on), summary=summary)

    def failures(self) -> list[tuple[dict[str, Any], list[str]]]:
        """``[(entry, [verdicts in fail_on])]`` for every failing request."""
        out = []
        for e in self.requests:
            hits = _failing(e, self.fail_on)
            if hits:
                out.append((e, hits))
        return out

    def exit_code(self) -> int:
        """0 nothing matches ``fail_on``, 1 otherwise."""
        return 1 if self.failures() else 0

    # ------------------------------------------------------------------ terminal
    def headline(self) -> str:
        s = self.summary
        parts = [f"{s.get('requests', len(self.requests))} requests"]
        perf = _counts(s.get("perf") or {}, PERF_VERDICTS[:-1])
        resp = _counts(s.get("response") or {}, RESPONSE_VERDICTS[:-1])
        if perf:
            parts.append(f"perf: {perf}")
        if resp:
            parts.append(f"response: {resp}")
        parts.append(_overall(s))
        q = _queries_line(s)
        if q:
            parts.append(q)
        return " · ".join(parts)

    def to_terminal(self, *, color: bool = False, details: bool = True) -> str:
        def paint(text: str, verdict: str, width: int) -> str:
            padded = text.ljust(width)
            code = _COLORS.get(verdict)
            if not color or not code or code == "0":
                return padded
            return f"\x1b[{code}m{padded}\x1b[0m"

        rows = []
        for e in self.requests:
            p, r, srv = e["perf"], e["response"], e["server"]
            med = (
                f"{_fmt_ms((p['before'] or {}).get('median'))} → "
                f"{_fmt_ms((p['after'] or {}).get('median'))}"
                if p.get("before") and p.get("after")
                else "-"
            )
            qb, qa = _queries(srv.get("before")), _queries(srv.get("after"))
            q = f"{_fmt_q(qb)} → {_fmt_q(qa)}" if qb is not None or qa is not None else "-"
            pv = p["verdict"] + (" (q)" if p.get("reason") == "queries" else "")
            rows.append((e["name"], pv, p["verdict"], med, _fmt_pct(p["change_pct"]), q, r))
        name_w = max([7, *(min(len(r[0]), 40) for r in rows)])
        med_w = max([21, *(len(r[3]) for r in rows)])
        head = (
            f"{'request'.ljust(name_w)}  {'perf'.ljust(10)}  {'median before → after'.ljust(med_w)}"
            f"  {'Δ'.rjust(7)}  {'queries'.ljust(9)}  response"
        )
        lines = [self._title(), "", head]
        for name, pv, pkey, med, pct, q, r in rows:
            short = name if len(name) <= 40 else name[:39] + "…"
            resp = r["verdict"] + (
                f" ({r['reason']})" if r["verdict"] != "same" and r.get("reason") else ""
            )
            lines.append(
                f"{short.ljust(name_w)}  {paint(pv, pkey, 10)}  {med.ljust(med_w)}  {pct.rjust(7)}"
                f"  {q.ljust(9)}  {paint(resp, r['verdict'], 0)}"
            )
        if details:
            lines += self._details()
        lines += ["", self.headline()]
        if not self.summary.get("server_side"):
            lines.append(
                "server-side metrics: unavailable (pass --store with the app's Redline store, "
                "and make sure the app runs with Redline)"
            )
        fails = self.failures()
        if fails:
            names = ", ".join(e["name"] for e, _ in fails[:5]) + (" …" if len(fails) > 5 else "")
            lines.append(
                f"FAIL: {len(fails)} of {len(self.requests)} requests match --fail-on "
                f"{','.join(self.fail_on)}: {names}"
            )
        else:
            on = ",".join(self.fail_on) or "none"
            lines.append(f"PASS: nothing matches --fail-on {on}")
        return "\n".join(lines) + "\n"

    def _title(self) -> str:
        b = self.baseline or {}
        git = b.get("git") or {}
        sha = (git.get("sha") or "")[:8]
        bits = [x for x in (b.get("label") and repr(b["label"]), sha) if x]
        bits.append(_fmt_time(b.get("created_at")))
        status = "" if self.status == "done" else f" [{self.status}]"
        return (
            f"redline check: {self.suite.get('name', '?')}{status} – compared with baseline "
            f"{' '.join(bits)}"
        )

    def _details(self) -> list[str]:
        out: list[str] = []
        for e in self.requests:
            r, srv = e["response"], e["server"]
            lines: list[str] = []
            if r["verdict"] in ("changed", "broken"):
                lines += [f"  {x}" for x in _diff_lines(e, 12)]
            for issue in srv.get("new_issues") or []:
                lines.append(f"  new issue: {issue.get('kind')} – {issue.get('title') or ''}")
            for issue in srv.get("fixed_issues") or []:
                lines.append(f"  fixed issue: {issue.get('kind')} – {issue.get('title') or ''}")
            vol = [i["path"] for i in r.get("ignored") or [] if i.get("reason") == "volatile"]
            if vol and r["verdict"] != "n/a":
                shown = ", ".join(vol[:5]) + (f" (+{len(vol) - 5})" if len(vol) > 5 else "")
                lines.append(f"  ignored (changes on every call): {shown}")
            if lines:
                out += ["", f"{e['name']}:", *lines]
        return out

    # ------------------------------------------------------------------ markdown
    def to_markdown(self) -> str:
        s = self.summary
        fails = self.failures()
        status = (
            f"**FAIL** – {len(fails)} of {len(self.requests)} requests match "
            f"`{','.join(self.fail_on)}`"
            if fails
            else "**PASS**"
        )
        lines = [f"### Redline refactor check: {_md(self.suite.get('name', '?'))}", ""]
        lines.append(f"{status} · {_md(self.headline())}")
        b = self.baseline or {}
        sha = ((b.get("git") or {}).get("sha") or "")[:8]
        lines.append("")
        lines.append(
            f"Baseline {_md(b.get('label') or '')} {('`' + sha + '`') if sha else ''} "
            f"({_fmt_time(b.get('created_at'))})".replace("  ", " ")
        )
        lines += [
            "",
            "| Request | Perf | Median before → after | Δ | Queries | Response |",
            "|---|---|---|---:|---|---|",
        ]
        for e in self.requests:
            p, r, srv = e["perf"], e["response"], e["server"]
            med = (
                f"{_fmt_ms(p['before']['median'])} → {_fmt_ms(p['after']['median'])}"
                if p.get("before") and p.get("after")
                else "–"
            )
            qb, qa = _queries(srv.get("before")), _queries(srv.get("after"))
            q = f"{_fmt_q(qb)} → {_fmt_q(qa)}" if qb is not None and qa is not None else "–"
            pv = p["verdict"] + (" (queries)" if p.get("reason") == "queries" else "")
            rv = r["verdict"]
            if rv in ("changed", "broken") and r.get("reason"):
                rv += f" ({r['reason']})"
            pct = _fmt_pct(p["change_pct"]) if p["change_pct"] is not None else "–"
            lines.append(f"| {_md(e['name'])} | {_md(pv)} | {med} | {pct} | {q} | {_md(rv)} |")
        for e in self.requests:
            r, srv = e["response"], e["server"]
            body: list[str] = []
            if r["verdict"] == "broken" and r.get("reason"):
                body.append(f"broken: {r['reason']}")
            if r["verdict"] in ("changed", "broken"):
                body += _diff_lines(e, 60)
            for issue in srv.get("new_issues") or []:
                body.append(f"new issue: {issue.get('kind')} – {issue.get('title') or ''}")
            for issue in srv.get("fixed_issues") or []:
                body.append(f"fixed issue: {issue.get('kind')} – {issue.get('title') or ''}")
            if not body:
                continue
            summary = f"{_html(e['name'])} – {_html(r['verdict'])}"
            lines += [
                "",
                f"<details><summary>{summary}</summary>",
                "",
                "```diff",
                *[x.replace("```", "``\u200b`") for x in body],
                "```",
                "</details>",
            ]
        if not s.get("server_side"):
            lines += ["", "_Server-side metrics unavailable (no tagged traces in the store)._"]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ JUnit
    def to_junit(self) -> str:
        name = str(self.suite.get("name") or "redline check")
        fails = {id(e): hits for e, hits in self.failures()}
        root = ET.Element("testsuites", name="redline check")
        ts = ET.SubElement(
            root,
            "testsuite",
            name=name,
            tests=str(len(self.requests)),
            failures=str(len(fails)),
            errors="0",
            skipped="0",
        )
        total = 0.0
        for e in self.requests:
            after = e["perf"].get("after") or {}
            seconds = (after.get("median") or 0.0) / 1000.0
            total += seconds
            tc = ET.SubElement(
                ts,
                "testcase",
                classname=f"redline.check.{name}",
                name=e["name"],
                time=f"{seconds:.6f}",
            )
            hits = fails.get(id(e))
            if hits:
                p, r = e["perf"], e["response"]
                reasons = []
                if p["verdict"] in hits:
                    why = p.get("reason") or p.get("note") or ""
                    reasons.append(f"perf {p['verdict']} ({why}, {_fmt_pct(p['change_pct'])})")
                if r["verdict"] in hits:
                    reasons.append(f"response {r['verdict']} ({r.get('reason') or ''})")
                failure = ET.SubElement(
                    tc, "failure", message="; ".join(reasons), type=",".join(hits)
                )
                failure.text = "\n".join(_diff_lines(e, 100)) or None
            out = (
                f"perf {e['perf']['verdict']}, response {e['response']['verdict']}, "
                f"{e['method']} {e['path']}"
            )
            ET.SubElement(tc, "system-out").text = out
        ts.set("time", f"{total:.6f}")
        ET.indent(root)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode")
            + "\n"
        )


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _html(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
