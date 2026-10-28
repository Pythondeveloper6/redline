"""Persistent, exact-match issue acknowledgements. Raw findings remain available."""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Mapping


def validate(data) -> dict:
    if not isinstance(data, Mapping) or set(data) - {
        "kind",
        "route",
        "fingerprint",
        "location",
        "reason",
        "expires_at",
    }:
        raise ValueError("Unknown suppression fields")
    for key in ("kind", "reason"):
        if not isinstance(data.get(key), str) or not data[key].strip() or len(data[key]) > 500:
            raise ValueError(f"{key} is required and must be at most 500 characters")
    if not any(data.get(k) for k in ("route", "fingerprint", "location")):
        raise ValueError("Choose at least one exact route, fingerprint or callsite location")
    for key in ("route", "fingerprint", "location"):
        if key in data and (
            not isinstance(data[key], str) or not data[key] or len(data[key]) > 2000
        ):
            raise ValueError(f"Invalid {key}")
    expiry = data.get("expires_at")
    if expiry is not None and (
        isinstance(expiry, bool)
        or not isinstance(expiry, (int, float))
        or not math.isfinite(expiry)
    ):
        raise ValueError("expires_at must be a finite epoch timestamp")
    return {**data, "id": uuid.uuid4().hex, "created_at": time.time()}


def matches(
    rule: dict,
    *,
    kind: str,
    route: str,
    fingerprint: str | None,
    location: str | None,
    now: float | None = None,
) -> bool:
    now = time.time() if now is None else now
    return (
        rule["kind"] == kind
        and (rule.get("expires_at") is None or rule["expires_at"] > now)
        and all(
            rule.get(key) is None or rule[key] == value
            for key, value in (
                ("route", route),
                ("fingerprint", fingerprint),
                ("location", location),
            )
        )
    )
