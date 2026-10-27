"""Scenario generators for ``redline load init``.

``from_openapi(spec)``
    Every ``GET`` operation becomes a request. Path parameters are filled with the schema's
    ``example`` / ``examples`` / ``default`` / first ``enum`` value, else ``1`` (or a fixed
    UUID for ``format: uuid``, ``"example"`` for strings). Operations with a *required*
    query or header parameter are filled the same way but get ``weight = 0`` – they stay in
    the file for you to review and enable. Unsafe methods (POST/PUT/PATCH/DELETE) are skipped
    unless ``include_writes``; then they are added with ``weight = 0`` and the request body's
    example (if any).
``from_traces(store)``
    The top endpoints by request count in a window, each with a concrete path from a recent
    trace, weighted by count. Only method and path are copied: never bodies or headers, and a
    path containing a redacted value is skipped. Non-GET endpoints only with
    ``include_writes`` (``weight = 0``, no body).
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["from_openapi", "from_traces", "load_openapi", "to_json", "to_toml"]

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
SAFE_METHODS = ("get", "head")
WRITE_METHODS = ("post", "put", "patch", "delete")
_PATH_PARAM = re.compile(r"\{([^{}/]+)\}")
_FIXED_UUID = "00000000-0000-4000-8000-000000000001"
DEFAULT_PROFILE: dict[str, Any] = {
    "mode": "steps",
    "start_rps": 5,
    "end_rps": 50,
    "step_rps": 5,
    "step_duration_s": 20,
    "stop_on": {"error_rate": 0.05},
}


# ---------------------------------------------------------------------------- OpenAPI
def load_openapi(source: str) -> dict[str, Any]:
    """Read an OpenAPI document (JSON; YAML when PyYAML is installed) from a URL or file."""
    if re.match(r"^https?://", source):
        req = urllib.request.Request(source, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            raise ValueError(
                f"{source}: not JSON (install PyYAML to read YAML OpenAPI documents)"
            ) from None
        doc = yaml.safe_load(text)
    if not isinstance(doc, Mapping) or "paths" not in doc:
        raise ValueError(f"{source}: not an OpenAPI document (no 'paths')")
    return dict(doc)


def _resolve(doc: Mapping[str, Any], node: Any, depth: int = 0) -> Any:
    if isinstance(node, Mapping) and "$ref" in node and depth < 20:
        ref = node["$ref"]
        if isinstance(ref, str) and ref.startswith("#/"):
            target: Any = doc
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, Mapping) or part not in target:
                    return {}
                target = target[part]
            return _resolve(doc, target, depth + 1)
        return {}
    return node


def _example(doc: Mapping[str, Any], param: Mapping[str, Any]) -> Any:
    if "example" in param:
        return param["example"]
    examples = param.get("examples")
    if isinstance(examples, Mapping):
        for ex in examples.values():
            ex = _resolve(doc, ex)
            if isinstance(ex, Mapping) and "value" in ex:
                return ex["value"]
    schema = _resolve(doc, param.get("schema") or {})
    return _schema_example(doc, schema)


def _schema_example(doc: Mapping[str, Any], schema: Any, depth: int = 0) -> Any:
    schema = _resolve(doc, schema)
    if not isinstance(schema, Mapping) or depth > 6:
        return 1
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for combo in ("anyOf", "oneOf", "allOf"):
        options = schema.get(combo)
        if isinstance(options, list) and options:
            for option in options:
                option = _resolve(doc, option)
                if isinstance(option, Mapping) and option.get("type") != "null":
                    return _schema_example(doc, option, depth + 1)
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "string")
    if typ == "string":
        fmt = schema.get("format")
        if fmt == "uuid":
            return _FIXED_UUID
        if fmt == "date":
            return "2024-01-01"
        if fmt == "date-time":
            return "2024-01-01T00:00:00Z"
        return "example"
    if typ == "boolean":
        return True
    if typ == "number":
        return 1.0
    if typ == "array":
        return [_schema_example(doc, schema.get("items") or {}, depth + 1)]
    if typ == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = schema.get("required") or list(props)
        return {
            name: _schema_example(doc, props[name], depth + 1) for name in required if name in props
        }
    return 1


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_fmt_value(v) for v in value)
    return str(value)


def from_openapi(
    doc: Mapping[str, Any],
    *,
    include_writes: bool = False,
    base_url: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Build a scenario dict from an OpenAPI 3 (or Swagger 2) document."""
    from urllib.parse import quote, urlencode

    requests: list[dict[str, Any]] = []
    paths = doc.get("paths") or {}
    for raw_path, item in paths.items():
        item = _resolve(doc, item)
        if not isinstance(item, Mapping) or not isinstance(raw_path, str):
            continue
        shared = [_resolve(doc, p) for p in item.get("parameters") or []]
        for method in (*SAFE_METHODS, *WRITE_METHODS):
            op = item.get(method)
            if not isinstance(op, Mapping):
                continue
            if method == "head" and "get" in item:
                continue
            write = method in WRITE_METHODS
            if write and not include_writes:
                continue
            params: dict[tuple[str, str], Mapping[str, Any]] = {}
            for p in [*shared, *(_resolve(doc, p) for p in op.get("parameters") or [])]:
                if isinstance(p, Mapping) and "name" in p and "in" in p:
                    params[(p["in"], p["name"])] = p
            path = raw_path
            for m in _PATH_PARAM.finditer(raw_path):
                pname = m.group(1)
                p = params.get(("path", pname), {"schema": {"type": "integer"}})
                value = quote(_fmt_value(_example(doc, p)), safe="")
                path = path.replace(m.group(0), value)
            query = {}
            needs_review = False
            headers: dict[str, str] = {}
            for (where, pname), p in params.items():
                if not p.get("required"):
                    continue
                if where == "query":
                    query[pname] = _fmt_value(_example(doc, p))
                    needs_review = True
                elif where == "header":
                    headers[pname] = _fmt_value(_example(doc, p))
                    needs_review = True
                elif where == "cookie":
                    needs_review = True
            if query:
                path += "?" + urlencode(query)
            req: dict[str, Any] = {
                "name": str(op.get("operationId") or f"{method.upper()} {raw_path}"),
                "method": method.upper(),
                "path": path if path.startswith("/") else "/" + path,
                "weight": 0 if (write or needs_review) else 1,
            }
            if headers:
                req["headers"] = headers
            if write:
                body = _request_body_example(doc, op)
                if body is not None:
                    req["json"] = body
            requests.append(req)
    if not requests:
        raise ValueError("the OpenAPI document has no usable operations")
    if not any(r["weight"] > 0 for r in requests):
        requests[0]["weight"] = 1  # keep the scenario valid; review before running
    info = doc.get("info") or {}
    return {
        "name": name or str(info.get("title") or "OpenAPI scenario"),
        "base_url": base_url or _openapi_base(doc),
        "timeout_s": 10,
        "requests": requests,
        "profile": dict(DEFAULT_PROFILE),
    }


