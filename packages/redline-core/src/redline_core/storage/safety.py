"""Storage placement checks; never changes an application database."""

from __future__ import annotations

import logging
import os
import re
import shlex
import socket
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
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
            from .private import ensure_private_dir

            # O_EXCL create: never overwrites, never writes through a symlink
            ensure_private_dir(resolved.parent)
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


#: Filesystems where SQLite WAL's shared-memory file is unreliable or unsupported.
UNRELIABLE_WAL_FILESYSTEMS = frozenset(
    {
        "9p",
        "afpfs",
        "cifs",
        "fuse",
        "fuse.grpcfuse",
        "fuse.sshfs",
        "fuseblk",
        "nfs",
        "nfs4",
        "smb3",
        "smbfs",
        "virtiofs",
    }
)


def _nearest_parent(path: str) -> str:
    probe = os.path.dirname(os.path.abspath(path))
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        probe = parent if parent != probe else ""
    return probe


def probe_wal(path: str) -> bool:
    """Create a throwaway SQLite database beside ``path`` and require WAL there.

    This is the only honest self-test for network/virtual mounts: the mount type is a
    hint, while ``PRAGMA journal_mode`` says whether SQLite can actually use WAL.
    """
    parent = _nearest_parent(path)
    if not parent:
        raise OSError("No existing parent directory")
    fd, database = tempfile.mkstemp(dir=parent, prefix=".redline-wal-check-", suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(database)
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        finally:
            conn.close()
        return str(mode).lower() == "wal"
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(database + suffix)
            except FileNotFoundError:
                pass


def _unescape_mount(value: str) -> str:
    for encoded, decoded in (("\040", " "), ("\011", "\t"), ("\012", "\n"), ("\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def filesystem_type(path: str) -> str | None:
    """The mount filesystem type for ``path`` when Linux exposes ``mountinfo``."""
    if not sys.platform.startswith("linux"):
        return None
    target = os.path.realpath(os.path.abspath(path))
    best_mount, best_type = "", None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                fields = left.split()
                if len(fields) < 5 or not right:
                    continue
                mount = _unescape_mount(fields[4])
                if target == mount or target.startswith(mount.rstrip("/") + "/"):
                    if len(mount) >= len(best_mount):
                        best_mount = mount
                        best_type = right.split()[0]
    except OSError:
        return None
    return best_type


_WORKER_FLAG = re.compile(r"^(?:-w|--workers)(?:=(.*))?$")


def _gunicorn_workers(cmd_args: str) -> int | None:
    try:
        parts = shlex.split(cmd_args)
    except ValueError:
        return None
    found = None
    for i, part in enumerate(parts):
        if part.startswith("-w") and len(part) > 2 and part[2:].isdigit():  # -w4
            found = int(part[2:])
            continue
        match = _WORKER_FLAG.match(part)
        if not match:
            continue
        value = (
            match.group(1)
            if match.group(1) is not None
            else (parts[i + 1] if i + 1 < len(parts) else "")
        )
        if value.strip().isdigit():
            found = int(value)
    return found


def detect_workers(environ: Mapping[str, str] | None = None) -> tuple[int, str] | None:
    """The worker-process count a deployment declares in its environment, with the
    variable it came from: ``WEB_CONCURRENCY`` (gunicorn, uvicorn and most platforms),
    ``--workers``/``-w`` in ``GUNICORN_CMD_ARGS``, or ``UVICORN_WORKERS``. ``None`` when
    none of them is set to a number. The largest wins."""
    environ = os.environ if environ is None else environ
    found: list[tuple[int, str]] = []
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = str(environ.get(name, "")).strip()
        if raw.isdigit():
            found.append((int(raw), f"{name}={raw}"))
    gunicorn = _gunicorn_workers(str(environ.get("GUNICORN_CMD_ARGS", "")))
    if gunicorn is not None:
        found.append((gunicorn, f"GUNICORN_CMD_ARGS --workers {gunicorn}"))
    return max(found, key=lambda f: f[0]) if found else None
