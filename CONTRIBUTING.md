# Contributing to Redline

Thank you for considering it. This document is the whole of what you need: how to get a
working environment, how to run the checks, how the packages relate, and the conventions
the codebase enforces.

By participating you agree to the [Code of Conduct](https://github.com/Pythondeveloper6/redline/blob/main/CODE_OF_CONDUCT.md).

---

## The layout

One repository, one [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/),
five distributions released in lockstep:

| Directory | Distribution | Module | Depends on |
| --- | --- | --- | --- |
| `packages/redline-core` | `redline-core` | `redline_core` | nothing (ships the bundled UI) |
| `packages/django-redline` | `django-redline` | `django_redline` | core (exact pin), Django |
| `packages/fastapi-redline` | `fastapi-redline` | `fastapi_redline` | core (exact pin), FastAPI, SQLAlchemy |
| `packages/flask-redline` | `flask-redline` | `flask_redline` | core (exact pin), Flask, SQLAlchemy |
| `packages/litestar-redline` | `litestar-redline` | `litestar_redline` | core (exact pin), Litestar, SQLAlchemy |

Plus the React frontend (`frontend/`), the shared documentation site (`mkdocs.yml`,
`docs/`), runnable examples (`examples/django`, `examples/fastapi`, `examples/flask`), benchmarks
(`benchmarks/`) and helper scripts (`scripts/`).

**Where does a change go?** If it is about *what the profiler records or reports* (trace
model, recorder, SQL fingerprinting, detectors, EXPLAIN analysis, storage, redaction,
the JSON API, the load tester), it belongs in the core, and every adapter picks it up. If it is about
*how a framework talks to the core* (middleware, SQL hooks, URL routing, management
commands, fix suggesters for an ORM), it belongs in that adapter. The core imports no
framework, ever.

## Set up

```bash
git clone https://github.com/Pythondeveloper6/redline redline
cd redline
uv sync --all-packages
```

That creates `.venv/` with all three packages installed in editable mode plus the dev
tools. Prefix commands with `uv run`; there is no need to activate anything.

## Run the tests

Each package has its own pytest configuration (pytest-django needs a settings module that
the others must not see), so the suites run one per package. From the repository root:

```bash
uv run python scripts/run_tests.py                  # all five packages, one summary
uv run python scripts/run_tests.py core flask       # a subset
uv run python scripts/run_tests.py core -- -x -k n_plus_one   # extra pytest arguments
uv run python scripts/run_tests.py --cov            # with coverage
```

or one package directly, from its directory:

```bash
(cd packages/redline-core && uv run pytest)
(cd packages/django-redline && uv run pytest)
```

Release checks that need the network: `scripts/check_clean_install.py` (fresh-venv install
from PyPI or built wheels) and `scripts/check_store_upgrade.py` (a store written by an
older release opens with this one); the *Compatibility* workflow runs both weekly.
To measure overhead on your own app over HTTP, see `benchmarks/real_app.py`.

Postgres-only tests (EXPLAIN against a real server) skip cleanly when no Postgres server
is available, and they say so in the output.

## Frontend

The UI is a React 19 + Vite + TypeScript + Tailwind app in `frontend/`. Its build output
is committed into `packages/redline-core/src/redline_core/ui/static/`, so the wheels
ship a ready-made bundle and users never need Node.js.

After changing anything under `frontend/`, rebuild the bundle:

```bash
scripts/build_ui.sh
```

CI fails if the committed bundle is stale, so always commit the rebuilt static files
together with the source change.

## Lint and format

```bash
uv run ruff check .
uv run ruff format .
uv run python scripts/check_versions.py
```

## Build the docs

```bash
uv run --with mkdocs-material mkdocs serve
uv run --with mkdocs-material mkdocs build --strict   # what CI runs
```

The site is published to <https://pythondeveloper6.github.io/redline/> by
`.github/workflows/docs.yml` when changes land on `main`.

## Conventions this codebase enforces

1. **The request path never raises into the application.** Capture failures are logged
   (rate-limited) and the request continues unaffected. A test should prove any new
   failure mode does the same.
2. **The recorder costs nothing when inactive.** Anything that runs on every query or
   every request must short-circuit when no trace is active. Benchmarks in `benchmarks/`
   guard this.
3. **Captured data is redacted before it is stored.** Anything that reaches the store
   passes through `redact.py`. Adding a new capture point means adding it to the
   redaction tests.
4. **Storage is separate from the application database.** Redline never writes to the
   app's own database. New stores must pass the store contract tests in `redline_core`.
5. **Lockstep versions.** All five packages carry the same version and the adapters pin
   `redline-core==` that version. `scripts/check_versions.py` enforces it.

## Commit messages

Say what changed and why in the body. The history is the design record.

## Releasing

Releases are cut by the maintainer. The packages are not on PyPI yet, so users install from
GitHub; this repository contains no PyPI publish automation. Only the documentation is
deployed automatically (to GitHub Pages).
