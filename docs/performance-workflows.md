# Performance checks and analysis tools

[← Index](index.md)

The v0.3 development build connects findings, response comparison and regression checks.
The UI exposes the same analysis engines as the command line. Open **More → Analysis**
for source analysis, recording health, suite coverage and suppressions.

## Try it in the UI

1. Run one of the demo shops and visit `/patterns/` (Django) or `/patterns` (the other
   shops). Open the recorded request's **Issues** tab. The original implementation runs
   four queries and deliberately repeats aggregates, uses deep OFFSET pagination and
   constructs full model objects.
2. Press **Try detector thresholds** to evaluate different thresholds against the recorded
   evidence. This preview does not change server settings or saved findings. Missing or
   truncated evidence cannot be reconstructed.
3. In **Analysis → Detector samples**, record the nine synthetic samples to inspect
   additional findings, including pool waits, large binary cache entries and sampled JSON
   decoding. Their operations and timings are illustrative, not application measurements;
   synthetic requests cannot be replayed as real endpoints.
4. Create a check suite with the rules below, record a baseline, then restart the shop with
   `SHOP_REFACTOR=1` and compare. The pattern endpoint drops from four queries to two and
   avoids full model construction. It deliberately reverses the response array and changes
   `total` by 0.005 to exercise the explicit comparison rules.

The demo settings explicitly enable check runs and source analysis from the UI. Keep
these development settings scoped to the demo. For test servers, use ports 8011 or higher.
The examples' existing READMEs explain the two terminals: the app directory for the server,
and the repository root for the commands.

For an in-process check of all four demos without opening server ports:

```bash
uv run --no-sync python scripts/check_demo_patterns.py
```

## Endpoint budgets and response rules

In **Checks → New suite**, each request has **Budgets and response rules**. Saving,
editing and exporting a suite preserves these rules.

```toml
name = "product report"
repeat = 20
warmup = 3
environment = { dataset = "shop-fixture-1", database = "SQLite fixture" }

[[requests]]
name = "performance patterns"
path = "/patterns"
array_keys = { items = "id" }
tolerances = { total = { absolute = 0.01 } }
budgets = { max_queries = 4, max_median_ms = 500, forbidden_issues = [] }
```

Use `/patterns/` for Django. Choose budgets for your environment; the example's latency
limit is illustrative.

| Rule | Meaning |
|---|---|
| `max_queries` | Maximum observed queries in any timed call; a whole number |
| `max_external_calls` | Maximum observed outbound HTTP calls in any timed call; a whole number |
| `max_median_ms` | Upper bound on the current run's client-side median latency |
| `max_p95_ms` | Upper bound on the current run's interpolated p95; use enough repeats |
| `forbidden_issues` | Issue kinds that must not occur, for example `n_plus_one` |
| `array_keys` | Match elements at a configured JSON path by an identity field |
| `tolerances` | Allow a numeric difference at a configured JSON path |

Budgets are independent of improvement against the baseline. A slow baseline does not
make a budget pass. Missing required measurements, incomplete server metric collection,
and broken responses cannot pass a budget. Query counts remain usable when spans are
truncated; issue and external-call coverage require complete evidence.

Budget failures appear in the suite badges, report summary, per-request details, JSON,
Markdown, terminal output and JUnit. The default `--fail-on` includes `budget`; use an
explicit list or `none` to change that policy.

Array matching is opt-in: ordering may be part of an endpoint's contract. Every element
must have a unique non-null string or numeric identity; missing or duplicate identities
fail comparison. Added and removed elements remain differences. Paths within a matched
array refer to the aligned baseline order, so prefer wildcard ignore paths inside it.

Numeric tolerance accepts a difference no greater than
`max(absolute, relative * abs(baseline))`. Relative values are fractions: `0.01` means 1%.
Booleans, missing fields and type changes are not hidden by a numeric tolerance. Ignored
paths retain precedence. The report lists the configured comparison rules; a `same`
verdict under these rules means equivalent under those rules, not byte-identical.

## Environment and coverage

