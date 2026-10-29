# Database failures, jobs and shared environments

These tools are part of the **0.4.0a1 development release**. They extend the existing
request profiler; they do not replace application tests, a database migration system or
production error monitoring. Recording does not start an exporter. Active load/check,
online compilation and export commands contact only their explicitly selected targets.
Production rollout is gated by the deployment-specific evidence described in
[Production](production.md).

## SQL failures that the application catches

An instrumented SQLAlchemy statement that raises now leaves a SQL span with its error
category, SQLSTATE/native code when available, duration and source location. A handler
may catch the exception and return HTTP 200; its recording still has a `sql_error` issue.
Failed statements are counted as attempted queries. A later successful statement does
not inherit the error. Sampling's error keep rule includes observed SQL failures, and a
span-limit overflow keeps a failure count with an incomplete-evidence finding.

Open **Requests → a recording → Issues**, then open the affected SQL span. The drawer
shows the driver error, raw rowcount and available connection evidence. Request search
also searches the configured request ID, user ID and job ID tags. Existing tag filtering
works through `GET api/traces?tag=request_id:example-id`.

`application_error` groups unhandled request/job exceptions by type and source location.
The existing Issues and release-history views can therefore compare those occurrences
across releases. Handled application exceptions outside database instrumentation are not
inferred automatically.

## Engine and connection coverage

Explicitly registered SQLAlchemy engines work as before. For engines created dynamically:

```python
settings = {"instrument_engines": "all"}
```

This installs process-level discovery hooks and lazily instruments engines used by an
active opted-in recorder. The mode must be enabled in each worker process that needs it.
`explicit` is the default; Flask-SQLAlchemy discovery continues to work in the Flask
adapter. `flask_sqlalchemy` is also accepted as a descriptive compatibility setting.
No database URL, password or connection string is used as an alias.

SQL evidence can include physical-connection age, observed statement number, pool recycle
and pre-ping settings, and bounded per-process hourly observations. `connection_churn`
requires at least 100 observed creations with recycling configured;
`fresh_connection_failure` requires three observed first-statement disconnects within five
seconds of creation in an hour. These are correlations, not diagnoses. Initial connections
or driver bootstrap statements made before observation can be unknown. Pool invalidation
is a separate `connection_failure`; it does not invent an executed SQL statement.
Per-process hourly gauges are also flushed to the store's `lifecycle_hourly` history as
max-per-hour rows, including for traces that sampling drops. `GET api/lifecycle` returns
them for dashboards. They are observed gauges, not a sum across processes, and carry no DSN
or credentials.

A raw rowcount of `-1` means unavailable. `dml_rowcount_unknown` on SQL Server can suggest
reviewing NOCOUNT, OUTPUT/returning results, batches and driver behavior; it does not prove
NOCOUNT is enabled or that a write failed. A 404 alone never establishes that cause.

`sql_retry_storm` groups attempts of the same statement and callsite inside one trace. It
requires at least three attempts after a transient, disconnect, deadlock or timeout failure
(the threshold is configurable). It reports the categories and total time; it never retries
or resets anything.

Direct session switches are observed without issuing probes or resets. SQL Server's
NOCOUNT/XACT_ABORT/ANSI/ARITHABORT switches, transaction isolation level,
`IDENTITY_INSERT`, language/date/timeout/deadlock settings and `sp_set_session_context`
keys are recognized alongside selected PostgreSQL `SET`/`set_config` options and MySQL
session/user variables. A checkin finding requires a configured expected value. The
default expectation is only `NOCOUNT = OFF`, and SQL Server context keys are declared
explicitly:

```python
settings = {
    "session_expected": {"NOCOUNT": "OFF", "XACT_ABORT": "OFF"},
    "session_expected_context": ["person_id", "endpoint", "request_id"],
}
```

Expected context keys are quiet when set with `@read_only = 1`; a leftover key set with
`@read_only = 0`, or a key outside the list, is reported as a lower-severity finding.
Values are stored as digests, never plaintext.

