# Feature backlog

Status: proposals for the next releases, not commitments or a claim that v0.4 is
complete. The open report items are tracked in the [roadmap](roadmap.md); this page
turns them into concrete, reviewable work.

The list is ordered for **developer value and reliability** in fast-development
workflows: generated code, frequent refactors, automated edits and agents that need
evidence rather than guesses. Each item must keep Redline's existing rules: bounded
capture, explicit opt-in for outbound data, and no claim that absence from sampled
traffic proves absence of a problem.

## Priority summary

| Priority | Feature | Report link | Primary value |
|---|---|---|---|
| P0 | Error fingerprint and cross-release regression view | F-07 | Turns scattered failures into a stable trend |
| P0 | Fix verification loop | F-22 | Proves a change fixed the issue without a regression |
| P0 | CI policy packs | F-01, F-02, F-19 | Makes performance and safety rules reviewable in the repo |
| P0 | Instrumentation health report | F-20 | Prevents false confidence from missing evidence |
| P1 | Explain every finding and rank by observed impact | F-17, F-18 | Makes findings trustworthy and reviewable |
| P1 | RQ and Dramatiq adapters | R-07 | Extends job evidence beyond Celery |
| P1 | `dbapi.wrap` for non-SQLAlchemy projects | 0.6 | Covers generated projects that skip the ORM |
| P1 | Postgres store | 0.6 | Durable multi-container storage |
| P1 | Read-only evidence API for automation clients | F-22 extension | Lets tools query the same evidence a reviewer sees |
| P2 | Golden traces and deterministic replay | F-06 extension | Replays representative behaviour after a change |
| P2 | Cross-release plan diff | R-01 extension | Catches plan regressions hidden behind unchanged SQL |
| P2 | Optional external diagnosis package | 0.6 | Adds ranked explanations without expanding the default trust boundary |

## P0: reliability before more analysis

### 1. Error fingerprint and cross-release regression view

Group SQL and connection failures by a stable fingerprint built from the normalized
vendor code, SQL fingerprint, operation, and callsite. Show first seen, last seen,
release, affected routes, and recurrence after a fix.

**Why now:** a failure code alone is too coarse; a message alone is too unstable.
A stable fingerprint makes an error recurrence visible across deploys and generated
edits.

**Acceptance criteria:**

- The fingerprint ignores literals, whitespace, parameter values, and unstable
  message text.
- The view shows evidence and a link to the original spans.
- The fingerprint is persisted with bounded length and never includes secrets.
- An error that disappears from sampled traffic is labelled “not observed”, not
  “fixed”.

### 2. Fix verification loop

Link an issue to one or more check suites and report three independent outcomes:
the issue disappeared, the stored responses stayed equivalent, and the measured
performance did not regress. Absence from a sample is not proof of resolution.

**Why now:** fast edits need a reviewable answer to “did this actually fix it?”,
not a second manual investigation.

**Acceptance criteria:**

- A verification run records the baseline, the new run and the comparison.
- The result distinguishes `fixed`, `still_present`, `not_observed` and
  `not_comparable`.
- Response equivalence uses the existing ignored/volatile-field rules.
- Performance comparisons retain the existing noise handling and sample sizes.
- The UI and JSON report expose the same verdict and evidence.

### 3. CI policy packs

Keep budgets, forbidden issue kinds, and suppressions in the repository as a
reviewable policy file. Policies should support route scopes, reasons, owners and
expiry for suppressions. CI fails on the policy result, not on an implicit global
default.

**Why now:** generated changes often satisfy a local test but quietly increase query
counts, latency or risky SQL patterns. A policy file makes the intent explicit.

**Acceptance criteria:**

- A documented schema validates budgets, forbidden kinds, route scopes and
  suppressions.
- Expired suppressions fail validation.
- Markdown and JUnit output include the violated policy and its source location.
- Policy evaluation is deterministic and does not require a running UI.
- The existing `--ruff` pass-through remains separate: Redline reports it, it does
  not become a linter.

### 4. Instrumentation health report

One bounded page and API response covering active integrations, missing profiles,
truncated traces, dropped writes, disabled capture, unobserved routes and incomplete
analysis. It should explain what Redline could not see.

**Why now:** a green dashboard built from missing data is worse than a red one.

**Acceptance criteria:**

- Every signal links to the evidence or configuration that produced it.
- Truncation and sampling are reported separately from errors.
- The report never claims route coverage from an empty or tiny sample.
- Storage queue drops and writer failures are visible without reading logs.

## P1: developer workflow and coverage

### 5. Explain every finding and rank by observed impact

Standardize an evidence block for every issue kind: matching spans, threshold,
grouping rule, callsite, sample size and limitations. Add a ranking based on observed
cost and affected requests, with overlapping costs marked as non-additive.

**Why now:** a finding without evidence is hard to review and easy to “fix” in the
wrong place.

