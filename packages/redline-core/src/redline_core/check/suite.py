"""Check suites: which read-only requests to replay, and how (``CONTRACT.md`` section 19.1).

A suite is TOML (``tomllib`` on Python 3.11+) or JSON::

    name = "orders refactor"
    base_url = "http://127.0.0.1:8000"
    headers = { Authorization = "Bearer ${env:API_TOKEN}" }
    repeat = 20
    warmup = 3

    [thresholds]
    slower_pct = 10
    slower_ms = 2
    max_query_increase = 0

    [[requests]]
    name = "list orders"
    path = "/orders?page=1"
    ignore = ["data[*].created_at", "meta.request_id"]

Only ``GET``, ``HEAD`` and ``OPTIONS`` are accepted: a refactor check replays the same
requests many times against two versions of an application, which is only safe for
requests that change nothing. Paths are relative to ``base_url`` (absolute URLs are
refused, so a suite cannot be pointed at another host by accident).

``${env:NAME}`` placeholders in header values are expanded by :meth:`Suite.headers_for`
when a run starts and never written back: :meth:`Suite.to_dict`, :meth:`Suite.to_toml`
and the stored/baseline copies keep the placeholder.

JSON paths (``ignore`` rules and diff output): dotted keys, ``[n]`` indexes, ``[*]`` any
index, ``*`` any key; a key with dots, brackets, quotes or whitespace (or a literal ``*``)
is written ``["a.b"]``; ``$`` alone is the whole body. A rule also covers everything below
the path it names (``meta`` ignores ``meta.request_id``).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ANY_INDEX",
    "ANY_KEY",
    "READ_ONLY_METHODS",
    "CheckRequest",
    "Suite",
    "SuiteError",
    "Thresholds",
    "expand_env",
    "format_path",
    "parse_path",
    "path_matches",
    "redact_suite",
]

READ_ONLY_METHODS = ("GET", "HEAD", "OPTIONS")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE", "CONNECT", "TRACE")
MAX_REPEAT = 1000
MAX_WARMUP = 100
MAX_TIMEOUT_S = 600.0
DEFAULT_REPEAT = 20
DEFAULT_WARMUP = 3
DEFAULT_TIMEOUT_S = 10.0

_SUITE_KEYS = {
    "name",
    "base_url",
    "headers",
    "timeout_s",
    "repeat",
    "warmup",
    "thresholds",
    "requests",
}
_THRESHOLD_KEYS = {"slower_pct", "slower_ms", "max_query_increase"}
_REQUEST_KEYS = {
    "name",
    "method",
    "path",
    "headers",
    "perf",
    "response",
    "ignore",
    "ignore_headers",
    "expect_status",
}
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_ENV = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_ANY = re.compile(r"\$\{env:([^}]*)\}?")
_AUTH_SCHEMES = {"", "bearer", "basic", "token", "digest"}


class SuiteError(ValueError):
    """A suite is invalid. ``path`` names the offending key (``requests[1].method``),
    ``request`` the request's name when known, ``source`` the suite file."""

    def __init__(
        self,
        message: str,
        path: str | None = None,
        *,
        request: str | None = None,
        source: str | None = None,
    ) -> None:
        self.message = message
        self.path = path
        self.request = request
        self.source = source
        super().__init__(self._render())

    def _render(self) -> str:
        src = f"{self.source}: " if self.source else ""
        where = self.path or ""
        if self.request:
            where = f"{where} ({self.request!r})" if where else repr(self.request)
        return f"{src}{where + ': ' if where else ''}{self.message}"

    def with_source(self, source: str) -> SuiteError:
        return SuiteError(self.message, self.path, request=self.request, source=source)


# ============================================================================ JSON paths
class _Wildcard:
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


#: ``[*]``: any list index.
ANY_INDEX = _Wildcard("[*]")
#: ``*``: any object key.
ANY_KEY = _Wildcard("*")

Token = str | int | _Wildcard
_SAFE_KEY = re.compile(r"[^\s.\[\]\"'*]+")


