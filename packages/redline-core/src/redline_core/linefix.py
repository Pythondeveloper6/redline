"""Tiny helper for suggesters: a unified diff that rewrites one source line.

``line_diff`` finds the file of a :class:`~redline_core.model.Callsite` (project-relative
path), checks that the line still reads like the captured ``code`` and replaces
``old`` with ``new`` in it. It never writes anything.
"""

from __future__ import annotations

import difflib
import os

from .model import Callsite

__all__ = ["line_diff"]


def line_diff(project_root: str, callsite: Callsite | None, old: str, new: str) -> str | None:
    """Unified diff replacing the first ``old`` by ``new`` on the callsite's line, or ``None``."""
    if callsite is None or not old or old == new:
        return None
    for base in (project_root, os.getcwd(), ""):
        path = os.path.join(base, callsite.file) if base else callsite.file
        if os.path.isfile(path):
            break
    else:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None
    i = callsite.line - 1
    if not 0 <= i < len(lines) or old not in lines[i]:
        return None
    changed = list(lines)
    changed[i] = lines[i].replace(old, new, 1)
    diff = difflib.unified_diff(
        lines, changed, fromfile=f"a/{callsite.file}", tofile=f"b/{callsite.file}", n=3
    )
    text = "".join(d if d.endswith("\n") else d + "\n" for d in diff)
    return text or None
