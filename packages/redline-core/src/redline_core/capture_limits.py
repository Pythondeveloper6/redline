"""Bounded JSON-safe capture without inspecting arbitrary application objects.

The byte budget uses compact, ASCII-escaped JSON (``ensure_ascii=True`` and
``separators=(",", ":")``). It also bounds compact UTF-8 JSON, whose encoding is no
larger. Only exact built-in containers/primitives are traversed; custom mappings,
sequences and scalar subclasses are replaced with ``None`` without calling their
iteration, conversion or representation methods. A true truncation flag means data was
omitted, replaced or shortened; it must remain visible to callers.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

_NO_FAST: Any = object()
_JSON_DUMPS = json.dumps
_SURROGATE = re.compile("[\ud800-\udfff]")


def _fast_bounded(
    value: Any, *, max_bytes: int, max_depth: int, max_items: int, max_string_bytes: int
) -> Any:
    """Return ``value`` unchanged when it is small, exact-builtin and JSON-safe.

    No truncation is needed in that case, so the slower traversal would return the same
    object with ``truncated=False``. Anything outside the ordinary shape falls back.
    """
    if json.dumps is not _JSON_DUMPS:  # someone inspects/replaces encoding itself
        return _NO_FAST
    remaining = [max_items]

    def visit(item: Any, depth: int) -> bool:
        kind = type(item)
        if kind is str:
            return len(item) <= max_string_bytes and _SURROGATE.search(item) is None
        if kind in (type(None), bool):
            return True
        if kind is int:
            return item.bit_length() <= 64
        if kind is float:
            return math.isfinite(item)
        if kind not in (dict, list) or depth >= max_depth:
            return False
        if remaining[0] < 1:
            return False
        entries = item.items() if kind is dict else enumerate(item)
        for key, child in entries:
            if kind is dict and (
                type(key) is not str
                or len(key) > max_string_bytes
                or _SURROGATE.search(key) is not None
            ):
                return False
            remaining[0] -= 1
            if remaining[0] < 0 or not visit(child, depth + 1):
                return False
        return True

    if not visit(value, 0):
        return _NO_FAST
    try:
        encoded = _JSON_DUMPS(
            value, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return _NO_FAST
    return value if len(encoded) <= max_bytes else _NO_FAST


def bounded_value(
    value: Any,
    *,
    max_bytes: int,
    max_depth: int = 8,
    max_items: int = 100,
    max_string_bytes: int = 2000,
) -> tuple[Any, bool]:
    """Return a JSON-safe value within byte, depth and total container-item budgets.

    ``max_bytes`` must be at least four, the space needed for the universal ``null``
    fallback. ``max_items`` is shared by the complete traversal, not applied anew to
    every nested container. Strings are sliced to a bounded prefix before encoding, so
    a very large input is never copied or encoded in full merely to measure its size.
    Depth is limited to 64 to stay below interpreter recursion limits.
    Dictionary keys must be exact strings; colliding shortened keys are omitted.
    """
    for name, limit in (
        ("max_bytes", max_bytes),
        ("max_depth", max_depth),
        ("max_items", max_items),
        ("max_string_bytes", max_string_bytes),
    ):
        if type(limit) is not int or limit < (4 if name == "max_bytes" else 0):
            raise ValueError(f"{name} must be an integer >= {4 if name == 'max_bytes' else 0}")

    if max_depth > 64:
        raise ValueError("max_depth must be <= 64")

    fast = _fast_bounded(
        value,
        max_bytes=max_bytes,
        max_depth=max_depth,
        max_items=max_items,
        max_string_bytes=max_string_bytes,
    )
    if fast is not _NO_FAST:
        return fast, False

    remaining_items = max_items
    active: set[int] = set()

    def fallback(budget):
        return (None, 4, True, True) if budget >= 4 else (None, 0, True, False)

    def string(text, budget):
        if budget < 2:
            return None, 0, True, False
        # A code point occupies at least one UTF-8 byte. The preliminary slice bounds
        # encoding work even when text is several gigabytes long.
        prefix = text[: min(max_string_bytes, max_bytes)]
        encoded = prefix.encode("utf-8", errors="replace")
        shortened = encoded[:max_string_bytes].decode("utf-8", errors="ignore")
        truncated = len(prefix) != len(text) or shortened != prefix
        token = json.dumps(shortened, ensure_ascii=True)
        if len(token) <= budget:
            return shortened, len(token), truncated, True
        # The prefix is already bounded; binary search avoids repeatedly scanning a
        # giant original string while respecting JSON escape expansion.
        low, high = 0, len(shortened)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(shortened[:middle], ensure_ascii=True)) <= budget:
                low = middle
            else:
                high = middle - 1
        result = shortened[:low]
        return result, len(json.dumps(result, ensure_ascii=True)), True, True

    def visit(item, budget, depth):
        nonlocal remaining_items
        kind = type(item)
        if kind is str:
            return string(item, budget)
        if kind in (type(None), bool, int, float):
            if kind is float and not math.isfinite(item):
                return fallback(budget)
            if kind is int and item.bit_length() > max(64, budget * 4):
                return fallback(budget)
            try:
                token = json.dumps(item, ensure_ascii=True, allow_nan=False)
            except (ValueError, OverflowError):
                return fallback(budget)
            if len(token) <= budget:
                return item, len(token), False, True
            return fallback(budget)
        if kind not in (dict, list, tuple):
            return fallback(budget)
        if id(item) in active or depth >= max_depth:
            return fallback(budget)
        if budget < 2:
            return None, 0, True, False
        active.add(id(item))
        used = 2  # reserve both delimiters
        truncated = False
        result = {} if kind is dict else []
        try:
            entries = item.items() if kind is dict else enumerate(item)
            for key, child in entries:
                if remaining_items == 0:
                    truncated = True
                    break
                remaining_items -= 1
                separator = 1 if result else 0
                if kind is dict:
                    if type(key) is not str:
                        truncated = True
                        continue
                    safe_key, key_bytes, key_cut, accepted = string(
                        key, budget - used - separator - 1
                    )
                    truncated |= key_cut
                    if not accepted:
                        truncated = True
                        break
                    if safe_key in result:
                        truncated = True
                        continue
                    overhead = separator + key_bytes + 1
                else:
                    safe_key = None
                    overhead = separator
                safe, child_bytes, child_cut, accepted = visit(
                    child, budget - used - overhead, depth + 1
                )
                truncated |= child_cut
                if not accepted:
                    truncated = True
                    break
                if kind is dict:
                    result[safe_key] = safe
                else:
                    result.append(safe)
                used += overhead + child_bytes
            return result, used, truncated, True
        except RuntimeError:
            # Another thread may mutate a built-in dictionary during observation.
            # Keep the bounded prefix rather than letting capture break the caller.
            return result, used, True, True
        finally:
            active.remove(id(item))

    safe, _, truncated, _ = visit(value, max_bytes, 0)
    return safe, truncated
