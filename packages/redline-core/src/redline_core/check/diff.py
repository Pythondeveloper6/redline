"""Response capture and comparison (``CONTRACT.md`` sections 19.2–19.4).

A captured response is a plain dict (what baselines store)::

    {"status": 200, "content_type": "application/json", "headers": {...},
     "body_kind": "json" | "text" | "hash", "body": ... | "sha256": "...", "size": 1234}

* Bodies up to 1 MiB are kept: JSON (a ``*json`` content type, or a body that starts with
  ``{``/``[`` and parses) as the parsed value, anything else that decodes as UTF-8 as
  text. Larger or binary bodies are kept as SHA-256 + size only.
* Bodies and headers go through the :class:`~redline_core.redact.Redactor` (JSON keys
  masked at any depth, text through ``Redactor.text``), on **both** sides of a comparison,
  so a masked secret never shows up as a difference.
* Headers compared: ``content-type`` always, plus :data:`COMPARED_HEADERS` minus the
  request's ``ignore_headers``. Header differences appear in the diff as
  ``header:<name>`` paths.

Volatile fields: :func:`detect_volatile` diffs two responses of the same request (the
first and last timed calls); every JSON path that differs, every differing text line
(``line:<n>``, 1-based) and every differing header is volatile and excluded later.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from itertools import zip_longest
from typing import Any

from .suite import Token, format_path, parse_path, path_matches

__all__ = [
    "COMPARED_HEADERS",
    "MAX_BODY_BYTES",
    "MAX_DIFF_ENTRIES",
    "MAX_TEXT_DIFF_LINES",
    "capture",
    "compare_captures",
    "detect_volatile",
    "json_diff",
    "redact_json",
    "text_diff",
    "truncate_value",
]

MAX_BODY_BYTES = 1 << 20
MAX_DIFF_ENTRIES = 50
MAX_TEXT_DIFF_LINES = 200
MAX_VALUE_CHARS = 200
MAX_VOLATILE = 1000
#: Response headers compared besides ``content-type`` (lower-case).
COMPARED_HEADERS = (
    "content-type",
    "location",
    "cache-control",
    "content-encoding",
    "content-language",
    "content-disposition",
    "vary",
    "allow",
    "access-control-allow-origin",
)
_VOLATILE_LINE = "\x00<volatile line>"


# ============================================================================ capture
def _media_type(value: str | None) -> str | None:
    if not value:
        return None
    return ";".join(p.strip().lower() for p in value.split(";") if p.strip()) or None


def capture(
    status: int | None,
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
    content: bytes,
    *,
    redactor: Any = None,
    ignore_headers: Iterable[str] = (),
) -> dict[str, Any]:
    """Normalise one HTTP response for storing and comparing."""
    from ..redact import Redactor

    r = redactor if redactor is not None else Redactor()
    items = headers.items() if isinstance(headers, Mapping) else headers
    lowered: dict[str, str] = {}
    for k, v in items:
        key = str(k).lower()
        lowered[key] = f"{lowered[key]}, {v}" if key in lowered else str(v)
    dropped = {h.lower() for h in ignore_headers} - {"content-type"}
    subset = {k: lowered[k] for k in COMPARED_HEADERS if k in lowered and k not in dropped}
    subset = {k.lower(): v for k, v in r.headers(subset).items()}
    ctype = _media_type(lowered.get("content-type"))
    out: dict[str, Any] = {
        "status": status,
        "content_type": ctype,
        "headers": subset,
        "size": len(content),
    }
    if len(content) > MAX_BODY_BYTES:
        return _hashed(out, content)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _hashed(out, content)
    base = (ctype or "").split(";")[0]
    if base.endswith("json") or text.lstrip()[:1] in ("{", "["):
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            pass
        else:
            out["body_kind"] = "json"
            out["body"] = redact_json(r, data)
            return out
    out["body_kind"] = "text"
    out["body"] = r.text(text)
    return out


def redact_json(redactor: Any, data: Any) -> Any:
    """Mask the values of secret keys at any depth (``Redactor.mapping`` without its
    500-character cut of long strings, so a long value still diffs exactly)."""
    from ..redact import MASK

    if isinstance(data, dict):
        return {
            k: MASK if redactor.is_secret_key(str(k)) else redact_json(redactor, v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_json(redactor, v) for v in data]
    return data


def _hashed(out: dict[str, Any], content: bytes) -> dict[str, Any]:
    out["body_kind"] = "hash"
    out["sha256"] = hashlib.sha256(content).hexdigest()
    return out


# ============================================================================ JSON diff
def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def truncate_value(value: Any, limit: int = MAX_VALUE_CHARS) -> Any:
    """``value`` itself when its JSON form fits in ``limit`` characters, else that JSON
    cut to ``limit`` characters + ``…`` (a string)."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) <= limit:
        return value
    return text[:limit] + "…"


