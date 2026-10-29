# v0.4: database failures, jobs and shared environments

All changes stay local. No push, remote PR, tag or publication is authorized.
All implementation, tests, UI, examples and documentation are consolidated directly on
`release/v0.4`. There is one final merge from that branch into `main` and one coordinated
release for all five packages, after validation and owner approval. There is no separate
feature-branch merge or split feature release. `main` remains unchanged until then.
Live integration and CI qualification remain publication gates.

Routine CI and documentation workflows run only on pushes to main, including merges.
Publishing trigger policy is tracked separately from local implementation.

## Combined release scope

| Area | Deliverables |
|---|---|
| SQL foundations | Vendor registry, SQL Server identity, bracket identifiers, batch classification, comment placeholders |
| Failures | Handled SQL errors, stable issue groups, raw rowcounts, structured error categories |
| Connections | Optional all-engine capture, lifecycle evidence, observed session changes, conservative leak and churn findings |
| SQL inspection | Estimated SQL Server plans, conversion warnings, missing-index candidates, explicit unsupported operations |
| SQL checks | Offline AST extraction and lints, bounded scanning, JSON/JUnit output and baselines; explicit online compilation with documented limits |
| Privacy | SQL parameter capture policy, profiles, route rules, shared-dev preset, storage safeguards, log filtering |
| Correlation | Public tags and request-id correlation, searchable through existing tag filters |
| Jobs | Framework-independent job/stage/row-outcome helpers, optional Celery signals, correlation and task outcomes |
| Operations | Storage diagnostics, explicit role paths, nested framework behavior, optional export |
| UI | SQL failures and connection evidence, SQL checker, job browsing, shared-environment configuration visibility |
| Validation | Regression tests, driver-shaped fixtures, privacy sentinels, adapter checks, UI build and documentation |

## Additional real-work candidates

RW01, RW02, RW03 and RW09 have initial implementations in this release.
The remaining proposals are recorded for selection; they are not support promises.

| ID | Feature | Value |
|---|---|---|
| RW01 | Recording coverage diagnostics | Distinguish no SQL activity from an uninstrumented engine or an unobserved worker |
| RW02 | Transaction outcome evidence | Show observed commits before a later failure without claiming business-level completion |
| RW03 | Task retry visibility | Separate retry attempts from independent failures and show scheduled retries |
| RW04 | Cancellation and timeout outcomes | Distinguish a cancelled request/task from a successful or failed one |
| RW05 | Trace data deletion and retention preview | See what a cleanup removes and scope it by route, release or job |
| RW06 | Integration health page | Show configured integrations, observed span kinds and missing optional dependencies |
| RW07 | Connection exhaustion diagnostics | Correlate pool waits/timeouts with concurrent long-held transactions |
| RW08 | Query retry amplification | Show repeated failed attempts and cumulative cost, without issuing retries |
| RW09 | Redaction preview | Inspect the structure of a sanitized sample before enabling capture |
| RW10 | Portable diagnostic bundle | Export selected sanitized traces and configuration with an explicit preview |
| RW11 | Baseline validity checks | Warn about old baselines, changed settings and missing workload coverage |
| RW12 | Streaming trace lifecycle | Show partial streams, client disconnects and work continuing after headers |

## Accuracy requirements

Unknown rowcounts do not prove NOCOUNT. Observed SET statements do not prove all session
state. NOEXEC supports deferred name resolution and cannot certify schema correctness.
Offline OUTPUT lint does not prove triggers exist. No automatic retry, reset, DDL or
application import is performed by static analysis. Framework, database and queue support
must distinguish fixture verification from integration testing against a running service.
