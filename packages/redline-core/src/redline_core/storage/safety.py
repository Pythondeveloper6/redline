"""Storage placement checks; never changes an application database."""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

from ..conf import SettingsError


def prepare_path(settings):
    raw = settings.storage_path
    if raw == ":memory:":
        return raw
    # Only these fixed placeholders are supported; arbitrary formatting is rejected.
    path = (
        raw.replace("{hostname}", socket.gethostname())
        .replace("{pid}", str(os.getpid()))
        .replace("{role}", settings.storage_role)
    )
    if "{" in path or "}" in path:
        raise SettingsError("storage_path supports only {hostname}, {pid} and {role}")
    resolved = Path(path).resolve()
    in_repo = (
        any((p / ".git").exists() for p in resolved.parent.parents)
        or (resolved.parent / ".git").exists()
    )
    if in_repo:
        if settings.preset != "dev" and not settings.allow_storage_in_repo:
            raise SettingsError(
                "Redline storage is inside a repository. Set an absolute path "
                "outside it, or explicitly allow_storage_in_repo."
            )
        logging.getLogger("redline").warning(
            "Redline trace storage is inside a repository; prefer a private "
            "directory outside the checkout."
        )
        if resolved.parent.name == ".redline":
            resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            ignore = resolved.parent / ".gitignore"
            if not ignore.exists():
                ignore.write_text("*\n")
    return path