Generic string-valued options are stored as digests; expected values use the same SQL
value spelling. `SET LOCAL` is not treated as a persistent change, and observed PostgreSQL
transaction rollbacks restore tracked state. Stored procedures, raw driver cursors and
unobserved resets are unknown. Checkin findings require an active recording at checkin;
a connection returned after recording ends cannot retroactively amend a stored trace.
No automatic connection reset or query retry is performed.

## SQL Server plans and catalogs

SQLAlchemy's `mssql` dialect is recorded as `mssql`. On-demand estimated plans use a
dedicated connection, separate SHOWPLAN ON/query/OFF calls and rollback. If restoring the
session fails, the connection is invalidated. Permission failures are returned as EXPLAIN
errors; the database login needs SHOWPLAN and permissions on referenced objects.

The parser shows operators, estimated costs/rows, plan-affecting conversions and
missing-index candidates. Suggestions never create indexes. Catalog inspection supports
one simple `schema.table` reference, selected large column types, existing index columns
and visible enabled triggers. Unqualified names use `dbo`; workload catalog verification
is limited to `dbo`. Missing metadata permissions produce a limitation, not a clean bill
of health.

Actual XML plans are opt-in through `explain_analyze = true`, require a synchronous driver
with `nextset()`, execute the read query and roll back the transaction. Result consumption
is bounded to 100,000 rows and 5 MB of XML. STATISTICS IO summaries recognize English
messages only. Rollback cannot undo external side effects of database functions. Async
SQL Server actual-plan collection is not implemented. Estimated/actual driver behavior
still requires validation against a live SQL Server before claiming production support;
XML fixtures and fake executors are not that validation.

## SQL checks without importing an application

```bash
redline check-sql app/ services/ --dialect mssql
redline check-sql app/ --dialect mssql --json
redline check-sql app/ --dialect mssql --junit sql-results.xml
redline check-sql app/ --dialect mssql --write-baseline sql-baseline.json
redline check-sql app/ --dialect mssql --baseline sql-baseline.json
```

The AST extractor recognizes `text()`/`sqlalchemy.text()`, literal execute calls, module
constants and supported constant-only f-strings. Dynamic expressions, syntax errors and
scan limits are visible as unresolved input. Source is capped at 200 KB per file; a scan
is capped at 200 files, 2 MB and 20,000 filesystem entries. Dependencies and generated
folders are excluded, and symlinks are not followed. Local bindings that shadow constants
are not replaced with guesses.

Rules include bind-like text in comments, bare SQL Server OUTPUT, switches without a
reset in the same statement, INSERT SELECT star projections, and missing binds when the
parameter mapping is literal. They are conservative candidates: a reset in a separate
finally block is not currently proven. Bare OUTPUT does not prove a trigger exists.
Use a reviewed baseline or `-- redline: ignore[rule-name]` for accepted candidates.

Exit codes are 0 for no new findings, 1 for new findings, and 2 for invalid/unresolved
input. An empty finding list does not prove schema correctness.

**Analysis → SQL checker** uses the same offline engine on pasted source. It honors
`analysis_from_ui` and read-only policy. It accepts no filesystem root, DSN or online SQL.

## Explicit online compilation and schema drift

```bash
# Explicit Python files, isolated development database, optional SQLAlchemy dependency:
redline check-sql app/queries.py --dialect mssql --dsn "$DEV_DATABASE_DSN"
redline check-sql app/queries.py --dsn "$DEV_DATABASE_DSN" \
  --schema-snapshot schema-snapshot.json --json
```

Run the snapshot command after a migration to compare the default schema's column names,
types and nullability, plus SQL Server trigger and routine-parameter metadata. It updates
the local snapshot and, when a previous snapshot exists, recompiles only extracted
statements that reference an added, removed or changed table (including a table whose
triggers changed). An unchanged schema skips statement compilation; routine-parameter
changes recompile everything because those dependencies are not statically resolved. It
does not automatically run migrations or certify that all application SQL was extracted.
Snapshots are capped at 200 tables, 500 columns per table, 1000 triggers and 2000 routine
parameters.

