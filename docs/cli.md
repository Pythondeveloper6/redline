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
redline config                        print the effective configuration
redline doctor                        check the setup
redline hash-password                 make a password hash for access = "basic"
```

Exit codes: `0` success, `1` a check or threshold failed, `2` a usage or scenario error,
`130` interrupted with Ctrl-C.

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

### `redline config`

<!-- verify: redline config / doctor / hash-password -->
Prints the **effective configuration**: every setting's final value, which layer it came
from (default, preset, file, env or framework) and any warnings. Secrets are shown as
`***`. Use it first when a setting seems to be ignored.

```bash
REDLINE_PRESET=production redline config
```

See [Configuration](configuration.md).

### `redline doctor`

<!-- verify: redline config / doctor / hash-password -->
Checks that the setup is sound:

| Check | Looks at |
|---|---|
| Storage | the store path exists or can be created, and is writable |
| UI bundle | the prebuilt UI is present in `redline-core` |
| Access policy | is it safe for the chosen preset? (an open policy under `production`, `read_only` off) |
| Token strength | a short or guessable token |
| Proxies | `trusted_proxies` too wide, or a proxy in front but none trusted |

Run it in your deploy pipeline and after changing access settings.

### `redline hash-password`

<!-- verify: redline config / doctor / hash-password -->
Prompts for a password and prints a PBKDF2-SHA256 hash to put in `basic_users`. The
password is never echoed or stored. See [Access control](access-control.md#basic).

## Django

| Command | Does |
|---|---|
| `manage.py redline_clear [--noinput]` | Delete every recorded trace (asks to confirm unless `--noinput`). History is kept |
| `manage.py redline_prune [--max-age-days N] [--max-rows N]` | Apply [retention](storage.md#retention) now. `0` disables that rule for this run |
| `manage.py redline_scan [PATHS] [--json] [--fail] [--dialect django\|jinja] [--no-drf]` | [Static N+1 scan](static-scan.md) of templates and DRF serializers |
| `manage.py redline_scenario` | Generate a load-test scenario from your URLconf <!-- verify: redline_scenario --> |

## Flask

| Command | Does |
|---|---|
| `flask redline clear [--yes]` | Delete every recorded trace |
| `flask redline prune [--max-age-days N] [--max-rows N]` | Apply retention now |
| `flask redline scan` | Static N+1 scan of Jinja templates <!-- verify: flask redline scan / scenario --> |
| `flask redline scenario` | Generate a load-test scenario from the `url_map` <!-- verify: flask redline scan / scenario --> |

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