**Acceptance criteria:**

- Each issue kind documents its threshold and false-positive guards.
- The evidence block is available in JSON, the API and the UI.
- Ranking labels sampled counts and estimated savings.
- Overlapping or nested costs are never presented as additive savings.

### 6. RQ and Dramatiq adapters

Implement RQ and Dramatiq on the same job contract as Celery: request-to-job
correlation, job/stage outcomes, retries, bounded row outcomes and failure-stage
evidence.

**Why now:** job evidence should not depend on the queue library a project happens
to use.

**Acceptance criteria:**

- The same job, stage and outcome fields are produced for all three integrations.
- Correlation works across enqueue, execution and retry.
- Missing or unsupported hooks degrade to an explicit limitation, not silent loss.
- Adapters have contract tests against the core job model.

### 7. `dbapi.wrap` for non-SQLAlchemy projects

Provide an explicit DB-API wrapper for projects that use drivers directly. Capture
statement text, timing, row evidence and failures with the same redaction and
truncation rules as the SQLAlchemy integration.

**Why now:** generated projects increasingly use a thin driver or a query builder
that is not SQLAlchemy.

**Acceptance criteria:**

- The wrapper works with a documented DB-API 2.0 subset and fails safely outside it.
- Parameters are bounded and redacted before storage.
- Cursor proxy behaviour preserves the driver's fetch and rowcount semantics.
- Instrumentation can be disabled without changing application call sites.
- Unsupported drivers produce a clear startup warning.

### 8. Postgres store

Add a Postgres-backed store with migrations, bounded writes, retention and the same
read APIs as SQLite. Keep SQLite as the local/default store and do not silently
migrate an existing file.

**Why now:** multi-container deployments need a store that is designed for shared
access, not a warning around SQLite's limits.

**Acceptance criteria:**

- Schema migrations are versioned and tested against a real Postgres server.
- Writer batching, retention and dropped-write counters have the same semantics.
- Connection failures degrade to counted drops rather than blocking requests.
- A migration path from SQLite is explicit, resumable and opt-in.
- The UI and CLI work against both stores without conditional behaviour leaks.

### 9. Read-only evidence API for automation clients

Expose redacted traces, issues, comparisons, schema snapshots and instrumentation
health through a read-only API with explicit scopes. A future MCP-compatible layer
can sit on top; the core API must not need one.

**Why now:** automation clients need the same evidence reviewers use. They should
not scrape HTML or receive broader credentials than necessary.

**Acceptance criteria:**

- No mutation endpoints are exposed through the read-only surface.
- Every response applies the configured redaction and byte bounds.
- Access uses the existing token/basic rules and can be scoped separately.
- Pagination, stable IDs and `not_observed` semantics are documented.
- The API is versioned and has contract tests.

## P2: deeper analysis

### 10. Golden traces and deterministic replay

Store representative traces, plans and schema snapshots as fixtures and provide a
replay command. Re-run analysis and comparisons without relying on live traffic.

**Why now:** a deterministic fixture makes a refactor reviewable even when the
original request is hard to reproduce.

**Acceptance criteria:**

- Fixtures are bounded, redacted and checked into a documented directory.
- Replay produces stable fingerprints and issue IDs.
- Missing or incompatible fixtures fail with a clear message.
- Live traffic is never required to reproduce a stored finding.

### 11. Cross-release plan diff

Compare actual plans and row estimates across releases, not just SQL text. Show
operator changes, scan changes, implied conversions and missing-index evidence.

**Why now:** identical SQL can regress when statistics, indexes or schema drift
change.

**Acceptance criteria:**

- The diff identifies the plan source (actual or estimated) and the SQL Server
  version.
- Unstable plan fields are ignored or labelled.
- The report links a changed plan to the observed query cost.
- Missing actual-plan permission is reported as a limitation.

### 12. Optional external diagnosis package

Offer ranked explanations and suggested fixes as a separate, explicitly installed
package. It must never send trace data, SQL, parameters or source by default, and
must work with a user-supplied endpoint or local model.

**Why now:** diagnosis can be useful, but it must not become an implicit data
boundary change in the core profiler.

**Acceptance criteria:**

- The core package has no dependency on the diagnosis package.
- Sending requires an explicit command and a visible preview of the payload.
- Redaction runs before serialization, not after.
- Responses are stored as suggestions with confidence and limitations, never as
  facts.
- Disabling the package leaves no runtime hooks behind.

## Sequencing

1. Error fingerprints, instrumentation health and policy packs.
2. Fix verification loop and evidence standardization.
3. RQ/Dramatiq and `dbapi.wrap`.
4. Postgres store.
5. Read-only evidence API.
6. Golden replay, plan diff and optional diagnosis.

This order puts observability trust and CI enforcement before broader integrations,
and keeps every outbound-data feature opt-in.
