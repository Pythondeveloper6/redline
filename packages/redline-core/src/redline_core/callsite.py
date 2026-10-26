"""Cheap capture of the *project* frame that triggered a query.

Walking the stack with ``sys._getframe`` and reading ``f_code``/``f_lineno`` costs well under
a microsecond per frame; what makes naive implementations slow is classifying every
filename and reading source lines on each query. Both are cached here: the per-filename
verdict ("project file, shown as ``shop/views.py``" or "library, skip") lives in a dict
keyed by the code object's filename, and source lines go through :mod:`linecache` only for
the single innermost frame.

A frame is *project code* when its file is under ``project_root`` (default: the working
directory) and is not:

* the standard library or anything under ``site-packages``/``dist-packages``;
* Redline itself (``redline_core``, ``django_redline``, ``fastapi_redline``);
* a frozen or synthetic file (``<frozen …>``, ``<string>``);
* under one of the extra ``exclude`` path fragments.
"""

from __future__ import annotations

import linecache
import os
import sys
import sysconfig
import threading
from collections.abc import Iterable
from types import FrameType

from .model import Callsite, Frame

__all__ = [
    "MAX_STACK",
    "CallsiteCapturer",
    "capture",
    "default_capturer",
    "set_default_capturer",
]

#: How many project frames a :class:`Callsite` keeps (innermost last).
MAX_STACK = 12
_MAX_WALK = 128  # never walk deeper than this, whatever the recursion depth

_REDLINE_PACKAGES = ("redline_core", "django_redline", "fastapi_redline")


def _stdlib_dirs() -> tuple[str, ...]:
    dirs = set()
    for key in ("stdlib", "platstdlib"):
        try:
            path = sysconfig.get_paths().get(key)
        except Exception:  # pragma: no cover - exotic interpreters
            path = None
        if path:
            dirs.add(os.path.realpath(path) + os.sep)
            dirs.add(os.path.abspath(path) + os.sep)
    return tuple(dirs)


class CallsiteCapturer:
    """Finds project frames on the current stack; one per configured project root."""

    def __init__(self, project_root: str | None = None, exclude: Iterable[str] = ()) -> None:
        root = os.path.abspath(project_root or os.getcwd())
        self.project_root = root.rstrip(os.sep) + os.sep
        self._real_root = os.path.realpath(root).rstrip(os.sep) + os.sep
        self._stdlib = _stdlib_dirs()
        markers = [f"{os.sep}{pkg}{os.sep}" for pkg in _REDLINE_PACKAGES]
        markers += [f"{os.sep}site-packages{os.sep}", f"{os.sep}dist-packages{os.sep}"]
        self._exclude = tuple(markers) + tuple(exclude)
        # filename -> display path (project file) or None (skip). Plain dict reads and
        # writes are atomic, so concurrent fills at worst compute a verdict twice.
        self._cache: dict[str, str | None] = {}

    # classification --------------------------------------------------------
    def classify(self, filename: str) -> str | None:
        """Display path for a project ``filename``, or ``None`` if it is not project code."""
        try:
            return self._cache[filename]
        except KeyError:
            pass
        verdict = self._classify(filename)
        self._cache[filename] = verdict
        return verdict

    def _classify(self, filename: str) -> str | None:
        if not filename or filename.startswith("<"):
            return None
        path = os.path.abspath(filename)
        if any(marker in path for marker in self._exclude):
            return None
        if path.startswith(self._stdlib):
            return None
        for root in (self.project_root, self._real_root):
            if path.startswith(root):
                return path[len(root) :].replace(os.sep, "/")
        real = os.path.realpath(path)
        if real.startswith(self._real_root) and not real.startswith(self._stdlib):
            return real[len(self._real_root) :].replace(os.sep, "/")
        return None

    def clear_cache(self) -> None:
        self._cache.clear()

    # capture ---------------------------------------------------------------
    def capture(self, skip: int = 1, frame: FrameType | None = None) -> Callsite | None:
        """The innermost project frame above the caller, with its project stack.

        ``skip`` frames are skipped first (1 = the caller of :meth:`capture`). Returns
        ``None`` when no project frame is on the stack.
        """
        if frame is None:
            try:
                frame = sys._getframe(skip)
            except ValueError:
                return None
        classify = self.classify
        found: list[tuple[str, int, str]] = []
        depth = 0
        f: FrameType | None = frame
        while f is not None and depth < _MAX_WALK:
            code = f.f_code
            display = classify(code.co_filename)
            if display is not None:
                found.append((display, f.f_lineno or 0, code.co_name))
                if len(found) >= MAX_STACK:
                    break
            f = f.f_back
            depth += 1
        if not found:
            return None
        found.reverse()  # outermost first, innermost last
        file, line, function = found[-1]
        source = linecache.getline(self._abspath(file), line).strip() or None
        return Callsite(
            file=file,
            line=line,
            function=function,
            code=source,
            stack=tuple(Frame(file=a, line=b, function=c) for a, b, c in found),
        )

    def _abspath(self, display: str) -> str:
        return os.path.join(self.project_root, display.replace("/", os.sep))


_default: CallsiteCapturer | None = None
_default_lock = threading.Lock()


def default_capturer() -> CallsiteCapturer:
    """The process-wide capturer (created on first use for the current directory)."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = CallsiteCapturer()
    return _default


def set_default_capturer(capturer: CallsiteCapturer) -> None:
    global _default
    _default = capturer


def capture(skip: int = 1) -> Callsite | None:
    """Capture with the default capturer; see :meth:`CallsiteCapturer.capture`."""
    return default_capturer().capture(skip=skip + 1)
