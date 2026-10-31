"""Owner-only files for Redline's store (POSIX; a no-op on Windows).

The store holds captured requests – headers, SQL, bodies – so the directory Redline creates
for it is ``0700``, the database and its ``-wal``/``-shm`` files and the files of load runs
are ``0600``, and a new store directory gets a ``.gitignore`` of ``*`` so it is never
committed by accident. A directory that already existed (e.g. the project root) is left
alone: neither its mode nor a ``.gitignore`` is touched.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

__all__ = ["POSIX", "ensure_private_dir", "open_private", "precreate", "tighten"]

POSIX = os.name == "posix"
_GITIGNORE = "# Created by Redline: captured requests never belong in version control.\n*\n"


def ensure_private_dir(directory: str | os.PathLike[str], *, gitignore: bool = True) -> Path:
    """Create ``directory`` (and parents) if missing; a directory created here is ``0700``
    and, with ``gitignore``, gets a ``.gitignore`` containing ``*``. A directory named
    ``.redline`` gets the ``.gitignore`` even when it already existed (never overwritten).
    """
    path = Path(directory)
    created = not path.exists()
    if created:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if POSIX:
            with contextlib.suppress(OSError):
                os.chmod(path, 0o700)  # mkdir's mode is filtered by the umask
    if gitignore and (created or path.name == ".redline"):
        _write_gitignore(path)
    return path


def _write_gitignore(directory: Path) -> None:
    target = directory / ".gitignore"
    try:
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except OSError:  # it exists (never overwritten) or the directory is read-only
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_GITIGNORE)


def open_private(path: str | os.PathLike[str], mode: str = "ab") -> int:
    """``os.open`` flags for ``mode`` (``"ab"``/``"wb"``/``"w"``), creating the file ``0600``.

    Returns the file descriptor (wrap it with :func:`os.fdopen`). An existing file keeps
    its contents and is tightened to ``0600``. A symlink at ``path`` is refused (where the
    platform supports it), so a link planted in a shared directory cannot redirect the
    write or the ``chmod``.
    """
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_APPEND if "a" in mode else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    if POSIX:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
    return fd


def tighten(*paths: str | os.PathLike[str]) -> None:
    """``chmod 0600`` the files that exist and are readable by others (POSIX only)."""
    if not POSIX:
        return
    for p in paths:
        with contextlib.suppress(OSError):
            if os.stat(p).st_mode & 0o077:
                os.chmod(p, 0o600)


def precreate(path: str | os.PathLike[str]) -> None:
    """Create an empty ``0600`` file at ``path`` unless it exists (so SQLite, which gives
    its ``-wal``/``-shm`` files the database file's mode, never makes them world-readable).
    """
    with contextlib.suppress(FileExistsError):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
