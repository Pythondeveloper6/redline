# Security

[← Index](index.md)

Redline records the most sensitive thing about your application: what it was asked to do
and what it said back. This page is the threat model, what is protected by default, what is
**not**, and how to expose the UI safely.

To report a vulnerability privately, see `SECURITY.md` in the repository.

## What Redline stores

For every kept request, on local disk, in `.redline/redline.db`:

| Data | Stored as | Redacted? |
|---|---|---|
| Request and response **headers** | name and value | values of `redact_headers` are masked |
| **Query string** | key and value | values whose key matches `redact_keys` are masked |
| **Request body** | text, capped at `max_body_bytes` | JSON and form keys matching `redact_keys` are masked; other content is not |
| **Response body** | text, capped | same rules, and only if `capture_response_body` is on |
| **SQL text** and **parameters** | verbatim SQL; parameters as JSON | parameters bound to a column matching `redact_keys` are masked |
| **Callsites** | file, line, function and the **source line** | no |
| **Tracebacks** and exception messages | text | no |
| **Exception locals** (project frames: names and `repr` of variables) | text, at most 30 per frame, 300 characters each | variables whose **name** matches `redact_keys` are masked; containers have secret keys masked to a depth of 4, and the whole `repr` goes through the free-text rule (`key=value` pairs, URLs). Values under innocent names are **not** |
| **Log records** emitted during the request | level, logger, message (2,000 characters), callsite | the free-text rule: `key=value`, `key: value` and `"key": "value"` pairs whose key matches `redact_keys` are masked, and so are URL passwords and secret query values. Bare secrets in a sentence are not |
| **Normalised SQL summaries** (`trace_sql`) | statements with literals replaced by `?` | contains no parameter values |
| **Outbound URL and cache key patterns** | digit runs, UUIDs and long hex strings replaced by `*` | by pattern only; keys and URLs that embed other secrets are stored as seen |
| **Release name** | text | no |
| **Load-run definitions** | the scenario | secret-looking header values, body keys and URL secrets are masked |
| Outbound **URLs** (`http_out`) | URL | the password of `scheme://user:password@host` and query-string values whose key matches `redact_keys` are masked |
| The request **path** | text | no |
| Client address | `REMOTE_ADDR` / ASGI client | no |

Everything is JSON in a SQLite file readable by anyone who can read the file.

## Redaction

Redaction is **on by default** and cannot be turned off by accident: there is no
"disable redaction" switch. It runs on the trace before it is handed to the store, so a
secret never reaches disk. It is **key-based**: it looks at the *name* of a field, never at
its value.

### Defaults

| Where | What is masked | Replaced with |
|---|---|---|
| Log messages | `key=value`, `key: value` and `"key": "value"` pairs whose key matches `redact_keys`; URL passwords and secret query values inside the text | `[redacted]` |
| Exception locals | the value of a variable whose name matches `redact_keys`; secret keys of containers (4 levels deep); and the free-text rule over the whole `repr` | `[redacted]` |
| Headers | `authorization`, `cookie`, `set-cookie`, `x-api-key`, `proxy-authorization` (case-insensitive) | `[redacted]` |
| Query strings, form bodies, JSON bodies (any depth) | any key matching `(?i)pass(word)?\|secret\|token\|api[_-]?key\|(?<![a-z])auth(orization)?(?![a-z])\|session\|csrf\|card\|cvv\|ssn\|private[_-]?key` | `[redacted]` |
| Outbound URLs | the same key rule on the query string, and the password in `user:password@host` | `[redacted]`, or `%5Bredacted%5D` in the userinfo so the URL still parses |
| SQL parameters | values bound to a column whose name matches the same pattern (`SET "password" = %s`, `INSERT ... ("token") VALUES (%s)`) | `[redacted]` |
| Bodies | cut at `max_body_bytes`, **after** masking, so a cut never exposes half a secret | |
| Binary bodies | replaced by `<N bytes binary>` | |

