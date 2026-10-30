"""flask-redline: the Flask adapter for Redline.

::

    from flask import Flask
    from sqlalchemy import create_engine
    from flask_redline import Redline

    app = Flask(__name__)
    Redline(app, engine=create_engine("sqlite:///app.db"))   # UI at /_redline/

    # application factory
    redline = Redline(engine=engine)
    redline.init_app(app)
"""

from __future__ import annotations

from ._httpx import instrument_httpx, uninstrument_httpx
from ._middleware import RedlineMiddleware
from ._requests import instrument_requests, uninstrument_requests
from ._security import RedlineHTTPError, check_csrf, local_only, loopback_guard
from ._sqlalchemy import instrument_engine
from .integration import Redline
from .suggest import SQLAlchemyFixSuggester

__version__ = "0.5.0a1"

__all__ = [
    "Redline",
    "RedlineHTTPError",
    "RedlineMiddleware",
    "SQLAlchemyFixSuggester",
    "__version__",
    "check_csrf",
    "instrument_engine",
    "instrument_httpx",
    "instrument_requests",
    "local_only",
    "loopback_guard",
    "uninstrument_httpx",
    "uninstrument_requests",
]
