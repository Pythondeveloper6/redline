"""The query-count ratchet: numbers that may only get better.

A ratchet file is a small, committed JSON document with one entry per endpoint (from
``redline check ratchet``) or per test (from the pytest plugin)::

    {"format": "redline-check-ratchet", "version": 1,
     "endpoints": {"orders list": {"median_ms": 12.3, "queries": 4}},
     "tests": {"tests/test_orders.py::test_list": {"queries": 4}}}

A **regression** is more queries than recorded, or a median latency above the recorded one by
more than both tolerances (queries are exact, timings are noisy). An **improvement** lowers
the recorded numbers when the file is updated; nothing is ever loosened automatically. The
file is written with sorted keys and a trailing newline, so diffs stay minimal.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FORMAT",
    "LATENCY_MS",
    "LATENCY_PCT",
    "VERSION",
    "RatchetError",
    "RatchetResult",
    "compare",
    "load",
    "observe",
    "write",
]

FORMAT = "redline-check-ratchet"
VERSION = 1
#: A median must exceed the recorded one by more than both of these to count as slower.
LATENCY_PCT = 30.0
LATENCY_MS = 20.0


class RatchetError(ValueError):
    """The ratchet file is unreadable or not a ratchet file."""


@dataclass
class RatchetResult:
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    unchanged: int = 0
    #: What the file should hold after an update: tighter numbers, never looser.
    updated: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.regressions and not self.broken

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "regressions": self.regressions,
            "improvements": self.improvements,
            "new": self.new,
            "missing": self.missing,
            "broken": self.broken,
            "unchanged": self.unchanged,
        }

    def to_markdown(self, title: str = "Redline ratchet") -> str:
        lines = [f"### {title}", "", "**PASS**" if self.ok else "**FAIL**", ""]
        for heading, items in (
            ("Regressions", self.regressions),
            ("Broken (not judged)", self.broken),
            ("Improvements", self.improvements),
            ("New", self.new),
            ("Not in this run", self.missing),
        ):
            if items:
                lines += [f"**{heading}**", *[f"- {i}" for i in items], ""]
        return "\n".join(lines).rstrip() + "\n"


def _empty(section: str) -> dict[str, Any]:
    return {"format": FORMAT, "version": VERSION, section: {}}


def load(path: str, section: str = "endpoints") -> dict[str, dict[str, Any]]:
    """The entries of ``section`` in ``path``; ``FileNotFoundError`` when it does not exist."""
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except ValueError as exc:
            raise RatchetError(f"{path}: not valid JSON ({exc})") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise RatchetError(f"{path}: not a redline ratchet file")
    if data.get("version") != VERSION:
        raise RatchetError(f"{path}: unsupported ratchet version {data.get('version')!r}")
    entries = data.get(section) or {}
    if not isinstance(entries, dict) or any(not isinstance(v, dict) for v in entries.values()):
        raise RatchetError(f"{path}: malformed {section!r} section")
    return {str(k): dict(v) for k, v in entries.items()}


def write(path: str, entries: Mapping[str, Mapping[str, Any]], section: str = "endpoints") -> None:
    """Write ``entries`` into ``section``, keeping the file's other section. Deterministic."""
    try:
        with open(path, encoding="utf-8") as fh:
            document = json.load(fh)
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise RatchetError(f"{path}: not a redline ratchet file; refusing to overwrite")
    except FileNotFoundError:
        document = _empty(section)
    except ValueError:
        raise RatchetError(f"{path}: not valid JSON; refusing to overwrite") from None
    document["version"] = VERSION
    document[section] = {k: _clean(entries[k]) for k in sorted(entries)}
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ratchet-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _clean(entry: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if entry.get("queries") is not None:
        out["queries"] = int(entry["queries"])
    if entry.get("median_ms") is not None:
        out["median_ms"] = round(float(entry["median_ms"]), 1)
    return out


def observe(requests: list[Mapping[str, Any]], *, latency: bool = True) -> dict[str, dict]:
    """Ratchet entries from a recorded run's ``requests`` (baseline entry shape)."""
    out: dict[str, dict[str, Any]] = {}
    for r in requests:
        if r.get("broken"):
            out[str(r["name"])] = {"broken": True}
            continue
        entry: dict[str, Any] = {}
        server = r.get("server")
        q = server.get("queries_median") if isinstance(server, Mapping) else None
        if isinstance(q, (int, float)) and not isinstance(q, bool):
            entry["queries"] = int(-(-q // 1))  # ceil: a median of 3.5 is "4 queries"
        perf = r.get("perf")
        median = perf.get("median") if isinstance(perf, Mapping) else None
        if latency and isinstance(median, (int, float)) and not isinstance(median, bool):
            entry["median_ms"] = float(median)
        out[str(r["name"])] = entry
    return out


def _slower(old: float, new: float, pct: float, ms: float) -> bool:
    return new > old + ms and new > old * (1 + pct / 100.0)


def compare(
    recorded: Mapping[str, Mapping[str, Any]],
    observed: Mapping[str, Mapping[str, Any]],
    *,
    latency_pct: float = LATENCY_PCT,
    latency_ms: float = LATENCY_MS,
) -> RatchetResult:
    """Judge ``observed`` against ``recorded``; ``result.updated`` is the tightened file."""
    result = RatchetResult()
    updated: dict[str, dict[str, Any]] = {k: dict(v) for k, v in recorded.items()}
    for name in sorted(observed):
        now = observed[name]
        if now.get("broken"):
            result.broken.append(f"{name}: too many failed calls to judge")
            continue
        old = recorded.get(name)
        if old is None:
            result.new.append(f"{name}: {_describe(now)}")
            updated[name] = _clean(now)
            continue
        entry = dict(old)
        changed = regressed = False
        if now.get("queries") is not None:
            oq = old.get("queries")
            if oq is None:
                entry["queries"] = int(now["queries"])
                changed = True
            elif now["queries"] > oq:
                result.regressions.append(f"{name}: {now['queries']} queries, was {oq}")
                regressed = True
            elif now["queries"] < oq:
                result.improvements.append(f"{name}: {now['queries']} queries, was {oq}")
                entry["queries"] = int(now["queries"])
                changed = True
        if now.get("median_ms") is not None:
            om = old.get("median_ms")
            nm = float(now["median_ms"])
            if om is None:
                entry["median_ms"] = nm
                changed = True
            elif _slower(float(om), nm, latency_pct, latency_ms):
                result.regressions.append(f"{name}: median {nm:.1f} ms, was {float(om):.1f} ms")
                regressed = True
            elif _slower(nm, float(om), latency_pct, latency_ms):
                result.improvements.append(f"{name}: median {nm:.1f} ms, was {float(om):.1f} ms")
                entry["median_ms"] = nm
                changed = True
        if regressed:
            continue  # a regression never rewrites its own entry
        if changed:
            updated[name] = _clean(entry)
        else:
            result.unchanged += 1
    result.missing = [f"{name}" for name in sorted(recorded) if name not in observed]
    result.updated = {k: _clean(v) for k, v in updated.items()}
    return result


def _describe(entry: Mapping[str, Any]) -> str:
    parts = []
    if entry.get("queries") is not None:
        parts.append(f"{int(entry['queries'])} queries")
    if entry.get("median_ms") is not None:
        parts.append(f"median {float(entry['median_ms']):.1f} ms")
    return ", ".join(parts) or "no numbers"
