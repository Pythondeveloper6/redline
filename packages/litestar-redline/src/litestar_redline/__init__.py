"""litestar-redline: the Litestar + SQLAlchemy adapter for Redline.

::

    from litestar import Litestar
    from sqlalchemy.ext.asyncio import create_async_engine
    from litestar_redline import RedlinePlugin

    engine = create_async_engine("sqlite+aiosqlite:///app.db")
    app = Litestar(route_handlers=[...], plugins=[RedlinePlugin(engine=engine)])
    # UI at /_redline/
"""

from __future__ import annotations

from ._httpx import instrument_httpx, uninstrument_httpx
from ._middleware import RedlineMiddleware
from ._security import RedlineHTTPError, csrf_guard, local_only, loopback_guard
from ._sqlalchemy import instrument_engine
from .plugin import RedlinePlugin
from .suggest import SQLAlchemyFixSuggester

__version__ = "0.1.0a1"

__all__ = [
    "RedlineHTTPError",
    "RedlineMiddleware",
    "RedlinePlugin",
    "SQLAlchemyFixSuggester",
    "__version__",
    "csrf_guard",
    "instrument_engine",
    "instrument_httpx",
    "local_only",
    "loopback_guard",
    "uninstrument_httpx",
]