def parse_path(text: str) -> tuple[Token, ...]:
    """``"data[*].created_at"`` → ``("data", ANY_INDEX, "created_at")``.

    Raises ``ValueError`` with a short reason on bad syntax.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("must be a non-empty JSON path")
    s = text.strip()
    if s == "$":
        return ()
    i = 0
    if s.startswith("$.") or s.startswith("$["):
        i = 1 if s[1] == "[" else 2
    tokens: list[Token] = []
    n = len(s)
    first = True
    while i < n:
        ch = s[i]
        if ch == "[":
            if s.startswith("[*]", i):
                tokens.append(ANY_INDEX)
                i += 3
            elif i + 1 < n and s[i + 1] == '"':
                try:
                    key, end = json.JSONDecoder().raw_decode(s, i + 1)
                except ValueError:
                    raise ValueError(f"unterminated quoted key at {i}") from None
                if not isinstance(key, str) or end >= n or s[end] != "]":
                    raise ValueError(f'expected "] after the quoted key at {i}')
                tokens.append(key)
                i = end + 1
            else:
                m = re.match(r"\[(\d+)\]", s[i:])
                if not m:
                    raise ValueError(f'expected [n], [*] or ["key"] at {i}')
                tokens.append(int(m.group(1)))
                i += m.end()
        else:
            if ch == ".":
                if first:
                    raise ValueError("must not start with '.'")
                i += 1
            elif not first:
                raise ValueError(f"expected '.' or '[' at {i}")
            m = re.match(r"[^.\[]*", s[i:])
            key = m.group(0) if m else ""
            if not key:
                raise ValueError(f"empty key at {i}")
            if '"' in key or "]" in key:
                raise ValueError(f'write the key {key!r} as ["..."]')
            tokens.append(ANY_KEY if key == "*" else key)
            i += len(key)
        first = False
    return tuple(tokens)


def format_path(tokens: tuple[Token, ...] | list[Token]) -> str:
    """The inverse of :func:`parse_path`; ``()`` is ``"$"``."""
    if not tokens:
        return "$"
    out: list[str] = []
    for tok in tokens:
        if tok is ANY_INDEX:
            out.append("[*]")
        elif tok is ANY_KEY:
            out.append(".*" if out else "*")
        elif isinstance(tok, int):
            out.append(f"[{tok}]")
        elif _SAFE_KEY.fullmatch(tok) and tok != "$":
            out.append(f".{tok}" if out else tok)
        else:
            out.append(f"[{json.dumps(tok, ensure_ascii=False)}]")
    return "".join(out)


def path_matches(pattern: tuple[Token, ...], concrete: tuple[str | int, ...]) -> bool:
    """``pattern`` names ``concrete`` or one of its ancestors."""
    if len(pattern) > len(concrete):
        return False
    for p, c in zip(pattern, concrete, strict=False):
        if p is ANY_INDEX:
            if not isinstance(c, int):
                return False
        elif p is ANY_KEY:
            if not isinstance(c, str):
                return False
        elif type(p) is not type(c) or p != c:
            return False
    return True


# ============================================================================ model
@dataclass(frozen=True)
class Thresholds:
    """When a request counts as slower (19.4): significant AND > ``slower_pct`` % AND >
    ``slower_ms`` ms; more than ``max_query_increase`` extra queries is slower regardless."""

    slower_pct: float = 10.0
    slower_ms: float = 2.0
    max_query_increase: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slower_pct": self.slower_pct,
            "slower_ms": self.slower_ms,
            "max_query_increase": self.max_query_increase,
        }


@dataclass(frozen=True)
class CheckRequest:
    name: str
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    perf: bool = True
    response: bool = True
    ignore: tuple[str, ...] = ()
    ignore_headers: tuple[str, ...] = ()
    expect_status: tuple[int, ...] | None = None

    def ignore_patterns(self) -> list[tuple[Token, ...]]:
        return [parse_path(p) for p in self.ignore]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "method": self.method, "path": self.path}
        if self.headers:
            out["headers"] = dict(self.headers)
        if not self.perf:
            out["perf"] = False
        if not self.response:
            out["response"] = False
        if self.ignore:
            out["ignore"] = list(self.ignore)
        if self.ignore_headers:
            out["ignore_headers"] = list(self.ignore_headers)
        if self.expect_status is not None:
            es = self.expect_status
            out["expect_status"] = es[0] if len(es) == 1 else list(es)
        return out


@dataclass(frozen=True)
class Suite:
    name: str
    requests: tuple[CheckRequest, ...]
    base_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    repeat: int = DEFAULT_REPEAT
    warmup: int = DEFAULT_WARMUP
    thresholds: Thresholds = field(default_factory=Thresholds)
    #: The file the suite was read from (not serialised).
    source: str | None = field(default=None, compare=False)

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_dict(
        cls, data: Any, *, source: str | None = None, default_name: str = "refactor check"
    ) -> Suite:
        try:
            return _parse_suite(data, default_name=default_name, source=source)
        except SuiteError as exc:
            raise (exc.with_source(source) if source and not exc.source else exc) from None

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> Suite:
        """Load a ``.toml`` or ``.json`` suite; errors name the file."""
        p = Path(path)
        label = str(path)
        if not p.is_file():
            raise SuiteError("file not found", source=label)
        suffix = p.suffix.lower()
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SuiteError(f"cannot read: {exc}", source=label) from None
        if suffix == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SuiteError(f"invalid JSON: {exc}", source=label) from None
        elif suffix == ".toml":
            data = _toml_loads(text, label)
        else:
            raise SuiteError(f"unsupported suite type {suffix!r}; use .toml or .json", source=label)
        return cls.from_dict(data, source=label, default_name=p.stem)

    # ------------------------------------------------------------------ helpers
    def replace(self, **changes: Any) -> Suite:
        from dataclasses import replace

        return replace(self, **changes)

    def request(self, name: str) -> CheckRequest | None:
        return next((r for r in self.requests if r.name == name), None)

    def headers_for(
        self, request: CheckRequest, environ: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Suite headers merged with the request's (case-insensitive, request wins), with
        ``${env:NAME}`` expanded from ``environ`` (default ``os.environ``)."""
        merged: dict[str, str] = {}
        for source in (self.headers, request.headers):
            for key, value in source.items():
                for existing in [k for k in merged if k.lower() == key.lower()]:
                    del merged[existing]
                merged[key] = value
        env = os.environ if environ is None else environ
        return {k: expand_env(v, env, f"{request.name}: header {k}") for k, v in merged.items()}

    def env_names(self) -> list[str]:
        """Every ``${env:NAME}`` the suite refers to, in order of first use."""
        names: list[str] = []
        values = list(self.headers.values())
        for r in self.requests:
            values += r.headers.values()
        for v in values:
            for m in _ENV.finditer(v):
                if m.group(1) not in names:
                    names.append(m.group(1))
        return names

    def to_dict(self) -> dict[str, Any]:
        """The suite as written (placeholders kept, defaults filled in)."""
        out: dict[str, Any] = {"name": self.name}
        if self.base_url is not None:
            out["base_url"] = self.base_url
        if self.headers:
            out["headers"] = dict(self.headers)
        out["timeout_s"] = self.timeout_s
        out["repeat"] = self.repeat
        out["warmup"] = self.warmup
        out["thresholds"] = self.thresholds.to_dict()
        out["requests"] = [r.to_dict() for r in self.requests]
        return out

    def to_toml(self) -> str:
        """The suite as a TOML document (round-trips through :meth:`from_dict`)."""
        return to_toml(self.to_dict())

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"


