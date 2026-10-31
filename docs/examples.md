# Example projects

The repository ships a runnable shop project per framework under `examples/`. Each seeds a
small database and exposes deliberately bad endpoints (N+1 queries, duplicate queries, a full
table scan, a slow call, an unhandled error) so every feature has something to show. Names,
data and endpoints are invented.

| Framework | Directory | Port |
|---|---|---|
| Django | `examples/django` | 8013 |
| FastAPI | `examples/fastapi` | 8011 |
| Flask | `examples/flask` | 8012 |
| Litestar | `examples/litestar` | 8014 |

Each directory has its own README with the exact commands and an endpoint table. Typical run,
from a checkout:

```bash
uv sync --all-packages
cd examples/fastapi
uv run --project ../.. python seed.py
uv run --project ../.. uvicorn shop.main:app --port 8011
# open http://127.0.0.1:8011/_redline/
```

## Smoke test

`scripts/smoke_examples.py` copies each example to a temporary directory, seeds a small
database, boots the app in-process, requests its endpoints and the Redline UI, and fails if
any response is not successful:

```bash
uv run python scripts/smoke_examples.py            # all four
uv run python scripts/smoke_examples.py flask      # one
```
