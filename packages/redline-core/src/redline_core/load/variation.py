"""What varies between requests: identities, data feeds and token refresh.

Three optional scenario blocks make the offered load look like many different users:

``identities`` / ``identities_file`` / ``identity_mode``
    Named sets of headers and cookies, one picked per request (``round_robin``,
    ``random`` or ``sticky``). Values may read the environment with ``${VAR}``, so secrets
    stay out of the scenario file. Only the identity *name* is ever recorded (as the
    ``identity`` trace tag and in the run summary), never a header or cookie value.
``data``
    CSV or JSON Lines files whose columns fill ``{data:source.column}`` in a request's
    path, headers and body. All references to one source within a request use the same row.
``auth``
    A token provider (a command, a token request or a Python callable) refreshed in each
    worker before the token expires, off the request path and outside the latency numbers.

Everything here is local: scenario files are trusted, and none of these keys is accepted
from the UI or the HTTP API (they can run a command and read files).
"""

from __future__ import annotations

import asyncio
import csv
import importlib
import json
import math
import os
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profile import ScenarioError, _unknown, parse_duration

__all__ = [
    "LOCAL_ONLY_KEYS",
    "AuthError",
    "AuthSpec",
    "DataSource",
    "Identity",
    "TokenManager",
    "identity_tag",
    "load_data_rows",
    "load_identities",
    "parse_auth",
    "parse_data_sources",
    "parse_identities",
]

#: Scenario keys that can run code or read files: refused from the UI and the HTTP API.
LOCAL_ONLY_KEYS = (
    "auth",
    "identities",
    "identities_file",
    "identity_mode",
    "data",
    "flows",
    "metric_sources",
)

IDENTITY_MODES = ("round_robin", "random", "sticky")
PICK_MODES = ("sequential", "random", "zipf")
MAX_ROWS = 1_000_000
MAX_IDENTITIES = 100_000
_NAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_COOKIE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DATA_REF = re.compile(r"^data:([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$")


class AuthError(RuntimeError):
    """A token could not be obtained or refreshed."""