def expand_env(value: str, environ: Mapping[str, str], where: str = "") -> str:
    """Replace ``${env:NAME}``; a variable that is not set is a :class:`SuiteError`."""
    if "${env:" not in value:
        return value

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in environ:
            raise SuiteError(f"environment variable {name} is not set", where or None)
        return environ[name]

    return _ENV.sub(sub, value)


# ============================================================================ validation
def _toml_loads(text: str, label: str) -> Any:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - exercised on 3.10 only
        try:
            import tomli as tomllib
        except ImportError:
            raise SuiteError(
                "reading TOML suites needs Python 3.11+ (or the 'tomli' package); "
                "on Python 3.10 write the suite as .json",
                source=label,
            ) from None
    try:
        return tomllib.loads(text)
    except ValueError as exc:  # TOMLDecodeError subclasses ValueError
        raise SuiteError(f"invalid TOML: {exc}", source=label) from None


def _unknown(data: Mapping[str, Any], allowed: set[str], path: str, request: str | None) -> None:
    import difflib

    for key in data:
        if not isinstance(key, str):
            raise SuiteError(f"keys must be strings (got {key!r})", path or None, request=request)
        if key in allowed:
            continue
        hint = difflib.get_close_matches(key, sorted(allowed), n=1)
        extra = (
            f"; did you mean {hint[0]!r}?" if hint else f"; allowed: {', '.join(sorted(allowed))}"
        )
        raise SuiteError(f"unknown key{extra}", f"{path}.{key}" if path else key, request=request)


