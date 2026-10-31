"""Install-time handling of optional outbound-call integrations.

Adapters call :func:`auto_instrument` once their recorder is configured. Depending on the
``auto_instrument`` setting it logs one warning about installed but uninstrumented
integrations (``"warn"``, the default), instruments them (``"on"``), or does nothing
(``"off"``). Provider libraries are never imported unless they are being instrumented.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from .integration_status import integration_status

logger = logging.getLogger("redline")

#: Integrations with a process-wide ``instrument_<name>()`` and no required arguments.
AUTO_INTEGRATIONS: tuple[str, ...] = ("httpx", "requests", "redis", "boto3")

_warned: set[tuple[str, ...]] = set()


def _instrument(name: str) -> bool:
    module = importlib.import_module(f"redline_core.contrib.{name}")
    return bool(getattr(module, f"instrument_{name}")())


def auto_instrument(settings: Any) -> list[str]:
    """Apply ``settings.auto_instrument``; returns the names instrumented by this call."""
    mode = getattr(settings, "auto_instrument", "off")
    if mode == "off" or not getattr(settings, "enabled", True):
        return []
    pending = [
        str(item["name"])
        for item in integration_status()
        if item["name"] in AUTO_INTEGRATIONS and item["installed"] and not item["instrumented"]
    ]
    if not pending:
        return []
    if mode == "on":
        done = []
        for name in pending:
            try:
                if _instrument(name):
                    done.append(name)
            except Exception:  # an optional integration must never break application startup
                logger.warning("Redline could not instrument %s", name, exc_info=True)
        return done
    key = tuple(pending)
    if key not in _warned:
        _warned.add(key)
        logger.warning(
            "Redline: installed but not instrumented integrations: %s. Their calls are not "
            'captured. Set auto_instrument="on" (REDLINE_AUTO_INSTRUMENT=on) to instrument '
            "them, call redline_core.contrib.<name>.instrument_<name>() yourself, or set "
            'auto_instrument="off" to silence this warning.',
            ", ".join(pending),
        )
    return []
