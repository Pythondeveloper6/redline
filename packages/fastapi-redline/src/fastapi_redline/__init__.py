"""fastapi-redline: the FastAPI + SQLAlchemy adapter for Redline.

::

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import create_async_engine
    from fastapi_redline import install

    app = FastAPI()
    engine = create_async_engine("sqlite+aiosqlite:///app.db")
    install(app, engine=engine)          # UI at /_redline/
"""

from __future__ import annotations

from ._httpx import instrument_httpx, uninstrument_httpx
from ._middleware import RedlineMiddleware
from ._security import RedlineHTTPError, csrf_guard, local_only, loopback_guard
from ._sqlalchemy import instrument_engine
from .integration import Redline, install
from .suggest import SQLAlchemyFixSuggester

__version__ = "0.1.0a1"

__all__ = [
    "Redline",
    "RedlineHTTPError",
    "RedlineMiddleware",
    "SQLAlchemyFixSuggester",
    "__version__",
    "csrf_guard",
    "install",
    "instrument_engine",
    "instrument_httpx",
    "local_only",
    "loopback_guard",
    "uninstrument_httpx",
]
