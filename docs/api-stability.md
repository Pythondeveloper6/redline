# API stability and deprecation policy

[← Index](index.md)

Redline is **alpha** (`0.x`). This page says which names you can build on, how they may
change before 1.0, and how a change is announced. It is the authoritative list; older
design notes cited in code comments describe how features were specified and are not a
compatibility promise.

## Versioning

The five distributions (`redline-core`, `django-redline`, `fastapi-redline`,
`flask-redline`, `litestar-redline`) are released **in lockstep**: one version number, and an
adapter only supports the `redline-core` of the same release. Mixing versions is
unsupported (`scripts/check_versions.py` enforces it in CI).

Before 1.0:

- A **minor** bump (`0.5` → `0.6`) may change or remove public API, but only after the
  deprecation period below, and every such change is listed under **Changed** or
  **Removed** in the [changelog](changelog.md).
- A **pre-release** (`0.6.0a1`, `a2` …) may change anything that was *added* in the same
  pre-release series. Things that shipped in a final or earlier minor release follow the
  deprecation rule.
- Patch releases (`0.5.1`) fix bugs only.

From 1.0, the project follows semantic versioning for everything listed as public below.

## What is public

Public means: importable from the names below, documented on this site, and covered by the
deprecation rule. Everything else is **internal**, including any module or name starting
with `_`, and any module not listed here, even if it is importable.

| Surface | Public names |
|---|---|
| `redline_core` | everything in `redline_core.__all__`: `configure`, `get_recorder`, `Recorder`, `Settings`, `SettingsError`, `span`, `record_span`, `record_query`, `tag`, `job`, `stage`, `row_outcome`, `start_trace`, `finish_trace`, `current`, the model types (`Trace`, `Span`, `SpanContext`, `TraceBuilder`, `Issue`, `Suggestion`, `Callsite`, `Frame`) and the extension points `Detector`, `FixSuggester`, `GenericSuggester` |
| `redline_core.testing` | the pytest helpers documented in [Performance workflows](performance-workflows.md) |
| `redline_core.contrib.*` | the `instrument_*` / `uninstrument_*` functions of each integration documented in [Integrations](integrations.md) |
| `redline_core.storage` | `SQLiteStore` construction from a path and the `Store` protocol for custom stores ([Storage](storage.md)); the SQL schema itself is internal |
| `django_redline` | the app label, the URL include, the settings documented in [Django](django.md), `span` |
| `fastapi_redline` | everything in `fastapi_redline.__all__` (`install`, `Redline`, `RedlineMiddleware`, `instrument_engine`, guards) |
| `flask_redline` | everything in `flask_redline.__all__` (`Redline`, `RedlineMiddleware`, `instrument_engine`, guards) |
| `litestar_redline` | everything in `litestar_redline.__all__` (`RedlinePlugin`, `RedlineMiddleware`, `instrument_engine`, guards) |
| Settings | every key documented in [Settings](settings.md), its environment variable and its `redline.toml` / `[tool.redline]` spelling |
| CLI | `redline` subcommands, their documented flags and **exit codes** ([CLI](cli.md)). Human-readable output is *not* stable; use `--json` / JUnit / Markdown outputs for machines |
| HTTP API | the endpoints documented in [HTTP API](http-api.md) under the UI mount. Fields may be *added* to responses at any time; clients must ignore unknown fields |
| Pytest plugin | the fixtures and markers documented in [Performance workflows](performance-workflows.md) |
| Files | the refactor-check baseline and suite formats, the load `scenario.toml` format |

Not public, and free to change in any release: the bundled UI's HTML/JS, log message text,
the SQLite schema (it is migrated automatically, see below), detector heuristics and
thresholds (an issue may appear or disappear when detection improves), fix-suggestion text,
and anything under `redline_core.ui`, `redline_core.storage.sqlite` internals,
`redline_core.check.*` internals and `redline_core.load.*` internals.

## Deprecation rule

A public name, setting, flag or endpoint is removed or changed incompatibly only after:

1. It has emitted a `DeprecationWarning` (Python API, settings) or a `deprecated:` line on
   stderr (CLI) naming the replacement, for **at least one minor release**; and
2. The deprecation is listed under **Deprecated** in the changelog of the release that
   introduced the warning, and the removal under **Removed** in the later release.

Settings keep their old spelling as an alias during the deprecation period.

Security fixes are the one exception: a default may tighten immediately (for example a
capture setting turning off) when keeping it would expose data. The changelog marks such
changes **Security**.

## Stored data

The trace store carries a schema version and is migrated forward in place when a newer
release opens it (additive migrations, one transaction, safe across processes). Every
published release's store format is opened by the current code in CI
(`scripts/check_store_upgrade.py`, the *Compatibility* workflow). Opening a store with an
**older** release than the one that last wrote it is not supported. The store is a
development artifact: there is no promise to keep its contents across a major version, but
a breaking store change would be announced one minor release ahead.
