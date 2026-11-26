"""Shared pieces of the agent tools: the tool type, argument validation, output bounding and
secret scrubbing, and the server options that switch the state-changing tools on."""

from __future__ import annotations

import contextvars
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "MAX_ITEMS",
    "MAX_STR",
    "OPTIONS",
    "ServerOptions",
    "Tool",
    "api_get",
    "api_post",
    "clip",
    "current_options",
    "obj",
    "read_local_file",
    "resolve_local",
    "safe_url",
    "scrub_text",
    "validate",
]

MAX_ITEMS = 100
MAX_STR = 2000
MAX_DEPTH = 8
MAX_OUTPUT = 400_000
MAX_LOCAL_FILE = 5_000_000


@dataclass(frozen=True)
class ServerOptions:
    """What the operator allowed when starting ``redline mcp``. Everything is off by default."""

    store_path: str | None = None
    #: start and stop load runs (``--allow-load``); needs ``load_target``
    allow_load: bool = False
    #: the only URL a started run may send load to (``--load-target``)
    load_target: str | None = None
    #: delete finished load runs (``--allow-delete-runs``)
    allow_delete_runs: bool = False

    def allows(self, gate: str | None) -> bool:
        if gate is None:
            return True
        return {"load": self.allow_load, "delete_runs": self.allow_delete_runs}.get(gate, False)


#: The options of the call in progress (set by :func:`~redline_core.agent.tools.call_tool`).
OPTIONS: contextvars.ContextVar[ServerOptions | None] = contextvars.ContextVar(
    "redline_agent_options", default=None
)


def current_options() -> ServerOptions:
    return OPTIONS.get() or ServerOptions()


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[Any, dict[str, Any]], dict[str, Any]]
    #: ``None`` for a read-only tool, else the server option that must be on (``load``,
    #: ``delete_runs``). A gated tool is neither listed nor callable without it.
    gate: str | None = None
    #: longest string kept in the result (the plan text is the one tool that needs more)
    max_str: int = MAX_STR


def obj(props: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(required),
        "additionalProperties": False,
    }


# ------------------------------------------------------------------ argument validation
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def _check(schema: Mapping[str, Any], value: Any, name: str, depth: int = 0) -> None:
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                _check(option, value, name, depth + 1)
                return
            except ValueError:
                continue
        raise ValueError(f"{name} has the wrong type or shape")
    kind = schema.get("type")
    if kind:
        ok = isinstance(value, _TYPES[kind]) and not (kind != "boolean" and isinstance(value, bool))
        if not ok:
            label = {"integer": "a whole number", "number": "a number"}.get(kind, f"a {kind}")
            raise ValueError(f"{name} must be {label}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{name} must be one of: {', '.join(map(str, schema['enum']))}")
    if kind in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{name} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{name} must be at most {schema['maximum']}")
    if kind == "string":
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{name} is longer than {schema['maxLength']} characters")
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{name} is required")
    if kind == "array":
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{name} has more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                _check(schema["items"], item, f"{name}[{i}]", depth + 1)
    if kind == "object" and "properties" in schema and depth < 4:
        props = schema["properties"]
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(props)
            if unknown:
                raise ValueError(f"unknown argument(s): {', '.join(sorted(map(str, unknown)))}")
        for key in schema.get("required", ()):
            if key not in value or value[key] in (None, ""):
                raise ValueError(f"{key} is required")
        for key, sub in value.items():
            if key in props:
                _check(props[key], sub, key if depth == 0 else f"{name}.{key}", depth + 1)


def validate(tool: Tool, args: Any) -> None:
    """Check ``args`` against the tool's JSON schema (the subset the tools use)."""
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _check(tool.schema, args, "arguments")


# ------------------------------------------------------------------ output bounding + secrets
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_KEY_VALUE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)(\s*[=:]\s*)"
    r"(?!\*\*\*)[^\s;&,'\"]+"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_SENSITIVE_KEY = re.compile(
    r"(?i)(^|_|-)(password|passwd|pwd|secret|token|authorization|cookie|api_?key|credential)s?($|_|-)"
)
_HEADER_BAGS = {"headers", "cookies", "header_values"}


