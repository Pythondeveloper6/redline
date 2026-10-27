# Redline demo shop (Django)

A tiny shop with deliberately slow endpoints, to show what Redline finds and how it fixes them.

## Run it

From the repository root:

```bash
uv sync --all-packages
uv run python examples/django/manage.py migrate
uv run python examples/django/manage.py seed        # ~400k reviews, takes a few seconds
uv run python examples/django/manage.py runserver
```

Open <http://127.0.0.1:8000/>, click through the endpoints, then open
<http://127.0.0.1:8000/redline/> and log in as **admin / admin**. This account is for the demo only.

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
  curl -s -o /dev/null -w "%{http_code} $u\n" "http://127.0.0.1:8000$u"
done
curl -s -o /dev/null -H "Authorization: Bearer sk-live-123" http://127.0.0.1:8000/api/products/
```

`shopsite/settings.py` lowers `SLOW_QUERY_MS` to 25 so the review scan counts as slow on a fast laptop.
Traces are stored in `examples/django/.redline/redline.db`. Delete them with `manage.py redline_clear`.

## Load test it

With the app running (see above), run the bundled scenario from the repository root:

```bash
uv run --package redline-core --extra load redline load examples/django/scenario.toml \
    --store examples/django/.redline/redline.db
```

It ramps from 5 to 50 rps in steps of 5, 10 s each (about 100 s), and writes the run to the
app's store. `--store` matters: the CLI defaults to `.redline/redline.db` relative to where
you run it, which is the repository root here, not the app's store. Then open the Load page
at **http://127.0.0.1:8000/redline/#/load** (log in as admin / admin first; the link the CLI
prints uses `/_redline/`, which this app does not mount) to see the per-step latency, the knee,
and the bottlenecks found under load.
