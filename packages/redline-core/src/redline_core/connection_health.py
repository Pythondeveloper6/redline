"""Bounded, per-process pool observations. No connection strings or credentials."""

from __future__ import annotations

import os
import threading
import time
import weakref
from collections import deque

_lock = threading.Lock()
_pools = weakref.WeakKeyDictionary()
_pid = os.getpid()
_clock = time.monotonic


def _state(pool):
    global _pid, _pools, _lock
    # Also handles inherited objects after prefork without attributing parent activity.
    if _pid != os.getpid():
        _pid, _pools, _lock = os.getpid(), weakref.WeakKeyDictionary(), threading.Lock()
    now = _clock()
    state = _pools.setdefault(
        pool, {"connects": deque(maxlen=10000), "fresh_failures": deque(maxlen=1000)}
    )
    for values in state.values():
        while values and values[0] < now - 3600:
            values.popleft()
    return state, now


def connected(pool):
    if _pid != os.getpid():
        _state(pool)
    with _lock:
        state, now = _state(pool)
        state["connects"].append(now)


def evidence(pool, age, statement_number, error):
    if _pid != os.getpid():
        _state(pool)
    with _lock:
        state, now = _state(pool)
        if (
            error
            and error.get("is_disconnect")
            and age is not None
            and age <= 5
            and statement_number == 1
        ):
            state["fresh_failures"].append(now)
        return {
            "observed_connects_hour": len(state["connects"]),
            "observed_fresh_failures_hour": len(state["fresh_failures"]),
            "pid": os.getpid(),
        }
