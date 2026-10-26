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
from ._sqlalchemy import instrument_engine
from .integration import Redline, install
from .suggest import SQLAlchemyFixSuggester

__version__ = "0.1.0a1"

__all__ = [
    "Redline",
    "RedlineMiddleware",
    "SQLAlchemyFixSuggester",
    "__version__",
    "install",
    "instrument_engine",
    "instrument_httpx",
    "uninstrument_httpx",
]
