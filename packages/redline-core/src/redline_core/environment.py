"""Production detection for the adapters' ``install_if_dev`` helpers.

Redline records headers, bodies and SQL, so it should not attach to a production process by
accident. :func:`production_signals` lists why a process looks like production;
:func:`refuse_in_production` is the one call the adapters make: it logs a warning and
returns ``True`` when attaching should be skipped. ``REDLINE_ALLOW_PRODUCTION=1`` overrides.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

__all__ = [
    "ALLOW_PRODUCTION_ENV",
    "ENVIRONMENT_VARIABLES",
    "production_signals",
    "refuse_in_production",
]

logger = logging.getLogger("redline")

ALLOW_PRODUCTION_ENV = "REDLINE_ALLOW_PRODUCTION"
#: Variables whose value ``prod``/``production`` marks a production process.
ENVIRONMENT_VARIABLES = ("REDLINE_ENV", "APP_ENV", "ENV", "ENVIRONMENT", "FLASK_ENV", "DJANGO_ENV")
_PRODUCTION = frozenset({"prod", "production"})
_TRUE = frozenset({"1", "true", "yes", "on"})


def production_signals(
    environ: Mapping[str, str] | None = None, *, debug: bool | None = None
) -> list[str]:
    """Reasons this process looks like production (empty: looks like development).

    ``debug`` is the framework's debug flag when the framework treats ``False`` as a
    production signal (Django's ``DEBUG``); ``None`` means "not a signal".
    """
    environ = os.environ if environ is None else environ
    signals = []
    for name in ENVIRONMENT_VARIABLES:
        value = str(environ.get(name, "")).strip().lower()
        if value in _PRODUCTION:
            signals.append(f"{name}={value}")
    if debug is False:
        signals.append("DEBUG is False")
    return signals


def refuse_in_production(
    framework: str,
    environ: Mapping[str, str] | None = None,
    *,
    debug: bool | None = None,
) -> bool:
    """``True`` (after logging a warning) when Redline should not attach."""
    environ = os.environ if environ is None else environ
    signals = production_signals(environ, debug=debug)
    if not signals:
        return False
    if str(environ.get(ALLOW_PRODUCTION_ENV, "")).strip().lower() in _TRUE:
        logger.warning(
            "Redline (%s) attached although this looks like production (%s): %s is set.",
            framework,
            ", ".join(signals),
            ALLOW_PRODUCTION_ENV,
        )
        return False
    logger.warning(
        "Redline (%s) was not attached: this looks like production (%s). "
        "Set %s=1 to attach anyway.",
        framework,
        ", ".join(signals),
        ALLOW_PRODUCTION_ENV,
    )
    return True
