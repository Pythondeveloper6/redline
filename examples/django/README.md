# Redline demo shop (Django)

A tiny shop with deliberately slow endpoints, to show what Redline finds and how it fixes them.

## Run it

From the repository root:

```bash
uv sync --all-packages
uv run python examples/django/manage.py migrate
uv run python examples/django/manage.py seed        # ~400k reviews, takes a few seconds
uv run python examples/django/manage.py runserver 8014
```

Open <http://127.0.0.1:8014/>, click through the endpoints, then open
<http://127.0.0.1:8014/redline/> and log in as **admin / admin**. This account is for the demo only.

## The broken endpoints

| URL | What is wrong | What Redline shows |
|---|---|---|
| `/orders/` | one query per order for its items | N+1 with a `prefetch_related("items")` diff |
| `/items/` | one query per item for its product | N+1 with a `select_related("product")` diff |
| `/orders/page/` | the same N+1, inside a template loop | N+1 attributed to the `render()` line, with a diff |
| `/dashboard/` | the same `COUNT(*)` twice | duplicate query |
| `/reviews/search/?q=battery` | `LIKE` plus `ORDER BY` on unindexed columns of a 400k-row table | slow query; EXPLAIN shows `SCAN shop_review` and a temp B-tree, and suggests an index |
| `/slow/` | CPU loop plus `time.sleep` | open `/slow/?_redline=profile` to get a flamegraph |
| `/boom/` | raises `RuntimeError` | a 500 trace with the traceback |
| `/api/products/` | a JSON API | send `-H "Authorization: Bearer secret"`; the header is stored redacted |
| `/async/summary/` | an async view (async ORM) | a trace with the async view span |
| `/cached/` | low-level cache use | cache spans: a miss on the first request, then hits |

```bash
for u in / /orders/ /items/ /orders/page/ /dashboard/ "/reviews/search/?q=battery" \
         /slow/ "/slow/?_redline=profile" /boom/ /async/summary/ /cached/ /cached/; do
  curl -s -o /dev/null -w "%{http_code} $u\n" "http://127.0.0.1:8014$u"
done
curl -s -o /dev/null -H "Authorization: Bearer sk-live-123" http://127.0.0.1:8014/api/products/
```

`shopsite/settings.py` lowers `SLOW_QUERY_MS` to 25 so the review scan counts as slow on a fast laptop.
Traces are stored in `examples/django/.redline/redline.db`. Delete them with `manage.py redline_clear`.

## Try the refactor check

`check.toml` is a ready-made suite of read-only endpoints (the N+1 one, the slow one, one with
volatile fields). Setting `SHOP_REFACTOR=1` simulates a refactor of the app, so you can see what
`redline check` reports without touching the code:

| Endpoint | What `SHOP_REFACTOR=1` does | Expected verdict |
|---|---|---|
| `GET /orders/` | adds `prefetch_related("items")` | faster, queries 31 -> 2, fixed N+1 |
| `GET /dashboard/` | adds a 50 ms sleep | slower |
| `GET /api/products/` | `price` now has one decimal (`1528.00` -> `1528.0`) | changed (`products[n].price`) |
| `GET /async/summary/` | raises an error | broken (200 -> 500) |
| `GET /status/` | unchanged; `now` and `request_id` differ on every call | same (volatile fields ignored) |

```bash
# 1. old code: start the app (as above) and, from the repository root in another terminal,
#    record a baseline
uv run python examples/django/manage.py runserver 8014 --noreload
uv run --package redline-core --extra load redline check record examples/django/check.toml \
    --base-url http://127.0.0.1:8014 --store examples/django/.redline/redline.db

# 2. "refactored" code: restart the app with the toggle, then compare
SHOP_REFACTOR=1 uv run python examples/django/manage.py runserver 8014 --noreload
uv run --package redline-core --extra load redline check compare examples/django/check.toml \
    --base-url http://127.0.0.1:8014 --store examples/django/.redline/redline.db --markdown report.md --junit report.xml
echo $?   # 1: the refactor made things slower, changed a response and broke an endpoint
```

`--store` matters: the CLI defaults to `.redline/redline.db` relative to where you run it, which is
the repository root here, not the app's store. In the UI, open the Checks page, create a suite from
the routes Redline has seen, record a baseline,
restart with the toggle and press Compare (log in as admin / admin first; `DEBUG = True` here, so `checks_from_ui` is on).

## Load test it

With the app running (see above), run the bundled scenario from the repository root:

```bash
uv run --package redline-core --extra load redline load examples/django/scenario.toml \
    --store examples/django/.redline/redline.db
```

It ramps from 5 to 50 rps in steps of 5, 10 s each (about 100 s), and writes the run to the
app's store. `--store` matters: the CLI defaults to `.redline/redline.db` relative to where
you run it, which is the repository root here, not the app's store. Then open the Load page
at **http://127.0.0.1:8014/redline/#/load** (log in as admin / admin first; the link the CLI
prints uses `/_redline/`, which this app does not mount) to see the per-step latency, the knee,
and the bottlenecks found under load.

## Additional real-work workflows

The [Flask playground](../flask/README.md#v04-real-work-playground) demonstrates the
combined SQL-failure, dynamic-engine, privacy, job, SQL-checker, schema-snapshot and
secondary-store workflows with disposable databases. Run the shared no-socket verification
from the repository root:

```bash
.venv/bin/python scripts/check_demo_patterns.py
.venv/bin/python scripts/check_real_world_workflows.py
```

The first command covers the existing patterns on all four adapters; the second exercises
the Flask playground and a separate generic worker process. Neither command establishes
MySQL/SQL Server or broker/prefork support. Use the playground's explicit disposable-driver
probe and framework-native application tests for the actual deployment stack.

## Shared Docker test environment

The [shared Compose stack](../testing/README.md) builds this project and the other
examples, supplies MySQL, SQL Server, PostgreSQL and Redis, and runs complete package,
live-driver, Celery prefork and browser tests. It also provides token-protected demo UIs
on loopback ports 8011–8014. Run from the repository root:

```bash
docker compose -f examples/compose.yaml up --build \
  --abort-on-container-exit --exit-code-from tests tests
```

A green configuration check is insufficient: the command must exit zero and the current
`examples/testing/results/summary.json` must say `qualified: true`. See the shared guide
for native amd64 SQL Server requirements, interactive demo commands and owned-data cleanup.