Kept requests record the target process's Python version, framework and installed
framework version when available. Check reports warn when observed target runtime
metadata changes or disappears. This is the application's runtime, not the runner's.

**Target environment labels** in the suite editor add dataset, database, Python, framework
and configuration labels. These are user declarations, not automatic database-version or
dataset verification. Hardware, data contents and shared-service state are not proven
equivalent by matching labels. Older baselines may lack metadata.

**Analysis → Suite coverage** compares a suite with the latest 500 retained traces. It
recognizes common Django, Flask, FastAPI and Litestar route parameters, shows missing and
single-input routes, and lists suite requests absent from observed traffic. Distinct
configured paths and headers count as different suite inputs. Unobserved routes and
unexercised branches remain unknown; this is not a completeness guarantee.

## pytest and no-query assertions

The core package registers a pytest plugin. Tests still need an installed adapter and an
instrumented in-process test client. The suite editor displays a pytest assertion snippet
for each request's query, outbound-call and issue policy.

```python
import pytest

@pytest.mark.redline(max_queries=5, max_external_calls=0, no_n_plus_one=True)
def test_products(client):
    assert client.get("/products").status_code == 200
```

Each captured request must meet the limits. No captured requests fails the assertion
rather than silently passing. Capture is context-local and includes sampled-out active
requests; it does not enable disabled instrumentation or ignored paths. Work in another
process or a thread that does not inherit context is outside this capture.

```python
from redline_core.testing import assert_no_queries

# Inside an active instrumented request, for example during serialization:
with assert_no_queries():
    result = serialize_preloaded_objects(objects)
```

Unexpected access raises with a callsite when available; an existing exception is
preserved. A route-wide no-SQL policy can also use `max_queries = 0` in a suite.

Stored test requests carry a `test_name` tag. Inspect them through **Analysis → Recording
health** and open the request for SQL, findings and exceptions. The UI configures and
explains policies; it does not execute arbitrary pytest or shell commands.

## Additional runtime findings

| Kind | Evidence | Setting and default |
|---|---|---|
| `repeated_writes` | Repeated individual INSERT/UPDATE statements; excludes executemany | `repeated_writes_threshold = 5` |
| `deep_offset` | Observed terminal OFFSET literal or integer bind | `deep_offset_rows = 10000` |
| `database_round_trips` | Many queries and substantial cumulative query cost | `round_trip_count = 10`, `round_trip_ms = 100` |
| `connection_pool_wait` | Captured connection checkout waits | `pool_wait_ms = 50` |
| `repeated_scan` | Distinct aggregates with the same table and predicate shape | `repeated_scans_threshold = 2` |
| `large_response` | Adapter-observed response bytes | `large_response_bytes = 1000000` |
| `many_orm_instances` | Django model construction or SQLAlchemy objects loaded into a session | `orm_instances_threshold = 500` |
| `large_cache_entry` | Known byte length of an already available binary value | `large_cache_bytes = 1000000` |
| `slow_cache_decode` | Explicitly instrumented decode block | `cache_decode_ms = 50` |
| `json_decode_hotspot` | Time attributed to standard-library JSON decoding in a profile | `json_decode_ms = 50` |

A zero threshold disables its corresponding rule. Counts are per request unless noted.
Round-trip findings avoid duplicating the existing count budget and repeated-query groups.
Similar aggregate shapes do not prove identical parameters or redundant work. Bulk writes
can change signals, validation and return behavior. Model counts do not prove objects are
unnecessary; Django includes newly constructed objects as well as database-loaded ones.
Response bytes do not measure network compression.

Redis reads and supported writes, and Django cache reads, can observe binary values
without copying them. Arbitrary objects and strings are not serialized or encoded just to
measure size. Network/cache operation time is not mislabeled as decode time. For a decode
block you control:

```python
import json
from redline_core.cache_payload import cache_decode

with cache_decode("reports:daily", payload=raw_bytes, backend="redis"):
    report = json.loads(raw_bytes)
```

Only size and duration are recorded, not the payload. Existing cache efficiency analysis
continues to report misses, unread writes and stampedes.

