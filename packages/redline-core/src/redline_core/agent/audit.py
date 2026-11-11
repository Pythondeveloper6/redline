"""Append-only record of agent tool calls. Argument values are never written."""

from __future__ import annotations

import json
import os
import threading
import time

__all__ = ["AuditLog"]


class AuditLog:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, tool: str, args: object, ok: bool, ms: float) -> None:
        keys = sorted(str(k) for k in args) if isinstance(args, dict) else []
        line = json.dumps(
            {
                "ts": round(time.time(), 3),
                "tool": str(tool)[:100],
                "args_keys": keys[:20],
                "ok": bool(ok),
                "ms": round(ms, 2),
            }
        )
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (line + "\n").encode())
            finally:
                os.close(fd)