def _ignored(path: tuple[str | int, ...], rules: list[tuple[Token, ...]]) -> bool:
    return any(path_matches(rule, path) for rule in rules)


def json_diff(
    before: Any, after: Any, *, ignore: Iterable[tuple[Token, ...]] = ()
) -> Iterator[dict[str, Any]]:
    """Every difference as ``{path, kind, before, after}``; ``kind`` is ``added``,
    ``removed``, ``changed`` or ``type``. Arrays are compared index by index. Values are
    *not* truncated here (see :func:`truncate_value`)."""
    rules = list(ignore)
    yield from _walk(before, after, (), rules)


def _entry(path: tuple, kind: str, before: Any, after: Any) -> dict[str, Any]:
    return {"path": format_path(path), "kind": kind, "before": before, "after": after}


def _walk(a: Any, b: Any, path: tuple, rules: list) -> Iterator[dict[str, Any]]:
    if rules and _ignored(path, rules):
        return
    ka, kb = _kind(a), _kind(b)
    if ka != kb:
        yield _entry(path, "type", a, b)
        return
    if ka == "object":
        for key, value in a.items():
            sub = (*path, key)
            if key not in b:
                if not (rules and _ignored(sub, rules)):
                    yield _entry(sub, "removed", value, None)
            else:
                yield from _walk(value, b[key], sub, rules)
        for key, value in b.items():
            if key not in a:
                sub = (*path, key)
                if not (rules and _ignored(sub, rules)):
                    yield _entry(sub, "added", None, value)
    elif ka == "array":
        for i, (va, vb) in enumerate(zip_longest(a, b, fillvalue=_MISSING)):
            sub = (*path, i)
            if vb is _MISSING:
                if not (rules and _ignored(sub, rules)):
                    yield _entry(sub, "removed", va, None)
            elif va is _MISSING:
                if not (rules and _ignored(sub, rules)):
                    yield _entry(sub, "added", None, vb)
            else:
                yield from _walk(va, vb, sub, rules)
    elif a != b:
        yield _entry(path, "changed", a, b)


_MISSING = object()


# ============================================================================ text diff
def _masked_lines(text: str, lines: set[int]) -> list[str]:
    out = text.splitlines()
    for n in lines:
        if 1 <= n <= len(out):
            out[n - 1] = _VOLATILE_LINE
    return out


def text_diff(
    before: str, after: str, *, volatile_lines: Iterable[int] = (), limit: int = MAX_TEXT_DIFF_LINES
) -> list[str]:
    """Unified diff (``before`` → ``after``), volatile line numbers masked on both sides,
    at most ``limit`` lines (the last says how many were cut). ``[]`` when equal."""
    vol = set(volatile_lines)
    a, b = _masked_lines(before, vol), _masked_lines(after, vol)
    if a == b:
        return []
    lines = [
        line.replace(_VOLATILE_LINE, "<volatile line>")
        for line in difflib.unified_diff(a, b, "before", "after", lineterm="", n=2)
    ]
    if len(lines) > limit:
        cut = len(lines) - (limit - 1)
        lines = [*lines[: limit - 1], f"… {cut} more diff lines"]
    return lines


