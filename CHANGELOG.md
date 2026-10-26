# Changelog

All notable changes to the three packages in this repository (`redline-core`,
`django-redline` and `fastapi-redline`) are documented here. They are versioned and
released in lockstep.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
version is below `1.0` the public API may change between minor versions.

## Unreleased — 0.1.0a1

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