def _number(
    value: Any,
    path: str,
    *,
    minimum: float,
    maximum: float | None = None,
    integer: bool = False,
    strict_min: bool = False,
) -> Any:
    kinds = (int,) if integer else (int, float)
    bad = isinstance(value, bool) or not isinstance(value, kinds)
    if not bad and isinstance(value, float) and value != value:  # NaN
        bad = True
    if not bad:
        bad = value <= minimum if strict_min else value < minimum
    if not bad and maximum is not None:
        bad = value > maximum
    if bad:
        kind = "an integer" if integer else "a number"
        lo = f"> {minimum:g}" if strict_min else f"≥ {minimum:g}"
        hi = f" and ≤ {maximum:g}" if maximum is not None else ""
        raise SuiteError(f"must be {kind} {lo}{hi} (got {value!r})", path)
    return value


def _headers(value: Any, path: str, request: str | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SuiteError("must be a table/object of header names to values", path, request=request)
    out: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or not _HEADER_NAME.fullmatch(key):
            raise SuiteError(f"invalid header name {key!r}", path, request=request)
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            raise SuiteError(f"must be a string (got {val!r})", f"{path}.{key}", request=request)
        text = str(val)
        for m in _ENV_ANY.finditer(text):
            if not _ENV.fullmatch(m.group(0)):
                raise SuiteError(
                    f"bad placeholder {m.group(0)!r}; write ${{env:NAME}}",
                    f"{path}.{key}",
                    request=request,
                )
        if "\n" in text or "\r" in text:
            raise SuiteError("header values must be one line", f"{path}.{key}", request=request)
        out[key] = text
    return out


def validate_base_url(value: Any, path: str = "base_url") -> str:
    if not isinstance(value, str) or not re.match(r"^https?://[^/\s]+", value.strip()):
        raise SuiteError(f"must be an http:// or https:// URL (got {value!r})", path)
    return value.strip().rstrip("/")


def _parse_suite(data: Any, *, default_name: str, source: str | None) -> Suite:
    if not isinstance(data, Mapping):
        raise SuiteError("a suite must be a table/object at the top level")
    _unknown(data, _SUITE_KEYS, "", None)
    name = data.get("name", default_name)
    if not isinstance(name, str) or not name.strip():
        raise SuiteError(f"must be a non-empty string (got {name!r})", "name")
    base_url = data.get("base_url")
    if base_url is not None:
        base_url = validate_base_url(base_url)
    headers = _headers(data.get("headers"), "headers", None)
    timeout_s = float(
        _number(
            data.get("timeout_s", DEFAULT_TIMEOUT_S),
            "timeout_s",
            minimum=0,
            maximum=MAX_TIMEOUT_S,
            strict_min=True,
        )
    )
    repeat = _number(
        data.get("repeat", DEFAULT_REPEAT), "repeat", minimum=1, maximum=MAX_REPEAT, integer=True
    )
    warmup = _number(
        data.get("warmup", DEFAULT_WARMUP), "warmup", minimum=0, maximum=MAX_WARMUP, integer=True
    )
    thresholds = _thresholds(data.get("thresholds"))
    raw = data.get("requests")
    if raw is None:
        raise SuiteError("is required: a list of requests to check", "requests")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise SuiteError("must be a non-empty list", "requests")
    requests = tuple(_request(r, i) for i, r in enumerate(raw))
    seen: dict[str, int] = {}
    for i, r in enumerate(requests):
        if r.name in seen:
            raise SuiteError(
                f"duplicate request name (also requests[{seen[r.name]}]); names must be unique",
                f"requests[{i}].name",
                request=r.name,
            )
        seen[r.name] = i
    return Suite(
        name=name.strip(),
        requests=requests,
        base_url=base_url,
        headers=headers,
        timeout_s=timeout_s,
        repeat=repeat,
        warmup=warmup,
        thresholds=thresholds,
        source=source,
    )


def _thresholds(value: Any) -> Thresholds:
    if value is None:
        return Thresholds()
    if not isinstance(value, Mapping):
        raise SuiteError("must be a table/object", "thresholds")
    _unknown(value, _THRESHOLD_KEYS, "thresholds", None)
    d = Thresholds()
    return Thresholds(
        slower_pct=float(
            _number(value.get("slower_pct", d.slower_pct), "thresholds.slower_pct", minimum=0)
        ),
        slower_ms=float(
            _number(value.get("slower_ms", d.slower_ms), "thresholds.slower_ms", minimum=0)
        ),
        max_query_increase=_number(
            value.get("max_query_increase", d.max_query_increase),
            "thresholds.max_query_increase",
            minimum=0,
            integer=True,
        ),
    )


def _request(raw: Any, index: int) -> CheckRequest:
    path = f"requests[{index}]"
    if not isinstance(raw, Mapping):
        raise SuiteError("must be a table/object with at least a path", path)
    label = raw.get("name") if isinstance(raw.get("name"), str) else None
    _unknown(raw, _REQUEST_KEYS, path, label)

    method = raw.get("method", "GET")
    if not isinstance(method, str) or not method.strip():
        raise SuiteError(f"must be a string (got {method!r})", f"{path}.method", request=label)
    method = method.strip().upper()
    if method not in READ_ONLY_METHODS:
        if method in WRITE_METHODS:
            msg = (
                f"{method} is refused: a refactor check replays every request many times, so "
                "only read-only methods (GET, HEAD, OPTIONS) are allowed"
            )
        else:
            msg = f"must be one of {', '.join(READ_ONLY_METHODS)} (got {method!r})"
        raise SuiteError(msg, f"{path}.method", request=label)

    url_path = raw.get("path")
    if not isinstance(url_path, str) or not url_path.strip():
        raise SuiteError(f"must be a path such as '/orders' (got {url_path!r})", f"{path}.path")
    url_path = url_path.strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url_path) or url_path.startswith("//"):
        raise SuiteError(
            f"absolute URLs are refused (got {url_path!r}); write the path and set base_url",
            f"{path}.path",
            request=label,
        )
    if not url_path.startswith("/"):
        raise SuiteError(f"must start with '/' (got {url_path!r})", f"{path}.path", request=label)
    if any(c.isspace() for c in url_path):
        raise SuiteError("must not contain whitespace", f"{path}.path", request=label)

    name = raw.get("name", f"{method} {url_path}")
    if not isinstance(name, str) or not name.strip():
        raise SuiteError(f"must be a non-empty string (got {name!r})", f"{path}.name")
    name = name.strip()

    flags = {}
    for key in ("perf", "response"):
        value = raw.get(key, True)
        if not isinstance(value, bool):
            raise SuiteError(
                f"must be true or false (got {value!r})", f"{path}.{key}", request=name
            )
        flags[key] = value
    if not flags["perf"] and not flags["response"]:
        raise SuiteError("perf and response are both off: nothing to check", path, request=name)

    ignore = _string_list(raw.get("ignore"), f"{path}.ignore", name)
    for i, rule in enumerate(ignore):
        try:
            parse_path(rule)
        except ValueError as exc:
            raise SuiteError(
                f"bad JSON path {rule!r}: {exc}", f"{path}.ignore[{i}]", request=name
            ) from None
    ignore_headers = _string_list(raw.get("ignore_headers"), f"{path}.ignore_headers", name)
    for h in ignore_headers:
        if not _HEADER_NAME.fullmatch(h):
            raise SuiteError(f"invalid header name {h!r}", f"{path}.ignore_headers", request=name)

    expect = raw.get("expect_status")
    expect_t: tuple[int, ...] | None = None
    if expect is not None:
        items = expect if isinstance(expect, (list, tuple)) else [expect]
        if not items or any(
            isinstance(s, bool) or not isinstance(s, int) or not 100 <= s <= 599 for s in items
        ):
            raise SuiteError(
                f"must be a status code or a list of them (got {expect!r})",
                f"{path}.expect_status",
                request=name,
            )
        expect_t = tuple(items)

    return CheckRequest(
        name=name,
        method=method,
        path=url_path,
        headers=_headers(raw.get("headers"), f"{path}.headers", name),
        perf=flags["perf"],
        response=flags["response"],
        ignore=tuple(ignore),
        ignore_headers=tuple(h.lower() for h in ignore_headers),
        expect_status=expect_t,
    )