The `auth` rule is deliberate: it matches `auth` and `authorization` only as a **whole word**,
so `author`, `author_id`, `authenticated`, `authority` and `oauth_provider` stay visible,
which would otherwise hide half a blog's data.

### Customising

```python
REDLINE = {
    "REDACT_HEADERS": ["authorization", "cookie", "set-cookie", "x-api-key",
                       "proxy-authorization", "x-internal-token"],
    "REDACT_KEYS": [
        r"(?i)pass(word)?|secret|token|api[_-]?key|(?<![a-z])auth(orization)?(?![a-z])"
        r"|session|csrf|card|cvv|ssn|private[_-]?key",
        r"(?i)^iban$|national_id",
    ],
}
```

!!! danger "Your list replaces the default"

    `redact_headers` and `redact_keys` **replace** the built-ins, they do not add to them.
    If you set `redact_keys` to one pattern, the default pattern is gone. Copy the
    defaults into your list and add to them. There is no warning when you narrow the list.

### What redaction does NOT catch

Be clear-eyed about this. Anything not matched by **key name** is stored in clear.

| Not masked | Why | What to do |
|---|---|---|
| A secret in the **URL path** (`/reset/<token>/`, `/users/<email>/`) | The path is not a key/value pair | Put the path in `ignore_paths`, or avoid secrets in paths |
| **Personal data** under ordinary keys (`email`, `name`, `address`, `phone`, `dob`) | They do not match the default pattern | Add patterns to `redact_keys`, or turn bodies off |
| **Secrets in plain-text, HTML or other non-JSON/form bodies** | Only JSON and URL-encoded forms are parsed | Turn off `capture_request_body` / `capture_response_body`, or `ignore_paths` |
| **Literal values inlined in SQL text** (raw SQL built with f-strings) | Masking covers bound parameters, not the statement text | Use bound parameters |
| **SQL parameters** bound to columns whose names look innocent (`value`, `data`, `blob`) | Matching is on the column name | Add patterns, or accept it |
| **Exception messages and tracebacks** (`ValueError: bad token abc123`) | Free text | Do not put secrets in exception messages |
| **Source lines** in callsites | They are your code | n/a, but note they land on disk |
| **Other headers** that carry secrets (`Referer` with a token in the URL, custom `X-...` auth headers) | Only the listed header names are masked | Extend `redact_headers` |
| **Multipart uploads** | The Django adapter does not read them; FastAPI stores up to `max_body_bytes` of the raw multipart text | Turn off request body capture for upload routes with `ignore_paths` |

!!! tip "Verify with your own traffic"

    Send a request containing a fake secret under each field name your app really uses,
    then open **Request / Response**. The demo does this: `POST /checkout` with
    `Authorization`, `card_number` and `password` shows all three redacted.

### Logs and locals

Two 0.2 features capture more of your application's internals than 0.1 did, so they deserve
the same scrutiny as request bodies.

- **Log messages** are formatted, redacted for `key=value` pairs, and capped at 2,000
  characters. A log line like `Created user alice@example.com with token=abc123` stores the
  email and masks the token. If your logs carry personal data, raise `log_level`, or set
  `capture_logs = False`.
