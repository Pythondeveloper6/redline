# Redline demo: a Flask shop

A small SQLAlchemy 2.0 shop (Customer, Product, Order, OrderItem, Tag many-to-many, and a
400k-row `audit_log`) with deliberately bad views, so every Redline feature has something
to show. It is the same shop as [`examples/fastapi`](../fastapi), served by Flask.

```bash
# from the repository root (once)
uv sync --all-packages

cd examples/flask
uv run --project ../.. python seed.py                                # creates shop.db (~2 s)
uv run --project ../.. flask --app shop.main run --port 8012         # http://127.0.0.1:8012/
```

Open **http://127.0.0.1:8012/_redline/** (localhost only: `access="local"` is set in `shop/main.py` and no `permission` is configured; without an explicit `access`, Redline answers 404 outside debug mode),
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
curl -X POST localhost:8012/checkout -H 'content-type: application/json' \
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
# terminal 1 – the app, from examples/flask: old code first
cd examples/flask
uv run --project ../.. flask --app shop.main run --port 8012
```

```bash
# terminal 2 – from the repository root: record a baseline against the old code
uv run --package redline-core --extra load redline check record examples/flask/check.toml \
    --base-url http://127.0.0.1:8012 --store examples/flask/.redline/redline.db
```

```bash
# terminal 1: stop the app (Ctrl-C) and start the "refactored" code
SHOP_REFACTOR=1 uv run --project ../.. flask --app shop.main run --port 8012
```

```bash
# terminal 2 – from the repository root: compare against the baseline
uv run --package redline-core --extra load redline check compare examples/flask/check.toml \
    --base-url http://127.0.0.1:8012 --store examples/flask/.redline/redline.db --markdown report.md --junit report.xml
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
at **http://127.0.0.1:8012/_redline/#/load** (the CLI prints the link to this run) to see the
per-step latency, the knee, and the bottlenecks found under load.

## v0.4 real-work playground

Use only the disposable shop databases and invented payloads shown here. The examples
are application behavior to inspect, not instructions to enable write actions against a
production database. SQL Server is not emulated by calling SQLite `mssql`.

For a complete in-process check from the repository root, without opening any server port:

```bash
.venv/bin/python scripts/check_real_world_workflows.py
```

This creates temporary databases and verifies HTTP responses, persisted traces, UI APIs,
query/error assertions, SQL baseline/JUnit output, store audit and export-preview commands,
SQL compilation/snapshots, a separate worker process, secondary read-only browsing and a
production-preset capture-policy smoke test. It does not validate a real SQL Server,
a Celery broker, multiple production workers or every deployment's filesystem.

