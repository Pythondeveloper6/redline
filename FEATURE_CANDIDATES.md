# Redline feature candidates

Status: proposals for selection, not an implementation commitment.
Current work remains on **v0.3**. Nothing here changes the release version or authorizes a release. The original handoff grouped some proposals under v0.4; the selected work now targets v0.3; F03, F08, F14 and F16 target the next release.

This document combines the 22 additional proposals with the 14 performance patterns from `HANDOFF.md`. Existing capabilities and overlaps are identified so they are not mistaken for new work.

## Selection guide

- Select proposals by ID: **F01–F22** for additional features, **P01–P14** for original patterns.
- Effort is relative, not a schedule: **S** = focused change; **M** = several components; **L** = substantial integration.
- Unselected proposals remain a backlog. No implementation starts merely because a feature appears here.
- Every selected detector needs documented evidence, thresholds, limitations, false-positive guards, tests, and a way to verify the fix.

## Additional proposals: testing and comparison

| ID | Feature | Proposed behavior and value | Current relationship | Effort |
|---|---|---|---|---|
| F01 | pytest performance assertions | Mark tests with query limits, forbidden issue kinds, and external-call limits. Fail at the test that introduced the problem. | pytest plugin is planned but unbuilt. | M |
| F02 | Per-endpoint performance budgets | Require a route to stay within absolute query and latency budgets, even when its baseline was already slow. Define the latency statistic and sample requirements explicitly. | Extends existing relative regression thresholds and query-increase checks. | M |
| F03 | Compare two running versions | Alternate requests between old and new servers to reduce timing drift between separate runs. Require equivalent datasets and request configuration. | Existing checks record one app version and compare later. | L |
| F04 | Compare JSON arrays by identity | Opt in to matching array elements by a key such as `id`, avoiding misleading diffs when order changes. Define duplicate and missing-key behavior. | Existing arrays are compared by index. | M |
| F05 | Explicit numeric tolerances | Allow selected numeric paths to differ by a configured absolute or relative tolerance; retain exact comparison elsewhere. Tolerances must be chosen by the user. | Extends current response comparison. | S–M |
| F06 | Baseline compatibility checks | Record Python, framework, database, relevant configuration, and a user-supplied dataset label. Warn when comparison conditions differ, without capturing secrets. | Extends existing Git and release metadata. | M |
| F07 | Check-suite coverage report | Compare suites against observed routes and show missing routes or routes represented by only one input. Label coverage as observed traffic coverage, not proof of complete behavior coverage. | Builds on suites generated from captured traffic. | M |
| F08 | Flamegraph comparison | Highlight functions that became slower or faster across comparable profiles. Normalize profiles carefully and expose sampling uncertainty. | Planned but unbuilt. | L |

## Additional proposals: performance detection

| ID | Feature | Proposed behavior and value | Evidence and limitations | Effort |
|---|---|---|---|---|
| F09 | Repeated individual writes | Find repeated `INSERT` or `UPDATE` statements from one callsite and suggest evaluating bulk operations. | Use captured SQL, operation, count, and callsite. A bulk rewrite may change signals, validation, ordering, or return behavior; do not claim automatic equivalence. | M |
| F10 | Deep OFFSET pagination | Flag expensive high-offset queries and explain cursor/keyset pagination where suitable. | Use parsed OFFSET and observed cost; pagination semantics and stable ordering matter. | S–M |
| F11 | Excessive database round trips | Identify requests where many individually fast queries consume substantial total time without matching a classic N+1. | Use count, cumulative cost, callsites, and overlap-aware timing. Coordinate with existing `too_many_queries` to avoid duplicate noise. | S–M |
| F12 | Connection-pool wait issues | Turn existing pool-wait spans into findings with wait duration and checkout evidence. | Existing SQLAlchemy pool-wait spans provide a starting point. Do not assume increasing pool size is always the fix. | M |
| F13 | Unexpected database access | Declare a route or code block “no SQL” and report the exact access that violates that expectation. | Useful for cached routes and serialization. Requires an explicit expectation, not a heuristic guess. | M |
| F14 | Potentially serial external calls | Identify costly non-overlapping outbound calls and suggest investigating concurrency. | Traces establish timing, not independence. Treat as a candidate, with dependency and ordering caveats. | M |
| F15 | Response payload budgets | Flag responses exceeding a byte budget and compare payload growth after changes. | Independent of SQL row counts. Define compressed versus uncompressed size and streaming behavior without buffering entire bodies. | M |
| F16 | Cold versus warm behavior | Run distinct cold/warm scenarios to expose cache-dependent performance. | Cold-state setup must be explicit and reproducible. Avoid implicit cache clearing or shared-state mutation. | M–L |

