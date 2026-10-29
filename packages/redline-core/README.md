<p align="center">
  <a href="https://pythondeveloper6.github.io/redline/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Pythondeveloper6/redline/main/docs/assets/logo-dark.svg">
      <img alt="Redline" src="https://raw.githubusercontent.com/Pythondeveloper6/redline/main/docs/assets/logo-light.svg" height="56">
    </picture>
  </a>
</p>

# redline-core

The framework-agnostic engine behind **Redline**, a self-hosted request profiler and
backend analyzer and load tester for Django, FastAPI, Flask and Litestar.

`redline-core` holds everything that does not depend on a web framework:

- the trace/span data model and a `contextvars` recorder that follows async tasks and explicitly
  propagated thread contexts; ordinary new threads need explicit context propagation;
- SQL fingerprinting, project-only callsite capture and secret redaction;
- detectors for N+1 queries, duplicate queries, slow queries and query budgets, plus
  event-loop blocking, long transactions, huge and unbounded results, `len(qs)` vs `.exists()`,
  slow serializers and slow, duplicated or retried external calls;
- an on-demand `EXPLAIN` runner with plan analysis for PostgreSQL, MySQL, SQLite and SQL Server (with documented driver limits);
- an optional `pyinstrument` flamegraph profiler (`redline-core[profile]`);
- a separate SQLite store with a background writer, retention, hourly rollups and release
  history;
- a load tester (`redline load`, extra `load`) with an open-model scheduler, knee detection
  and per-step bottlenecks;
- a refactor check (`redline check`, extra `load`): record a baseline of read-only requests,
  compare a new version, per-endpoint verdicts on responses and speed, JUnit and Markdown output;
- optional integrations in `redline_core.contrib` (SQLAlchemy, httpx, requests, redis,
  asyncpg, psycopg, Marshmallow, Strawberry, Graphene);
- the access policy and layered configuration shared by every adapter;
- the framework-neutral JSON API and the prebuilt web UI.

It has **no runtime dependencies**.

## You probably want an adapter

Install the package for your framework; it pulls in `redline-core` for you:

Published adapters are available on PyPI and pull in the matching core automatically.
Choose `django-redline`, `fastapi-redline`, `flask-redline` or `litestar-redline`:

```console
pip install --pre django-redline
```

This checkout describes the next v0.4 release. PyPI behavior depends on the installed
version; unreleased changes require a local source installation.

Extras on the core requirement: `profile` (flamegraphs), `load` (the load tester), and
`httpx`, `requests`, `redis`, `asyncpg`, `psycopg`, `strawberry`, `graphene`. Documentation:
<https://pythondeveloper6.github.io/redline/>.

## Timing your own code

```python
import redline_core

@redline_core.span("pricing.compute")
def compute_price(order): ...

async def handler():
    async with redline_core.span("fetch-rates", kind="custom", provider="ecb"):
        ...
```

`span()` does nothing (and costs almost nothing) outside a recorded request.


## v0.4 real-world workflows

The next release adds:

- Handled SQL failures with vendor/SQLSTATE/native-code classification, raw rowcounts,
  failed-attempt counts and stable issue groups, even when the application returns HTTP 200.
- SQL Server estimated plans, guarded actual-plan collection and catalog candidates;
  explicit engine ownership, optional all-engine observation and observed session-state evidence.
- Offline `redline check-sql` for supported raw-SQL Python shapes, JSON/JUnit reports,
  baselines, explicit compile checks and schema snapshots. Static checks are conservative.
- Generic background jobs, stages, row outcomes, retries and optional Celery signals;
  a Jobs page and commit-before-failure evidence for investigating partial job completion.
- SQL value policies, privacy profiles, route rules, request/user correlation and redaction preview.
- Bounded capture, failure-safe observation, storage auditing, read-only worker-store browsing,
  and explicit sanitized OTLP summary export.

Use these to investigate a caught database error, a migration that broke raw SQL, a worker
that committed before failing, an N+1 endpoint, or a performance refactor whose responses
must remain equivalent. The UI exposes SQL evidence, jobs, offline SQL checks and privacy
preview alongside the existing timelines, plans, profiles and refactor reports.

For production choose the **record-and-inspect** preset, private storage and authenticated
access. Replay, load/refactor runs, source analysis and database plan execution are disabled
by default there. Live MySQL/SQL Server drivers, broker/prefork behavior and the intended
filesystem/deployment must be qualified before rollout; fixture tests do not establish
compatibility with every codebase.

See the detailed [workflow guide](https://github.com/Pythondeveloper6/redline/blob/main/docs/real-world-workflows.md),
[production guide](https://github.com/Pythondeveloper6/redline/blob/main/docs/production.md)
and [feature catalog](https://github.com/Pythondeveloper6/redline#feature-catalog).

Please test Redline on a real development or staging project and
[open an issue](https://github.com/Pythondeveloper6/redline/issues/new) for bugs or feature
requests. Include framework/database/driver versions and a small sanitized reproduction.
Never post credentials or private recordings; follow the security policy for vulnerabilities.

## License

MIT