Compilation uses SQLite EXPLAIN, PostgreSQL EXPLAIN without ANALYZE, MySQL server-side
PREPARE/DEALLOCATE, or SQL Server NOEXEC with cleanup on a dedicated connection. SQL
Server parameter types are read from `sp_describe_undeclared_parameters` and declared in
the NOEXEC batch; a single read statement also gets `sp_describe_first_result_set` so a
literal `row["col"]` read that the SELECT no longer returns is reported as a low-confidence
candidate. Only supported single statements are accepted. Parameter values are NULL
placeholders; type-dependent validity, temporary objects, stored procedures and runtime
permissions may remain unresolved. In particular, SQL Server NOEXEC supports deferred name
resolution. Use an isolated development login/database: server compilation can involve
extensions or planner functions. Driver messages and DSNs are not included in
machine-readable results.

### Watching a development schema

```bash
# One check (exit 1 on drift or a compile error), suitable for cron:
redline schema-watch app/queries.py --dsn "$DEV_DATABASE_DSN" \
  --snapshot schema-snapshot.json

# Poll every five minutes and raise a schema_drift Issue in the UI:
redline schema-watch app/queries.py --dsn "$DEV_DATABASE_DSN" \
  --snapshot schema-snapshot.json --interval 300 --store .redline/redline.db
```

The first run writes the baseline without alerting. Later runs compile only statements that
reference affected tables, and `--full` forces a complete pass. `--store` writes one
synthetic `TASK` trace carrying a `schema_drift` issue (affected table and routine names
only; no rows or SQL text). The command can also run from a Celery beat task or a CI job.
Exit `0` means no drift, `1` means drift or compile errors, and `2` means the watch could
not run (driver, DSN or permissions).

## Safer shared-development recording

```toml
preset = "shared-dev"
access = "token"
token_file = "/run/secrets/redline-token"
storage_path = "/var/lib/redline/{role}.db"
storage_role = "web"
instrument_engines = "all"

[redact]
profiles = ["credentials", "pii", "health", "payment"]
routes = { "POST /private/*" = "values", "PUT /medical/*" = "body" }
```

Generate a strong token and provide it through a private file or environment; do not put
it in a repository. The preset disables bodies, locals, SQL values, replay, load/check/UI
analysis and enables read-only UI behavior. Captured logs and free-text exception details are disabled; review
application logging separately because Redline does not suppress the application's own handlers. Explicit settings override preset defaults.

`capture_sql_params` accepts true, false or `reads_only`. False removes SQL literals and
parameters, omits source snippets associated with those SQL spans, withholds SQL error
messages that might echo values, and disables EXPLAIN for that recording. `reads_only`
retains values only for conservatively recognized read queries. SELECT parameters can
contain personal data too; shared-dev defaults to false.

Profiles are key/table-pattern starting points, not compliance guarantees. Add
`redact_keys`/`redact_tables` for application-specific patterns. Route rules match
`METHOD route-template` with shell-style wildcards. `values` preserves JSON structure
while masking values; `body` removes body contents. Both also mask SQL values, exception
messages/locals, captured log messages and query values before persistence. Free-form
text outside a known key cannot reliably be classified. Profile names are `pii`, `health`,
`credentials` and `payment`.

**Analysis → Redaction preview** uses current key/profile/route rules with invented JSON
samples. It sends the sample to this instance but does not save a trace. Capture switches
are separate from redaction. `redline audit-store --store /path/to/redline.db` reports
bounded possible unmasked-key counts without printing stored values.

`capture_loggers` and `ignore_loggers` accept logger-name glob patterns. With
`features.replay_writes = false`, the browser only offers GET/HEAD replay. This is a UI
control, not authorization for the application's own write endpoints.

## Request and user correlation

Configured request/response ID headers become a `request_id` tag; response IDs take
precedence at completion. Incoming IDs are available when a job is published. IDs are
labels, never authorization credentials. The default headers are X-Request-ID,
X-Correlation-ID and `traceparent`. A valid W3C `traceparent` also fills the OTel trace and
parent-span ids, and published jobs carry a child `traceparent`.

```python
from redline_core import tag
from redline_core.users import from_attr

settings = {"user_resolver": from_attr("state.user_id")}  # ASGI request state
# Django: from_attr("user.pk"); Flask: from_attr("person_id")
tag("operation", "daily-summary")  # no-op outside recording
```

