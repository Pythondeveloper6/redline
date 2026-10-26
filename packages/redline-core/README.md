# redline-core

The framework-agnostic engine behind **Redline**, a self-hosted request profiler and
backend analyzer for Django and FastAPI.

`redline-core` holds everything that does not depend on a web framework:

- the trace/span data model and a `contextvars` recorder that is correct across threads
  and `asyncio` tasks and costs next to nothing when no request is being recorded;
- SQL fingerprinting, project-only callsite capture and secret redaction;
- detectors for N+1 queries, duplicate queries, slow queries and query budgets;
- an on-demand `EXPLAIN` runner with plan analysis for PostgreSQL, MySQL and SQLite;
- an optional `pyinstrument` flamegraph profiler (`redline-core[profile]`);
- a separate SQLite store with a background writer, retention and aggregates;
- the framework-neutral JSON API and the prebuilt web UI.

It has **no runtime dependencies**.

## You probably want an adapter

Install the package for your framework; it pulls in `redline-core` for you:

```console
pip install django-redline     # Django
pip install fastapi-redline    # FastAPI / Starlette + SQLAlchemy
```

For flamegraphs add the profiler extra: `pip install "redline-core[profile]"`.

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
