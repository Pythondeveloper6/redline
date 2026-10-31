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
cookie_secure = true
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
instead of committing it to a file. Use a randomly generated signing key of at least
32 characters and a strong token of at least 24 characters. Length checks do not verify
randomness. Different codebases and
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

Run `redline doctor` **inside the actual container**, with the same mount and working
directory as the app. Its `storage WAL` check creates a throwaway database beside the store
and requires `journal_mode=wal` to work; its `filesystem` check warns on Linux mount types
commonly used by network/virtual shares. The check does not write to the configured store.
The test suite also writes to one file from two independent OS processes, but only your
deployment's volumes and process model prove your topology.

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

The complete Compose baseline passed on 2026-10-29 (run
`20261029T052447Z-caaef5f7`): 2,120 Python tests, zero failures/errors/skips, plus frontend,
demo, docs and packaging checks. The committed validation record is
`IMPLEMENTATION_STATUS.md` in the repository. Main CI and the actual production topology
still require their own evidence; an environment restriction is never a passing assertion.

| Area | Confirmed baseline | Still required for the target deployment |
|---|---|---|
| Capture boundaries | Cycle/Unicode/custom-object/size-limit and safe-fallback tests | Representative memory/latency at intended concurrency and payload sizes |
| Privacy and storage loss | Sentinels, effective-write probes, deterministic queue/writer failures and worker shutdown | Actual disk exhaustion, permissions, storage volume and sustained concurrency |
| Framework SQL recording | Live Django backend and Flask/FastAPI SQLAlchemy sync/async cases on MySQL, SQL Server and PostgreSQL | Exact deployed backend/driver versions, reconnect behavior and deployment topology |
| SQL Server tooling | Live estimated/actual plans, catalogs, compilation nonexecution and setting cleanup | Least-privilege production-like permissions; Django SQL Server EXPLAIN remains unsupported |
| Jobs | Redis-delivered Celery prefork success/failure/retry, correlation and shutdown-drain checks | Actual broker/deployment, restart and expected worker concurrency |
| Browser | Real Chromium Jobs/detail, SQL checker, privacy and mobile overflow checks | Full accessibility review and target-browser coverage |

The Compose baseline uses Python 3.13, Django 5.2 and the driver versions recorded in the
run summary. It does not certify all supported versions; the broader matrix runs in main
CI. SQL Server's supported container qualification requires native Linux amd64, even if
an emulated local run succeeds. Record server/process versions with deployment results.

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


## Enforce the recording-only configuration before deployment

From **inside the deployment container**, with the same config/environment/working
location as the app, run:

```bash
redline doctor --production --strict --json
```

`--production` validates the configuration you supplied; it does not switch presets or
make an unsafe configuration safe. `--strict` returns nonzero for warnings as well as
failures. This gate checks recording-only settings, private persistent storage and
retention, authenticated operator access, explicit secure cookies and restricted proxy
trust. It keeps provider/auth/storage probes from ordinary `doctor`. Application-defined
access hooks remain warnings because a CLI cannot prove their authorization behavior.
Use a token-backed configuration for an automated strict gate, or review the hook and its
warning explicitly. Never print real token/signing values in deployment logs.

If settings are overridden in application code, this CLI cannot see those overrides.
Confirm the worker's effective configuration and test its access boundary as well. A
successful configuration gate does not validate TLS, real database drivers, disk failure
behavior, concurrency or the contents of actual captured data.

## Monitor profiler health independently

Poll the protected `GET <mount>/api/health` endpoint with your operator credentials.
A healthy supported process returns HTTP 200. Degraded, closed, disabled or unavailable
recording health returns 503. It reports:

- queue depth/capacity and writer-started/alive state;
- completed-recording capture/sink failures separately from storage loss;
- cumulative dropped items by reason, writer/flush/shutdown failures and written count;
- shutdown completion and the serving process PID.

Do **not** use this endpoint as the application's load-balancer readiness probe. A profiler
problem should alert operators without taking a healthy application out of service.
The snapshot does not query the initialized trace database, so it remains useful while
SQLite queries are blocked or the connection has closed. It is authenticated, contains
no trace payload, storage path or exception message, and uses the same access policy as
the other operator endpoints. The UI's Recording health panel shows the same information.

Counters are process-local and cumulative, including historical failures; a degraded
status can persist after recovery. Monitor counter increases/rates and individual workers,
not just the last status response. Unsupported custom health providers are reported as
unknown. Queue depth includes markers and lifecycle work; written counts describe write
processing rather than an fsync/durability guarantee. Continue independent application
monitoring and inspect truncated evidence too.

## Verify graceful shutdown and failure behavior

The SQLite writer stops admission before its stop marker. `close(timeout=10)` reports
whether shutdown completed. If the deadline expires, queued items may be discarded and
counted, while an in-flight batch may still finish later. A live writer retains ownership
of its connection until it stops. This improves isolation; it does not make forced kills
lossless. Adapters still perform their normal flush/close lifecycle, so size the server's
grace period to include both and test it in the actual process topology.

Deterministic tests cover full queues, dead writers, injected write failure/recovery,
thread-start failure, failed logging and concurrent save/close accounting. Validate actual
volume permissions, disk exhaustion, worker restarts and intended concurrency in staging.
Read `flush()` success together with loss counters: barrier completion is not proof that
every trace persisted. Emergency rollback remains `enabled = false`, followed by normal
application restart/reconfiguration; no production traffic generator is enabled by this work.

The close timeout bounds admission/queue/join/lock waits. It is not a hard real-time
bound on operating-system filesystem calls. Let the process manager enforce the final
grace deadline and document the acceptable loss on forced termination. Worker processes
without a web endpoint can expose `recorder.operational_health()` and
`store.operational_health()` through their existing private metrics/logging integration;
the library does not start a monitoring server or aggregate those counters automatically.
