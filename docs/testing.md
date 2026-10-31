# Full-stack release testing

The example projects share [`examples/compose.yaml`](https://github.com/Pythondeveloper6/redline/blob/main/examples/compose.yaml).
It provisions disposable PostgreSQL, MySQL, SQL Server and Redis services with health checks,
and an image containing all framework adapters, database drivers, Celery and Chromium.

From the repository root:

```bash
docker compose -f examples/compose.yaml up --build \
  --abort-on-container-exit --exit-code-from tests tests
```

The runner executes the complete package suites, including network tests, live database
request/transaction/privacy cases, plans and compile checks, a real Redis/prefork worker,
and browser tests of the built UI. It also checks demos, docs, lint, package metadata and
fresh wheel installation. Integration skips or missing reports fail the run.

`examples/testing/results/summary.json` must show `qualified: true` for the current run,
and the Compose command must exit zero. Results are ignored by Git. Each run keeps
separate logs, JUnit reports and actual dependency versions. Browser screenshots support
review but do not replace a full accessibility audit or production load testing.

Interactive demo services are available through the `demos` profile with host loopback
ports 8011–8014 and disposable token authentication. The shops use SQLite; the integration
matrix separately exercises real databases. All database credentials/data are test-only.

SQL Server's image requires Linux amd64. Microsoft does not support emulation on Apple
Silicon; failures there remain failures. Native amd64 CI provides the supported environment.
The existing CI trigger remains **main pushes only**, with no PR/manual test trigger and
no implicit publishing authorization.

See the [complete test-project guide](https://github.com/Pythondeveloper6/redline/blob/main/examples/testing/README.md)
for prerequisites, commands, demo URLs, driver coverage, cleanup and current verification
limits. A passing Compose baseline is one release gate; validate actual deployment
storage, concurrency, drivers, permissions and framework versions as described in
[Production](production.md).


## The query-count ratchet

A ratchet is a committed file of numbers that may only improve: a pull request that adds
queries to an endpoint or a test fails, one that removes queries tightens the file.

**Endpoints**, from a [refactor-check suite](refactor-check.md):

```bash
redline check ratchet checks/orders.toml --store .redline/check.db --update   # create it
redline check ratchet checks/orders.toml --store .redline/check.db           # CI: fail on regressions
redline check ratchet checks/orders.toml --store .redline/check.db --update   # after a green run: tighten
```

The file defaults to `<suite>.ratchet.json` (`--ratchet-file`). Per request it stores the
median query count (needs `--store`, see
[server-side metrics](refactor-check.md#server-side-metrics)) and the median latency.
Query counts are exact: one more query is a regression. A latency regression needs the median
to grow by more than both `--latency-pct` (30) and `--latency-ms` (20); `--no-latency` tracks
queries only. Without `--update` nothing is written (and the file must exist); with it, the
file is written only when nothing regressed: improvements lower the numbers, new endpoints
are added, nothing is ever raised. Keys are sorted, so diffs stay small. A request that is
broken (at least half of its calls failed) is reported and fails the run. `--json` and
`--markdown` write the verdict; the exit code is `1` on regressions.

**Tests**, with the pytest plugin (tests that use the `redline` marker or `--redline-all`):

```bash
pytest --redline-ratchet tests/queries.ratchet.json                          # fail on more queries than recorded
pytest --redline-ratchet tests/queries.ratchet.json --redline-ratchet-update # tighten / add, only if the whole session passed
```

Each test is keyed by its node id and records the query count of its busiest request. The
same file can hold both the `endpoints` and `tests` sections.
