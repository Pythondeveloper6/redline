"""Strawberry GraphQL integration: a ``resolver`` span per non-trivial field resolution.

::

    schema = strawberry.Schema(Query, extensions=[RedlineExtension])

Works for sync and async resolvers. Attribute resolvers that do no work are dropped, so
the trace shows the resolvers that ran SQL or took time. SQL executed inside a resolver
becomes a child of its span, which lets the N+1 detector report
``N+1: 24 queries on shop_customer from resolver orders.*.customer``.

``strawberry-django`` needs nothing extra: its resolvers run through this same hook, and
the Django adapter already records the ORM queries they issue.
"""

from __future__ import annotations

from typing import Any

from strawberry.extensions import SchemaExtension

from ._resolver import trace_resolver

__all__ = ["RedlineExtension"]


class RedlineExtension(SchemaExtension):
    def resolve(self, _next: Any, root: Any, info: Any, *args: Any, **kwargs: Any) -> Any:
        return trace_resolver(_next, root, info, args, kwargs)