def _string_list(value: Any, path: str, request: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SuiteError("must be a list of strings", path, request=request)
    if any(not isinstance(v, str) or not v.strip() for v in value):
        raise SuiteError("must be a list of non-empty strings", path, request=request)
    return [v.strip() for v in value]


# ============================================================================ redaction
def redact_suite(suite: Suite | Mapping[str, Any], redactor: Any = None) -> dict[str, Any]:
    """The suite dict safe to store and serve (like §13 run configs): secret header values
    are masked unless they are only a ``${env:NAME}`` placeholder (optionally after an
    auth scheme such as ``Bearer``); secrets in ``base_url``/``path``/``name`` are masked."""
    from ..redact import MASK, Redactor

    r = redactor if redactor is not None else Redactor()
    data = suite.to_dict() if isinstance(suite, Suite) else json.loads(json.dumps(suite))

    def headers(value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        out = {}
        masked = r.headers(value)
        for k, v in value.items():
            secret = masked.get(k) == MASK or r.is_secret_key(k)
            if not secret:
                out[k] = r.text(str(v)) if isinstance(v, str) else v
            elif isinstance(v, str) and _only_placeholder(v):
                out[k] = v
            else:
                out[k] = MASK
        return out

    if "headers" in data:
        data["headers"] = headers(data["headers"])
    if isinstance(data.get("base_url"), str):
        data["base_url"] = r.url(data["base_url"])
    for item in data.get("requests") or []:
        if isinstance(item, dict):
            if "headers" in item:
                item["headers"] = headers(item["headers"])
            if isinstance(item.get("path"), str):
                item["path"] = r.url(item["path"])
            if isinstance(item.get("name"), str):
                item["name"] = r.text(item["name"])
    return data


def _only_placeholder(value: str) -> bool:
    if not _ENV.search(value):
        return False
    rest = _ENV.sub("", value).strip().lower()
    return rest in _AUTH_SCHEMES


# ============================================================================ TOML writer
def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        inner = ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    raise ValueError(f"TOML cannot express {value!r}")


def to_toml(data: Mapping[str, Any]) -> str:
    """A small TOML writer for suite dicts (scalars, inline tables, ``[thresholds]``,
    ``[[requests]]``)."""
    lines = ["# Redline refactor check suite – see `redline check --help`.", ""]
    for key, value in data.items():
        if value is None or key in ("thresholds", "requests"):
            continue
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    if data.get("thresholds"):
        lines += ["", "[thresholds]"]
        for key, value in data["thresholds"].items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for req in data.get("requests") or []:
        lines += ["", "[[requests]]"]
        for key, value in req.items():
            if value is not None:
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"
