<p align="center">
  <a href="https://pythondeveloper6.github.io/redline/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Pythondeveloper6/redline/main/docs/assets/logo-dark.svg">
      <img alt="Redline" src="https://raw.githubusercontent.com/Pythondeveloper6/redline/main/docs/assets/logo-light.svg" height="56">
    </picture>
  </a>
</p>

# redline-core

The framework-agnostic engine behind **Redline**, a self-hosted request profiler and
backend analyzer and load tester for Django, FastAPI, Flask and Litestar.

`redline-core` holds everything that does not depend on a web framework:

- the trace/span data model and a `contextvars` recorder that is correct across threads
  and `asyncio` tasks and costs next to nothing when no request is being recorded;
- SQL fingerprinting, project-only callsite capture and secret redaction;
- detectors for N+1 queries, duplicate queries, slow queries and query budgets, plus
  event-loop blocking, long transactions, huge and unbounded results, `len(qs)` vs `.exists()`,
  slow serializers and slow, duplicated or retried external calls;
- an on-demand `EXPLAIN` runner with plan analysis for PostgreSQL, MySQL and SQLite;
- an optional `pyinstrument` flamegraph profiler (`redline-core[profile]`);
- a separate SQLite store with a background writer, retention, hourly rollups and release
  history;
- a load tester (`redline load`, extra `load`) with an open-model scheduler, knee detection
  and per-step bottlenecks;
- a refactor check (`redline check`, extra `load`): record a baseline of read-only requests,
  compare a new version, per-endpoint verdicts on responses and speed, JUnit and Markdown output;
- optional integrations in `redline_core.contrib` (SQLAlchemy, httpx, requests, redis,
  asyncpg, psycopg, Marshmallow, Strawberry, Graphene);
- the access policy and layered configuration shared by every adapter;
- the framework-neutral JSON API and the prebuilt web UI.

It has **no runtime dependencies**.

## You probably want an adapter

Install the package for your framework; it pulls in `redline-core` for you:

Redline is not on PyPI yet: install from GitHub, naming the adapter **and** `redline-core` in
one command (the adapter pins the core exactly). Adapters: `django-redline`,
`fastapi-redline`, `flask-redline`, `litestar-redline`.

```console
pip install \
  "django-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/django-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"
```

Extras on the core requirement: `profile` (flamegraphs), `load` (the load tester), and
`httpx`, `requests`, `redis`, `asyncpg`, `psycopg`, `strawberry`, `graphene`. Documentation:
<https://pythondeveloper6.github.io/redline/>.

## Timing your own code

```python
import redline_core

@redline_core.span("pricing.compute")
def compute_price(order): ...

async def handler():
    async with redline_core.span("fetch-rates", kind="custom", provider="ecb"):
        ...
```

`span()` does nothing (and costs almost nothing) outside a recorded request.

## License

MIT