## Python source analysis

Use **Analysis → Python source** to paste or open a Python file, load the example, or scan
the configured `project_root`. Set `analysis_from_ui = true` explicitly, or leave it at
`"auto"` in framework debug mode. The production preset disables it; read-only mode refuses
the POST operations. The UI never accepts an arbitrary server filesystem root.

```bash
redline analyze ./app --json
redline analyze ./app --fail
# Explicit larger-project budget (defaults remain conservative):
redline analyze ./app --max-files 1000 --max-project-bytes 16000000 --json
```

Exit codes: `0` completed, `1` findings with `--fail`, `2` invalid/unreadable input or an
incomplete scan. Code is parsed and never imported or executed. Source inputs are capped
at 200 KB each; projects at 200 Python files, 2 MB and 20,000 visited entries. Dependency/generated directories
and symlinks are skipped. CLI users can raise the project budget with `--max-files`
(up to 10,000) and `--max-project-bytes` (up to 64,000,000). The same options are
available on `redline check-sql`; the per-file and 20,000-entry limits still apply.
UI scans keep the defaults. Errors, skipped files and limits remain visible.

Supported candidates include queryset operations inside loops, Python-side aggregation
and filtering, repeated fixed-range scans, repeated sorting, list membership inside loops,
and known blocking calls inside async functions. Queryset inference recognizes Django-style
chains and known SQLAlchemy Session/AsyncSession annotations or direct constructors;
Known `get`, `scalar`, `scalars`, `execute` and `query` calls are recognized.
Module-level local helpers are summarized through up to eight call edges; shadowed
helper names are excluded. A query in the first iterable is evaluated once and is
not classified as repeated by that loop; calls in later iterables can repeat.
Session identity-map hits, short-circuit branches and intentional chunking still need
runtime review. Cross-module helpers, unknown factories and dynamic aliases may be missed. Findings are suggestions for review,
not proof of runtime evaluation, unused data or a safe transformation. Preserve null,
precision, duration rounding, hashing, ordering and mutation behavior when fixing them.

## On-demand database checks

Expand a request's **SQL** group and press **Inspect columns / indexes**. Redline reads
catalog metadata through the adapter's executor and reports selected TEXT/JSON/binary
columns, covering-index candidates for supported aggregate shapes, and an indexed
generated JSON expression that matches the query. Existing indexes are inspectable.

This first pass requires a single unqualified table and conservative expression matches.
Generated expressions are read for PostgreSQL/MySQL; SQLite column inspection does not
reconstruct generated expressions from DDL. Unsupported shapes remain explicit limitations.
No application query, DDL or index creation is executed by this inspection.

EXPLAIN additionally flags plans that actually discard many more rows than they return.
That evidence does not prove the planner selected the wrong index. Compare plans and
measured behavior before changing indexes; consider write cost and visibility effects.

## Evidence, impact and verification

The **Issues** page can sort by observed cumulative issue time or occurrences. These
describe retained traffic, reflect sampling and can overlap across findings; they are not
predicted savings. Each request's Issues tab shows supporting spans, thresholds and
evidence completeness.

**Verify a fix** opens a suite seeded from the affected real request, with that issue kind
forbidden. Record before the change and compare afterwards. Missing metrics cannot satisfy
the policy. Review response differences and measured gains alongside issue resolution;
absence from an incomplete observation is not proof of a fix.

**Analysis → Suppressions** accepts an exact route, fingerprint and/or callsite with an
issue kind, reason and optional expiry. Multiple selectors must all match. Suppressed
findings remain stored and can be shown on the Issues page. Expired rules stop matching.
Suppressions affect presentation; pytest assertions and check budgets still inspect raw
findings. Removing a suppression does not delete traces.

**Recording health** shows retained/drop counts, profiling availability and recorded test
requests. A request's **Time breakdown and recording health** partitions wall time into
database-only, external-only, pool-wait-only, cross-category overlap and remaining time.
Concurrent spans are not added twice. Remaining time is uninstrumented time, not measured
CPU. Truncated spans, dropped logs and profile errors remain visible.
