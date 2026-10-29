# v0.4 release validation

All implementation, tests, examples, UI and documentation are together on local
`release/v0.4`. All five distributions currently use `0.4.0a1`.
There is one planned final merge to `main` and one coordinated release. Nothing has
been pushed, tagged or published. `HANDOFF.md` remains unchanged and uncommitted.

## Implemented scope

- SQL failure evidence, structured vendor errors, raw rowcounts, comment-placeholder
  checks, optional all-engine observation and observed connection/session-state findings.
- SQL Server estimated and guarded actual plans, catalog inspection, offline SQL checks,
  explicit compilation and schema snapshots, with documented support limits.
- Jobs, stages, bounded row outcomes, Celery signals, request/user correlation and
  commit-before-failure evidence; Jobs and SQL/privacy tools in the UI.
- Production record-and-inspect defaults, bounded capture, safe privacy fallbacks,
  failure-safe SQL observers, recorder ownership, storage loss counters, readonly
  secondary browsing and duplicate-ID refusal.
- Root and package READMEs, detailed production/settings/security/API/workflow guides,
  expanded demos and driver probes. See [README](README.md),
  [workflow guide](docs/real-world-workflows.md) and [release plan](RELEASE_0_4_PLAN.md).

These are bounded implementations, not a promise of compatibility with every Python
codebase. Full distributed trace stitching, a general OTLP span/log exporter, RQ/Dramatiq
adapters and arbitrary shared-network-filesystem storage are not implemented support.

## Local results (2026-10-29)

| Check | Result |
|---|---|
| Core excluding three server-dependent files | 1,472 passed, 6 optional-service skips |
| Three server-dependent files | 18 passed; 21 setup errors at socket bind (`PermissionError: Operation not permitted`), no assertion failures |
| Django | 199 passed, 1 optional PostgreSQL skip |
| FastAPI | 125 passed |
| Flask | 132 passed |
| Litestar | 114 passed |
| Optional Celery 5.6.3 | Eager success/failure/retry test passed using cached dependencies; no broker/prefork claim |
| Frontend helpers | 35 passed; TypeScript and production build passed |
| Demos | All four pattern workflows and the real-world workflow harness passed in-process |
| Documentation | Strict MkDocs build passed |
| Workflow definitions | Actionlint passed for CI, docs and release workflows |
| Code hygiene | Ruff check/format, whitespace and lockstep version checks passed; offline lock check passed |
| Packages | Five wheels and source distributions built; wheel imports/assets/type markers checked in a fresh environment using existing third-party dependencies |
| Browser | Chrome desktop checks of safe-fallback request/SQL/payload views and synthetic Jobs list/detail; full mobile/accessibility/live-server review remains outstanding |

The core skips cover optional Celery, Redis and PostgreSQL services. Celery was checked
separately in eager mode. Real SQL Server/MySQL probes were not executed against external
databases; driver-shaped fixtures and probe guard tests are not live qualification.

## Reproducible full-stack tests

The committed test-project setup is in `examples/compose.yaml` and `examples/testing/`.
It runs the five complete suites, live PostgreSQL/MySQL/SQL Server and Redis/prefork
checks, Chromium desktop/mobile flows, demos, docs and package smoke tests. See
[the shared guide](examples/testing/README.md). The first full container run failed qualification: live plans and Redis/prefork passed,
but test-runner configuration, DSN, metadata and repeat-run issues caused other failures.
Corrections and local regressions are present; the entire container suite must be rerun.
This session cannot access OrbStack's socket and elevated Docker execution is disabled.
Do not treat test definitions or earlier partial successes as full qualification.

## Remaining release gates

1. Rebuild and rerun the entire Compose suite after the harness corrections. The
   container run did execute the network tests; a stop test exposed a startup-timer
   assumption, now replaced by completion-driven synchronization. Require zero failed,
   errored or skipped tests in the new report. The authoring shell still blocks sockets.
2. Run the disposable MySQL/SQL Server probes described in
   [the Flask demo](examples/flask/README.md), then validate the actual framework/driver,
   database version, worker lifecycle, storage volume and concurrency used in deployment.
   Qualify broker delivery/retries/prefork separately from Celery eager mode.
3. Complete full UI/mobile/accessibility review and deployment load/overhead checks.
   Earlier benchmark figures do not measure this release.
4. Review the combined committed changes, then perform the single authorized final
   merge when validation is accepted. CI/docs run on main pushes; successful main CI
   builds the exact tested commit. Branch protection must prevent direct main pushes
   if literal merges-only enforcement is required.
5. Publishing additionally requires the exact package version in
   `REDLINE_RELEASE_VERSION`, existing environment approvals, and owner authorization.
   See [RELEASING.md](RELEASING.md). Merging alone does not authorize publication.

This is an unreleased alpha candidate, not a production-qualified release yet.
