# GraphQL

[← Index](index.md)

GraphQL is the natural home of the N+1: a query asks for `orders { customer { name } }`
and the default resolver loads each customer separately. The SQL is fine one query at a
time and bad in total, and the SQL alone does not tell you *which field* to fix.

Redline adds a `resolver` span for each non-trivial field resolution. SQL that runs inside
a resolver nests under that span, so the N+1 detector can name the field:

```text
N+1: 24 queries on shop_customer from resolver orders.*.customer
```

List indexes in the path are shown as `*`, so `orders.0.customer`, `orders.1.customer` and
so on are one issue. The path is also stored on the issue (`extra["resolver_path"]`).

## Strawberry

```python
import strawberry
from redline_core.contrib.strawberry import RedlineExtension

schema = strawberry.Schema(Query, extensions=[RedlineExtension])
```

Works for sync and async resolvers. Attribute resolvers that do no work are dropped, so the
trace shows resolvers that ran SQL or took time.

`strawberry-django` needs nothing extra: its resolvers run through the same hook, and the
Django adapter already records the ORM queries they issue.

The `redline-core[strawberry]` and `redline-core[graphene]` extras install the matching
GraphQL library if you want them pinned together.

## Graphene

```python
from redline_core.contrib.graphene import RedlineMiddleware

result = schema.execute(query, middleware=[RedlineMiddleware()])
```

Coroutine and promise results are handled.

## What it looks like

In the request's **Timeline** a resolver span (kind `resolver`) sits between the view and
its SQL, with `path`, `field` and `parent_type` in its data. In **Issues** the N+1 card
names the resolver path and, for a Django project, the usual `select_related` or
`prefetch_related` diff. For GraphQL the right fix is often a `DataLoader` rather than an
ORM change; the diff is a hint for the ORM-level fix when the resolver reads a model
relation directly.

| Span data | Meaning |
|---|---|
| `path` | `orders.*.customer` with list indexes as `*` |
| `field` | the field name |
| `parent_type` | the GraphQL type that owns the field |

## Tips

- **One endpoint, many operations.** All GraphQL requests hit `POST /graphql`, so they
  aggregate on the [Endpoints](issues-and-endpoints.md#endpoints) page as one route. The
  resolver paths in [Issues](issues-and-endpoints.md#issues) are how you tell operations
  apart.
- **Query size.** A deeply nested query can create thousands of resolver spans.
  `max_spans_per_trace` (default 2000) still applies: counts stay exact, spans beyond the
  cap are folded.
- **Load testing.** A scenario can POST a fixed query body:

  ```toml
  [[requests]]
  name = "orders"
  method = "POST"
  path = "/graphql"
  json = { query = "{ orders { id customer { name } } }" }
  ```

  See [Load testing](load-testing.md).