## Additional proposals: understanding and verification

| ID | Feature | Proposed behavior and value | Current relationship and limitations | Effort |
|---|---|---|---|---|
| F17 | Rank fixes by observed impact | Combine occurrence frequency, affected requests, and measured cost to prioritize work. | Extend current issue and aggregate views. Label sampled counts and estimated savings; do not present overlapping costs as additive gains. | M |
| F18 | Explain every finding | Show the spans, threshold, grouping rule, and limitations behind each finding: for example, “12 matching calls from this line; threshold 5.” | Builds on existing issue details and suggestion confidence; standardize supporting evidence across kinds. | M |
| F19 | Scoped issue suppressions | Suppress an accepted finding by route, fingerprint, or callsite, with a reason and optional expiry. Keep suppressed findings inspectable. | Adds targeted noise management beyond threshold tuning. Define effects on CI separately from visibility. | M |
| F20 | Instrumentation health report | Show active integrations, missing profiles, truncated traces, dropped writes, and missing server metrics. | Consolidates existing signals and adds coverage diagnostics so users understand observation gaps. | M |
| F21 | Time breakdown without double counting | Separate database, outbound calls, pool waits, and remaining time using interval overlap. | Extend timeline interpretation. Remaining time is uninstrumented time, not automatically CPU time; define nesting and concurrency attribution. | M–L |
| F22 | Automatic verification after a fix | Link a finding to a check suite and show whether it disappeared, responses remained equivalent, and measured performance improved. | Connects existing detectors and refactor checks. Require comparable instrumentation; absence from sampled data alone does not prove resolution. | M–L |

## Original performance patterns from the handoff

These preserve the original candidate set. Some are already supported, some extend existing behavior, and some need additional instrumentation. Runtime observations alone cannot establish how application code used fetched values.

| ID | Pattern | Existing support | Proposed approach and limits |
|---|---|---|---|
| P01 | N+1 queries | Existing `n_plus_one` detector and template/serializer static scans. | Extend static analysis to queries in plain Python loops, with Django and SQLAlchemy awareness. Avoid treating every query in a loop as an unconditional defect. |
| P02 | Aggregates computed in Python | Partial: `inefficient_count`. | Static patterns such as `sum(x.field for x in queryset)` and counters over querysets. Runtime row volume can support an investigation but does not prove aggregation or unused results. |
| P03 | Repeated scans of the same data | Proposed. | Group queries in a request by table and predicate structure, with different projections or aggregates. Suggest examining a combined or conditional aggregate only when predicates and semantics permit it. |
| P04 | Loading unneeded columns | Partial: `large_result` covers row count. | Use optional catalog metadata to identify full-row queries fetching large TEXT, JSON, or BLOB columns. Suggest `.only()`, `.defer()`, or `load_only()` for review; query shape alone cannot prove a column is unused. |
| P05 | Full ORM objects for reports | Proposed. | Count model instances built using suitable Django or SQLAlchemy hooks and correlate with report routes or serialization. Suggest projections where applicable; instance count alone cannot prove objects are unnecessary. |
| P06 | Filtering in Python instead of SQL | Proposed. | Static comprehension or loop patterns over known querysets. Runtime fetched-versus-used evidence requires additional instrumentation. Preserve null, comparison, and application-specific semantics. |
| P07 | Targeted indexes | Existing index advisor. | Retain and improve existing advice where specific gaps are identified; this is not an entirely new detector. |
| P08 | Covering indexes | Partial. | Extend index advice for queries whose filters, grouping, and projection could be served by a suitable covering index. Account for backend rules, index size, write overhead, and PostgreSQL `INCLUDE` semantics. |
| P09 | Planner selected a slower access path | Partial: EXPLAIN and plan analysis. | Report plan evidence such as large examined-versus-returned row counts and investigate suitable existing indexes. An unused index alone does not prove the planner chose incorrectly; stronger claims require controlled comparison. |
| P10 | Repeated JSON parsing instead of a precomputed column | Proposed. | Database checks for JSON extraction matching an existing generated expression or column; profiler evidence for costly JSON decoding. These are distinct signals and require semantic validation before recommending a rewrite. |
| P11 | Repeated Python passes over one collection | Proposed. | Static detection of fixed-range outer loops that repeatedly scan the same collection. Suggest one-pass bucketing where data dependencies permit. |
| P12 | Sorting or searching inside loops | Proposed. | Static detection of invariant `sorted()`/`.sort()` calls or repeated membership tests against list-like collections. Suggest sorting once, `bisect`, or a set where mutation, ordering, hashing, and equality semantics allow. |
| P13 | Oversized cache payloads | Partial: existing cache efficiency analysis. | Add bounded payload-size and encoding/decoding measurements where integrations can observe them. Report large or expensive entries without capturing sensitive payloads or adding expensive serialization solely for measurement. |
| P14 | Summing durations in Python | Shares detection with P02. | Identify duration aggregation candidates. State explicitly that database versus Python rounding, precision, null handling, and duration semantics need human review and response verification. |

