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

Open **http://127.0.0.1:8000/_redline/** (localhost only: no `permission` is configured),
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
