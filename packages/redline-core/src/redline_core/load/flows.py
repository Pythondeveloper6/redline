"""Chained requests (flows), value extraction and response assertions.

A *flow* is an ordered list of requests one arrival runs as a journey (list, open one item
from that list, save it). A step can ``extract`` values from its response (a JSON path, a
header or a regular expression) into named variables that later steps use as
``{var:name}`` in the path, headers, JSON and raw body. A request (in a flow or not) can
also ``expect`` things of its response, so a ``200`` with an error body, an empty list or a
truncated payload counts as a failure (error class ``assertion``) instead of a success.

Extraction and assertions only read the response. Nothing here is ever sent anywhere.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .profile import ScenarioError, _unknown

__all__ = [
    "ExtractError",
    "check_expect",
    "jsonpath",
    "parse_expect",
    "parse_extract",
    "render_vars",
    "render_vars_value",
    "run_extract",
    "var_refs",
]

_VAR = re.compile(r"\{var:([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_TOKEN = re.compile(r"\.([A-Za-z0-9_-]+)|\[(-?\d+)\]")
_MISSING = object()
MAX_VALUE = 4_096


class ExtractError(ValueError):
    """A step's response did not hold a value a later step needs."""


# ---------------------------------------------------------------------------- json path
def jsonpath(obj: Any, path: str) -> Any:
    """``$.a.b[0].c`` against parsed JSON; ``_MISSING`` when something on the way is absent."""
    if not path.startswith("$"):
        raise ValueError(f"a JSON path starts with '$' (got {path!r})")
    rest = path[1:]
    pos = 0
    cur = obj
    while pos < len(rest):
        m = _TOKEN.match(rest, pos)
        if m is None:
            raise ValueError(f"cannot read JSON path {path!r} at {rest[pos:]!r}")
        pos = m.end()
        if m.group(1) is not None:
            if not isinstance(cur, Mapping) or m.group(1) not in cur:
                return _MISSING
            cur = cur[m.group(1)]
        else:
            i = int(m.group(2))
            if not isinstance(cur, list) or not -len(cur) <= i < len(cur):
                return _MISSING
            cur = cur[i]
    return cur


def _check_path(path: Any, where: str) -> str:
    if not isinstance(path, str):
        raise ScenarioError("must be a JSON path such as '$.items[0].id'", where)
    try:
        jsonpath({}, path)
    except ValueError as exc:
        raise ScenarioError(str(exc), where) from None
    return path


# ---------------------------------------------------------------------------- variables
def var_refs(text: str) -> list[str]:
    return _VAR.findall(text)


def render_vars(text: str, values: Mapping[str, str] | None) -> str:
    if "{var:" not in text:
        return text

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if values is None or name not in values:
            raise ExtractError(f"variable {name!r} was not extracted")
        return values[name]

    return _VAR.sub(sub, text)