### Original proposed implementation groups

The handoff suggested these groups; none has been selected:

1. **Runtime detectors:** P03, runtime evidence for P02/P06/P14, P05, P04, P13.
2. **Database checks:** P08, P09, database aspects of P10.
3. **Static Python analysis:** extensions for P01, P02/P06, P11, P12.

Static Python analysis should follow existing scan conventions, with text and JSON output, documented exit behavior, and framework-aware guards. Catalog inspection and extra database queries should be explicit, on-demand work rather than new hidden request-path overhead.

## Recommended starting selections

For the current **v0.3 Compare/CI** focus:

- **F01:** pytest performance assertions.
- **F02:** per-endpoint performance budgets.
- **F04:** JSON array comparison by identity.
- **F06:** baseline compatibility checks.
- **F20:** instrumentation health report.

For additional runtime/database findings with concrete evidence:

- **F09:** repeated individual writes.
- **F10:** deep OFFSET pagination.
- **F12:** connection-pool wait issues.

These are recommendations for selection, not agreed release scope.

## Overlaps to resolve before implementation

- **P02/P14:** share an aggregation detector; duration-specific guidance belongs in the finding text.
- **P02/P06:** runtime row-volume observations are supporting evidence, not proof of Python-side processing.
- **F09/P01:** repeated writes should have dedicated semantics rather than blindly reusing read-oriented N+1 advice.
- **F11/existing query limits:** coordinate total-time and count rules to avoid multiple findings for one problem.
- **F12/F21:** share pool-wait attribution.
- **F15/P04/P13:** measure different payloads: HTTP responses, database columns, and cache entries.
- **F17/F18/F20:** use common evidence and observation-quality metadata.
- **F22:** reuses existing refactor checks; it does not require a second comparison engine.

## Existing capabilities that should not be proposed as new

- Refactor-check JSON, Markdown, and JUnit output.
- Response diffs with ignored and automatically identified volatile fields.
- Query-count regression checks and timing verdicts with noise handling.
- Load testing with latency/error gates and bottleneck analysis.
- Release history and before/after comparisons.
- Existing SQL, transaction, serialization, outbound-call, result-size, and async-blocking detectors.
- Cache efficiency aggregates and recorded SQLAlchemy pool-wait spans.

## Sources and technical foundations

- Local implementation and specifications: `CONTRACT.md`, `HANDOFF.md`, `CHANGELOG.md`, `docs/roadmap.md`, `docs/refactor-check.md`, `docs/detectors.md`, `docs/transactions.md`, and the corresponding core modules.
- [pytest custom markers and plugin examples](https://docs.pytest.org/en/stable/example/markers.html): foundation for F01.
- [PostgreSQL LIMIT and OFFSET](https://www.postgresql.org/docs/18/queries-limit.html): skipped OFFSET rows still require computation; ordering matters for pagination.
- [SQLAlchemy engine and connection documentation](https://docs.sqlalchemy.org/en/21/core/connections.html): batching can reduce database round trips, subject to backend and statement behavior.

## Selection record

- Selected IDs: F01, F02, F04–F07, F09–F13, F15, F17–F22, and P01–P14.
- Deferred to the next release: F03, F08, F14, F16.
- UI requirement: every selected capability needs a UI workflow or inspectable report and reproducible test case.
- Target version: v0.3.
- Implementation order: see the staged expansion in PLAN.md.
- Acceptance criteria: define after selection, before implementation.
