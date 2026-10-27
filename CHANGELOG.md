# Changelog

All notable changes to the packages in this repository (`redline-core`, `django-redline`,
`fastapi-redline`, `flask-redline` and `litestar-redline`) are documented here. They are
versioned and released in lockstep.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
version is below `1.0` the public API may change between minor versions.

## Unreleased: 0.2.0a1

Frameworks, analysis, load testing and history. Everything in 0.1 keeps working: a 0.1
store is migrated in place when 0.2 first opens it, and every new field has a default.

### Added: frameworks

- `flask-redline`: `Redline(app, engine=...)` and `init_app`, Flask-SQLAlchemy engines
  (binds included) found automatically, `flask redline clear|prune|scan|scenario`.
- `litestar-redline`: `RedlinePlugin(settings, engine, path, guards, store)`, picks up
  `advanced-alchemy` engines.
- Django Ninja routes resolved to the real operation (view name and route template).
- All adapters: `Server-Timing` header, floating badge, Server-Sent Events `api/stream`,
  snapshot download, `X-Frame-Options: DENY` on Redline's own responses.

### Added: analysis

- New detectors: `blocking_in_async`, `large_result`, `unbounded_query`,
  `inefficient_count`, `slow_serializer`, `long_transaction`, `external_in_transaction`,
  `slow_external`, `duplicate_external`, `retry_storm`.
- New span kinds: `serializer`, `resolver`, `transaction`, `blocking`, `pool_wait`; SQL spans
  gain `rows`, `has_limit`, `in_transaction`.
- An event-loop lag monitor and watchdog that name the blocking call, active only while a
  request is in flight.
- Log capture per request (`capture_logs`, `log_level`, `max_logs_per_trace`) and exception
  frames with redacted locals (`capture_locals`).
- GraphQL: Strawberry and Graphene resolver spans, with N+1 reported against the resolver
  path.
- `redline_core.contrib`: SQLAlchemy (moved from `fastapi-redline`, which re-exports it),
  httpx, requests, redis-py, asyncpg, psycopg 3 and Marshmallow integrations.
- Database page: SQL search by fingerprint, a workload index advisor and unused-index
  detection (`GET api/sql`, `GET api/indexes`).
- Cache page (hit rate, unread writes, stampedes) and External calls page (latency,
  duplicates, retries) (`GET api/cache`, `GET api/external`).
- Static N+1 scan of templates and DRF serializers (`manage.py redline_scan`,
  `flask redline scan`).

### Added: load testing

- `redline load`: an open-model, coordinated-omission-corrected load generator running in
  separate processes; constant, ramp and step profiles; Poisson arrivals; `stop_on`; knee
  detection; `--fail-p95-ms` and `--fail-error-rate` for CI.
- `redline load init --from-openapi | --from-traces` to generate scenarios; Django and Flask
  scenario generators.
- Runs are stored with per-step histograms and shown on the Load page with a per-step
  bottleneck analysis (`GET api/runs`, `GET api/runs/{id}/bottlenecks`).
- Optional start-from-the-UI (`load_from_ui`, off by default; target is always the app).

### Added: history

- Hourly rollups with mergeable latency histograms, releases, an issue index that survives
  trace pruning, kept `history_retention_days` (default 90).
- Release stamping (`release` setting; `REDLINE_RELEASE`, common CI variables, or git).
- History page and `GET api/history`, `api/releases`, `api/history/compare` with
  regressed / improved / new / gone verdicts and new / resolved / persisting issues.

### Added: UI

- Command palette (`Ctrl-K`), keyboard shortcuts, Live mode, open-in-editor links
  (`editor` setting), copy as cURL / HTTPie, replay, compare two requests, Logs and Exception
  tabs, offline snapshots.

### Added: configuration and access control

- Presets `dev`, `staging` and `production`; `redline.toml` / `[tool.redline]`; `REDLINE_*`
  environment variables; layered precedence.
- One access policy for all adapters: `local`, `token`, `basic`, `framework`, `custom`,
  `open` (needs `REDLINE_ALLOW_OPEN=1`); `allowed_ips`, `allowed_hosts`, `trusted_proxies`,
  `read_only`, per-feature switches, `ui_enabled`, `mount_path`, login with a scoped cookie
  and a rate limit.
- `redline config`, `redline doctor`, `redline hash-password`; `GET api/config`,
  `POST api/login`, `POST api/logout`; a login screen and a Settings page.

### Changed

- Storage schema version 2 (migrated in place, backfilled from existing traces).
- The default `redact_keys` pattern matches `auth` / `authorization` only as whole words and
  also covers `private_key`.
- Documentation is published to GitHub Pages.
- The `redline` console script is installed with `redline-core`.

### Security

- New error codes for access and safety: `login_required`, `read_only`, `load_disabled`.
- Starting a load run from the UI accepts only paths, never a host.

## 0.1.0a1

Initial alpha: request profiling, N+1 detection with fix suggestions, EXPLAIN analysis,
flamegraphs, web UI for Django and FastAPI.

### Security

- FastAPI: the default `local_only` guard now also requires a loopback `Host` header
  (`localhost`, `*.localhost`, `127.x.x.x`, `[::1]`), closing a DNS-rebinding read of the
  `GET` endpoints. `loopback_guard(allowed_hosts=[...])` allows other names.
- Django: Redline's views call `request.get_host()`, so `ALLOWED_HOSTS` is enforced on them
  without `CommonMiddleware`.
- EXPLAIN: `is_explainable` now refuses every row-locking clause (`FOR SHARE`,
  `FOR NO KEY UPDATE`, `FOR KEY SHARE`, `LOCK IN SHARE MODE`), matches keywords
  case-insensitively, and re-reads the raw SQL pessimistically so quoting tricks
  (backslash strings, `$$`, MySQL `/*! */` and `--`) cannot hide a second statement.
- Both adapters send `X-Frame-Options: DENY` on the UI; the SPA only links `http(s)`
  suggestion URLs and URL-encodes trace ids.

### Changed

- FastAPI access/CSRF refusals and `HTTPException`s from auth dependencies now use the
  `{"error": {"code", "message"}}` shape (`unauthorized`, `forbidden`, `csrf_failed`).
- `max_rows = 0` now disables the row cap (it used to delete everything); `0` or `None`
  disable any of `max_rows`, `max_age_days` and the new `max_spans_per_trace`.
- `?_redline=` accepts `profile`, `1`, `true`, `yes` and `on` on both adapters (parsed in
  `redline_core.sampling.profile_requested`).
- A 4xx is never an error: Django's `Http404`/`PermissionDenied` are no longer recorded
  as the trace's `error` nor kept by `always_keep_errors`.
- FastAPI with `enabled=False` installs no middleware and no SQLAlchemy listeners.

### Added

- `max_spans_per_trace` (default 2000): spans beyond it are counted, not stored; counts and
  N+1/slow-query issues stay exact; the trace is marked `truncated` and the UI says so.
- Django `REDLINE["STORE"]`, the counterpart of FastAPI's `install(store=...)`.
- `STORAGE_PATH` / `PROJECT_ROOT` accept any `os.PathLike`.
- `SQLiteStore` is fork-safe (gunicorn `--preload`): its writer starts lazily per process.
- Measured overhead, storage size and writer throughput (`benchmarks/`).