# ---------------------------------------------------------------------------- identities
@dataclass(frozen=True)
class Identity:
    name: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)

    def request_headers(self) -> dict[str, str]:
        """Headers to send: the identity's headers plus its cookies as one ``Cookie``."""
        out = dict(self.headers)
        if self.cookies:
            out["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return out


def identity_tag(name: str) -> str:
    """The identity name made safe for the ``X-Redline-Run`` header and a trace tag."""
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", name)[:64] or "identity"


def _expand_env(value: str, where: str) -> str:
    def sub(m: re.Match[str]) -> str:
        var = m.group(1)
        if var not in os.environ:
            raise ScenarioError(f"environment variable {var} is not set", where)
        return os.environ[var]

    return _ENV.sub(sub, value)


def _identity(raw: Any, where: str, *, expand: bool) -> Identity:
    if not isinstance(raw, Mapping):
        raise ScenarioError("must be a table/object with a name", where)
    _unknown(raw, {"name", "headers", "cookies"}, where)
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ScenarioError(
            f"must be 1-64 letters, digits, '_', '.', '@' or '-' (got {name!r})", f"{where}.name"
        )
    parts: dict[str, dict[str, str]] = {}
    for key, pattern in (("headers", _HEADER), ("cookies", _COOKIE)):
        value = raw.get(key) or {}
        if not isinstance(value, Mapping):
            raise ScenarioError("must be a table/object of names to values", f"{where}.{key}")
        out: dict[str, str] = {}
        for k, v in value.items():
            if not isinstance(k, str) or not pattern.match(k):
                raise ScenarioError(f"invalid name {k!r}", f"{where}.{key}")
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise ScenarioError("must be a string", f"{where}.{key}.{k}")
            text = str(v)
            if "\r" in text or "\n" in text:
                raise ScenarioError("must not contain a line break", f"{where}.{key}.{k}")
            out[k] = _expand_env(text, f"{where}.{key}.{k}") if expand else text
        parts[key] = out
    return Identity(name=name, headers=parts["headers"], cookies=parts["cookies"])


def parse_identities(data: Mapping[str, Any]) -> tuple[tuple[dict[str, Any], ...], str | None, str]:
    """Validate the identity keys of a scenario (structure only; no file or env access).

    Returns ``(identities, identities_file, identity_mode)`` with the identities as the
    raw (still unexpanded) dicts; :func:`load_identities` resolves them.
    """
    raw = data.get("identities")
    items: tuple[dict[str, Any], ...] = ()
    if raw is not None:
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ScenarioError("must be a non-empty list", "identities")
        if len(raw) > MAX_IDENTITIES:
            raise ScenarioError(f"at most {MAX_IDENTITIES} identities", "identities")
        for i, item in enumerate(raw):
            _identity(item, f"identities[{i}]", expand=False)  # shape only; env read at run time
        items = tuple(dict(i) for i in raw)
    path = data.get("identities_file")
    if path is not None and (not isinstance(path, str) or not path.strip()):
        raise ScenarioError(f"must be a file path (got {path!r})", "identities_file")
    mode = data.get("identity_mode", "round_robin")
    if mode not in IDENTITY_MODES:
        raise ScenarioError(
            f"must be one of {', '.join(IDENTITY_MODES)} (got {mode!r})", "identity_mode"
        )
    return items, (path.strip() if isinstance(path, str) else None), mode


def _read_rows(path: Path, where: str) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ScenarioError(f"cannot read {path.name}: {exc.strerror or exc}", where) from None
    rows: list[dict[str, str]] = []
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                raise ScenarioError(f"{path.name} line {n} is not valid JSON", where) from None
            if not isinstance(obj, Mapping):
                raise ScenarioError(f"{path.name} line {n} must be an object", where)
            rows.append({str(k): ("" if v is None else str(v)) for k, v in obj.items()})
            if len(rows) > MAX_ROWS:
                raise ScenarioError(f"{path.name} has more than {MAX_ROWS} rows", where)
        return rows
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        rows.append({str(k): (v or "") for k, v in row.items() if k is not None})
        if len(rows) > MAX_ROWS:
            raise ScenarioError(f"{path.name} has more than {MAX_ROWS} rows", where)
    return rows


def load_identities(
    items: Sequence[Mapping[str, Any]], file: str | None, base_dir: str | None = None
) -> list[Identity]:
    """Resolve the scenario's identities: inline ones, then the file's, ``${VAR}`` expanded.

    CSV columns: ``name``, ``header:<Header-Name>`` and ``cookie:<name>``. JSON Lines
    objects: ``{"name": ..., "headers": {...}, "cookies": {...}}``.
    """
    out = [_identity(item, f"identities[{i}]", expand=True) for i, item in enumerate(items)]
    if file:
        path = Path(file)
        if not path.is_absolute() and base_dir:
            path = Path(base_dir) / path
        rows = _read_rows(path, "identities_file")
        if path.suffix.lower() in (".jsonl", ".ndjson"):
            for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines()):
                if line.strip():
                    out.append(_identity(json.loads(line), f"identities_file[{i}]", expand=True))
        else:
            for i, row in enumerate(rows):
                headers = {k[7:]: v for k, v in row.items() if k.startswith("header:") and v}
                cookies = {k[7:]: v for k, v in row.items() if k.startswith("cookie:") and v}
                out.append(
                    _identity(
                        {"name": row.get("name", ""), "headers": headers, "cookies": cookies},
                        f"identities_file row {i + 2}",
                        expand=True,
                    )
                )
    names = [i.name for i in out]
    if len(set(names)) != len(names):
        dup = next(n for n in names if names.count(n) > 1)
        raise ScenarioError(f"identity names must be unique (duplicate {dup!r})", "identities")
    if len(out) > MAX_IDENTITIES:
        raise ScenarioError(f"at most {MAX_IDENTITIES} identities", "identities")
    return out


class IdentityPicker:
    """Chooses an identity per request: the same union order across all workers."""

    def __init__(self, identities: Sequence[Identity], mode: str, worker: int, workers: int):
        self.identities = list(identities)
        self.mode = mode
        self.worker = worker
        self.workers = max(1, workers)
        self._n = 0

    def pick(self, rng: random.Random) -> Identity:
        ids = self.identities
        if self.mode == "random":
            return rng.choice(ids)
        if self.mode == "sticky":  # one identity per worker (one virtual user per process)
            return ids[self.worker % len(ids)]
        index = (self.worker + self._n * self.workers) % len(ids)
        self._n += 1
        return ids[index]


# ---------------------------------------------------------------------------- data feeds
@dataclass(frozen=True)
class DataSource:
    name: str
    file: str
    pick: str = "sequential"
    zipf_s: float = 1.1

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "file": self.file, "pick": self.pick}
        if self.pick == "zipf":
            out["zipf_s"] = self.zipf_s
        return out