Resolvers are synchronous Python callables invoked late in request handling. Return only
an identifier string/integer, not a name or e-mail. Errors are recorded as a resolver-error
tag and do not break responses.

## Jobs, stages and row outcomes

```python
from redline_core import Recorder, Settings, job, stage, row_outcome
from redline_core.storage import SQLiteStore

settings = Settings(storage_path="/var/lib/redline/worker.db", capture_locals=False)
store = SQLiteStore.from_settings(settings)
recorder = Recorder(settings, store)
with job("daily-import", recorder=recorder, task_id="run-123"):
    with stage("write"):
        row_outcome("succeeded")
    with stage("summary"):
        pass
store.flush()
```

A job is a TASK recording, searchable by its name/id and visible on **Jobs**. Stage spans,
SQL, outcomes and exception evidence use existing recording/issue views. Row outcomes are
bounded counters grouped by a digest of the reason; row IDs and samples are not stored.
An observed commit before a later failure is called out without claiming the whole job
completed. Nested jobs restore the outer recording context.

For Celery, install Celery in the worker environment and configure a recorder/store in
that worker process:

```python
from redline_core.contrib.celery import instrument_celery
integration = instrument_celery(celery_app, recorder)
# integration.uninstall() removes this instance's signal receivers.
```

Success, failure and retry outcomes are recorded. Publication can carry the current
request/parent trace ID. Arguments and results are not captured. Eager-mode tests require
no broker; prefork/broker behavior must be tested in the deployment. A task revoked before
it starts has no recording. RQ and Dramatiq integrations are not implemented.

## Storage and mixed frameworks

Use private storage outside the repository, preferably a Docker named volume on a local
filesystem. Non-dev presets refuse an in-repository path unless
`allow_storage_in_repo = true`. Dev warns, and a `.redline` directory is ignored locally.
`{role}`, `{hostname}` and `{pid}` placeholders are supported; they do not replace a
filesystem/WAL compatibility check. SQLite remains unsuitable for arbitrary network shares.

Set `extra_stores` to existing worker store files to browse their request/job traces from
the web UI. Secondary files are opened read-only; they contribute browsing/profile lookup,
not history, aggregates, live-tail events or mutations. Files should have independent
trace IDs. Duplicate IDs are marked in lists; inspection, profiles and single-ID deletion
are unavailable for those IDs. Independent file reads are not an atomic snapshot.
EXPLAIN/catalog inspection of secondary traces is refused to avoid running a worker's SQL
through an unrelated web-process database binding.

For a Flask application mounted inside FastAPI, use the outer recorder/store:

```python
flask_redline.Redline(flask_app, recorder=outer_redline.recorder, mode="nested")
```

An active outer recording receives Flask route/view spans and errors rather than a second
trace. The adapter test uses Starlette WSGIMiddleware and its copied context. It does not
claim equivalent behavior for every WSGI/ASGI bridge.

## Tests, import checks and explicit export

The pytest marker now defaults to rejecting SQL errors. It also accepts
`no_session_leaks`, `max_round_trips`, and existing query/outbound/issue budgets.
`no_sql_errors=False` is an explicit opt-out. `--redline-all` applies SQL-error assertions
to every test; a test that records nothing fails. Direct services can use
`recorder.recording()` inside `redline_capture()`. Truncation remains a failure for
assertions that require complete evidence.

`redline check-imports module:attribute` explicitly imports a development target in a
subprocess and loads registered task modules. It executes module import code; run it only
in the intended development environment. Lazy imports and task branches are not executed.
Use Ruff and application tests directly for Python correctness; Redline is not a linter.

```bash
redline export --store /path/to/redline.db             # OTLP JSON preview only
redline export --store /path/to/redline.db \
  --endpoint http://127.0.0.1:4318/v1/traces --send      # explicit span transmission
redline export --store /path/to/redline.db \
  --endpoint http://127.0.0.1:4318/v1/traces \
  --logs-endpoint http://127.0.0.1:4318/v1/logs --send
```

