"""Explicit response equivalence rules and absolute endpoint budgets."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .suite import format_path, parse_path, path_matches

NUMERIC_BUDGETS = ("max_queries", "max_median_ms", "max_p95_ms", "max_external_calls")


def number(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, float) and math.isfinite(value)
    )


def validate_rules(raw: Mapping[str, Any]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in ("array_keys", "tolerances", "budgets"):
        value = raw.get(name, {})
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} must be a table")
        out[name] = dict(value)
    for name in ("array_keys", "tolerances"):
        for path, value in out[name].items():
            parse_path(path)
            if name == "array_keys":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("array_keys values must be non-empty identity field names")
            else:
                if not isinstance(value, Mapping) or not value:
                    raise ValueError("tolerances need absolute and/or relative values")
                if set(value) - {"absolute", "relative"}:
                    raise ValueError("unknown tolerance key; use absolute or relative")
                if any(not number(v) or v < 0 for v in value.values()):
                    raise ValueError("tolerances must be finite non-negative numbers")
                out[name][path] = dict(value)
    budgets = out["budgets"]
    if set(budgets) - {*NUMERIC_BUDGETS, "forbidden_issues"}:
        raise ValueError("unknown budget key")
    for key in NUMERIC_BUDGETS:
        if key in budgets and (not number(budgets[key]) or budgets[key] < 0):
            raise ValueError(f"budgets.{key} must be a finite non-negative number")
        if key in {"max_queries", "max_external_calls"} and key in budgets:
            if not isinstance(budgets[key], int):
                raise ValueError(f"budgets.{key} must be a whole number")
    kinds = budgets.get("forbidden_issues", [])
    if not isinstance(kinds, list) or any(not isinstance(k, str) or not k.strip() for k in kinds):
        raise ValueError("budgets.forbidden_issues must be a list of non-empty strings")
    return out


def rule_at(rules: Mapping[str, Any], path: tuple) -> Any:
    for text, value in rules.items():
        pattern = parse_path(text)
        if len(pattern) == len(path) and path_matches(pattern, path):
            return value
    return None


def align_arrays(a: list, b: list, field: str, missing: Any) -> tuple[list, list]:
    def index(items: list) -> dict:
        result = {}
        for item in items:
            key = item.get(field) if isinstance(item, dict) else None
            if not isinstance(key, str) and not number(key):
                raise ValueError(f"identity field {field!r} must be a string or finite number")
            # Numeric identities compare numerically; booleans never reach here.
            key = ("string" if isinstance(key, str) else "number", key)
            if key in result:
                raise ValueError(f"duplicate identity in field {field!r}")
            result[key] = item
        return result

    left, right = index(a), index(b)
    keys = [*left, *(k for k in right if k not in left)]
    return [left.get(k, missing) for k in keys], [right.get(k, missing) for k in keys]


def numeric_equal(a: Any, b: Any, tolerance: Mapping[str, float]) -> bool:
    if not number(a) or not number(b):
        return False
    try:
        allowed = max(tolerance.get("absolute", 0), tolerance.get("relative", 0) * abs(a))
        return abs(a - b) <= allowed
    except OverflowError:
        return False


def remap_volatile(
    paths: list[str], source: Any, target: Any, array_keys: Mapping[str, str]
) -> list[str]:
    """Map current-run volatile indices to baseline identities before comparing arrays."""
    result = []
    for text in paths:
        try:
            tokens = parse_path(text)
        except ValueError:
            result.append(text)
            continue
        left, right, mapped = source, target, []
        valid = True
        for token in tokens:
            destination = token
            if isinstance(token, int) and isinstance(left, list):
                if token >= len(left):
                    valid = False
                    break
                field = rule_at(array_keys, tuple(mapped))
                if field is not None:
                    value = left[token].get(field) if isinstance(left[token], dict) else None
                    if not isinstance(value, str) and not number(value):
                        valid = False
                        break
                    matches = (
                        [
                            i
                            for i, item in enumerate(right or [])
                            if isinstance(item, dict)
                            and (
                                (isinstance(value, str) and isinstance(item.get(field), str))
                                or (number(value) and number(item.get(field)))
                            )
                            and item.get(field) == value
                        ]
                        if isinstance(right, list)
                        else []
                    )
                    if len(matches) != 1:
                        valid = False
                        break
                    destination = matches[0]
                left = left[token]
                right = (
                    right[destination]
                    if isinstance(right, list) and destination < len(right)
                    else None
                )
            elif isinstance(token, str):
                left = left.get(token) if isinstance(left, dict) else None
                right = right.get(token) if isinstance(right, dict) else None
            mapped.append(destination)
        if valid:
            result.append(format_path(tuple(mapped)))
    return result


def evaluate_budgets(budgets: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict]:
    """Missing observations fail closed; a baseline is not needed for absolute budgets."""
    perf = after.get("perf") or {}
    server = after.get("server") or {}
    observed = {
        "max_queries": server.get("queries_max"),
        "max_external_calls": server.get("external_calls_max"),
        "max_median_ms": perf.get("median") if perf.get("samples_ms") else None,
        "max_p95_ms": perf.get("p95") if perf.get("samples_ms") else None,
    }
    metrics_complete = server.get("calls") in (None, after.get("calls"))
    results = []
    for key, limit in budgets.items():
        if key == "forbidden_issues":
            if not limit:
                continue
            value = sorted({i["kind"] for i in server.get("issues", [])} & set(limit))
            known = (
                "issues" in server
                and server.get("complete_spans", True)
                and metrics_complete
                and not after.get("broken")
            )
            passed = known and not value
        else:
            value = observed[key]
            known = number(value) and not after.get("broken")
            if key in ("max_queries", "max_external_calls"):
                known = known and metrics_complete
            passed = known and value <= limit
        results.append(
            {
                "rule": key,
                "limit": limit,
                "observed": value,
                "passed": bool(passed),
                "reason": None if known else "required metrics unavailable",
            }
        )
    return results
