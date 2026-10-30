# Redline — profiler, backend analyzer and stress tester for Django + FastAPI

## Context
django-silk is the only self-hosted request profiler with a UI, and it has stalled. It is Django-only and has no async support. It also stores secrets in plaintext, bloats the app DB, and has no flamegraphs, N+1 grouping or EXPLAIN. FastAPI has nothing comparable. Load testers (Locust, k6, Gatling) only see the app from outside. No open-source tool connects load results to what happens inside the server, such as which query breaks at 150 users.

Redline combines four parts: **Profile**, **Analyze**, **Stress** and **Compare/CI**. Each version stays small, and v0.1 already beats silk. The repos are **private and never published**, following the owner's standing rule.

Decisions made with the owner:

| Decision | Choice |
|---|---|
| Name | Redline |
| Folder | `/Volumes/Data/apps/redline` |
| UI | React + Vite + TypeScript, prebuilt and shipped inside the wheel (same as celery-manager) |
| Plan depth | Full roadmap; v0.1 in detail |
| Visibility | Private, never published |

PyPI names are all free (checked): `redline-core`, `django-redline`, `fastapi-redline`.

## Repository layout
This follows the virtual uv workspace in `feature-rollout`. See `feature-rollout/pyproject.toml`, `packages/*`, `scripts/smoke_wheels.py` and `scripts/check_versions.py`.

```
redline/
  pyproject.toml            # virtual workspace, shared dev group, ruff (copy feature-rollout's)
  packages/
    redline-core/           # redline_core   — framework-agnostic engine + bundled UI   (py>=3.10)
    django-redline/         # django_redline — Django adapter                           (py>=3.12, Django>=5.2,<6.2)
    fastapi-redline/        # fastapi_redline — FastAPI/Starlette + SQLAlchemy adapter  (py>=3.10)
  frontend/                 # React+Vite+TS+Tailwind source; builds into redline_core/ui/static
  examples/django  examples/fastapi   # demo shops with seeded data and deliberate N+1s / slow queries
  benchmarks/               # overhead benchmarks (follow django-signal-tracer/benchmarks)
  docs/ mkdocs.yml          # one shared docs site, a copy of the django-vantage docs system (BUILD-BRIEF rules)
  scripts/                  # check_versions.py, smoke_wheels.py, build_ui.sh, screenshots.py
  .github/workflows/ci.yml  # ruff, test matrix, UI build, mkdocs --strict, wheel smoke. No publishing.
```

Rules from `package-ideas/BUILD-BRIEF.md` apply:
- hatchling builds, version `0.1.0a1`
- repo-local git author `pythondeveloper6 <pythondeveloper6@gmail.com>`, with the Co-Authored-By trailer on every commit
- uv-managed Python (not the broken Homebrew one)
- docs, README and community files at django-vantage quality

## Core architecture (redline-core)
Every later version builds on these pieces.

| Module | Responsibility |
|---|---|
| `model.py` | `Trace` (one request) → `Span` tree. Span kinds: `sql`, `cache`, `http_out`, `template`, `middleware`, `view`, `custom`. Trace tags include `run_id` and `step`, which the v0.2 load tester uses to correlate. |
| `recorder.py` | Current trace and span held in `contextvars`, using the same pattern as `django-signal-tracer/src/signal_tracer/trace.py`. It must be correct across async, `sync_to_async` and threadpools, and cost nothing when inactive. Has a `redline.span("name")` context manager/decorator for user code. |
| `callsite.py` | Cheap stack capture at query time, filtered to project code (excluding site-packages and redline itself). Gives the file:line attribution. |
| `sql.py` | SQL fingerprinting (literal and IN-list normalisation) and the duplicate/similar grouping key. |
| `detect.py` | Detectors over a finished trace: **N+1** (same fingerprint from the same callsite ≥ threshold), exact duplicates, slow queries, too many queries. They produce `Issue` objects. Adapters plug in a `FixSuggester`. |
| `explain.py` | On-demand EXPLAIN runner. SELECT only, inside a rolled-back read-only transaction, using the captured params. Backends: Postgres `EXPLAIN (FORMAT JSON)` (ANALYZE only behind a setting), MySQL `FORMAT=JSON`, SQLite `EXPLAIN QUERY PLAN`. `plan_analysis.py` flags seq scans over big tables, sorts that spill to disk, missing indexes on filter/join columns, and emits a `CREATE INDEX` suggestion. |
| `profiler.py` | Optional `pyinstrument` extra. Profiles sampled or opted-in requests (`?_redline=profile` or sample rate) and stores speedscope-format JSON. |
| `redact.py` | On by default. Masks headers (Authorization, Cookie, Set-Cookie, X-Api-Key), masks body/query keys matching password/token/secret/key, caps body size, and has configurable patterns. |
| `storage/` | `Store` protocol. Default `SQLiteStore` is a **separate file** (`.redline/redline.db`, WAL), never the app DB, which fixes silk's bloat and self-recording. A background writer thread with a queue keeps the request path off disk I/O. Retention by age and row count. Postgres store comes later. |
| `sampling.py` | Capture policy: `all` in dev; in prod, a sample rate plus always keeping slow (>N ms) and 5xx requests. |
| `api.py` | Framework-neutral JSON handlers: list/filter traces, trace detail, endpoint aggregates (p50/p95/p99, query counts), issues, run EXPLAIN, delete/clear. Adapters only route to these. |
| `ui/static/` | Built SPA, served by the adapters with relative `base: './'`, as in `celery-manager/frontend/vite.config.ts`. |
| `conf.py` | Typed settings dataclass shared by both adapters. Each adapter maps its own settings into it. |

