# Configuration

[← Index](index.md)

<!-- verify: configuration (presets, redline.toml, REDLINE_* env, redline config) -->

Redline has one configuration surface for every framework. The same settings, the same
names, the same validation: only the place you write the framework-specific part differs.
This page is about *how settings are combined*. The settings themselves are in the
[Settings reference](settings.md), and the access-related ones in
[Access control](access-control.md).

## Layers and precedence

Settings are layered. Each layer overrides the one before it:

| # | Layer | Where | Example |
|---|---|---|---|
| 1 | **Built-in defaults** | `redline_core.conf.Settings` | `sample_rate = 1.0` |
| 2 | **Preset** | the `preset` setting | `preset = "production"` |
| 3 | **Config file** | `redline.toml` or `[tool.redline]` in `pyproject.toml` | `sample_rate = 0.2` |
| 4 | **Environment** | `REDLINE_<SETTING>` variables | `REDLINE_SAMPLE_RATE=0.1` |
| 5 | **Framework settings** | `REDLINE = {...}`, `app.config["REDLINE"]`, `install(settings=...)`, `RedlinePlugin(settings=...)` | `"SAMPLE_RATE": 0.05` |

The last one wins. This lets you keep sensible defaults in a committed file, override them
per environment with variables (no code change, no rebuild of the image), and still have
the application code say the final word where it must.

A **preset** only changes *defaults*: any value set in layers 3 to 5 overrides it. Setting
`preset = "production"` and `sample_rate = 0.5` gives you production behaviour with 50%
sampling.

!!! tip "See what is in effect"

    ```bash
    redline config
    ```

    prints the effective configuration, **where each value came from** (default, preset,
    file, env or framework) and any warnings. Secrets are shown as `***`. The same view is
    in the UI's **Settings** page and at `GET api/config`.

## Presets

Presets are named bundles of defaults for the three situations most people are in.

| Setting | `dev` (default) | `staging` | `production` |
|---|---|---|---|
| Sampling | keep everything | `sample_rate = 0.25` | `sample_rate = 0.05`, plus always keep slow requests and errors |
| Request and response bodies | captured | captured | **not** captured |
| Exception locals | captured | **not** captured | **not** captured |
| Log capture | `INFO` and above | `INFO` and above | `WARNING` and above |
| Access | `local` (Django: staff) | `token` | `token`, **required** |
| Read-only | no | no | **yes** |
| Badge | `auto` (on in debug) | `auto` | **off** |
| Load tests from the UI | off | off | off |

Choose one:

=== "Django"

    ```python
    # settings.py
    REDLINE = {"PRESET": "production"}
    ```

=== "FastAPI"

    ```python
    install(app, engine=engine, settings={"preset": "production"})
    ```

=== "Flask"

    ```python
    app.config["REDLINE"] = {"preset": "production"}
    Redline(app, engine=engine)
    ```

=== "Litestar"

    ```python
    RedlinePlugin(engine=engine, settings={"preset": "production"})
    ```

=== "Environment"

    ```bash
    REDLINE_PRESET=production
    ```