def parse_data_sources(raw: Any, base_dir: str | None) -> tuple[DataSource, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ScenarioError("must be a non-empty list", "data")
    out: list[DataSource] = []
    for i, item in enumerate(raw):
        where = f"data[{i}]"
        if not isinstance(item, Mapping):
            raise ScenarioError("must be a table/object", where)
        _unknown(item, {"name", "file", "pick", "zipf_s"}, where)
        name = item.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise ScenarioError(
                f"must be 1-32 letters, digits, '_' or '-' (got {name!r})", f"{where}.name"
            )
        if any(d.name == name for d in out):
            raise ScenarioError(f"duplicate data source {name!r}", f"{where}.name")
        file = item.get("file")
        if not isinstance(file, str) or not file.strip():
            raise ScenarioError("is required: a CSV or JSON Lines file", f"{where}.file")
        pick = item.get("pick", "sequential")
        if pick not in PICK_MODES:
            raise ScenarioError(
                f"must be one of {', '.join(PICK_MODES)} (got {pick!r})", f"{where}.pick"
            )
        s = item.get("zipf_s", 1.1)
        if isinstance(s, bool) or not isinstance(s, (int, float)) or not 0 < s <= 10:
            raise ScenarioError(f"must be a number in (0, 10] (got {s!r})", f"{where}.zipf_s")
        path = Path(file)
        if not path.is_absolute() and base_dir:
            path = Path(base_dir) / path
        out.append(DataSource(name=name, file=str(path), pick=pick, zipf_s=float(s)))
    return tuple(out)


def load_data_rows(source: DataSource) -> list[dict[str, str]]:
    rows = _read_rows(Path(source.file), f"data.{source.name}.file")
    if not rows:
        raise ScenarioError("has no rows", f"data.{source.name}.file")
    return rows


class DataFeed:
    """Rows of the scenario's data sources, picked per request (seeded)."""

    def __init__(
        self,
        sources: Sequence[DataSource],
        worker: int,
        workers: int,
        rows: Mapping[str, list[dict[str, str]]] | None = None,
    ) -> None:
        self.sources = {s.name: s for s in sources}
        # ``rows`` come inline when a controller sent them (an agent reads no files)
        self.rows = {s.name: (rows[s.name] if rows else load_data_rows(s)) for s in sources}
        self.worker = worker
        self.workers = max(1, workers)
        self._n: dict[str, int] = {s.name: 0 for s in sources}
        self._cum: dict[str, list[float]] = {}
        for s in sources:
            if s.pick == "zipf":
                total, cum = 0.0, []
                for rank in range(1, len(self.rows[s.name]) + 1):
                    total += 1.0 / (rank**s.zipf_s)
                    cum.append(total)
                self._cum[s.name] = cum

    def check_columns(self, refs: Sequence[tuple[str, str]]) -> None:
        for source, column in refs:
            rows = self.rows.get(source)
            if rows is None:
                raise ScenarioError(f"unknown data source {source!r}", "data")
            if column not in rows[0]:
                raise ScenarioError(f"data source {source!r} has no column {column!r}", "data")

    def new_context(self, rng: random.Random) -> Callable[[str, str], str]:
        """A lookup for one request: every column of a source comes from one row."""
        chosen: dict[str, dict[str, str]] = {}

        def lookup(source: str, column: str) -> str:
            row = chosen.get(source)
            if row is None:
                row = chosen[source] = self._pick(source, rng)
            try:
                return row[column]
            except KeyError:
                raise ValueError(f"data source {source!r} has no column {column!r}") from None

        return lookup

    def _pick(self, name: str, rng: random.Random) -> dict[str, str]:
        rows = self.rows[name]
        source = self.sources[name]
        if source.pick == "random":
            return rows[rng.randrange(len(rows))]
        if source.pick == "zipf":
            cum = self._cum[name]
            x = rng.random() * cum[-1]
            lo, hi = 0, len(cum) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if cum[mid] < x:
                    lo = mid + 1
                else:
                    hi = mid
            return rows[lo]
        n = self._n[name]
        self._n[name] = n + 1
        return rows[(self.worker + n * self.workers) % len(rows)]


def data_refs(text: str) -> list[tuple[str, str]]:
    """The ``(source, column)`` pairs a string refers to with ``{data:source.column}``."""
    out = []
    for token in re.findall(r"\{([^{}]*)\}", text):
        m = _DATA_REF.match(token.strip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def render_data(text: str, lookup: Callable[[str, str], str] | None) -> str:
    """Replace ``{data:source.column}`` in ``text`` using ``lookup``."""
    if lookup is None or "{data:" not in text:
        return text

    def sub(m: re.Match[str]) -> str:
        ref = _DATA_REF.match(m.group(1).strip())
        return lookup(ref.group(1), ref.group(2)) if ref else m.group(0)

    return re.sub(r"\{(data:[^{}]*)\}", sub, text)


def render_value(value: Any, lookup: Callable[[str, str], str] | None) -> Any:
    """:func:`render_data` through nested JSON values (strings only)."""
    if lookup is None:
        return value
    if isinstance(value, str):
        return render_data(value, lookup)
    if isinstance(value, list):
        return [render_value(v, lookup) for v in value]
    if isinstance(value, Mapping):
        return {k: render_value(v, lookup) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------- auth
@dataclass(frozen=True)
class AuthSpec:
    provider: str  # "command" | "request" | "python"
    command: tuple[str, ...] = ()
    request: dict[str, Any] = field(default_factory=dict)
    callable: str | None = None
    extract: str | None = None  # dotted JSON path, e.g. "data.access_token"
    expires_extract: str | None = None  # dotted JSON path of a lifetime in seconds
    ttl_s: float | None = None
    header: str = "Authorization"
    prefix: str = "Bearer "
    refresh_before_s: float = 30.0
    timeout_s: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"provider": self.provider, "header": self.header}
        if self.command:
            out["command"] = list(self.command)
        if self.request:
            out["request"] = dict(self.request)
        if self.callable:
            out["callable"] = self.callable
        out["timeout_s"] = self.timeout_s
        for key in ("extract", "expires_extract", "ttl_s"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        out["prefix"] = self.prefix
        out["refresh_before_s"] = self.refresh_before_s
        return out


_AUTH_KEYS = {
    "provider",
    "command",
    "request",
    "callable",
    "extract",
    "expires_extract",
    "ttl_s",
    "header",
    "prefix",
    "refresh_before_s",
    "timeout_s",
}


def parse_auth(raw: Any) -> AuthSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ScenarioError("must be a table/object", "auth")
    _unknown(raw, _AUTH_KEYS, "auth")
    provider = raw.get("provider")
    if provider not in ("command", "request", "python"):
        raise ScenarioError(
            f"must be command, request or python (got {provider!r})", "auth.provider"
        )
    command: tuple[str, ...] = ()
    request: dict[str, Any] = {}
    target: str | None = None
    if provider == "command":
        cmd = raw.get("command")
        if (
            not isinstance(cmd, (list, tuple))
            or not cmd
            or any(not isinstance(c, str) or not c for c in cmd)
        ):
            raise ScenarioError(
                "must be a list of program and arguments (no shell)", "auth.command"
            )
        command = tuple(cmd)
    elif provider == "request":
        req = raw.get("request")
        if not isinstance(req, Mapping):
            raise ScenarioError("must be a table with method and path", "auth.request")
        _unknown(req, {"method", "path", "headers", "json", "body", "base_url"}, "auth.request")
        path = req.get("path")
        base = req.get("base_url")
        if base is not None:
            from .scenario import validate_base_url

            base = validate_base_url(base, "auth.request.base_url")
        if not isinstance(path, str) or not (path.startswith("/") or base):
            raise ScenarioError("must be a path starting with '/'", "auth.request.path")
        method = str(req.get("method", "POST")).upper()
        if method not in ("GET", "POST", "PUT", "PATCH"):
            raise ScenarioError(f"unsupported method {method!r}", "auth.request.method")
        request = {**dict(req), "method": method}
    else:
        target = raw.get("callable")
        if not isinstance(target, str) or not re.fullmatch(r"[\w.]+:[\w.]+", target):
            raise ScenarioError("must look like 'package.module:function'", "auth.callable")
    ttl = raw.get("ttl_s")
    ttl_s = parse_duration(ttl, "auth.ttl_s") if ttl is not None else None
    before = raw.get("refresh_before_s", 30.0)
    refresh_before = parse_duration(before, "auth.refresh_before_s") if before else 0.0
    timeout = parse_duration(raw.get("timeout_s", 30.0), "auth.timeout_s")
    header = raw.get("header", "Authorization")
    if not isinstance(header, str) or not _HEADER.match(header):
        raise ScenarioError(f"invalid header name {header!r}", "auth.header")
    prefix = raw.get("prefix", "Bearer ")
    if not isinstance(prefix, str) or "\n" in prefix or "\r" in prefix:
        raise ScenarioError("must be a string", "auth.prefix")
    for key in ("extract", "expires_extract"):
        v = raw.get(key)
        if v is not None and (not isinstance(v, str) or not re.fullmatch(r"[\w.\[\]-]+", v)):
            raise ScenarioError("must be a dotted path such as 'data.token'", f"auth.{key}")
    return AuthSpec(
        provider=provider,
        command=command,
        request=request,
        callable=target,
        extract=raw.get("extract"),
        expires_extract=raw.get("expires_extract"),
        ttl_s=ttl_s,
        header=header,
        prefix=prefix,
        refresh_before_s=refresh_before,
        timeout_s=timeout,
    )


def _dig(obj: Any, path: str) -> Any:
    for part in re.findall(r"[^.\[\]]+", path):
        if isinstance(obj, Mapping):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


class TokenManager:
    """Fetches the token and keeps it fresh. The token is never logged or put in a message."""

    def __init__(self, spec: AuthSpec, base_url: str | None = None) -> None:
        self.spec = spec
        self.base_url = base_url
        self.value: str | None = None
        self.expires_at: float | None = None  # monotonic

    def header(self) -> tuple[str, str] | None:
        if self.value is None:
            return None
        return self.spec.header, f"{self.spec.prefix}{self.value}"

    def due_in(self) -> float | None:
        """Seconds until the next refresh is needed (``None``: the token does not expire)."""
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - self.spec.refresh_before_s - time.monotonic())

    async def refresh(self) -> None:
        spec = self.spec
        try:
            if spec.provider == "command":
                raw = await self._run_command()
            elif spec.provider == "request":
                raw = await self._call_request()
            else:
                raw = await self._call_python()
        except AuthError:
            raise
        except Exception as exc:  # never include the message: it may quote the credential
            raise AuthError(f"{spec.provider} provider failed: {type(exc).__name__}") from None
        token, lifetime = self._parse(raw)
        self.value = token
        ttl = lifetime if lifetime is not None else spec.ttl_s
        self.expires_at = time.monotonic() + ttl if ttl else None

    def _parse(self, raw: Any) -> tuple[str, float | None]:
        spec = self.spec
        lifetime: float | None = None
        if isinstance(raw, tuple):  # python provider: (token, ttl_s)
            raw, lifetime = raw[0], (float(raw[1]) if len(raw) > 1 and raw[1] else None)
        if isinstance(raw, str) and spec.extract:
            try:
                raw = json.loads(raw)
            except ValueError:
                raise AuthError("the token response is not JSON") from None
        if spec.extract:
            token = _dig(raw, spec.extract)
            if spec.expires_extract:
                found = _dig(raw, spec.expires_extract)
                if isinstance(found, (int, float)) and not isinstance(found, bool) and found > 0:
                    lifetime = float(found)
        else:
            token = raw
        if not isinstance(token, str) or not token.strip() or "\n" in token.strip():
            raise AuthError("the provider returned no usable token")
        if lifetime is not None and not math.isfinite(lifetime):
            lifetime = None
        return token.strip(), lifetime

    async def _run_command(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            *self.spec.command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.spec.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise AuthError("the command timed out") from None
        if proc.returncode != 0:
            raise AuthError(f"the command exited with status {proc.returncode}")
        return out.decode("utf-8", "replace").strip()

    async def _call_request(self) -> Any:
        import httpx

        req = dict(self.spec.request)
        base = req.get("base_url") or self.base_url
        kwargs: dict[str, Any] = {"headers": req.get("headers") or {}}
        if "json" in req:
            kwargs["json"] = req["json"]
        elif req.get("body") is not None:
            kwargs["content"] = str(req["body"]).encode()
        async with httpx.AsyncClient(base_url=base, timeout=self.spec.timeout_s) as client:
            response = await client.request(req["method"], req["path"], **kwargs)
        if response.status_code >= 400:
            raise AuthError(f"the token endpoint answered HTTP {response.status_code}")
        return response.text

    async def _call_python(self) -> Any:
        module, _, attr = str(self.spec.callable).partition(":")
        fn = getattr(importlib.import_module(module), attr)
        if asyncio.iscoroutinefunction(fn):
            return await fn()
        return await asyncio.get_running_loop().run_in_executor(None, fn)
