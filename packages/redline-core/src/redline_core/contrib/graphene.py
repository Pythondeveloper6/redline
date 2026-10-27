"""Graphene / graphql-core integration: a ``resolver`` span per non-trivial field.

::

    schema.execute(query, middleware=[RedlineMiddleware()])

Same semantics as :mod:`redline_core.contrib.strawberry`; coroutine and promise results
are handled.
"""

from __future__ import annotations

from typing import Any

from ._resolver import trace_resolver

__all__ = ["RedlineMiddleware"]


class RedlineMiddleware:
    def resolve(self, next: Any, root: Any, info: Any, **args: Any) -> Any:
        return trace_resolver(next, root, info, (), args)
