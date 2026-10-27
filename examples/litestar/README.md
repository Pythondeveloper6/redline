# Redline demo: a Litestar shop

A small SQLAlchemy 2.0 shop (Customer, Product, Order, OrderItem, Tag many-to-many, and a
400k-row `audit_log`) with deliberately bad endpoints, so every Redline feature has
something to show. It is the same shop as [`examples/fastapi`](../fastapi), served by
Litestar with `RedlinePlugin`.

```bash
# from the repository root (once)
uv sync --all-packages

cd examples/litestar
uv run --project ../.. python seed.py                      # creates shop.db (~2 s)
uv run --project ../.. uvicorn shop.main:app --port 8000   # http://127.0.0.1:8000/
```

Open **http://127.0.0.1:8000/_redline/** (localhost only: no `guards=` are configured),
then hit the endpoints:

| Endpoint | What Redline shows |
|---|---|
| `GET /orders` | N+1 on order items (async engine) -> `selectinload(Order.items)` diff |
| `GET /orders/customers` | N+1, many-to-one -> `joinedload(Order.customer)` diff |
| `GET /sync/orders` | a plain `def` handler (`sync_to_thread=True`) on a sync session, lazy-loaded m2m tags -> `selectinload(Order.tags)` |
| `GET /orders/1` | a healthy endpoint; aggregates as `/orders/{order_id}` |
| `GET /report/duplicates` | the same query four times |
| `GET /audit/search?q=refund` | slow query; open EXPLAIN to see the full table scan and a suggested index |
| `GET /slow` | slow endpoint spent in `asyncio.sleep` |
| `GET /boom` | an unhandled `ZeroDivisionError` (500) with its traceback |
| `GET /pricing` | outbound `httpx` calls as `http_out` spans (`instrument_httpx()`) |
| `GET /orders/2?_redline=profile` | adds a flamegraph (Profile tab) |
| `POST /checkout` | a body with `card_number`/`password` and an `Authorization` header, all redacted |

```bash
curl -X POST localhost:8000/checkout -H 'content-type: application/json' \
     -H 'Authorization: Bearer topsecret' \
     -d '{"email":"a@b.c","card_number":"4111111111111111","password":"hunter2"}'
```

Apply a suggested diff straight from the UI's "Copy diff" button with `git apply` or
`patch -p1` from `examples/litestar`.

See `shop/main.py` for the whole integration: one `RedlinePlugin(engine=...)` in the
plugins list, and `instrument_httpx()`.

## Load test it

With the app running (see above), run the bundled scenario from the repository root:

```bash
uv run --package redline-core --extra load redline load examples/litestar/scenario.toml \
    --store examples/litestar/.redline/redline.db
```

It ramps from 5 to 50 rps in steps of 5, 10 s each (about 100 s), and writes the run to the
app's store. `--store` matters: the CLI defaults to `.redline/redline.db` relative to where
you run it, which is the repository root here, not the app's store. Then open the Load page
at **http://127.0.0.1:8000/_redline/#/load** (the CLI prints the link to this run) to see the
per-step latency, the knee, and the bottlenecks found under load.
