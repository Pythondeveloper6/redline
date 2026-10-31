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

The MySQL cases (pymysql, aiomysql and asyncmy drivers, EXPLAIN, index advice, killed
connections) also run on every push in the lightweight `mysql` CI job against a plain
`mysql:8.4` service, without the full Compose stack; a skipped test fails that job.

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