The exporter sends a root recording span plus sanitized SQL spans using `db.system`,
`db.operation`, `db.statement` (the normalized fingerprint unless `--include-sql` is
explicit), `error.type` and selected numeric/status attributes. It also builds OTLP log
records for recorded issues. It omits bodies, parameter values, arbitrary tags and error
messages. A valid incoming `traceparent` supplies the exported trace id and parent span;
otherwise ids are derived from the recording id. It is not a production batching/retry
pipeline, and no exporter runs automatically during recording.

Routine CI and docs deployment run only on pushes to main, including merges. Package
publication follows a successful CI run on main only when the repository variable
`REDLINE_RELEASE_VERSION` opts in to the exact package version. The existing PyPI
environment approvals still apply. Tags and manual dispatch do not publish this release.
Local merges do not trigger remote jobs.


## Production validation targets

The production targets are Flask, FastAPI and Django with MySQL and SQL Server. Support
must be verified with the actual dialect/DBAPI and process model, not inferred from a
shared detector or SQLite demo.

| Combination | Required integration evidence |
|---|---|
| Flask + SQLAlchemy + MySQL | Handled errors, parameter style, counts, connection lifecycle, concurrency and shutdown |
| FastAPI + SQLAlchemy + MySQL | The same, with the actual sync/async driver and event-loop behavior |
| Django + MySQL backend | Native execute-wrapper failure metadata and row counts, transaction handling and framework middleware behavior |
| Flask/FastAPI + SQLAlchemy + SQL Server | pyodbc or the actual selected driver, code classification, session changes, reconnects and first-use failures |
| Django + SQL Server backend | The selected Django backend's vendor naming, native wrapper behavior and declared plan-executor capability |
| Celery worker, where used | Broker delivery, failure/retry signals, prefork/restart, SQL spans and correlation using explicit worker recorders |

The in-process demos exercise the common logic on disposable SQLite databases. Showplan
XML fixtures and fake cursor cleanup tests exercise parser/protocol behavior. Neither is
live SQL Server validation. Similarly, eager Celery execution does not prove broker or
prefork behavior. Keep those distinctions in the release validation report.

For production, record and inspect stored traffic with active tooling disabled. Do not
copy a demo's debug access, full sampling or active-job routes into a production codebase.
The detailed deployment and data-minimization recipe is on [Production](production.md).


### Native Django database evidence

Django's execute wrapper records caught database exceptions as failed SQL spans even
when the view returns HTTP 200. The trace retains a SQL failure count after its span
budget is exhausted, marking the resulting finding as incomplete evidence. Failed
statements cannot be explained. Native error codes are classified using the normalized
vendor; `microsoft`, `sql_server` and `sqlserver` identify SQL Server. Messages require
both `capture_error_details` and the SQL capture policy to allow them.

Fetch counting remains active when SQL parameter capture is disabled, so SQLite and
other cursors with unknown initial row counts can still report rows actually fetched.
Partially consumed results report only consumed rows. An uninspectable cursor leaves
rows unknown. Observation never consumes an `executemany` parameter generator.

Django's plan executor supports its existing PostgreSQL, MySQL and SQLite paths. The
Django SQL Server backend currently has **no estimated or actual plan executor**;
recording and structured SQL evidence do not imply EXPLAIN support. SQLAlchemy SQL
Server plan handling is a separate integration. Production disables plan execution by
default. Native SQL Server and MySQL driver/backend behavior still needs live validation
using the selected backend versions before rollout.


### Bounded capture in real workloads

Test oversized SQL, custom span data and tags as well as ordinary requests. Separate
`max_sql_bytes`, `max_span_data_bytes` and `max_tag_bytes` budgets limit captured values;
`max_trace_bytes` checks the completed serialized trace before save. Incomplete evidence
must be visible: preserved error counts do not reconstruct dropped SQL or parameters.
Include cycles, deep metadata, Unicode, uninspectable objects and a full writer queue in
the release checks. These limits complement privacy rules and deployment load tests.


The production preset uses tighter budgets than development: queue `64`, span count
`500`, SQL `16384`, span data `8192`, individual tags `2048` and completed trace `262144`
bytes. Use [the settings table](settings.md#capture-byte-budgets) for minimum values and
representation details. Incomplete SQL is not eligible for EXPLAIN; clients and tests must
keep the incomplete-evidence warning even when there are no retained error spans.
