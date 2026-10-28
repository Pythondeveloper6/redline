# Command line

[← Index](index.md)

Redline has two kinds of commands:

- the **`redline`** program, installed with `redline-core`. It works on files and a running
  app from the outside: load tests, scenarios, configuration checks.
- **framework commands** (`manage.py redline_*`, `flask redline ...`) that run inside your
  app and so know its settings, models and templates.

## `redline`

```text
redline load SCENARIO [options]       run a load test against a running app
redline load init ...                 write a scenario file
redline check record|compare|run|init|show   refactor check: prove a refactor is safe
redline config                        print the effective configuration
redline doctor                        check the setup
redline hash-password                 make a password hash for access = "basic"
```

Exit codes: `0` success, `1` a check or threshold failed, `2` a usage, configuration or
scenario error, `130` interrupted with Ctrl-C. `redline check` adds `3`, the app could not be
reached.

### `redline load`

Drives an app at a controlled request rate and records the run. `--fail-p95-ms` and
`--fail-error-rate` make it a CI gate. It is documented in full on the
[Load testing](load-testing.md) page, including the [options table](load-testing.md#cli-reference).

```bash
redline load scenario.toml --steps 10:100:10 --step-duration 20s
redline load scenario.toml --rps 50 --duration 60s --fail-p95-ms 400
```

### `redline load init`

Writes a scenario from an OpenAPI document or from the traffic Redline has captured.

```bash
redline load init --from-openapi http://localhost:8000/openapi.json -o scenario.toml
redline load init --from-traces --top 15 --window 7d -o scenario.toml
```

See [Generating scenarios](load-testing.md#generating-scenarios).

### `redline check`

The [refactor check](refactor-check.md): replay a suite of read-only requests against the old
code, then the new code, and compare the responses and the timings. Needs `redline-core[load]`
(httpx). Python 3.10 needs `tomli` for TOML suites (JSON suites always work).

```text
redline check record  SUITE [options]   run a suite and write a baseline
redline check compare SUITE [options]   run it again and compare with a baseline
redline check run     SUITE [options]   record if there is no baseline file yet, else compare
redline check init --from-traces [...]  write a suite from recorded read-only traffic
redline check show    REPORT.json       print a saved report again
```

`SUITE` is a `.toml` or `.json` [suite file](refactor-check.md#the-suite-file).

| Option | On | Meaning |
|---|---|---|
| `--base-url URL` | record, compare, run | Override the suite's `base_url` |
| `--store PATH` | record, compare, run | The app's Redline store, for [server-side metrics](refactor-check.md#server-side-metrics). Default: the configured `storage_path` when that file exists |
| `--no-store` | record, compare, run | Client-side metrics only |
| `--label TEXT` | record, compare, run | A label for this run (for example `before`) |
| `-q`, `--quiet` | record, compare, run | No progress line |
| `--out FILE` | record, run | Baseline file to write. Default `<suite>.baseline.json` next to the suite |
| `--baseline FILE` | compare, run | Baseline JSON. Default `<suite>.baseline.json` |
| `--baseline-id ID` | compare, run | A baseline stored in `--store` instead of a file. Not together with `--baseline` |
| `--json OUT` | compare, run | Write the full report as JSON |
| `--markdown OUT` | compare, run | Write a Markdown PR comment |
| `--junit OUT` | compare, run | Write JUnit XML, one test case per request |
| `--fail-on LIST` | compare, run, show | Verdicts that make the exit code `1`: any of `slower`, `changed`, `broken`, `missing`, `noisy`, `faster`, or `none`. Default `slower,changed,broken,missing` |
| `--slower-pct X`, `--slower-ms Y` | compare, run | Override `thresholds.slower_pct` and `thresholds.slower_ms` for this run |
| `--no-color` | compare, run, show | No ANSI colours |

`redline check init` takes `--from-traces` (required), `--store PATH`, `--route PATTERN`
(repeatable `fnmatch` pattern on the route), `--limit N` (default `20`), `--base-url URL`,
`-o`/`--out OUT` (default `check.toml`; `.json` writes JSON; `-` writes to stdout) and
`--force`. It writes `base_url` only when `--base-url` is given or the traces show a single
host; otherwise it leaves a commented `# base_url = "http://127.0.0.1:8000"` line to fill in.
It writes one request per read-only route Redline has seen, using the most recent
concrete path; header values are never copied, and an `Authorization` or `Cookie` header gets
a `${env:REDLINE_CHECK_AUTHORIZATION}` or `${env:REDLINE_CHECK_COOKIE}` placeholder.

`redline check show` takes the report JSON and prints it again; `--markdown OUT` and
`--junit OUT` write those files from it. It exits `0` unless `--fail-on` is given; with
`--fail-on` it exits `1` when a request matches it, as `compare` does.

```bash
redline check init --from-traces --route '/orders*' -o checks/orders.toml
redline check record checks/orders.toml --label before
redline check compare checks/orders.toml --markdown report.md --junit report.xml
```

Exit codes: `0` nothing matched `--fail-on`, `1` something did, `2` a usage, suite or baseline
error, `3` the app could not be reached at all, `130` interrupted (Ctrl-C or SIGTERM; a
`record` then writes no baseline). More in [Outputs and exit codes](refactor-check.md#outputs-and-exit-codes).

### `redline config`

Prints the **effective configuration**: every setting's final value, grouped (capture,
sampling, detectors, storage, access, features, other), which layer it came from (`default`,
`preset`, `file`, or `env REDLINE_X`) and any warnings. Secrets are shown as `***`. Use it
first when a setting seems to be ignored.

```bash
REDLINE_PRESET=production redline config
redline config --json                       # the same data as GET api/config
redline config --project-root ./backend     # where to look for redline.toml
```

It reads the file and environment layers only. Settings you give in code
(`REDLINE = {...}`, `install(settings=...)`) are not visible to a separate command; the
running app's complete view is the UI's Settings page and `GET api/config`. An invalid
configuration (for example `access = "token"` without a token, or a misspelled
`REDLINE_*` variable) prints the error and exits with `2`.

See [Configuration](configuration.md).

### `redline doctor`

Checks that the setup is sound. Each line is `OK`, `WARN` or `FAIL`; the command exits `1`
if any check fails or the configuration does not load, and `0` otherwise (warnings do not
fail it). It takes `--json` and `--project-root DIR` like `redline config`.

| Check | Looks at |
|---|---|
| `storage` | the store path exists or can be created, and is writable |
| `ui bundle` | the prebuilt UI is present in `redline-core` |
| `access` | `open` fails (warns when `REDLINE_ALLOW_OPEN=1` is set); `access` not set in the file or environment warns (FastAPI, Flask and Litestar answer `404` outside debug mode unless the app sets it in code); `trust_unix_socket` warns; `local` outside the `dev` preset warns, because behind a proxy nobody can reach Redline |
| `token` | shorter than 24 characters (warn), an unreadable `token_file` (fail), a `token_file` readable by other users (warn) |
| `basic` | an invalid hash (fail), fewer than 100 000 PBKDF2 iterations (warn) |
| `sessions` | `token` or `basic` without `secret_key`: logins are per worker process (warn) |
| `proxies` | `token`/`basic` with `trusted_proxies` empty (warn: behind a proxy every client shares one login rate-limit bucket, and `X-Forwarded-Proto` is not believed for the `Secure` cookie), or another non-`local` policy outside `dev` |
| `capture` | the `production` preset capturing request or response bodies or locals (warn) |
| `retention` | `history_retention_days` off, or `max_rows` and `max_age_days` both off (warn) |
| `load` | `load_from_ui` on but `httpx` missing (warn) |

It sees the file and environment layers only, like `redline config`. Run it in your deploy
pipeline and after changing access settings.

### `redline hash-password`

Prompts for a password twice (a mismatch exits `2`) and prints a PBKDF2-SHA256 hash to put
in `basic_users`. The password is never echoed or stored. When stdin is not a terminal it
reads one line from stdin instead, so it can be scripted. `--iterations N` sets the cost
(default 600 000, minimum 1 000). See [Access control](access-control.md#basic).

```bash
redline hash-password
echo 'correct horse' | redline hash-password --iterations 800000
```

## Django

| Command | Does |
|---|---|
| `manage.py redline_clear [--noinput]` | Delete every recorded trace (asks to confirm unless `--noinput`). History is kept |
| `manage.py redline_prune [--max-age-days N] [--max-rows N]` | Apply [retention](storage.md#retention) now. `0` disables that rule for this run |
| `manage.py redline_scan [PATHS] [--json] [--fail] [--dialect django\|jinja] [--no-drf]` | [Static N+1 scan](static-scan.md) of templates and DRF serializers |
| `manage.py redline_scenario [-o FILE] [--base-url URL] [--force]` | Generate a load-test [scenario](load-testing.md#generating-scenarios) from your URLconf (stdout without `-o`) |

## Flask

| Command | Does |
|---|---|
| `flask redline clear [--yes]` | Delete every recorded trace (asks to confirm unless `--yes`, `-y` or `--noinput`) |
| `flask redline prune [--max-age-days N] [--max-rows N]` | Apply retention now |
| `flask redline scan [--json] [--fail]` | [Static N+1 scan](static-scan.md) of the Jinja templates of the app and its blueprints |
| `flask redline scenario [-o FILE] [--base-url URL] [--force]` | Generate a load-test [scenario](load-testing.md#generating-scenarios) from the `url_map` (stdout without `-o`; default base URL `http://127.0.0.1:5000`) |

## FastAPI and Litestar

These adapters have no command group of their own. Use the `redline` program, and the store
directly for housekeeping:

```python
from fastapi_redline import install

redline = install(app, engine=engine)
redline.store.prune(max_age_s=86_400, max_rows=1_000)
```

Scenarios come from the app's OpenAPI document:

```bash
redline load init --from-openapi http://localhost:8000/openapi.json
```