For manual UI exploration, keep the development app from the setup above running on
port **8012**, then open [the workflow index](http://127.0.0.1:8012/workflows). No large
seed is needed for these routes:

| Action | What to inspect |
|---|---|
| `GET /workflows/handled-sql` | HTTP 200 with a caught SQL failure; open its `sql_error` issue and the failed SQL span |
| `GET /workflows/dynamic-engine` | A SQLAlchemy engine created inside the request; SQL is observed with `instrument_engines="all"` |
| `POST /workflows/private` | The application returns the invented payload, while Redline masks the stored request/response JSON, SQL parameters and captured log message |
| `POST /workflows/job` with `{"fail_summary": true}` | A TASK recording with write/summary stages, row-outcome counts, an exception and observed commit-before-failure evidence |
| `POST /workflows/job` with `{"fail_summary": false}` | A completed TASK recording with the same isolated write workflow |

The job writes only to its own in-memory SQLite database; it never commits to `shop.db`.
The failure route intentionally names an absent table and returns a degraded response
without creating that table. The following commands send invented examples:

```bash
curl http://127.0.0.1:8012/workflows/handled-sql \
  -H 'X-Request-ID: demo-request-1' -H 'X-Demo-User-ID: 73'
curl http://127.0.0.1:8012/workflows/dynamic-engine
curl -X POST http://127.0.0.1:8012/workflows/private \
  -H 'Content-Type: application/json' -d '{"notes":"invented private sample"}'
curl -X POST http://127.0.0.1:8012/workflows/job \
  -H 'Content-Type: application/json' -H 'X-Request-ID: demo-job-request' \
  -d '{"fail_summary":true}'
```

`X-Demo-User-ID` is deliberately a demo identifier, **not authentication**. A real project
must resolve an authenticated identifier. Every response gets an X-Request-ID; incoming
IDs are available when a nested job starts. Search Requests for `demo-request-1`, then
open [Jobs](http://127.0.0.1:8012/_redline/#/jobs) and follow each task to its evidence.
The job response also contains a direct recording URL. The privacy route masks Redline’s
stored copy; the application’s own terminal logger still emits its invented sample.
Redline does not sanitize an application’s separate logging pipeline.

Open [Analysis](http://127.0.0.1:8012/_redline/#/analysis) for the SQL checker, redaction
preview and recording-health panels. Paste `examples/flask/sql_samples.py` into the SQL
checker with dialect `mssql`, or run this from the repository root:

```bash
.venv/bin/python -m redline_core.cli check-sql examples/flask/sql_samples.py --dialect mssql
```

The file is intentionally bad, so findings and exit 1 are expected. It demonstrates
comment binds, bare OUTPUT, an enabled option without a reset and a missing literal
parameter binding. Its functions are static fixtures and must not be executed. Offline
lint does not establish that a target table has triggers or that its schema is correct.
For online compilation/snapshot examples against a disposable SQLite database, run the
in-process harness; use the detailed [workflow guide](../../docs/real-world-workflows.md)
for an explicitly selected isolated development DSN.

### Separate worker store

A broker-free generic worker helper can record an actual job from another Python process.
Choose a private trace path outside the checkout, run the worker, then restart the web
app with that existing store configured for browsing:

```bash
# From examples/flask; the job's business-data database is always in memory.
uv run --project ../.. python worker_demo.py --store /tmp/redline-demo-worker.db
# Stop only your own web server with Ctrl-C, then restart it:
SHOP_WORKER_STORE=/tmp/redline-demo-worker.db \
  uv run --project ../.. flask --app shop.main run --port 8012
```

Use `--succeed` to record a successful worker job. The web UI opens the worker trace file
read-only. Request/job/profile browsing includes secondary traces; history, aggregate
statistics, live events and mutations still use the primary store. Secondary SQL cannot
be explained through the web process's unrelated database binding. Worker arguments,
results and row identifiers are not captured by this example. This generic helper does
not claim to exercise Celery's broker or prefork behavior.

### Production-preset smoke check

The harness also creates a separate Flask application with `preset="production"`, a
throwaway token and its own temporary trace store. It verifies that bodies, locals, log
capture, error details and SQL values are disabled. A runtime-generated sentinel is sent
through SQL parameters, a response, a warning and an uncaught error; it must be absent
from every stored table. The error type remains available. Replay, load, checks, EXPLAIN
and profiling features are off, and authenticated attempts to run active actions or delete
traces are refused. The application logger still writes its own messages to the terminal;
Redline settings govern the recorded copy. This is a capture-policy regression check
using harmless SELECTs, not permission to send production traffic or
proof of production deployment readiness. Validate the actual driver, worker topology,
redaction policy and storage filesystem separately before deploying.

### Explicit MySQL and SQL Server driver probes

`driver_probe.py` is an **opt-in disposable-database integration probe**, not a production
health check. It requires `--disposable`, reads the DSN only from the named environment
variable and refuses URL database names outside `redline_test_*`. After connecting it also
runs `SELECT DATABASE()` or `SELECT DB_NAME()` and refuses to proceed unless the server's
actual selected database has that prefix. That check happens before any temporary-table
operation. A naming convention cannot prove that data is disposable; the operator must
choose the correct isolated target.

| Target | Probe driver | What the result establishes |
|---|---|---|
| MySQL | SQLAlchemy `mysql+pymysql` | Sync driver execution/error recording and optional estimated plan behavior on the selected test server |
| SQL Server | SQLAlchemy `mssql+pyodbc` with an installed Microsoft ODBC driver | Sync driver execution/error recording and optional SHOWPLAN behavior on the selected test server |
| Flask/FastAPI applications | Reuse the corresponding SQLAlchemy driver | The probe does not replace request/stream/thread/deployment tests in that application |
| Django applications | Its configured Django database backend | This SQLAlchemy probe does **not** validate the Django backend; run framework-native application smoke tests separately |
| Async drivers or queue workers | Not exercised by this probe | Test the actual async driver/broker/prefork topology separately |

Install the appropriate optional driver in the test environment; `pyodbc` also requires
the operating-system ODBC driver. Use a dedicated test login and trusted server certificate.
Keep credentials in the environment/secret store, not source files. For example, after
setting one DSN securely:

```bash
# From examples/flask. These variables are read, never printed by the probe:
# REDLINE_DEMO_MYSQL_DSN uses mysql+pymysql://.../redline_test_shop
# REDLINE_DEMO_MSSQL_DSN uses mssql+pyodbc://.../redline_test_shop?driver=...
uv run --project ../.. python driver_probe.py --vendor mysql --disposable
uv run --project ../.. python driver_probe.py --vendor mssql --disposable
```

The probe uses only a session-scoped temporary table (`CREATE TEMPORARY TABLE` on MySQL,
`#redline_probe` on SQL Server), rolls back, and disposes its owned physical connection.
It deliberately attempts a missing-column SELECT and verifies that a `schema`-category
SQL error was recorded with the correct vendor. It never changes a persistent application
table. Add `--plans` only to request a constant `SELECT 1` estimated plan; SQL Server needs
SHOWPLAN permission. A requested but unavailable plan returns exit 1 rather than claiming
success. Exit 2 is a safety/configuration refusal.

By default the trace store is temporary. To inspect the sanitized recording afterward,
add `--store /tmp/redline-driver-traces.db`, then configure that file as a read-only
secondary store in the development web UI. Active database inspection of secondary
recordings remains refused because its database binding is not established by the web
process. The probe never prints a DSN or driver free-text error, and a persistent trace
store uses production capture restrictions.

The in-process harness tests the consent/URL/actual-database refusal paths using fakes;
it does not connect to an external server. A passing fake guard test or SQLite demo is
not a passing MySQL/SQL Server integration run. Retain the real probe result for each
actual driver/server combination before declaring that deployment verified.

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
