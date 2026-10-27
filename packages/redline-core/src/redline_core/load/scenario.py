"""Scenario files: what to send, and how fast (``CONTRACT.md`` section 13).

A scenario is JSON, TOML (``tomllib`` on Python 3.11+, or the ``tomli`` package on 3.10)
or a Python file that defines ``SCENARIO = {...}``::

    name = "browse"
    base_url = "http://127.0.0.1:8000"
    headers = { Accept = "application/json" }
    timeout_s = 10

    [[requests]]
    name = "orders"
    path = "/api/orders/?page={random_int:1:20}"
    weight = 3

    [[requests]]
    method = "POST"
    path = "/api/cart/"
    json = { sku = "A-1", qty = 1 }
    expect_status = [200, 201]

    [profile]
    mode = "steps"
    start_rps = 10
    end_rps = 100
    step_rps = 10
    step_duration_s = "20s"
    stop_on = { error_rate = 0.05, p95_ms = 2000 }

Defaults: ``method`` GET, ``weight`` 1 (0 keeps a request in the file but never sends
it), ``expect_status`` any status below 400, ``timeout_s`` 10, profile constant 10 rps
for 30 s. Per worker process: ``connections`` 64 (HTTP/1.1 keep-alive pool) and
``max_inflight`` 1000 (arrivals beyond it are counted ``dropped``).

Path placeholders, rendered for every request: ``{random_int:A:B}`` (inclusive),
``{random_choice:a|b|c}`` and ``{uuid}``. Any other ``{...}`` in a path is an error, so
an OpenAPI template such as ``/orders/{id}`` cannot be sent by accident.
"""

from __future__ import annotations

import json
import os
import random
import re
import runpy
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profile import Profile, ScenarioError, _unknown, parse_duration, parse_profile

__all__ = [
    "METHODS",
    "RequestSpec",
    "Scenario",
    "ScenarioError",
    "load_scenario",
    "parse_scenario",
    "render_path",
]

METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_CONNECTIONS = 64
DEFAULT_MAX_INFLIGHT = 1_000
MAX_WORKERS = 256
MAX_CONNECTIONS = 10_000
MAX_INFLIGHT = 100_000

_SCENARIO_KEYS = {
    "name",
    "description",
    "base_url",
    "headers",
    "timeout_s",
    "requests",
    "profile",
    "workers",
    "connections",
    "max_inflight",
    "seed",
    "ui_url",
}
_REQUEST_KEYS = {"name", "method", "path", "weight", "headers", "json", "body", "expect_status"}

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_RANDOM_INT = re.compile(r"^random_int:(-?\d+):(-?\d+)$")
_RANDOM_CHOICE = re.compile(r"^random_choice:(.+)$")


@dataclass(frozen=True)
class RequestSpec:
    name: str
    method: str
    path: str
    weight: float = 1.0
    headers: dict[str, str] = field(default_factory=dict)
    json: Any = None
    body: str | None = None
    has_json: bool = False
    expect_status: tuple[int, ...] | None = None  # None = any status < 400

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "method": self.method,
            "path": self.path,
            "weight": self.weight,
        }
        if self.headers:
            out["headers"] = dict(self.headers)
        if self.has_json:
            out["json"] = self.json
        if self.body is not None:
            out["body"] = self.body
        if self.expect_status is not None:
            out["expect_status"] = list(self.expect_status)
        return out


@dataclass(frozen=True)
class Scenario:
    name: str
    base_url: str | None
    requests: tuple[RequestSpec, ...]
    profile: Profile
    headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    workers: int | None = None
    connections: int = DEFAULT_CONNECTIONS
    max_inflight: int = DEFAULT_MAX_INFLIGHT
    seed: int | None = None
    #: Where the Redline UI is mounted (a path on ``base_url`` or an absolute URL); only
    #: used for the link ``redline load`` prints.
    ui_url: str | None = None
    source: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # private ``_keys``, kept verbatim

    def replace(self, **changes: Any) -> Scenario:
        from dataclasses import replace

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "base_url": self.base_url,
            "headers": dict(self.headers),
            "timeout_s": self.timeout_s,
            "requests": [r.to_dict() for r in self.requests],
            "profile": self.profile.to_dict(),
            "connections": self.connections,
            "max_inflight": self.max_inflight,
        }
        if self.workers is not None:
            out["workers"] = self.workers
        if self.seed is not None:
            out["seed"] = self.seed
        if self.ui_url is not None:
            out["ui_url"] = self.ui_url
        return out