def scrub_text(text: str) -> str:
    """Mask URL credentials, ``password=…`` style pairs and bearer tokens in a string."""
    text = _URL_USERINFO.sub(r"\1***@", text)
    text = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return _BEARER.sub(lambda m: f"{m.group(1)} ***", text)


def safe_url(value: Any) -> str | None:
    """A URL without credentials, query or fragment (``None`` when it is not a URL)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    return f"{parts.scheme}://{host}{port}{parts.path}"


def clip(value: Any, depth: int = 0, max_str: int = MAX_STR) -> Any:
    """Bound a result in depth, item count and string length, and mask secrets.

    A string under a key such as ``password``, ``token`` or ``authorization`` is replaced,
    every header or cookie bag keeps its names only, and credentials inside URLs or
    ``key=value`` text are masked.
    """
    if isinstance(value, str):
        value = scrub_text(value)
        return value if len(value) <= max_str else value[:max_str] + "…"
    if depth > MAX_DEPTH:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:200]:
            key = str(k)
            if key.lower() in _HEADER_BAGS and isinstance(v, dict):
                out[key] = {str(h): "[redacted]" for h in list(v)[:50]}
            elif isinstance(v, str) and _SENSITIVE_KEY.search(key):
                out[key] = "[redacted]"
            else:
                out[key] = clip(v, depth + 1, max_str)
        return out
    if isinstance(value, (list, tuple)):
        return [clip(v, depth + 1, max_str) for v in list(value)[:MAX_ITEMS]]
    return value


# ------------------------------------------------------------------ reuse of the HTTP API logic
def _api(store: Any) -> Any:
    from ..api import RedlineAPI
    from ..conf import Settings

    return RedlineAPI(store, Settings(read_only=True), framework="other")


def _unwrap(status: int, data: Any, ok: tuple[int, ...], fallback: str) -> Any:
    if status not in ok:
        message = data.get("error", {}).get("message") if isinstance(data, dict) else None
        raise ValueError(message or fallback)
    return data


def api_get(store: Any, path: str, params: Mapping[str, Any] | None = None) -> Any:
    """``GET`` through the same code the HTTP API uses; raises ``ValueError`` on an error."""
    status, data = _api(store).dispatch("GET", path, params or {})
    return _unwrap(status, data, (200,), f"could not read {path}")


def api_post(store: Any, path: str, body: Mapping[str, Any]) -> Any:
    status, data = _api(store).dispatch("POST", path, None, body)
    return _unwrap(status, data, (200,), f"could not call {path}")


# ------------------------------------------------------------------ local files
def resolve_local(path: str, *, what: str, directory: bool = False) -> Path:
    """Resolve ``path`` inside the working directory (never above it, symlinks followed)."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError(f"{what} path is invalid")
    root = Path(os.getcwd()).resolve()
    try:
        resolved = (root / Path(path)).resolve(strict=True)
    except OSError:
        raise ValueError(f"{what} not found") from None
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"{what} must be inside the working directory")
    if directory != resolved.is_dir() or not (resolved.is_dir() or resolved.is_file()):
        raise ValueError(f"{what} must be a {'directory' if directory else 'file'}")
    return resolved


def read_local_file(path: str, suffixes: tuple[str, ...], *, what: str) -> str:
    """Read a text file under the working directory."""
    resolved = resolve_local(path, what=f"{what} file")
    if resolved.suffix.lower() not in suffixes:
        raise ValueError(f"{what} file must be one of: {', '.join(suffixes)}")
    if resolved.stat().st_size > MAX_LOCAL_FILE:
        raise ValueError(f"{what} file is larger than {MAX_LOCAL_FILE // 1_000_000} MB")
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read the {what} file: {type(exc).__name__}") from None
