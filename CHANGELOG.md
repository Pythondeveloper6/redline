# Changelog

All notable changes to the packages in this repository (`redline-core`, `django-redline`,
`fastapi-redline`, `flask-redline` and `litestar-redline`) are documented here. They are
versioned and released in lockstep.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
version is below `1.0` the public API may change between minor versions.

## 0.2.0a1 (2026-10-27)

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
- Optional start-from-the-UI (`load_from_ui`, off by default). It sends only paths, to the
  `base_url` setting or a loopback / `allowed_hosts` address; capped at one hour, 5 000 rps
  and 200 steps; run statuses `running`, `done`, `stopped`, `failed`; stopping a run from any
  worker.
- `redline load --ui-url` sets the Redline UI address printed after a run (default from
  `mount_path`, else `/_redline`).
- The knee rule: the first complete step with p95 above 2x the first step's, an error rate
  above 1%, or an achieved rate below 90% of the target; partial steps never decide it.

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
- Layers: defaults, adapter, preset, file, environment, framework. A native hook (Django
  staff check or `PERMISSION`, FastAPI `dependencies=`, Flask `permission=`, Litestar
  `guards=`) implies `access = "framework"`. `REDLINE_*` values are parsed by type (bools,
  JSON or comma lists, `k=v` tables, `none`); an unknown variable or file key is an error
  with a "did you mean" hint.
- Settings: `preset`, `access`, `access_check`, `allowed_ips` (widens `local`, restricts the
  other policies), `allowed_hosts`, `trusted_proxies`, `token`, `token_file`, `basic_users`,
  `secret_key`, `read_only`, `features`, `ui_enabled`, `mount_path`, `session_ttl_s`,
  `max_login_attempts`, `base_url`, `trust_unix_socket`, `cookie_secure`.
- `redline config`, `redline doctor`, `redline hash-password`; `GET api/config`,
  `POST api/login` and `POST api/logout` (both answer `200` JSON), `X-Redline-UI: 1`; a
  login screen and a Settings page. Under `token` and `basic` the UI shell is public and
  every `api/` call needs credentials. Sessions need `secret_key` with several workers.
- Live mode resumes by `seq` (`after=` or `Last-Event-ID`); the stream closes after five
  minutes on ASGI and about 25 seconds on WSGI servers and the UI reconnects.
- `GET api/indexes` returns candidates with `kind`, `confidence`, `verified`, plus
  `already_indexed`, `explained`, `unused_error` and `cached`; cached for 30 seconds,
  `refresh=1` recomputes; unused indexes are PostgreSQL only.

### Changed