# ---------------------------------------------------------------------------- loading
def load_scenario(source: str | os.PathLike[str] | Mapping[str, Any] | Scenario) -> Scenario:
    """Load and validate a scenario from a path (``.json``/``.toml``/``.py``) or a mapping."""
    if isinstance(source, Scenario):
        return source
    if isinstance(source, Mapping):
        return parse_scenario(source)
    path = Path(source)
    label = str(source)
    if not path.is_file():
        raise ScenarioError("file not found", source=label)
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        elif suffix == ".toml":
            data = _toml_loads(path.read_text(encoding="utf-8"))
        elif suffix == ".py":
            namespace = runpy.run_path(str(path))
            if "SCENARIO" not in namespace:
                raise ScenarioError("a Python scenario must define SCENARIO = {...}")
            data = namespace["SCENARIO"]
        else:
            raise ScenarioError(f"unsupported scenario type {suffix!r}; use .json, .toml or .py")
    except ScenarioError as exc:
        raise exc.with_source(label) from None
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"invalid JSON: {exc}", source=label) from None
    except ValueError as exc:  # tomllib.TOMLDecodeError subclasses ValueError
        raise ScenarioError(f"invalid TOML: {exc}", source=label) from None
    try:
        return parse_scenario(data, default_name=path.stem, source=label)
    except ScenarioError as exc:
        raise exc.with_source(label) from None


def _toml_loads(text: str) -> Any:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - exercised on 3.10 only
        try:
            import tomli as tomllib
        except ImportError:
            raise ScenarioError(
                "reading TOML on Python 3.10 needs the 'tomli' package "
                "(pip install tomli), or use a .json / .py scenario"
            ) from None
    return tomllib.loads(text)


# ---------------------------------------------------------------------------- validation
def parse_scenario(
    data: Any, *, default_name: str = "load test", source: str | None = None
) -> Scenario:
    if not isinstance(data, Mapping):
        raise ScenarioError("a scenario must be a table/object at the top level")
    _unknown(data, _SCENARIO_KEYS, "")

    name = data.get("name", default_name)
    if not isinstance(name, str) or not name.strip():
        raise ScenarioError(f"must be a non-empty string (got {name!r})", "name")

    base_url = data.get("base_url")
    if base_url is not None:
        base_url = validate_base_url(base_url, "base_url")

    headers = _headers(data.get("headers"), "headers")
    timeout_s = (
        parse_duration(data["timeout_s"], "timeout_s")
        if data.get("timeout_s") is not None
        else DEFAULT_TIMEOUT_S
    )

    raw_requests = data.get("requests")
    if raw_requests is None:
        raise ScenarioError("is required: a list of requests to send", "requests")
    if not isinstance(raw_requests, (list, tuple)) or not raw_requests:
        raise ScenarioError("must be a non-empty list", "requests")
    requests = tuple(_request(r, i) for i, r in enumerate(raw_requests))
    if not any(r.weight > 0 for r in requests):
        raise ScenarioError("at least one request needs a weight > 0", "requests")

    profile = parse_profile(data.get("profile"), "profile")
    workers = _int(data.get("workers"), "workers", minimum=1, maximum=MAX_WORKERS)
    connections = (
        _int(data.get("connections"), "connections", minimum=1, maximum=MAX_CONNECTIONS)
        or DEFAULT_CONNECTIONS
    )
    max_inflight = (
        _int(data.get("max_inflight"), "max_inflight", minimum=1, maximum=MAX_INFLIGHT)
        or DEFAULT_MAX_INFLIGHT
    )
    seed = data.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ScenarioError(f"must be an integer (got {seed!r})", "seed")
    ui_url = data.get("ui_url")
    if ui_url is not None:
        ui_url = validate_ui_url(ui_url, "ui_url")
    extra = {k: v for k, v in data.items() if isinstance(k, str) and k.startswith("_")}
    return Scenario(
        name=name.strip(),
        base_url=base_url,
        requests=requests,
        profile=profile,
        headers=headers,
        timeout_s=timeout_s,
        workers=workers,
        connections=connections,
        max_inflight=max_inflight,
        seed=seed,
        ui_url=ui_url,
        source=source,
        extra=extra,
    )


def validate_ui_url(value: Any, path: str = "ui_url") -> str:
    """The Redline UI location: a path (``/redline``) or an ``http(s)://`` URL."""
    if isinstance(value, str):
        value = value.strip()
        if re.match(r"^https?://[^/\s]+", value) or re.match(r"^/(?![/\\])\S*$", value):
            return value.rstrip("/") or "/"
    raise ScenarioError(
        f"must be a path such as '/redline' or an http(s):// URL (got {value!r})", path
    )


