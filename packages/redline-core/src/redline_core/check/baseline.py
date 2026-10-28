"""Baselines: what the old code did (``CONTRACT.md`` section 19.3).

A baseline is one JSON document::

    {"format": "redline-check-baseline", "version": 1, "id": "<run id>",
     "suite": {...redacted, ${env:..} kept...}, "label": "before", "base_url": "...",
     "git": {"sha": "...", "branch": "...", "dirty": false} | null, "release": "...",
     "created_at": 1767000000.0,
     "requests": [{"idx", "name", "method", "path", "calls", "failed", "broken",
                   "perf": {"samples_ms", "median", "p95", "mean", "stdev", "cv", "failed"},
                   "server": {...} | null, "response": {...capture...} | null,
                   "volatile": [paths], "volatile_headers": [names]}]}

Response bodies are redacted (:class:`~redline_core.redact.Redactor`) before they are
written, so a baseline can be committed or uploaded to CI artifacts.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "FORMAT",
    "VERSION",
    "Baseline",
    "BaselineError",
    "default_baseline_path",
    "git_info",
    "redact_capture",
]

FORMAT = "redline-check-baseline"
VERSION = 1
GIT_TIMEOUT_S = 2.0


class BaselineError(ValueError):
    """A baseline file cannot be read or is not a baseline this version understands."""


def _git(args: list[str], cwd: str, timeout: float) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # no git, not a directory, timeout…
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_info(cwd: str | os.PathLike[str] | None = None, *, timeout: float = GIT_TIMEOUT_S) -> dict:
    """``{sha, branch, dirty}`` of the checkout in ``cwd`` (best effort: ``None`` values
    when git or the repository is missing; never raises)."""
    where = os.fspath(cwd) if cwd is not None else os.getcwd()
    sha = _git(["rev-parse", "HEAD"], where, timeout)
    if not sha or not sha.strip():
        return {"sha": None, "branch": None, "dirty": None}
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], where, timeout)
    status = _git(["status", "--porcelain", "--untracked-files=no"], where, timeout)
    branch = (branch or "").strip() or None
    return {
        "sha": sha.strip(),
        "branch": None if branch == "HEAD" else branch,  # detached
        "dirty": None if status is None else bool(status.strip()),
    }


def redact_capture(capture: Mapping[str, Any] | None, redactor: Any = None) -> Any:
    """A captured response with its body and headers through the redactor (idempotent)."""
    if capture is None:
        return None
    from ..redact import Redactor
    from .diff import redact_json

    r = redactor if redactor is not None else Redactor()
    out = dict(capture)
    if isinstance(out.get("headers"), Mapping):
        out["headers"] = {k.lower(): v for k, v in r.headers(out["headers"]).items()}
    if out.get("body_kind") == "json":
        out["body"] = redact_json(r, out.get("body"))
    elif out.get("body_kind") == "text" and isinstance(out.get("body"), str):
        out["body"] = r.text(out["body"])
    return out


def default_baseline_path(suite_source: str | os.PathLike[str] | None) -> Path:
    """``<suite stem>.baseline.json`` next to the suite (``check.baseline.json`` in the
    working directory for a suite without a file)."""
    if not suite_source:
        return Path("check.baseline.json")
    p = Path(suite_source)
    return p.with_name(f"{p.stem}.baseline.json")


@dataclass
class Baseline:
    id: str
    suite: dict[str, Any]
    requests: list[dict[str, Any]]
    created_at: float
    label: str | None = None
    git: dict[str, Any] | None = None
    release: str | None = None
    base_url: str | None = None
    status: str = "done"
    #: The file it was read from or written to (not serialised).
    file: str | None = field(default=None, compare=False)

    def request(self, name: str) -> dict[str, Any] | None:
        return next((r for r in self.requests if r.get("name") == name), None)

    def ref(self) -> dict[str, Any]:
        """What a report says about the baseline it compared against."""
        return {
            "id": self.id,
            "file": self.file,
            "label": self.label,
            "git": self.git,
            "created_at": self.created_at,
        }

    def summary(self) -> dict[str, Any]:
        """Short totals (stored with the run)."""
        return {
            "requests": len(self.requests),
            "broken": sum(1 for r in self.requests if r.get("broken")),
            "calls": sum(int(r.get("calls") or 0) for r in self.requests),
            "failed": sum(int(r.get("failed") or 0) for r in self.requests),
            "server_side": any(r.get("server") for r in self.requests),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "id": self.id,
            "suite": self.suite,
            "label": self.label,
            "base_url": self.base_url,
            "git": self.git,
            "release": self.release,
            "created_at": self.created_at,
            "status": self.status,
            "requests": self.requests,
        }

    @classmethod
    def from_dict(cls, data: Any, *, file: str | None = None) -> Baseline:
        where = f"{file}: " if file else ""
        if not isinstance(data, Mapping):
            raise BaselineError(f"{where}a baseline must be a JSON object")
        if data.get("format") != FORMAT:
            raise BaselineError(
                f"{where}not a Redline check baseline (format {data.get('format')!r}, "
                f"expected {FORMAT!r})"
            )
        version = data.get("version")
        if version != VERSION:
            raise BaselineError(
                f"{where}unsupported baseline version {version!r} (this Redline reads version "
                f"{VERSION}); record a new baseline"
            )
        requests = data.get("requests")
        if not isinstance(requests, list) or not all(
            isinstance(r, Mapping) and isinstance(r.get("name"), str) for r in requests
        ):
            raise BaselineError(f"{where}'requests' must be a list of objects with a name")
        suite = data.get("suite")
        if not isinstance(suite, Mapping):
            raise BaselineError(f"{where}'suite' must be an object")
        created = data.get("created_at")
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            raise BaselineError(f"{where}'created_at' must be an epoch timestamp")
        return cls(
            id=str(data.get("id") or ""),
            suite=dict(suite),
            requests=[dict(r) for r in requests],
            created_at=float(created),
            label=data.get("label"),
            git=data.get("git") if isinstance(data.get("git"), Mapping) else None,
            release=data.get("release"),
            base_url=data.get("base_url"),
            status=str(data.get("status") or "done"),
            file=file,
        )

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> Baseline:
        label = os.fspath(path)
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise BaselineError(f"{label}: file not found") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise BaselineError(f"{label}: cannot read: {exc}") from None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BaselineError(f"{label}: invalid JSON: {exc}") from None
        return cls.from_dict(data, file=label)

    def redacted(self, redactor: Any = None) -> Baseline:
        """A copy with every captured response through the redactor."""
        from dataclasses import replace

        reqs = []
        for r in self.requests:
            item = dict(r)
            item["response"] = redact_capture(item.get("response"), redactor)
            reqs.append(item)
        return replace(self, requests=reqs)

    def write(self, path: str | os.PathLike[str], *, redactor: Any = None) -> Path:
        """Write atomically (temp file + rename), bodies redacted first."""
        target = Path(path)
        text = json.dumps(self.redacted(redactor).to_dict(), indent=2, ensure_ascii=False)
        parent = target.parent if str(target.parent) else Path(".")
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.file = os.fspath(target)
        return target