The `production` preset requires an access token (`access="token"` with `REDLINE_TOKEN` or
`token_file`). Without one, `redline doctor` and the startup log warn that the UI is
locked: see [Access control](access-control.md#production-read-only). The details and
reasoning are in [Production](production.md#the-production-preset).

## The config file

Redline looks for `redline.toml` in `project_root`, then for a `[tool.redline]` table in
`pyproject.toml` there. Set `REDLINE_CONFIG=/path/to/file.toml` to use another file.

Keys are the setting names, in lower case:

```toml
# redline.toml
preset = "staging"

sample_rate = 0.25
slow_query_ms = 50
ignore_paths = ["/static/", "/favicon.ico", "^/health", "^/metrics"]
release = "2026.10.3"

# access
access = "token"
allowed_ips = ["10.20.0.0/16"]
trusted_proxies = ["10.0.0.0/8"]

[features]
replay = false
load = false
```

The same content works under `[tool.redline]`:

```toml
# pyproject.toml
[tool.redline]
preset = "staging"
sample_rate = 0.25
```

!!! note "TOML needs Python 3.11 or the `tomli` package"

    On Python 3.10 without `tomli` the file is **ignored with a warning**, not an error. Run
    `redline config` to see warnings.

!!! danger "Never put secrets in a file you commit"

    `token`, `basic_users` hashes and similar belong in environment variables or in a
    `token_file` that is not in version control. A committed `redline.toml` is the place
    for thresholds and ignore lists.

## Environment variables

Every setting has a variable: `REDLINE_` plus the setting name in upper case. Values are
parsed by the setting's type.

| Type | Written as | Example |
|---|---|---|
| bool | `1`, `true`, `yes`, `on` / `0`, `false`, `no`, `off` | `REDLINE_ENABLED=0` |
| int, float | a number | `REDLINE_SAMPLE_RATE=0.1` |
| string | the text | `REDLINE_ACCESS=token` |
| list | comma-separated | `REDLINE_ALLOWED_IPS=10.0.0.0/8,192.168.0.0/16` |
| table | JSON | `REDLINE_FEATURES={"replay": false}` |

Some useful ones:

```bash
REDLINE_ENABLED=0                      # switch recording off without a deploy
REDLINE_PRESET=production
REDLINE_ACCESS=token
REDLINE_TOKEN=...                      # from your secret manager
REDLINE_RELEASE=$GIT_SHA
REDLINE_STORAGE_PATH=/var/lib/myapp/redline.db
REDLINE_CONFIG=/etc/myapp/redline.toml
REDLINE_ALLOW_OPEN=1                   # needed (with access=open) to expose the UI openly
```

<!-- verify: env value parsing formats (lists, tables, unknown variables) -->
An unknown key in the **file** or the framework dict is an error with a "did you mean"
hint, exactly as in [Settings](settings.md#rules-that-apply-to-every-setting).

## A complete example per framework

All four do the same thing: staging preset, 10% sampling, a release name, and a token from
the environment.

=== "Django"

    ```python
    # settings.py
    import os

    REDLINE = {
        "PRESET": "staging",
        "SAMPLE_RATE": 0.1,
        "RELEASE": os.environ.get("GIT_SHA"),
        "IGNORE_PATHS": ["/static/", "/media/", "/favicon.ico", "^/health"],
        # the token itself comes from REDLINE_TOKEN in the environment
    }
    ```

=== "FastAPI"

    ```python
    install(app, engine=engine, settings={
        "preset": "staging",
        "sample_rate": 0.1,
        "release": os.environ.get("GIT_SHA"),
        "ignore_paths": ["/static/", "/favicon.ico", "^/health"],
    })
    ```

=== "Flask"

    ```python
    app.config["REDLINE"] = {
        "preset": "staging",
        "sample_rate": 0.1,
        "release": os.environ.get("GIT_SHA"),
        "ignore_paths": ["/static/", "/favicon.ico", "^/health"],
    }
    Redline(app, engine=engine)
    ```

=== "Litestar"

    ```python
    RedlinePlugin(engine=engine, settings={
        "preset": "staging",
        "sample_rate": 0.1,
        "release": os.environ.get("GIT_SHA"),
        "ignore_paths": ["/static/", "/favicon.ico", "^/health"],
    })
    ```

## Checking your setup

```bash
redline config        # the effective configuration, its sources and any warnings
redline doctor        # checks: storage writable, UI bundle present, access policy safe, token strength, proxies
```

Run `redline doctor` as a deploy-time check. See the [CLI reference](cli.md).

## Which layer should I use?

| You want to... | Use |
|---|---|
| Set thresholds the whole team shares | `redline.toml` in the repo |
| Differ per environment without code changes | `REDLINE_*` variables |
| Supply a secret | environment variable or `token_file` |
| Pass a Python object (a `Store`, a callable) | the framework settings (it cannot be written in a file) |
| Make production safe in one line | `preset = "production"` |