def validate_base_url(value: Any, path: str = "base_url") -> str:
    if not isinstance(value, str) or not re.match(r"^https?://[^/\s]+", value.strip()):
        raise ScenarioError(f"must be an http:// or https:// URL (got {value!r})", path)
    return value.strip().rstrip("/")


def _int(value: Any, path: str, *, minimum: int, maximum: int | None = None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScenarioError(f"must be an integer ≥ {minimum} (got {value!r})", path)
    if maximum is not None and value > maximum:
        raise ScenarioError(f"must be ≤ {maximum} (got {value!r})", path)
    return value


def _headers(value: Any, path: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ScenarioError("must be a table/object of header names to values", path)
    out: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise ScenarioError(f"invalid header name {key!r}", path)
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            raise ScenarioError(f"must be a string (got {val!r})", f"{path}.{key}")
        out[key] = str(val)
    return out


def _request(raw: Any, index: int) -> RequestSpec:
    path = f"requests[{index}]"
    if not isinstance(raw, Mapping):
        raise ScenarioError("must be a table/object with at least a path", path)
    _unknown(raw, _REQUEST_KEYS, path)
    url_path = raw.get("path")
    if not isinstance(url_path, str) or not url_path.startswith("/"):
        raise ScenarioError(f"must be a path starting with '/' (got {url_path!r})", f"{path}.path")
    try:
        render_path(url_path, random.Random(0))
    except ValueError as exc:
        raise ScenarioError(str(exc), f"{path}.path") from None
    method = raw.get("method", "GET")
    if not isinstance(method, str) or method.upper() not in METHODS:
        raise ScenarioError(
            f"must be one of {', '.join(METHODS)} (got {method!r})", f"{path}.method"
        )
    method = method.upper()
    weight = raw.get("weight", 1)
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
        raise ScenarioError(f"must be a number ≥ 0 (got {weight!r})", f"{path}.weight")
    has_json = "json" in raw and raw["json"] is not None
    body = raw.get("body")
    if has_json and body is not None:
        raise ScenarioError("set either json or body, not both", path)
    if body is not None and not isinstance(body, str):
        raise ScenarioError(f"must be a string (got {type(body).__name__})", f"{path}.body")
    if has_json:
        try:
            json.dumps(raw["json"])
        except (TypeError, ValueError) as exc:
            raise ScenarioError(f"is not JSON-serialisable: {exc}", f"{path}.json") from None
    expect = raw.get("expect_status")
    expect_t: tuple[int, ...] | None = None
    if expect is not None:
        items = expect if isinstance(expect, (list, tuple)) else [expect]
        if not items or any(
            isinstance(s, bool) or not isinstance(s, int) or not 100 <= s <= 599 for s in items
        ):
            raise ScenarioError(
                f"must be a status code or a list of them (got {expect!r})",
                f"{path}.expect_status",
            )
        expect_t = tuple(items)
    name = raw.get("name") or f"{method} {url_path}"
    if not isinstance(name, str):
        raise ScenarioError(f"must be a string (got {name!r})", f"{path}.name")
    return RequestSpec(
        name=name,
        method=method,
        path=url_path,
        weight=float(weight),
        headers=_headers(raw.get("headers"), f"{path}.headers"),
        json=raw.get("json") if has_json else None,
        body=body,
        has_json=has_json,
        expect_status=expect_t,
    )


# ---------------------------------------------------------------------------- placeholders
def render_path(path: str, rng: random.Random) -> str:
    """Expand ``{random_int:A:B}``, ``{random_choice:a|b}`` and ``{uuid}``.

    Raises ``ValueError`` for anything else in braces.
    """
    if "{" not in path:
        return path

    def sub(m: re.Match[str]) -> str:
        token = m.group(1).strip()
        if token == "uuid":
            return uuid.UUID(int=rng.getrandbits(128), version=4).hex
        im = _RANDOM_INT.match(token)
        if im:
            lo, hi = int(im.group(1)), int(im.group(2))
            if hi < lo:
                raise ValueError(f"placeholder {{{token}}}: B must be ≥ A")
            return str(rng.randint(lo, hi))
        cm = _RANDOM_CHOICE.match(token)
        if cm:
            return rng.choice(cm.group(1).split("|"))
        raise ValueError(
            f"unknown placeholder {{{token}}}; use a concrete value or "
            "{random_int:A:B}, {random_choice:a|b} or {uuid}"
        )

    return _PLACEHOLDER.sub(sub, path)
