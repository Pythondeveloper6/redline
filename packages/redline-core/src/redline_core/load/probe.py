"""One tagged request before a run, to check the application records it.

A run only fills the bottleneck table when the application tags its traces with the run
(``X-Redline-Run``), which needs the run to exist in the application's store and recording
to be on. Sending the whole run first and finding an empty table afterwards wastes the
test, so one harmless GET goes first and the result is looked up. The probe never sends a
write: it uses the scenario's first enabled ``GET`` request and tags it ``step=probe``,
which the bottleneck analysis ignores.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from .scenario import Scenario, render_path

__all__ = ["ProbeFailed", "run_probe"]

POLL_S = 0.2


class ProbeFailed(RuntimeError):
    """The application did not record the probe request."""


def run_probe(
    scenario: Scenario,
    run_id: str,
    find_trace: Callable[[str], bool],
    *,
    timeout_s: float = 5.0,
) -> bool:
    """Send the probe and wait for ``find_trace(run_id)``; ``False`` when skipped.

    Raises :class:`ProbeFailed` when the application cannot be reached or no trace tagged
    with the run appears within ``timeout_s``.
    """
    import httpx

    request = next((r for r in scenario.requests if r.method == "GET" and r.weight > 0), None)
    if request is None or not scenario.base_url:
        return False
    headers = {**scenario.headers, **request.headers, "X-Redline-Run": f"{run_id};step=probe"}
    path = render_path(request.path, random.Random(0))
    try:
        with httpx.Client(base_url=scenario.base_url, timeout=scenario.timeout_s) as client:
            client.request("GET", path, headers=headers)
    except httpx.HTTPError as exc:
        raise ProbeFailed(
            f"cannot reach {scenario.base_url} for the probe request: {type(exc).__name__}"
        ) from None
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if find_trace(run_id):
                return True
        except Exception:  # a store that cannot be read is the same as "not found" here
            pass
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_S)
    raise ProbeFailed(
        "the application is not recording this run. Likely causes: the application uses a "
        "different store than the one this run was written to, sampling dropped the "
        "request, or recording disabled (or Redline is not installed in the application)"
    )