# ============================================================================ volatility
def detect_volatile(first: Mapping[str, Any] | None, last: Mapping[str, Any] | None) -> dict:
    """``{"volatile": [paths], "volatile_headers": [names]}`` from two responses of the
    same request. JSON bodies give JSON paths, text bodies ``line:<n>``, a changed hash
    (or a change of body kind) ``$``."""
    out: dict[str, list[str]] = {"volatile": [], "volatile_headers": []}
    if not first or not last:
        return out
    ha, hb = first.get("headers") or {}, last.get("headers") or {}
    out["volatile_headers"] = sorted(k for k in set(ha) | set(hb) if ha.get(k) != hb.get(k))
    kind = first.get("body_kind")
    if kind != last.get("body_kind"):
        out["volatile"] = ["$"]
    elif kind == "json":
        paths: list[str] = []
        for entry in json_diff(first.get("body"), last.get("body")):
            paths.append(entry["path"])
            if len(paths) >= MAX_VOLATILE:
                paths = ["$"]
                break
        out["volatile"] = paths
    elif kind == "text":
        a = (first.get("body") or "").splitlines()
        b = (last.get("body") or "").splitlines()
        out["volatile"] = [
            f"line:{i}" for i, (x, y) in enumerate(zip_longest(a, b), start=1) if x != y
        ][:MAX_VOLATILE]
    elif kind == "hash" and first.get("sha256") != last.get("sha256"):
        out["volatile"] = ["$"]
    return out


# ============================================================================ compare
def _line_numbers(paths: Iterable[str]) -> set[int]:
    out = set()
    for p in paths:
        if p.startswith("line:") and p[5:].isdigit():
            out.add(int(p[5:]))
    return out


def compare_captures(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    ignore: Iterable[str] = (),
    volatile: Iterable[str] = (),
    volatile_headers: Iterable[str] = (),
    max_entries: int = MAX_DIFF_ENTRIES,
) -> dict[str, Any]:
    """Diff two captured responses (status is the caller's business).

    Returns ``{diff: [...≤ max_entries], diff_total, text_diff: [...]|None,
    ignored: [{path, reason}]}``; ``reason`` is ``"ignore"`` (a suite rule) or
    ``"volatile"`` (changes on every call).
    """
    ignore = list(dict.fromkeys(ignore))
    volatile = [p for p in dict.fromkeys(volatile) if p not in ignore]
    vol_headers = sorted({h.lower() for h in volatile_headers} - {"content-type"})
    ignored = [{"path": p, "reason": "ignore"} for p in ignore]
    ignored += [{"path": p, "reason": "volatile"} for p in volatile]
    ignored += [{"path": f"header:{h}", "reason": "volatile"} for h in vol_headers]

    entries: list[dict[str, Any]] = []
    if before.get("content_type") != after.get("content_type"):
        entries.append(
            _entry_str(
                "header:content-type",
                "changed",
                before.get("content_type"),
                after.get("content_type"),
            )
        )
    hb, ha = before.get("headers") or {}, after.get("headers") or {}
    for name in sorted(set(hb) | set(ha)):
        if name == "content-type" or name in vol_headers or hb.get(name) == ha.get(name):
            continue
        kind = "added" if name not in hb else "removed" if name not in ha else "changed"
        entries.append(_entry_str(f"header:{name}", kind, hb.get(name), ha.get(name)))

    text: list[str] | None = None
    kb, ka = before.get("body_kind"), after.get("body_kind")
    rules: list[tuple[Token, ...]] = []
    for p in [*ignore, *volatile]:
        try:
            rules.append(parse_path(p))
        except ValueError:  # ``line:<n>`` and other non-JSON entries
            continue
    whole_body_ignored = any(rule == () for rule in rules)
    if whole_body_ignored:
        pass
    elif kb != ka:
        entries.append(_entry_str("$", "type", kb, ka))
    elif kb == "json":
        entries.extend(json_diff(before.get("body"), after.get("body"), ignore=rules))
    elif kb == "text":
        text = text_diff(
            before.get("body") or "",
            after.get("body") or "",
            volatile_lines=_line_numbers(volatile),
        )
        if text:
            entries.append(_entry_str("$", "changed", "(text)", "(text)"))
        else:
            text = None
    elif kb == "hash" and (
        before.get("sha256") != after.get("sha256") or before.get("size") != after.get("size")
    ):
        entries.append(
            _entry_str(
                "$",
                "changed",
                f"sha256:{before.get('sha256')} ({before.get('size')} bytes)",
                f"sha256:{after.get('sha256')} ({after.get('size')} bytes)",
            )
        )
    shown = [
        {**e, "before": truncate_value(e["before"]), "after": truncate_value(e["after"])}
        for e in entries[:max_entries]
    ]
    return {"diff": shown, "diff_total": len(entries), "text_diff": text, "ignored": ignored}


def _entry_str(path: str, kind: str, before: Any, after: Any) -> dict[str, Any]:
    return {"path": path, "kind": kind, "before": before, "after": after}
