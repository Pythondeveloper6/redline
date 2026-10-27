# Redline demo: a Flask shop

A small SQLAlchemy 2.0 shop (Customer, Product, Order, OrderItem, Tag many-to-many, and a
400k-row `audit_log`) with deliberately bad views, so every Redline feature has something
to show. It is the same shop as [`examples/fastapi`](../fastapi), served by Flask.

```bash
# from the repository root (once)
uv sync --all-packages

cd examples/flask
uv run --project ../.. python seed.py                                # creates shop.db (~2 s)
uv run --project ../.. flask --app shop.main run --port 8000         # http://127.0.0.1:8000/
```

Open **http://127.0.0.1:8000/_redline/** (localhost only: `access="local"` is set in `shop/main.py` and no `permission` is configured; without an explicit `access`, Redline answers 404 outside debug mode),
then hit the endpoints:

| Endpoint | What Redline shows |
|---|---|
| `GET /orders` | N+1 on order items -> `selectinload(Order.items)` diff |
| `GET /orders/customers` | N+1, many-to-one -> `joinedload(Order.customer)` diff |
| `GET /orders/tags` | lazy-loaded many-to-many tags -> `selectinload(Order.tags)` |
| `GET /orders/page` | the N+1 hidden in a Jinja template: queries nest under the `template` span |
| `GET /orders/1` | a healthy view; aggregates as `/orders/<int:order_id>` |
| `GET /report/duplicates` | the same query four times |
| `GET /audit/search?q=refund` | slow query; open EXPLAIN to see the full table scan and a suggested index |
| `GET /slow` | slow view spent in `time.sleep` |
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
`patch -p1` from `examples/flask`. Manage the recorded traces with
`flask --app shop.main redline clear` and `flask --app shop.main redline prune`.

See `shop/main.py` for the whole integration: one `Redline(app, engine=...)` call and
`instrument_httpx()`.

## Try the refactor check

`check.toml` is a ready-made suite of read-only endpoints (the N+1 one, the slow one, one with
volatile fields). Setting `SHOP_REFACTOR=1` simulates a refactor of the app, so you can see what
`redline check` reports without touching the code:

| Endpoint | What `SHOP_REFACTOR=1` does | Expected verdict |
|---|---|---|
| `GET /orders` | fixes the N+1 with `selectinload` | faster, queries 31 -> 2, fixed N+1 |
| `GET /orders/1` | adds a 50 ms sleep | slower |
| `GET /report/duplicates` | `products` becomes a string | changed (`products`, type) |
| `GET /orders/tags` | raises an error | broken (200 -> 500) |
| `GET /status` | unchanged; `now` and `request_id` differ on every call | same (volatile fields ignored) |

```bash
# 1. old code: start the app (as above) and, from the repository root in another terminal,
#    record a baseline
cd examples/flask
uv run --project ../.. flask --app shop.main run --port 8000
uv run --package redline-core --extra load redline check record examples/flask/check.toml \
    --base-url http://127.0.0.1:8000 --store examples/flask/.redline/redline.db

# 2. "refactored" code: restart the app with the toggle, then compare
SHOP_REFACTOR=1 uv run --project ../.. flask --app shop.main run --port 8000
uv run --package redline-core --extra load redline check compare examples/flask/check.toml \
    --base-url http://127.0.0.1:8000 --store examples/flask/.redline/redline.db --markdown report.md --junit report.xml
echo $?   # 1: the refactor made things slower, changed a response and broke an endpoint
```

`--store` matters: the CLI defaults to `.redline/redline.db` relative to where you run it, which is
the repository root here, not the app's store. In the UI, open the Checks page, create a suite from
the routes Redline has seen, record a baseline,
restart with the toggle and press Compare (the UI starts runs because `checks_from_ui` defaults to on in debug mode; set `checks_from_ui = true` otherwise).

## Load test it

With the app running (see above), run the bundled scenario from the repository root:

```bash
uv run --package redline-core --extra load redline load examples/flask/scenario.toml \
    --store examples/flask/.redline/redline.db
```

It ramps from 5 to 50 rps in steps of 5, 10 s each (about 100 s), and writes the run to the
app's store. `--store` matters: the CLI defaults to `.redline/redline.db` relative to where
you run it, which is the repository root here, not the app's store. Then open the Load page
at **http://127.0.0.1:8000/_redline/#/load** (the CLI prints the link to this run) to see the
per-step latency, the knee, and the bottlenecks found under load.
