"""django-redline: the Django adapter for Redline.

Add ``"django_redline"`` to ``INSTALLED_APPS``, ``"django_redline.middleware.RedlineMiddleware"``
near the top of ``MIDDLEWARE`` and ``path("redline/", include("django_redline.urls"))`` to
your URLconf. Every request is then recorded – SQL, cache and template spans, N+1 and
duplicate-query issues with ``select_related``/``prefetch_related`` fix diffs, one-click
EXPLAIN and opt-in flamegraphs – and browsable at ``/redline/``.

User code can time its own blocks with :func:`span` (re-exported from ``redline_core``)::

    from django_redline import span

    @span("pricing.compute")
    def compute_price(order): ...
"""

from __future__ import annotations

from redline_core import span

__version__ = "0.1.0a1"

__all__ = ["__version__", "span"]