- **Behaviour changes to know about when upgrading from 0.1:**
  - FastAPI, Flask and Litestar: when `access` is not set anywhere (code, `redline.toml`,
    `REDLINE_ACCESS`) and the app is **not in debug mode** (`app.debug`), every Redline URL
    answers `404` and an error is logged at startup. A same-host reverse proxy that adds no
    `X-Forwarded-For` (`proxy_pass http://127.0.0.1:8000;`) used to make every visitor
    "local". Set `access` explicitly: `"token"`/`"basic"` on a server, or `"local"` to
    accept that risk on a development machine. In debug mode the `local` default still
    applies.
  - Under `local`, a unix-socket peer (no client address) is no longer local unless
    `trust_unix_socket = true`.
  - FastAPI `install(dependencies=[])` and Litestar `RedlinePlugin(guards=[])` are the same
    as not passing them (the normal default rules), no longer `access = "framework"` with
    nothing to check. A FastAPI dependency that *returns* `False` now refuses with `403`
    (raising `HTTPException` remains the normal way).
  - Django: the default `PERMISSION` is **active superusers** (was: active staff). Use
    `"PERMISSION": "django_redline.conf.staff_permission"` for the old rule.
  - `X-Redline-Profile: 1` / `?_redline=profile` are honoured only in debug mode or when
    the request carries valid Redline credentials (login cookie, `Authorization` header,
    or a client the `local` policy lets in); `framework` hooks cannot run there, so with
    that policy only debug mode forces profiling. The `production` preset sets
    `profile = "off"`.
  - `X-Redline-Run` tags a trace only when the run exists in the store and is running (or
    ended less than a minute ago); a run started with `redline load --no-store` (or into
    another store) no longer tags the app's traces.
  - UI-started load runs target the server's own listening port (ASGI `scope["server"]`,
    WSGI `SERVER_PORT`) on `127.0.0.1`, never the `Host` header; without a known port (a
    unix socket) the `base_url` setting is required.
  - Replay sends no cookies unless "Send my cookies" is ticked.
  - `server_timing` now defaults to `"auto"` (on in debug mode only). It used to be on
    whenever recording was; set `server_timing = True` to keep the old behaviour.
  - `Sec-Fetch-Site: same-site` is refused on non-GET requests, like `cross-site`.
  - Under `local`, a loopback peer that sends `X-Forwarded-For`, `X-Real-IP` or `Forwarded`
    without being in `trusted_proxies` is refused (`403 forbidden`), so a reverse proxy on
    the same host no longer makes every visitor local.
  - `POST api/runs` needs the `base_url` setting unless the server's listening port is known
    (`400 invalid_body` otherwise).
  - FastAPI's `install(prefix=)`, Flask's `url_prefix` and Litestar's `path` default to
    `None` (then `mount_path`, then `/_redline`).
  - Under `token` and `basic` the API needs credentials on every `api/` call.
  - `GET api/stream` events carry an `id` (the trace's `seq`) and the server closes the
    stream periodically.
  - Log capture uses a log-record factory and follows your loggers' levels; Redline never
    adds a root handler or changes a level, and `log_level` is an extra minimum.
  - Replay refuses paths that point at another origin.
- Storage schema version 3 (migrated in place from 0.1 or 2): `saved_seq` gives the live
  stream an insertion order; writes use `BEGIN IMMEDIATE`; requests that matched no route
  are filed as `(unmatched)` in the history.
- The default `redact_keys` pattern matches `auth` / `authorization` only as whole words and
  also covers `private_key`.
- Documentation is published to GitHub Pages.
- The `redline` console script is installed with `redline-core`.

### Security

- New error codes for access and safety: `login_required`, `invalid_credentials`,
  `read_only`, `load_disabled`, `feature_disabled`, `open_refused`, `too_many_requests`.
- Starting a load run from the UI accepts only paths, never a host, and a forged `Host`
  header can no longer aim it at another machine; UI-started runs are capped.
- Access control: constant-time token and PBKDF2 password checks, signed `HttpOnly`,
  `SameSite=Strict` session cookies, a per-IP rate limit on failed credentials, and
  `X-Forwarded-*` believed only from `trusted_proxies`.
- CSRF is enforced by one shared gate on every non-GET call, login and logout included.
- Redaction: passwords in URL userinfo (`user:password@host`) and secret query values are
  masked in outbound URLs, log messages and exception locals; locals are masked in
  containers to a depth of four and through the free-text rule; secrets in a stored load
  run's config are masked.
- `Server-Timing` is off outside debug mode by default, since it is a side channel.
- An unset `access` no longer exposes Redline behind a same-host reverse proxy: outside
  debug mode the UI and API answer `404` until `access` is set (FastAPI, Flask, Litestar);
  unix-socket peers are not local unless `trust_unix_socket`. `redline doctor` warns.
- Empty `dependencies=[]` / `guards=[]` no longer turn the gate into an unchecked
  `framework` policy; a FastAPI dependency returning `False` refuses.
- Anonymous clients can no longer force profiling and storage with `X-Redline-Profile` /
  `?_redline=profile`, nor attach traffic to a load run with a made-up `X-Redline-Run`.
- A load run started from the UI can no longer be aimed at another loopback service
  through the `Host` header.
- Replay no longer sends the viewer's cookies by default (a recorded request replayed with
  the viewer's session would be a stored CSRF).
- The store directory Redline creates is `0700` with a `.gitignore` of `*`; the database,
  its `-wal`/`-shm` files and load-run files are `0600` (POSIX).
- `cookie_secure` (`"auto"`, `true`, `false`): `"auto"` sets `Secure` for https, including
  `X-Forwarded-Proto: https` from a `trusted_proxies` peer. Logging out revokes that session
  cookie in the process that answered (in memory until it expires). `redline doctor` warns
  when `trusted_proxies` is empty with `token`/`basic` (one shared login rate-limit bucket
  behind a proxy).
- `GET api/config` masks userinfo in `base_url`.

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
