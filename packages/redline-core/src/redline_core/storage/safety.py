"""Storage placement checks; never changes an application database."""

from __future__ import annotations

import logging
import os
import socket
import tempfile
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


def probe_writable(path: str) -> None:
    """Check effective storage access without modifying the configured database.

    Only a private temporary file in the nearest existing parent is created/written
    and removed. Raise OSError if an existing target cannot be opened for writing or
    the directory cannot support file creation (needed for SQLite journal/WAL files).
    Permission bits and access() alone do not establish this on special mounts/root.
    """
    target = os.path.abspath(path)
    probe = os.path.dirname(target)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        probe = parent if parent != probe else ""
    if not probe:
        raise OSError("No existing parent directory")
    if os.path.lexists(target):
        fd = os.open(target, os.O_WRONLY)
        os.close(fd)
    with tempfile.TemporaryFile(dir=probe, prefix=".redline-storage-check-") as temporary:
        temporary.write(b"x")
        temporary.flush()
