# v0.4 release validation

All implementation, tests, examples, UI and documentation are together on local
`release/v0.4.0a2-field-test`. All five distributions currently use `0.4.0a2`.
The field-evaluation corrections and current validation limits are recorded in
[the real-project report](reports/rqeeb-fastapi-field-test.md). The historical Compose
qualification below predates these changes and does not qualify this revision.
There is one planned final merge to `main` and one coordinated release. Nothing has
been pushed, tagged or published. `HANDOFF.md` remains unchanged and uncommitted.

## Implemented scope

- SQL failure evidence, structured vendor errors, raw rowcounts, comment-placeholder
  checks, optional all-engine observation and observed connection/session-state findings.
- SQL Server estimated and guarded actual plans, catalog inspection, offline SQL checks,
  explicit compilation and schema snapshots, with documented support limits.
- Jobs, stages, bounded row outcomes, Celery signals, request/user correlation,
  commit-before-failure evidence, per-stage outcomes and a 24-hour jobs summary;
  Jobs and SQL/privacy tools in the UI.
- Production record-and-inspect defaults, bounded capture, safe privacy fallbacks,
  failure-safe SQL observers, recorder ownership, storage loss counters, readonly
  secondary browsing and duplicate-ID refusal.
- OTLP trace/log export with sanitized SQL spans, W3C `traceparent` propagation, schema
  watch, SQL Server unused-index analysis and raw-SQL N+1 batching advice.
- `check-imports` follows statically visible imports inside task functions; `redline check
  compare/run --ruff` carries Ruff JSON into Markdown and JUnit.
- Storage self-test and cross-process coverage: `redline doctor` probes WAL beside the
  store and warns on Linux network/virtual mounts; a test writes one file from two
  independent OS processes. The overhead benchmark includes an mssql-shaped fake-DBAPI
  cell; its `full` mode measured +0.054 ms per request and +53.5 us per query (a narrow miss of the 50 us target).
- Root and package READMEs, detailed production/settings/security/API/workflow guides,
  expanded demos and driver probes. See [README](README.md),
  [workflow guide](docs/real-world-workflows.md) and [release plan](RELEASE_0_4_PLAN.md).

These are bounded implementations, not a promise of compatibility with every Python
codebase. RQ/Dramatiq adapters, `redline_core.dbapi.wrap`, the Postgres store, a dedicated
cross-deploy error-regression view and AI diagnosis are still open (see the 0.5/0.6 tables
in [the roadmap](docs/roadmap.md)). Arbitrary shared-network-filesystem storage is not
supported: `redline doctor` warns, and a writable store still requires WAL. Full
distributed trace stitching beyond explicit Celery jobs remains out of scope.

## Successful full-stack qualification (2026-10-29)

Run `20261029T052447Z-caaef5f7` completed with container exit code 0 and
`qualified: true`. The implementation at `8a7ea9f` passed every requested Compose step.
The commits after `8a7ea9f` (through the storage/benchmark and CI-hygiene work) have
targeted regression tests and the local gate described below, but they are **not** part of
that Compose run. A fresh full qualification is required before merge.
The machine-readable report and logs are under ignored `examples/testing/results/`.
This record changes documentation only; it does not change the tested implementation.

| Check | Result |
|---|---|
| Core, including network and live optional-service cases | 1,518 passed |
| Django | 203 passed |
| FastAPI | 125 passed |
| Flask | 132 passed |
| Litestar | 114 passed |
| Live integrations and harness guards | 28 passed |
| Python total | **2,120 passed; zero failures, errors or skips** |
| Frontend | Unit tests, TypeScript and actual UI build required by the successful image build |
| Browser | Real Chromium Jobs/detail, SQL checker, privacy preview and mobile overflow/screenshot tests passed |
| Real databases | PostgreSQL, MySQL and SQL Server sync/async capture, handled failures, privacy and rollback cases passed |
| Plans and compilation | All three vendor cases passed; SQL Server actual-plan/catalog and cleanup checks passed |
| Worker | Actual Redis delivery, Celery prefork success/failure/retry, correlation and shutdown-drain case passed |
| Demos | Both all-framework pattern and real-world workflow harnesses passed |
| Documentation and hygiene | Strict MkDocs, Ruff check/format and lockstep versions passed |
| Packages | Five wheels/source distributions, metadata validation, isolated wheel installation and wheel smoke passed |

Recorded dependency versions: Django 5.2.18, SQLAlchemy 2.1.6, Celery 5.6.3,
PyMySQL 1.2.3, pyodbc 5.3.0, aioodbc 0.5.0, mysqlclient 2.3.0, mssql-django 1.8.0,
Hatchling 1.33.0, Twine 6.2.0 and packaging 26.3. The Compose baseline uses Python 3.13,
PostgreSQL 16, MySQL 8.4, SQL Server 2022 Developer and Redis 7.4.

Earlier failed runs exposed and led to fixes for test-environment configuration leakage,
DSN formats, repeat-run virtual environments, metadata compatibility, effective storage
permissions and sampling/timer assumptions. Those earlier partial runs are superseded
by this complete green result.

## Remaining release steps

1. Review the combined release branch and authorize the single final merge into `main`.
   Nothing is pushed or merged by this validation record.
2. Require main CI to pass, including native Linux amd64 Compose qualification and the
   broader supported Python/framework matrix. CI/docs run on main pushes only; branch
   protection must enforce PR merges if direct main pushes must be prohibited.
3. Publishing requires owner authorization, the exact package version in
   `REDLINE_RELEASE_VERSION` and the configured environment approvals. All five packages
   are currently `0.4.0a2`; this is an alpha release. See [RELEASING.md](RELEASING.md).
4. Roll out record-and-inspect mode as a canary in the actual production topology.
   Validate deployment-specific filesystem, credentials, framework/driver versions,
   request concurrency, load/overhead and accessibility requirements. A passing Compose
   baseline does not establish compatibility with every codebase or arbitrary network
   filesystem. The mssql-shaped cell measures instrumentation only (fake DBAPI, no SQL
   Server latency); run it on the target hardware and revisit `profile` mode separately.

The complete local integration gate is green. Main CI and publication authorization
remain separate gates; no tag or publication has occurred.
