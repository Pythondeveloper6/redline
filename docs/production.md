# Production deployment

Redline's next release targets production recording across several codebases. Readiness
is established for a particular combination of framework, driver, process model, traffic
and storage; passing a SQLite unit test does not establish SQL Server or broker-worker
behavior. The release must retain its development status until the required deployment
checks below have evidence.

<a id="the-production-preset"></a>

## Start with recording and stored-data inspection

Use a separate configuration and private storage directory for **each application and
environment**. Do not point a profiler store at the application's database. For an initial
production canary, make active tools unavailable explicitly:

```toml
preset = "production"
access = "token"
token_file = "/run/secrets/redline-token"
# Supply REDLINE_SECRET_KEY separately through the deployment secret provider.
storage_path = "/var/lib/redline/myapp-production/{role}.db"
storage_role = "web"
release = "myapp-release-identifier"

sample_rate = 0.01
always_keep_errors = true
always_keep_slower_than_ms = 500
max_rows = 5000
max_age_days = 2
history_retention_days = 7
writer_queue_size = 64
max_spans_per_trace = 500
max_sql_bytes = 16384
max_span_data_bytes = 8192
max_tag_bytes = 2048
max_trace_bytes = 262144

capture_request_body = false
capture_response_body = false
capture_sql_params = false
capture_locals = false
capture_logs = false
capture_error_details = false
read_only = true
profile = "off"
badge = false
server_timing = false
load_from_ui = false
checks_from_ui = false
analysis_from_ui = false
explain_analyze = false

[features]
explain = false
profile = false
replay = false
replay_writes = false
load = false
checks = false
logs = false
locals = false
```

Supply `secret_key` through `REDLINE_SECRET_KEY` or the application's secret provider
instead of committing it to a file. Use a strong token of at least 24 characters. Different codebases and
environments should have different tokens/signing keys and storage access permissions.
A signing key is shared only among workers intentionally serving the same Redline UI.

The count/byte controls above match the production preset: queue `64`, span count `500`,
SQL text `16384`, span data `8192`, tag value `2048` and completed trace `262144` bytes.
The sample, trace/history retention and always-keep rules shown are canary choices.
The production preset is a starting point; explicit settings override it. Inspect the
resolved configuration with `redline config`, and run `redline doctor` in the deployment's
working directory. A configuration file alone is not evidence that the intended workers
loaded it. Confirm through the UI's effective configuration and recording health.

For Flask, FastAPI and Litestar, pass this settings mapping to the existing adapter. Django
maps the same settings from `REDLINE`, with its framework permission policy retained when
configured. An explicit native permission hook must authorize the intended operator, not
ordinary application users. Authentication details are in [Access control](access-control.md).

## Data minimization and privacy

Disabling SQL values withholds bound parameters, normalizes inline literals and removes
associated SQL source/fix snippets. This also makes EXPLAIN unavailable for those recordings.
`reads_only` is an explicit opt-in, not a privacy boundary: SELECT parameters can contain
personal data too.

Bodies, locals and logs are separate capture surfaces. Keep all three off for the first
production canary. Exception type and structured database codes are useful without treating
free-form exception messages as safe. Follow [Security](security.md) for the exact captured
surfaces and the current exception-detail controls. Profiles and route rules are pattern
matching, not a guarantee that arbitrary clinical notes, identifiers or secrets are absent.

Before rollout, use invented sentinel values in the actual application's headers, query
strings, routes, SQL, logs and exceptions. Inspect the stored trace, issue data, source
snippets and exported snapshot, then scan the disposable store. `redline audit-store`
reports bounded key-pattern candidates without printing values; it is not a complete
privacy audit and does not prove an existing store is clean. With
`capture_error_details = false`, Redline retains error types and structured database
codes without formatting exception messages or tracebacks; project exception locals
are also disabled. This preserves failure counts while reducing free-text exposure.

## Network and operator access

