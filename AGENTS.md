# AGENTS.md

Guidance for AI coding agents working in this repository. Humans: see `CONTRIBUTING.md`.

## Layout

- `packages/redline-core`: engine, CLI, API, detectors, storage, UI bundle.
- `packages/{django,fastapi,flask,litestar}-redline`: thin framework adapters.
- `frontend/`: the React UI, built into `packages/redline-core/src/redline_core/ui/static`
  and committed (rebuild with `scripts/build_ui.sh`).
- `docs/`: MkDocs site. `docs/llms.txt`, `docs/llms-full.txt` and `docs/reference/*.json` are
  **generated**: run `uv run python scripts/build_llms.py` after changing docs, the CLI or
  settings.
- `examples/`: one runnable demo shop per framework.

## Commands

```bash
uv sync --all-packages
uv run python scripts/run_tests.py            # every package (pytest-django needs its own session)
uv run python scripts/run_tests.py core -- -x # one package, extra pytest args
uv run ruff check . && uv run ruff format --check .
uv run python scripts/build_llms.py --check   # generated agent docs are current
cd frontend && npm run build && npm test
```

## Conventions

- Test first. New behaviour needs a test that failed before the change.
- Detectors need documented evidence, thresholds, limits and a false-positive test.
- Counts and costs from sampled traces are labelled sampled and never presented as additive.
- Prompt and completion text of LLM calls is never captured.
- Versions are lockstep across the five packages (`scripts/check_versions.py`).
- Do not push, tag, publish or open pull requests unless the owner asks.

## Using redline from an agent

Run `redline agent-skill --format agents` for the section that teaches an agent to find, fix
and verify findings, or see `docs/agents.md`.