## v0.1 — detailed build steps
Scope: profile, N+1 with fix diffs, EXPLAIN and the UI, for Django and FastAPI.

1. **Scaffold.** Copy the workspace, ruff, pytest, CI and docs skeleton from `feature-rollout` and `django-vantage/mkdocs.yml`. Run `git init -b main` and set the local author. Pick a brand colour: red, for Redline.
2. **Core engine.** Build `model`, `recorder`, `callsite`, `sql`, `redact`, `sampling`, `conf`, the `SQLiteStore` with writer thread and retention, and `api`. Unit-test the async and thread correctness of the contextvars recorder.
3. **Detectors.** Build N+1, duplicates, slow queries and query budget, plus the `FixSuggester` protocol. Test on hand-built traces.
4. **Django adapter.**
   - `RedlineMiddleware`, sync and async capable (`sync_capable`/`async_capable`), with view and middleware timing.
   - SQL capture via `connection.execute_wrapper` on every connection (`connection_created` signal).
   - Cache backend wrapper and template render timing.
   - `urls.py` serves the SPA and the JSON API at `redline/`. Staff-only by default, with a `REDLINE["PERMISSION"]` hook.
   - System checks, plus `redline_clear` and `redline_prune` commands.
   - **Django fix suggester:** map the SQL table to a model through the app registry. A repeated `WHERE fk_id = %s` on a child table becomes `prefetch_related("<related_name>")`. A repeated `WHERE id = %s` on the parent becomes `select_related("<fk>")`. The output is a unified diff at the callsite line when the queryset is found there, otherwise a textual hint.
5. **FastAPI adapter.**
   - A pure ASGI middleware (not `BaseHTTPMiddleware`), with route-template resolution so endpoints aggregate as `/orders/{id}`.
   - SQLAlchemy `before/after_cursor_execute` listeners for sync and async engines.
   - Outbound `httpx` hooks.
   - `install(app, engine=..., settings=...)` mounts the UI and API router at `/_redline`, with an auth dependency hook.
   - **SQLAlchemy fix suggester:** suggests `selectinload(Parent.rel)` or `joinedload(...)` from the mapper registry.
6. **EXPLAIN plus plan analysis.** Wire it into both adapters (Django `connections[alias]`, SQLAlchemy engine). The Postgres tests skip cleanly when no Postgres server is available.
7. **Profiler extra.** Add `redline-core[profile]` (pyinstrument) and speedscope JSON storage.
8. **Frontend** (React 19 + Vite + TS + Tailwind, same stack as `celery-manager/frontend`). Light and dark themes.
   - **Requests:** live list with polling; filters for path, method, status, duration and query count; a badge on requests with issues.
   - **Request detail:** summary cards; a waterfall timeline of the span tree; an SQL tab grouped by fingerprint with callsites; an Issues panel showing the N+1 fix diff with copy; an EXPLAIN drawer with the plan tree and flags; a flamegraph tab (own canvas icicle component reading speedscope JSON, with an "open in speedscope" export); a redacted headers/body tab.
   - **Endpoints:** a table of p50/p95/p99, query counts, error rate and a sparkline.
   - **Issues:** N+1s and slow queries deduplicated across requests, with first/last seen and occurrence count.
   - `scripts/build_ui.sh` builds the UI into `redline_core/ui/static`. CI fails if the bundle is stale.
9. **Examples.** A Django shop and a FastAPI shop with seed commands and deliberate problems: an N+1 on order items, a missing index, a duplicate query, and a slow endpoint.
10. **Benchmarks.** Measure overhead per request with Redline off, sampled and full. The target is <1 ms plus a few µs per query when not profiling, and the docs will publish the measured numbers.
11. **Docs and README.** Follow the vantage system: hero index, Guide (profiling, N+1, EXPLAIN, flamegraphs), Integrations (Django, FastAPI), Operations (settings reference, production mode, security/redaction, storage, overhead), an FAQ that includes "how finished is this?", and a roadmap page. Include light and dark Playwright screenshots of the examples.

