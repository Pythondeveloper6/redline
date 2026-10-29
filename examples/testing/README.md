# Full-stack example and release tests

This shared Compose project gives the four example applications one test environment:
PostgreSQL 16, MySQL 8.4, SQL Server 2022 Developer, Redis 7.4, Python 3.13,
Django 5.2, SQLAlchemy sync/async drivers, Microsoft ODBC Driver 18, Celery prefork,
and Chromium. The application workspace is installed from `uv.lock`; the explicit
integration-only driver overlay is in `requirements.txt`. This is a single Linux
qualification configuration, not the entire supported Python/framework version matrix.

## Run the full suite

Start OrbStack or Docker, then run from the repository root:

```bash
docker compose -f examples/compose.yaml up --build \
  --abort-on-container-exit --exit-code-from tests tests
```

The command builds the current checkout, waits for service health, creates the fixed
`redline_test_integration` database on the Compose SQL Server, and runs:

- Frontend unit tests, TypeScript checks and the actual UI build during image construction.
- Ruff, formatting and lockstep package versions.
- All five complete package test suites, including socket-dependent tests and the
  existing live PostgreSQL/Redis tests.
- Flask and FastAPI sync, FastAPI async, and Django backend HTTP flows against all
  three database services: success, caught SQL failure with HTTP 200, parameter privacy,
  rollback and subsequent transaction behavior.
- Estimated plans, explicit SQL compilation, proof that compile checks do not persist
  an INSERT, schema snapshots and connection-setting cleanup. SQL Server also has
  explicit actual-plan and catalog checks.
- A real Redis-delivered Celery prefork worker: success, failure after commit, retry
  attempts, parent/request correlation, SQL/stage/row evidence and postfork writer shutdown.
- Chromium against a real HTTP demo: Jobs/detail evidence, SQL checker, privacy preview,
  mobile overflow checks and screenshots. This is not a complete accessibility audit.
- Both feature-demo harnesses, strict docs, wheels/source distributions, package metadata,
  fresh wheel installation and import/asset smoke checks.

**Success requires exit code 0 and the current `summary.json` showing `qualified: true`.**
Missing services, drivers, browsers, JUnit reports, skipped tests, or failed assertions do
not pass qualification. Without the explicit `REDLINE_TEST_COMPOSE=1` flag, live tests
skip and the full runner refuses to run. Compose supplies the flag and test-only DSNs.

Reports survive container exit in ignored `examples/testing/results/`. The top-level
`summary.json` describes the latest run; uniquely named subdirectories contain its logs,
JUnit, package artifacts and version manifest. Browser screenshots are under `ui/`.
No test report or exported recording is committed.

```bash
# Inspect service startup problems; the tests service may not start if a DB is unhealthy.
docker compose -f examples/compose.yaml ps -a
docker compose -f examples/compose.yaml logs --tail=100 mssql mysql postgres redis

# Remove ONLY this disposable project's containers and test-data volumes when finished.
docker compose -f examples/compose.yaml down --volumes --remove-orphans
```

The full job also runs in the existing CI workflow, **only on pushes to main**. It does
not add a PR/manual trigger or publish packages. Running this command before merging
provides local evidence; a successful main CI run is still required before publication.

## Browse the test projects

The same built image can seed and serve all four examples:

```bash
docker compose -f examples/compose.yaml --profile demos up --build \
  fastapi-demo flask-demo litestar-demo django-demo
```

| Project | UI |
|---|---|
| FastAPI | http://127.0.0.1:8011/_redline/ |
| Flask + new real-work playground | http://127.0.0.1:8012/_redline/ |
| Litestar | http://127.0.0.1:8013/_redline/ |
| Django | http://127.0.0.1:8014/redline/ |

The demo UI uses the disposable token `redline-local-compose-demo-token`; enter it at
Redline's login screen. This handles Docker's bridge address rather than relying on
localhost-only request authorization. Published ports bind to host loopback only.
Native example commands keep their existing access defaults. Seed data and demo stores
are inside their containers; these are development examples, not deployment templates.
The interactive shops use SQLite; the separate mandatory integration matrix uses the
three real database services. Do not mistake a working SQLite demo for SQL Server testing.

## Platform and isolation

Allocate about 8–12 GiB RAM and sufficient image/build disk space. SQL Server requires
at least 2 GiB itself. Its official container is `linux/amd64`; Microsoft supports it
only on native Linux Intel/AMD x86-64 and does not support Rosetta/QEMU emulation.
On Apple Silicon OrbStack may emulate it, but an unhealthy/failing SQL Server keeps the
full suite red. Use native Linux amd64 (including the CI job) for supported qualification;
do not remove SQL Server to make this test pass.

See Microsoft's [container requirements](https://learn.microsoft.com/en-us/sql/linux/install-upgrade/quickstart-install-docker?view=sql-server-linux-ver16)
and [ODBC installation instructions](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server).
The SQL Server service uses Developer edition for test/development use. Review the vendor
terms when adopting the image. Passwords in this Compose file are public disposable
fixture credentials; never reuse them for any application environment.

No database or Redis ports are published, no host Docker socket is mounted, and no
production DSN is accepted by the provisioning helper. Database tests verify the actual
`redline_test_` database identity before application-table SQL, use uniquely named tables,
and remove only their own data. Worker cleanup targets its own process group and queue;
it does not purge an existing broker. Named volumes belong to this Compose project only.

## Current evidence

The first full container run started the real services and passed the three live plan
cases and Redis/prefork case, but qualification failed. Test-runner configuration leaked
an unknown environment flag into application settings, PostgreSQL fixtures received the
wrong DSN scheme, repeated runs reused a wheel environment, and packaging validation
rejected the build tool's newer metadata format. These are corrected in source; metadata
is explicitly 2.4 for the existing validation toolchain. Git is now included for the
baseline tests. Storage doctor and two machine-speed-dependent tests were corrected too.

The full rerun is still required. The authoring session cannot access OrbStack's socket,
so local regression checks do not replace a new container result. Use the full command
above; require exit zero and a new current summary with `qualified: true`.