def _request_body_example(doc: Mapping[str, Any], op: Mapping[str, Any]) -> Any:
    body = _resolve(doc, op.get("requestBody") or {})
    content = body.get("content") if isinstance(body, Mapping) else None
    if not isinstance(content, Mapping):
        return None
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        return None
    if "example" in media:
        return media["example"]
    return _schema_example(doc, media.get("schema") or {})


def _openapi_base(doc: Mapping[str, Any]) -> str:
    servers = doc.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], Mapping):
        url = str(servers[0].get("url") or "")
        if re.match(r"^https?://", url):
            return url.rstrip("/")
        if url.startswith("/") and url != "/":
            return DEFAULT_BASE_URL + url.rstrip("/")
    host = doc.get("host")  # Swagger 2
    if isinstance(host, str) and host:
        scheme = (doc.get("schemes") or ["http"])[0]
        return f"{scheme}://{host}{(doc.get('basePath') or '').rstrip('/')}"
    return DEFAULT_BASE_URL


# ---------------------------------------------------------------------------- traces
def from_traces(
    store: Any,
    *,
    window_s: float = 86_400.0,
    top: int = 10,
    include_writes: bool = False,
    base_url: str | None = None,
    mount_prefix: str = "/_redline",
) -> dict[str, Any]:
    """Build a scenario from captured traffic (``store.endpoints`` + ``store.list``)."""
    from ..storage import Filters

    requests: list[dict[str, Any]] = []
    stats = sorted(store.endpoints(window_s), key=lambda e: e.count, reverse=True)
    for ep in stats:
        if len(requests) >= top:
            break
        method = ep.method.upper()
        write = method not in ("GET", "HEAD")
        if write and not include_writes:
            continue
        route = ep.route or ""
        if route.startswith(mount_prefix):
            continue
        page = store.list(Filters(route=route, method=method), limit=5)
        if not page.items:  # endpoints() falls back to the path when there is no route
            page = store.list(Filters(q=route, method=method), limit=5)
        sample = next(
            (
                item["path"]
                for item in page.items
                if isinstance(item.get("path"), str)
                and item["path"].startswith("/")
                and "redacted" not in item["path"].lower()
                and "{" not in item["path"]
            ),
            None,
        )
        if sample is None:
            continue
        requests.append(
            {
                "name": f"{method} {route}",
                "method": method,
                "path": sample,
                "weight": 0 if write else int(ep.count),
            }
        )
    if not requests:
        raise ValueError("no captured endpoints in the window; browse the app first")
    if not any(r["weight"] > 0 for r in requests):
        requests[0]["weight"] = 1
    return {
        "name": "captured traffic",
        "base_url": base_url or DEFAULT_BASE_URL,
        "timeout_s": 10,
        "requests": requests,
        "profile": dict(DEFAULT_PROFILE),
    }


# ---------------------------------------------------------------------------- writers
def to_json(scenario: Mapping[str, Any]) -> str:
    return json.dumps(scenario, indent=2) + "\n"


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


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
    if value is None:
        raise ValueError("TOML cannot express null")
    return json.dumps(str(value))


def to_toml(scenario: Mapping[str, Any]) -> str:
    """A small TOML writer for scenario dicts (scalars, inline tables, ``[[requests]]``)."""
    lines: list[str] = ["# Redline load scenario – see `redline load --help`.", ""]
    tables = {}
    for key, value in scenario.items():
        if value is None:
            continue
        if key in ("requests", "profile"):
            tables[key] = value
            continue
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for req in tables.get("requests", []):
        lines += ["", "[[requests]]"]
        for key, value in req.items():
            if value is not None:
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    if "profile" in tables:
        lines += ["", "[profile]"]
        for key, value in tables["profile"].items():
            if value is not None:
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"