def render_vars_value(value: Any, values: Mapping[str, str] | None) -> Any:
    if isinstance(value, str):
        return render_vars(value, values)
    if isinstance(value, list):
        return [render_vars_value(v, values) for v in value]
    if isinstance(value, Mapping):
        return {k: render_vars_value(v, values) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------- extract
def parse_extract(raw: Any, where: str) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or not raw:
        raise ScenarioError("must be a table of variable names to sources", where)
    out: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        w = f"{where}.{name}"
        if not isinstance(name, str) or not _NAME.match(name):
            raise ScenarioError("variable names are letters, digits and '_'", w)
        if isinstance(spec, str):  # shorthand: a JSON path
            spec = {"json": spec}
        if not isinstance(spec, Mapping):
            raise ScenarioError("must be a JSON path or {json|header|regex = ...}", w)
        _unknown(spec, {"json", "header", "regex", "group"}, w)
        kinds = [k for k in ("json", "header", "regex") if k in spec]
        if len(kinds) != 1:
            raise ScenarioError("set exactly one of json, header or regex", w)
        kind = kinds[0]
        if kind == "json":
            out[name] = {"json": _check_path(spec["json"], f"{w}.json")}
        elif kind == "header":
            h = spec["header"]
            if not isinstance(h, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", h):
                raise ScenarioError("must be a header name", f"{w}.header")
            out[name] = {"header": h}
        else:
            rx = spec["regex"]
            group = spec.get("group", 1)
            if not isinstance(rx, str) or len(rx) > 500:
                raise ScenarioError("must be a regular expression", f"{w}.regex")
            try:
                compiled = re.compile(rx)
            except re.error as exc:
                raise ScenarioError(f"invalid regular expression: {exc}", f"{w}.regex") from None
            if (
                isinstance(group, bool)
                or not isinstance(group, int)
                or not (0 <= group <= compiled.groups)
            ):
                raise ScenarioError(f"group must be 0..{compiled.groups}", f"{w}.group")
            out[name] = {"regex": rx, "group": group}
    return out


def run_extract(
    specs: Mapping[str, Mapping[str, Any]],
    headers: Mapping[str, str],
    body: bytes,
) -> dict[str, str]:
    """Read every variable out of one response; :class:`ExtractError` names the first miss."""
    parsed: Any = _MISSING
    text: str | None = None
    out: dict[str, str] = {}
    for name, spec in specs.items():
        value: Any = None
        if "json" in spec:
            if parsed is _MISSING:
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = None
            found = jsonpath(parsed, spec["json"]) if parsed is not None else _MISSING
            value = None if found is _MISSING else found
        elif "header" in spec:
            value = headers.get(spec["header"])
        else:
            if text is None:
                text = body.decode("utf-8", "replace")
            m = re.search(spec["regex"], text)
            value = m.group(spec["group"]) if m else None
        if value is None or isinstance(value, (dict, list)) or value == "":
            raise ExtractError(f"could not extract {name!r}")
        text_value = str(value) if not isinstance(value, bool) else str(value).lower()
        if len(text_value) > MAX_VALUE or "\n" in text_value or "\r" in text_value:
            raise ExtractError(f"extracted {name!r} is not a usable value")
        out[name] = text_value
    return out


# ---------------------------------------------------------------------------- expect
_RULE_KINDS = ("json", "contains", "not_contains", "min_bytes", "header")


def parse_expect(raw: Any, where: str) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, str) or not raw:
        raise ScenarioError("must be a rule or a list of rules", where)
    out = []
    for i, rule in enumerate(raw):
        w = f"{where}[{i}]"
        if not isinstance(rule, Mapping):
            raise ScenarioError("must be a table", w)
        _unknown(rule, {*_RULE_KINDS, "exists", "equals"}, w)
        kinds = ["header"] if "header" in rule else [k for k in _RULE_KINDS if k in rule]
        if len(kinds) != 1:
            raise ScenarioError(f"set exactly one of {', '.join(_RULE_KINDS)}", w)
        kind = kinds[0]
        clean: dict[str, Any] = {kind: rule[kind]}
        if kind == "json":
            _check_path(rule["json"], f"{w}.json")
            if "equals" in rule:
                clean["equals"] = rule["equals"]
            clean["exists"] = bool(rule.get("exists", True))
        elif kind == "header":
            if not isinstance(rule["header"], str) or not re.fullmatch(
                r"[A-Za-z0-9-]{1,64}", rule["header"]
            ):
                raise ScenarioError("must be a header name", f"{w}.header")
            if "equals" in rule:
                clean["equals"] = str(rule["equals"])
            if "contains" in rule:
                clean["contains"] = str(rule["contains"])
        elif kind in ("contains", "not_contains"):
            if not isinstance(rule[kind], str) or not rule[kind]:
                raise ScenarioError("must be non-empty text", f"{w}.{kind}")
        else:
            n = rule["min_bytes"]
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise ScenarioError("must be an integer ≥ 0", f"{w}.min_bytes")
        out.append(clean)
    return tuple(out)


def check_expect(
    rules: Sequence[Mapping[str, Any]], headers: Mapping[str, str], body: bytes
) -> str | None:
    """``None`` when every rule holds, else a short reason (never a body or header value)."""
    parsed: Any = _MISSING
    for rule in rules:
        if "json" in rule:
            if parsed is _MISSING:
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = None
            found = jsonpath(parsed, rule["json"]) if parsed is not None else _MISSING
            if "equals" in rule:
                if found is _MISSING or found != rule["equals"]:
                    return f"json {rule['json']} does not equal the expected value"
            elif rule.get("exists", True):
                if found is _MISSING or found is None:
                    return f"json {rule['json']} is missing"
            elif found is not _MISSING and found is not None:
                return f"json {rule['json']} should be absent"
        elif "contains" in rule and "header" not in rule:
            if rule["contains"].encode() not in body:
                return "body does not contain the expected text"
        elif "not_contains" in rule:
            if rule["not_contains"].encode() in body:
                return "body contains forbidden text"
        elif "min_bytes" in rule:
            if len(body) < rule["min_bytes"]:
                return f"body is smaller than {rule['min_bytes']} bytes"
        elif "header" in rule:
            value = headers.get(rule["header"])
            if value is None:
                return f"header {rule['header']} is missing"
            if "equals" in rule and value != rule["equals"]:
                return f"header {rule['header']} has an unexpected value"
            if "contains" in rule and rule["contains"] not in value:
                return f"header {rule['header']} lacks the expected text"
    return None