Expose the application normally, but place the Redline mount behind the intended operator
network/authentication boundary. Use TLS. Configure `trusted_proxies` with the actual proxy
peers; do not trust every address for convenience. Set `cookie_secure = true` when the
operator entrypoint is always HTTPS and the proxy is configured correctly.

`access = "local"` can fail behind a Docker bridge, or accidentally accept a same-host
proxy. Use the token/framework policy instead of switching to open access. Test unauthenticated,
wrong-token, insufficient-role and cross-origin requests against every worker.

Read-only API policy does not govern the application's own endpoints. Replay is a browser
request to that application, so disabling `features.replay` is part of recording-only mode.
Estimated plans and catalog inspection also contact a database and are separate from reading
stored traces; they remain off in the recipe. Load generation, refactor-check runs, source
scans, online compilation and actual plans belong in an isolated development environment.

<a id="process-models"></a>

## Storage and process models

Use local disk or a local-filesystem Docker named volume with private directory/file
permissions. Do not share a writable SQLite file through NFS/SMB or an arbitrary remote
filesystem. WAL being available is necessary, but does not prove the underlying volume is
safe under multiple containers. A writable store refuses a non-WAL result. Non-development
presets refuse an in-repository store unless explicitly overridden.

`{role}`, `{hostname}` and `{pid}` path placeholders help choose separate files. They are not
an application namespace automatically added to a shared store. Use an application/environment
prefix yourself. Files from unrelated codebases must not be mixed accidentally.

| Deployment | Intended storage arrangement | Validation needed |
|---|---|---|
| One application, one process | One private local file | Retention, shutdown, disk errors and normal traffic |
| Several workers on one host | Local shared file only after a concurrent-writer test, or per-worker files | Contention, busy timeout, pruning, loss and shutdown |
| Web and task workers | Explicit web/worker roles, preferably separate files | Worker initialization, fork behavior and correlation |
| Several replicas/hosts | A private local file per replica | Replica-local inspection and lifecycle; no network-file sharing |
| Several independent codebases | Separate processes, settings, credentials and store paths | No cross-application traces or operator access |

The SQLite writer starts lazily and has fork/PID handling, but validate the actual gunicorn,
uWSGI, ASGI or task-worker start model. Database engines must also follow the database
library's fork lifecycle. Redline's storage handling does not make an inherited application
connection pool safe.

Multiple applications in one interpreter need explicit per-application recorders and
integration tests. The default recorder and some integration hooks are process-wide; do not
assume that repeatedly calling `configure()` provides application isolation. Queue-independent
`job()` accepts an explicit recorder. Initialize Celery instrumentation in the intended worker
lifecycle and confirm actual broker delivery, retries and prefork behavior.

`extra_stores` provides read-only request/job/profile browsing from existing worker files.
It does not combine history, aggregates, live-tail events or mutations. Independent files
should use independent trace IDs. Duplicate IDs are marked in request/job listings;
inspection, profiles and single-ID deletion are refused rather than choosing another
store's evidence. Cross-file reads are not an atomic distributed snapshot. Secondary
EXPLAIN/catalog requests are refused because a web process cannot establish the worker's
database binding. This is not a distributed store.

<a id="sampling"></a>

## Capacity and failure behavior

Sampling is applied when a recording finishes. Unretained requests still pay the span,
fingerprint and callsite cost. A sample rate of 1% is not 1% of the instrumentation overhead.
Slow/error keep rules can retain much more than the nominal sample during an incident.

