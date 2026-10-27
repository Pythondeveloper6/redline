# Configuration

[← Index](index.md)

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
| 2 | **Adapter defaults** | the framework adapter | Django: `sample_rate = 0.1` without `DEBUG`, `storage_path` and `project_root` from `BASE_DIR`; `access = "framework"` when the adapter has a native access hook |
| 3 | **Preset** | the `preset` setting | `preset = "production"` |
| 4 | **Config file** | `redline.toml` or `[tool.redline]` in `pyproject.toml` | `sample_rate = 0.2` |
| 5 | **Environment** | `REDLINE_<SETTING>` variables | `REDLINE_SAMPLE_RATE=0.1` |
| 6 | **Framework settings** | `REDLINE = {...}`, `app.config["REDLINE"]`, `install(settings=...)`, `RedlinePlugin(settings=...)` | `"SAMPLE_RATE": 0.05` |

The last one wins. This lets you keep sensible defaults in a committed file, override them
per environment with variables (no code change, no rebuild of the image), and still have
the application code say the final word where it must.

A **preset** only changes *defaults*: any value set in layers 4 to 6 overrides it. Setting
`preset = "production"` and `sample_rate = 0.5` gives you production behaviour with 50%
sampling. The `preset` itself is read from the highest layer that sets it (framework, then
env, then file, then adapter) and is `dev` if none does. The `features` table is merged key
by key across layers rather than replaced.

A **native access hook** (Django's superuser check or `PERMISSION`, a non-empty FastAPI
`dependencies=` or Litestar `guards=` list, a Flask `permission=`) sets
`access = "framework"` at the adapter layer, so any file, env or framework value for
`access` wins, but a preset does not: Django with the `production` preset keeps its
permission check. See [Access control](access-control.md#framework).

When **nothing sets `access`** (FastAPI, Flask, Litestar), the `local` default is served
only while the framework is in debug mode; otherwise Redline answers `404` and logs an
error at startup. Set `access` in any layer (file, `REDLINE_ACCESS`, code) to choose. See
[Access control](access-control.md#policies).

!!! tip "See what is in effect"

    ```bash
    redline config
    ```

    prints the effective configuration, **where each value came from** (default, adapter,
    preset, file or `env REDLINE_X`) and any warnings. Secrets are shown as `***`. It sees
    the file and environment layers only: settings you pass in code (`REDLINE = {...}`,
    `install(settings=...)`) are not visible to a separate command. The running app's
    complete view, including the framework layer, is the UI's **Settings** page and
    `GET api/config`.

## Presets

Presets are named bundles of defaults for the three situations most people are in.

| Setting | `dev` (default) | `staging` | `production` |
|---|---|---|---|
| `sample_rate` | `1.0` (keep everything) | `0.25` | `0.05` |
| `always_keep_errors` | `True` | `True` | `True` (slow requests are always kept too) |
| Request and response bodies | captured | captured | **not** captured |
| `capture_locals` | on | **off** | **off** |
| `log_level` | `INFO` | `INFO` | `WARNING` |
| `access` | `local` (Django: superusers; others: debug mode only until set) | `token` | `token`, **required** |
| `read_only` | no | no | **yes** |
| `badge` | `auto` (on in debug) | `auto` | **off** |
| `server_timing` | `auto` (on in debug) | `auto` | **off** |
| `load_from_ui` | off | off | off |
| `explain_analyze` | off | off | off |
| `profile` | `opt_in` | `opt_in` | **off** |

Outside `dev` the token must be at least 24 characters, or Redline refuses to start with a
message that includes a one-line way to generate one.

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

The `staging` and `production` presets use `access="token"`, which needs a token
(`REDLINE_TOKEN` or `token_file`). Without one Redline raises a `SettingsError` at startup
that says how to generate one. See [Access control](access-control.md#production-read-only). The details and
reasoning are in [Production](production.md#the-production-preset).

## The config file

Redline looks for `redline.toml` in `project_root`, then for a `[tool.redline]` table in
`pyproject.toml` there. It never searches parent directories. `project_root` is the
framework setting if given, else `REDLINE_PROJECT_ROOT`, else the adapter default (Django:
`BASE_DIR`), else the working directory. Set `REDLINE_CONFIG=/path/to/file.toml` to use
another file; it must exist. `redline.toml` can be flat, as below, or nest the keys under
`[redline]` or `[tool.redline]`.

An unknown key in the file is an error naming the file, with a "did you mean" hint.

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
| string | the text, as is (`token` and `secret_key` are kept verbatim) | `REDLINE_ACCESS=token` |
| optional (`str`, number or URL) | an empty value, `none` or `null` means unset | `REDLINE_MAX_ROWS=none` |
| `badge`, `server_timing` | `auto` or a bool | `REDLINE_BADGE=auto` |
| list | a JSON array or comma-separated | `REDLINE_ALLOWED_IPS=10.0.0.0/8,192.168.0.0/16` |
| table (`features`, `basic_users`) | a JSON object or `key=value` pairs separated by commas (feature values are bools) | `REDLINE_FEATURES=replay=false,load=false` |

Some useful ones:

```bash
REDLINE_ENABLED=0                      # switch recording off without a deploy
REDLINE_PRESET=production
REDLINE_ACCESS=token
REDLINE_TOKEN=...                      # from your secret manager
REDLINE_SECRET_KEY=...                 # signs login cookies; needed with several workers
REDLINE_RELEASE=$GIT_SHA
REDLINE_STORAGE_PATH=/var/lib/myapp/redline.db
REDLINE_CONFIG=/etc/myapp/redline.toml
REDLINE_ALLOW_OPEN=1                   # needed (with access=open) to expose the UI openly
```

Every `REDLINE_<NAME>` variable must name a setting. A typo such as `REDLINE_SAMPEL_RATE`
is a startup error with a "did you mean `REDLINE_SAMPLE_RATE`" hint, never a silent
default. The exceptions are `REDLINE_CONFIG`, `REDLINE_ALLOW_OPEN`, `REDLINE_RELEASE` and
`REDLINE_TEST_*`. A bad value is reported with the layer it came from. The same "did you
mean" error applies to unknown keys in the file and in the framework dict, exactly as in
[Settings](settings.md#rules-that-apply-to-every-setting).

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
redline doctor        # checks: storage writable, UI bundle present, access policy, token, sessions, proxies, retention
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
