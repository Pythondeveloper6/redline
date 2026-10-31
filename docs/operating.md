# Operating Redline: its own logs, metrics and health

[← Index](index.md) · [Production](production.md)

When Redline runs anywhere other than a laptop, you want to know when *it* is unhealthy:
dropping traces, failing to write, or misconfigured. This page lists the signals it gives
about itself.

## Logs

Redline uses the standard library `logging` module and never configures handlers itself;
records go wherever your application sends logs.

| Logger | What it reports | Typical level |
|---|---|---|
| `redline` | startup warnings (open access, uninstrumented integrations with `auto_instrument="warn"`, a refused `install_if_dev` in production), writer failures, dropped lifecycle data | `WARNING`, `ERROR` |
| `redline.check` | refactor-check runs that could not store a result or read traces | `ERROR` (with traceback) |
| `redline.load` | load-test sink failures | `ERROR` (with traceback) |
| `redline.scan` | static-scan limits reached | `WARNING` |

Messages start with `Redline` (or the command name) and never include captured request
data, SQL parameters or credentials: they name the operation and the exception *type*.
Debug-level records (catalog reads, executor lookup) are off unless you enable `DEBUG` for
`redline`.

A JSON log pipeline needs nothing special. With `python-json-logger`, for example:

```python
import logging
from pythonjsonlogger.json import JsonFormatter

handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger("redline").addHandler(handler)
logging.getLogger("redline").setLevel(logging.WARNING)
```

To stamp *your application's* log lines with the id of the recording they belong to, add the
`TraceIdFilter` from `redline_core.correlation` (see [Logs and exceptions](logs-and-exceptions.md)).

## Metrics

`GET {mount}/api/metrics` returns Prometheus text format, behind the same access policy as
the rest of the API (so scrape it with the Redline token, or from an allowed network):

| Series | Meaning |
|---|---|
| `redline_writer_queue_depth` / `redline_writer_queue_capacity` | traces waiting for the writer thread, and the bound |
| `redline_traces_saved_total` | traces persisted |
| `redline_traces_dropped_total{reason=...}` | traces lost: `queue_full`, `write_failed`, `closed`, `capture_failed` |
| `redline_write_failures_total` | writer batches that failed |

Suggested alerts: any increase of `redline_traces_dropped_total{reason="write_failed"}` or
`redline_write_failures_total` (disk full, permissions, store locked); a sustained queue depth
near capacity (the app produces traces faster than the store absorbs them: lower the sample
rate). See [HTTP API](http-api.md) for the exact format.

## Health and doctor

- `redline doctor` (add `--production` in staging/production) checks the store location,
  filesystem, WAL, UI bundle, access settings and capture settings from inside the process
  environment. Run it in the real container.
- `GET {mount}/api/meta` reports recording state and drop counters for the UI.
- `redline audit-store` counts possibly unmasked keys in a store without printing values.