The writer queue is bounded and drops/counts traces rather than blocking request code when
full. SQL text, span data and tags have separate byte budgets, and the completed serialized
trace is checked against `max_trace_bytes` before saving. Budget enforcement may shorten
or discard evidence; keep truncation/drop notices visible and inspect preserved failure
counts. See [Settings](settings.md#capture-byte-budgets) for defaults and scope.

These controls do not establish an absolute process-memory or disk-size ceiling. Active
requests, application objects, analysis, serialization and writer batches also consume memory.
Measure representative large traces as well as the median request. Retention runs
periodically, so a configured row cap is not an instantaneous disk-size guarantee.

Measure and monitor:

- Application throughput, median/tail latency, CPU and memory with recording disabled and enabled.
- Queue drops, writer failures, storage growth and time spent draining at shutdown.
- SQL-heavy requests, large results, errors, retries, streaming and concurrent jobs.
- Full disk, unwritable directories, SQLite contention and sudden process termination.

Use `GET api/meta` recording/drop indicators and the `redline` logger. SQLite's
`storage.dropped` includes queue overflow, closed-store loss and failed writer batches;
it does not distinguish all those causes. `flush()` signals that queued work was processed,
not that every trace persisted successfully: check loss counters and verify expected
recordings. Recorder-side capture/sink failures must also be checked in operational
logs and independent monitoring. Do not treat sampled
endpoint percentiles as an unbiased application SLO. Keep application monitoring independent
so a profiler failure cannot hide an application incident.

FastAPI/Litestar shutdown hooks and Flask/Django exit hooks flush pending traces. Give the
process enough graceful-shutdown time and verify that the hook runs in the deployment.
A forced kill can lose queued traces; state the acceptable loss window before rollout.

## Production release gate

The implementation includes local unit, adapter and fixture checks. SQL Server driver,
Celery broker/prefork, cross-container volume, sustained-load and real browser checks must
be recorded separately. The local validation report is `IMPLEMENTATION_STATUS.md` in the
repository. An environment restriction is a blocked test, not a passing product assertion.

The current local evidence and remaining qualification have different scopes:

| Area | Confirmed locally | Still required before deployment qualification |
|---|---|---|
| Capture boundaries | Deterministic cycle/Unicode/custom-object/size-limit tests and safe minimal-fallback tests | Representative memory/latency at intended request concurrency and payload sizes |
| Runtime privacy and storage loss | Local sentinel privacy checks and deterministic queue/writer-failure checks | Real disk-full/permissions, shared-volume and shutdown behavior in the target environment |
| Framework SQL recording | Native Django SQLite and SQLAlchemy adapter tests, including caught failures and capture-off row counts | Selected MySQL and SQL Server backend/DBAPI versions, reconnects, failed statements and session behavior |
| SQL Server tooling | Showplan/parser fixtures and fake-driver protocol/cleanup checks | Live isolated driver/server tests; Django SQL Server EXPLAIN remains unsupported |
| Jobs | Direct job and eager Celery execution tests | Actual broker delivery, retries, prefork/restart and worker lifecycle |

Local checks do not establish the unvalidated live behavior in the final column. Record
framework, database/server, backend/driver and process versions with the results.

A production recommendation requires evidence for each target deployment:

1. Exact supported Python/framework/driver/queue versions and the process/storage topology.
2. Unit and adapter suites, installed-wheel tests, UI checks and documentation build passing.
3. Live driver tests for failed statements, reconnects, parameter styles and session cleanup;
   SQL Server plan/compile tests only against an isolated database with appropriate permissions.
4. Broker-worker success/failure/retry, prefork/restart, correlation and graceful shutdown tests.
5. Concurrent writers, retention, backpressure and fault-injection tests under expected traffic.
6. Sentinel privacy checks across stored traces, issues, callsites, logs and exports.
7. A low-traffic canary with an immediate `enabled = false` rollback and independent monitoring.

The [full-stack Compose test project](testing.md) supplies a repeatable baseline for
these checks. Its current report must be green; merely adding its test files is not
qualification. Validate deployment-specific conditions as well.

Once these pass, expand one codebase at a time. Keep active tools disabled on production even
when the development demo exercises them. See [Real-world workflows](real-world-workflows.md)
for individual feature setup and [Performance](performance.md) for measurement limitations.