## Roadmap after v0.1 (outlined; each one gets its own detailed plan later)
- **v0.2 Stress.**
  - `redline load` CLI and a UI page, with the load generator running in **separate processes** (asyncio + httpx workers).
  - An open-model arrival-rate scheduler with HdrHistogram that corrects for coordinated omission, plus step-ramp mode that finds the knee (RPS vs p95 vs errors chart).
  - Scenarios generated from the Django URLconf, the FastAPI OpenAPI schema, or captured traffic with redaction.
  - Requests carry an `X-Redline-Run: <id>;step=<n>` header, so in-app traces are tagged and the **bottleneck per step** can be shown (query X: 3 ms → 410 ms at 150 users).
  - DB pool wait and lock detection (`pg_locks`); event-loop lag and threadpool saturation for FastAPI.
- **v0.3 Compare/CI.**
  - Runs and baselines tagged with git SHA, with diffs per endpoint and flamegraph diffs.
  - A pytest plugin with `@pytest.mark.redline(max_queries=..., no_n_plus_one=True)`.
  - `redline ci` produces JUnit output and a markdown PR comment (+queries, +p95, culprit line).
- **v1.0.**
  - Hardened production mode with a Postgres store.
  - Celery/RQ trace stitching; a memory-per-request extra (tracemalloc/memray).
  - In-process fault injection (latency or errors per endpoint or query).
  - Index what-if via HypoPG.
  - OTLP import/export.
  - Optional AI diagnosis (bring your own key) from the trace, SQL, plan and source to a ranked diagnosis plus patch.
  - Folding `django-signal-tracer` in as a panel.

## Execution notes
- The owner's latest instruction is "no more agents". Build inline unless the owner approves using subagents for this.
- Commit in small steps per build step above. Do not push, create a GitHub repo, or publish. The lead creates the private repo afterwards, on request.

## Verification (v0.1 is done when all of these pass)
1. `uv sync --all-packages`. `ruff check` and `ruff format --check` are clean.
2. Each package's test suite passes (`cd packages/<pkg> && uv run pytest`): Django 5.2/6.0/6.1, FastAPI with sync and async SQLAlchemy, and the Postgres EXPLAIN tests (skipped without a server, and they say so).
3. `scripts/build_ui.sh` runs and the bundle is current. `uv build` for all three packages, then `scripts/smoke_wheels.py` installs them into a fresh venv and checks that the static UI is included.
4. Run both examples. Hit the deliberately broken endpoints and check in the browser (Playwright):
   - the N+1 issue appears with the correct `prefetch_related`/`selectinload` diff;
   - EXPLAIN flags the missing index;
   - the flamegraph renders;
   - redaction hides the `Authorization` header.
   Save light and dark screenshots to `docs/assets/screenshots/`.
5. Benchmarks run and the numbers are recorded in the docs.
6. `uv run --with mkdocs-material mkdocs build --strict` passes.

## v0.2 build (branch `v0.2`, contract §8–§17)

Scope agreed with the owner on 2026-10-27: every analysis/UX feature from the follow-up list, Flask (and Litestar) adapters, stress testing, and history (before/after over time). Built in waves by subagents in their own git worktrees; the lead merges, rebuilds the UI bundle and regenerates `uv.lock` after each wave, then removes the worktrees.

| Wave | Slices |
|---|---|
| 0 | core foundation (model, settings, detector registry, histogram, logs/locals, Server-Timing) · `redline_core.contrib` (SQLAlchemy/httpx moved from fastapi-redline; requests, redis, asyncpg, psycopg) · UI quick wins (palette, keyboard, open-in-editor, cURL/HTTPie, replay, compare skeleton) |
| 1 | store v2 + history + API routes (Opus) · flask-redline · async blocking + transactions + pool wait (Opus) · Server-Timing/badge/SSE/snapshot in adapters |
| 2 | load tester `redline load` (Opus) · result-size/count/serializer/external detectors · workload index advisor + unused indexes (Opus) · GraphQL resolvers |
| 3 | UI pages: History + Load · UI pages: Database/Cache/External/logs/exception/live · litestar-redline + Django Ninja · static N+1 scan |
| 3b | **configuration and access control** (contract §18: presets dev/staging/production, `redline.toml` + `REDLINE_*` env, one access policy for all adapters — local/token/basic/framework/custom, IP allow-lists, trusted proxies, read-only mode, per-feature switches, login screen, Settings page, `redline config` / `doctor` / `hash-password`) (Opus) |
| 4 | docs · examples + CI matrix · security/code review (Opus) · browser verification, version 0.2.0a1 |


