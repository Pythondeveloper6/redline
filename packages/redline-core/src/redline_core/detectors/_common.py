"""Helpers shared by the detector modules."""

from __future__ import annotations

from ..model import Callsite

__all__ = ["issue_id", "plural", "where"]


def issue_id(kind: str, fingerprint: str | None, location: str | None) -> str:
    # imported lazily: ``redline_core.detect`` imports this package at module level
    from ..detect import issue_id as _issue_id

    return _issue_id(kind, fingerprint, location)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def where(callsite: Callsite | None) -> str:
    return f" from {callsite.location}" if callsite else ""