- **Exception locals** are the values of variables in project frames when a request raised.
  A model instance's `repr`, a request object, a dict of form data: the value is stored as
  printed. Names that look secret are masked; **a secret under an innocent name is not**. The
  `staging` and `production` presets turn `capture_locals` off. The `logs` and `locals`
  [feature switches](access-control.md#feature-switches) are the access-side counterpart.

Neither feature is a place to put secrets, and neither is a reason to log them: redaction
is a safety net, not a licence.

### The SQL EXPLAIN consequence

Because masked parameter values are what is stored, EXPLAIN of a query filtered on a
secret column runs with `[redacted]` as the value. The plan may differ from the real one.
See [EXPLAIN](explain.md#safety).

## Who can open the UI

The UI and API expose SQL, bodies, source lines and exception locals, and can run EXPLAIN.
Treat access to them as access to a database console with read-only privileges *and* to the
traffic.

From 0.2 there is **one access policy for every framework**, chosen with the `access`
setting: `local`, `token`, `basic`, `framework` (the adapter's native hook), `custom` or
`open`. On top of it you can add IP allow-lists, trusted proxies, a `read_only` mode and
per-feature switches. The full description, with recipes for a laptop, a VPN, staging,
production, a reverse proxy and Kubernetes, is on [Access control](access-control.md). The
sections below describe the defaults and the threats.

=== "Django"

    **Default: active staff users.** The check is
    `request.user.is_active and request.user.is_staff`.

    - An **anonymous** visitor to the page is redirected to the admin login (or
      `LOGIN_URL`).
    - A **signed-in non-staff** user gets `403`.
    - The **API** answers `403` JSON `{"error": {"code": "forbidden", ...}}`.

    Tighten it with `REDLINE["PERMISSION"]`, a callable `(request) -> bool` or a dotted
    path to one:

    ```python
    # myproject/redline.py
    def is_superuser(request):
        user = request.user
        return user.is_active and user.is_superuser

    # settings.py
    REDLINE = {"PERMISSION": "myproject.redline.is_superuser"}
    ```

    The callable is wrapped: an exception in it denies access (it **fails closed**).

=== "FastAPI, Flask and Litestar"

    **Default: `local`, loopback only.** Without `dependencies` (FastAPI), `permission`
    (Flask) or `guards` (Litestar), a request is served only when
    **both** hold, and a warning is logged at startup:

    - the client is loopback (`127.0.0.0/8`, `::1`, unix sockets);
    - the `Host` header names loopback: `localhost`, `*.localhost`, `127.x.x.x` or
      `[::1]`, with any port. This defeats DNS rebinding (below).

    A loopback peer that sends `X-Forwarded-For`, `X-Real-IP` or `Forwarded` without being
    in `trusted_proxies` is refused too (it is almost certainly a reverse proxy on the same
    host). Pass your own hook (it implies `access = "framework"`), or set `access`, for
    anything else. A refusal is `403` JSON `{"error": {"code": "forbidden", ...}}`. See
    [FastAPI](fastapi.md#access-control), [Flask](flask.md#access-control) and
    [Litestar](litestar.md#access-control).

### Cross-site request forgery

Reads are `GET`. Every other method (`DELETE` on traces and runs, `POST api/explain`,
`POST api/runs`, login and logout) is protected:

- **Django** runs Django's own CSRF machinery on every API request (token in the CSRF
  header, plus the Origin and Referer checks on HTTPS), even if `CsrfViewMiddleware` is not
  installed, and answers `403 csrf_failed` as JSON.
- The shared access gate (all four adapters) rejects `Sec-Fetch-Site: cross-site` **and
  `same-site`**, and any `Origin` (including `null`) whose host and port differ from the
  host the request was addressed to, with `403 csrf_failed` in the same JSON shape. This
  applies whatever access policy you chose. A request with no `Origin` is not a browser and
  passes.
- **Token and basic access** use an HttpOnly, SameSite=Strict session cookie scoped to the
  mount (`Secure` over https), so a cross-site request does not carry it.

### DNS rebinding

A web page on `evil.example` can re-point its own DNS name at `127.0.0.1`. Your browser
then sends that page's requests to your local server, as same-origin requests, so CORS and
the CSRF checks do not stop it reading `GET` endpoints. What gives it away is the `Host`
header, which still says `evil.example`.

- **FastAPI, Flask, Litestar:** the default `local` policy refuses any `Host` that is not a
  loopback name or address (`403 forbidden`). If you reach the app through a hosts-file
  alias, allow it explicitly with `allowed_hosts=["devbox"]` in the settings.
  With your own `dependencies` (real authentication), the attacker's page has no
  credentials for your host (cookies are bound to `evil.example`), so it is refused too.
- **Django:** every Redline view calls `request.get_host()`, so `ALLOWED_HOSTS` is enforced
  on them even without `CommonMiddleware` (an unknown host gets Django's `400`). With
  `DEBUG = True` and an empty `ALLOWED_HOSTS`, Django only accepts `localhost`,
  `127.0.0.1` and `[::1]`. The staff session cookie is not sent to the attacker's host name
  either. Do not combine `ALLOWED_HOSTS = ["*"]` with a `PERMISSION` that returns `True`.

### Framing

The UI page is served with `X-Frame-Options: DENY` by every adapter, so another site
cannot frame it and trick a click on **Clear all**.

### Putting it behind a proxy

!!! warning "Loopback is not a security boundary behind a proxy"

    If a reverse proxy forwards to your app on `127.0.0.1`, every request would look local.
    Redline refuses a loopback peer that carries forwarding headers unless it is a trusted
    proxy, but a proxy that strips them is not detectable, so use `access = "token"` and set
    `trusted_proxies` so the real client address is taken from `X-Forwarded-For` **only**
    when the request comes from your proxy (read from the right, skipping trusted hops).
    Without `trusted_proxies` Redline ignores that header, so a client cannot spoof its
    address. See
    [Access control: behind a reverse proxy](access-control.md#behind-a-reverse-proxy).

### Badge, Server-Timing and CSP

- The **floating badge** adds one `<script src>` tag, from Redline's own mount, to HTML pages
  in debug mode (`badge = "auto"`). It is off in the `production` preset. Under a strict
  CSP that allows only nonced scripts Redline skips the injection; see
  [Server-Timing and the badge](server-timing-badge.md#content-security-policy).
- **Server-Timing** (`server_timing`, default `"auto"`: debug mode only) exposes timings, a
  query count and a trace id to anyone who can read the response, including anonymous
  clients and the browser, which can be a side channel (for example to tell which accounts
  exist from query counts). No SQL or data. Outside debug mode leave it off unless you want
  it, and Redline warns when it is forced on outside `dev`.
- The **trace id** in `Server-Timing` lets someone who already has UI access find the trace;
  it grants nothing by itself.
- **Snapshots** are files containing a full trace. Whoever has the file has the data.

### Load from the UI

Starting a load run from the UI is **off by default** (`load_from_ui = False`, `403
load_disabled`). When on, its safety rests on these rules:

1. The request list may contain only **paths** (each must start with a single `/`). A scheme
   or host is rejected with `400 invalid_body`. The target is the `base_url` setting, or
   else the URL the request was addressed to, accepted only for a loopback host or one in
   `allowed_hosts`; a forged `Host` header cannot aim a run at another machine, and other
   hosts need `base_url`. Runs are capped at one hour, 5 000 rps and 200 steps.
2. It is gated by the same access policy, by `read_only` and by the `load` feature switch.
3. It is a real load: a user who may start runs can make your instance slow. Enable it only
   in environments where that is acceptable, and not in production.

The `redline load` command line is a different matter: it runs on the machine you run it
on, against the URL you give it, with the rights you have there. Point it only at systems you
own or are authorised to test.

### If you must expose it

In order of preference:

1. **Do not.** Use it in development and staging, and look at production traces by tunnelling
   (`ssh -L`) to a loopback-only instance.
2. Put it behind your SSO or an authenticating proxy **in addition to** the application's
   check, restricted to a small group.
3. Serve it only on an internal network or a VPN.

Never expose it with `dependencies=[]`, a permission that returns `True`, or `access = "open"`
(which Redline refuses without `REDLINE_ALLOW_OPEN=1` and warns about on every request).

## Data on disk

- The file is **not encrypted** and has the default permissions of the process that
  created it (your umask). On a shared machine restrict the directory
  (`chmod 700 .redline`).
- **Never commit it.** Add `.redline/` to `.gitignore`. Check your Docker build context
  and your backups too.
- It is **not your application database**: separate file, separate lifetime. Deleting it is
  always safe (`manage.py redline_clear` or **Clear all** in the UI).
- Traces live as long as `max_age_days` and `max_rows` say, **and no longer**: retention is
  the main control on how much data you hold. Keep both low in production. The longer-lived
  [history](history.md) tables (`history_retention_days`, 90 by default) hold only
  **aggregates**: counts, latency histograms, normalised SQL and issue titles. They contain
  no request bodies, headers or parameter values, but an issue title can contain a
  table name or a route.
- It is a **GDPR-style** concern as much as a security one: recorded bodies may be personal
  data. Retention, access control and the redaction patterns are your levers. Redline has
  no per-user erase; the nearest equivalent is `DELETE api/traces/<id>` or clearing
  everything.

## Other surfaces

| Surface | Behaviour |
|---|---|
| **Responses** | Every adapter: the page sets `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY` and `Cache-Control: no-store`. API responses are `no-store` and `nosniff` |
| **Static assets** | Served through the same access check. Hashed asset names are cached for a year; the path is resolved (symlinks and `..` included) and refused unless it stays inside the bundle directory, so `assets/../../conf.py` is a `404` |
| **`X-Redline-Trace` header** | Django only, and only when `DEBUG` is true: the id of the trace is added to the response, so you can find it. It is not sent in production |
| **Rendering** | The UI renders stored text through React, which escapes it: there is no `dangerouslySetInnerHTML` or `innerHTML` anywhere. The SQL highlighter returns tokens rendered as text nodes, the diff view renders each line as text, and a suggestion's docs link is only rendered when it is `http(s)` (never `javascript:`). Trace ids are URL-encoded in API calls |
| **Outbound network** | Redline makes **no** network calls of its own: no telemetry, no update check |
| **Dependencies** | `redline-core` has **no runtime dependencies** |
| **The profiler** | Optional; adds `pyinstrument` |

## Supply chain: installing from Git

Redline is not on PyPI yet. See
[Getting started](getting-started.md#install) for why you must name `redline-core` in the
same install command. Do not run an unqualified `pip install redline-core` against PyPI:
the project does not own that name there, and a third party could. Install from the GitHub
repository, and pin a tag or a commit in your lock file.

## A short threat model

| Attacker | Wants | Stopped by |
|---|---|---|
| Anonymous internet user | Read traces | Django: login and staff check. Others: the loopback default, a token or your own hook. **Not stopped** if you expose it open |
| Logged-in non-admin user | Read other users' request data | `PERMISSION`, `dependencies`, `permission`, `guards` or a token |
| A logged-in user who may start load runs | Make the server hit someone else, or overload it | Paths only (no host), `load_from_ui` off by default, `read_only`, the `load` switch |
| Someone who guesses or sniffs a token | Read everything | A long random token, HTTPS, login rate limit, constant-time comparison. Use a VPN as well |
| A malicious website the admin visits | Delete traces, trigger EXPLAIN | CSRF protections; `X-Frame-Options: DENY` |
| A malicious website (DNS rebinding) | Read `GET` endpoints of a local instance | FastAPI: the `Host` check of the loopback default, or real auth. Django: `ALLOWED_HOSTS` (enforced on Redline's views) and the session cookie |
| Someone with read access to the disk or backups | Everything stored | redaction, retention, file permissions. **Not** encryption |
| An admin with the UI | Run arbitrary SQL | Not possible: EXPLAIN takes a trace and span id, never SQL, and is limited to **recorded single read-only `SELECT`s** (no locking clause, no data-modifying CTE, no second statement; see [EXPLAIN: safety](explain.md#safety)), in a rolled-back transaction. They can read what was recorded and, if `explain_analyze` is on, make the server re-run recorded queries |
| Someone who can write the store file | Plant SQL for EXPLAIN | The same `is_explainable` check runs again before EXPLAIN, on the stored text; side-effecting *functions* in a planted `SELECT` would only run under `explain_analyze` (off by default), in a rolled-back transaction |