## v0.3 build (branch `release/v0.3`)

| Wave | Work | Owner model |
|---|---|---|
| 1 | Check engine: suite parsing, runner, stats, volatile detection, diff, baseline/report formats, CLI `redline check` (CONTRACT §19.1–19.4) | opus |
| 1 | Storage schema v4 + check tags in the writer + API routes + UI-launched check runs (§19.2 tags, §19.5) | opus |
| 1 | UI Checks pages against the §19.5 API with mocks (§19.6) | sonnet |
| 2 | Docs page, CLI reference, examples' suites, CHANGELOG; browser E2E on the examples | sonnet |
| later | `redline analyze`: static analysis of Python code for DB/logic bottlenecks (roadmap) | – |


## v0.3 selected expansion

Selection: F01, F02, F04–F07, F09–F13, F15, F17–F22 and P01–P14 from FEATURE_CANDIDATES.md. F03, F08, F14 and F16 are deferred to the next release. Existing P01/P07 behavior is extended only where a documented gap exists. No version bump, merge, tag or publication is implied.

Every selected feature must have a UI workflow or an inspectable report with a reproducible example. CLI and UI must use the same validation and engines. Developer integrations expose configuration and results in the UI; they do not execute arbitrary test commands from the browser.

| Stage | Work | UI acceptance | Status |
|---|---|---|---|
| A | F02/F04/F05: endpoint budgets, keyed arrays, numeric tolerances | Suite builder edits rules; saved rules survive reload/export; reports show violations and applied comparison rules | Implemented; API and helper checks passed |
| B | F01/F06/F07: pytest, baseline compatibility, suite coverage | Inspect policies, environment warnings and route/input coverage | Implemented; integration checks passed |
| C | F09–F13/F15 and runtime P03/P04/P05/P13 | Findings with evidence, configurable thresholds and deliberate demo cases | Implemented; deterministic findings and demo checks passed |
| D | Static P01/P02/P06/P11/P12/P14 and P10 profiler evidence | Analyze source through a bounded local workflow; browse findings and source locations | Implemented; bounded source checks passed |
| E | Database P08/P09/P10 | On-demand checks, catalog/plan evidence and explicit uncertainty | Implemented; catalog/plan checks passed on supported shapes |
| F | F17–F22: impact, evidence, suppressions, health, time breakdown, fix verification | End-to-end issue-to-check workflow and persistent scoped suppressions | Implemented; API/helper checks passed |
| G | Integration | Four framework demos, UI interactions, responsive/accessibility review, all package/lint/docs/build/version checks | Available checks passed; socket tests and visual review blocked |

No stage is complete until its backend, UI, tests and documentation are integrated. Timing-dependent tests must use deterministic data or bounded polling.


Validation details and feature-to-UI mapping are in IMPLEMENTATION_STATUS.md. No current CI success, merge, tag or publication is claimed. Browser
visual/accessibility review is pending because UI automation timed out; 21 existing server
setup tests cannot bind sockets in the current environment.


## 0.4.0a2 field-evaluation follow-up

| Work | Verification |
|---|---|
| Correct loop evaluation order and recognize SQLAlchemy get/scalar | Source regression cases and real-project rescan |
| Summarize local query helpers; improve SQLAlchemy advice | Helper/shadowing regressions and known missed project paths |
| Configurable bounded CLI project budgets | File/byte/boundary/symlink regressions and complete app scan |
| Conservative sampled-blocker attribution | Stack-name unit tests and existing blocking tests |
| Document actual onboarding and coverage | One real-project report, wheel smoke tests and package suites |


## 0.5.0a1 real-project coverage

| Report gap | Implementation | Verification |
|---|---|---|
| Helpers hidden by imports | Bounded import/re-export summaries | No-execution, cycle, shadow and filesystem-boundary regressions; full app rescan |
| Session annotation/factory types | Known SQLAlchemy provenance inference | Optional/Annotated/string/factory positive and negative cases |
| Recoverable dynamic SQL templates | Literal AST interpreter and project constants | Shadowing, limits, pure/impure helpers and import boundary tests |
| Missing SDK spans | Explicit boto3 hook | Fake-client safety tests and actual boto3 Stubber in target dependency environment |
| In-process background gaps | Explicit FastAPI scheduling helper | Separate parent/job traces, sync/async/callable and failed-task privacy tests |
| Unknown integration coverage | Health API + UI statuses | Import-free diagnostics tests and native-browser fixture render |
| Large UI chunk / offline regression | Lazy routes, stable shell, native imports | Build sizes, palette metadata, embedded snapshot tests and Safari render |
| Live infrastructure qualification | Existing isolated Compose release gate | Remains environment-dependent; no success claimed while Docker/socket access is denied |
