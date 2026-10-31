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
| Request and response **headers** | name and value | values of `redact_headers`, and of any header whose name matches `redact_keys` (`X-Auth-Token`, `X-CSRFToken`, `X-Session-Id`…), are masked |
| **Query string** | key and value | values whose key matches `redact_keys` are masked |
| **Request body** | text, capped at `max_body_bytes` | JSON, form and multipart keys matching `redact_keys` are masked; multipart files become `[file: name, N bytes]`; any other text (plain, XML, GraphQL, JSON that does not parse) goes through the free-text rule |
| **Response body** | text, capped | JSON, form and multipart keys are masked; HTML and other text are **not** scanned (cost); only if `capture_response_body` is on |
| **SQL text** and **parameters** | controlled by `capture_sql_params` | `false` withholds values and normalizes literals; `reads_only` retains values only for recognized reads; otherwise configured column/table/profile patterns mask parameters |
| **Callsites** | file, line, function and optional **source line** | SQL capture/route restrictions remove associated source and generated fix snippets; other captured source can contain literals |
| **Tracebacks** and exception messages | text | the free-text rule (URL passwords, secret query values, `key=value` secrets, JWTs, card numbers), chained exceptions included |
| **Exception locals** (project frames: names and `repr` of variables) | text, at most 30 per frame, 300 characters each | variables whose **name** matches `redact_keys` are masked; containers have secret keys masked to a depth of 4, and the whole `repr` goes through the free-text rule (`key=value` pairs, URLs). Values under innocent names are **not** |
| **Log records** emitted during the request | level, logger, message (2,000 characters), callsite | the free-text rule: `key=value`, `key: value` and `"key": "value"` pairs whose key matches `redact_keys` are masked, and so are URL passwords and secret query values. Bare secrets in a sentence are not |
| **Normalised SQL summaries** (`trace_sql`) | statements with literals replaced by `?` | contains no parameter values |
| **Outbound URL and cache key patterns** | digit runs, UUIDs and long hex strings replaced by `*` | cache keys: the part after a session prefix (`django.contrib.sessions.cache`, `session:`, `sess:`) or a secret-looking word (`api_key:`) is masked; URLs as below |
| **Release name** | text | no |
| **Load-run definitions** | the scenario | secret-looking header values, body keys and URL secrets are masked |
| Outbound **URLs** (`http_out`) | URL | the password of `scheme://user:password@host` and query-string values whose key matches `redact_keys` are masked |
| The request **path** | text | values of route parameters named like a secret (`token`, `uidb64`, `key`, `signature`, `code`, or matching `redact_keys`); for unmatched routes (404), long token-like segments |
| Client address | `REMOTE_ADDR` / ASGI client | no |

Everything is JSON in a SQLite file readable by anyone who can read the file.

## Redaction

Redaction is **on by default** and cannot be turned off by accident: there is no
"disable redaction" switch. It runs on the trace before it is handed to the store, so values recognized by the configured rules are masked before persistence.
Unrecognized sensitive data can still reach disk. It is mostly **key-based**: it looks at the *name* of a field
(header, query key, JSON key, column, table, route parameter). Free text (log messages,
exception messages, tracebacks, text bodies) additionally has JWTs and Luhn-valid card
numbers masked by shape.

### Defaults

| Where | What is masked | Replaced with |
|---|---|---|
| Log messages, exception messages and tracebacks, non-JSON request bodies | `key=value`, `key: value`, `"key": "value"` pairs and `<key>value</key>` elements whose key matches `redact_keys`; URL passwords and secret query values inside the text; JWTs; Luhn-valid card numbers (13–19 digits, optionally grouped) | `[redacted]` |
| Exception locals | the value of a variable whose name matches `redact_keys`; secret keys of containers (4 levels deep); and the free-text rule over the whole `repr` | `[redacted]` |
| Headers | `authorization`, `cookie`, `set-cookie`, `x-api-key`, `proxy-authorization` (case-insensitive), and any header whose name matches `redact_keys` | `[redacted]` |
| Query strings, form bodies, multipart fields, JSON bodies (any depth, GraphQL `variables` included) | any key matching `(?i)pass(word)?\|secret\|token\|api[_-]?key\|(?<![a-z])auth(orization)?(?![a-z])\|session\|csrf\|card\|cvv\|ssn\|private[_-]?key` | `[redacted]` |
| Outbound URLs | the same key rule on the query string, and the password in `user:password@host` | `[redacted]`, or `%5Bredacted%5D` in the userinfo so the URL still parses |
| SQL parameters | values bound to a column whose name matches the same pattern (`SET "password" = %s`, `INSERT ... ("token") VALUES (%s)`, `password = crypt(%s, ...)`, `ON CONFLICT ... DO UPDATE SET api_key = %s`), or to a table whose name matches it (`"authtoken_token"."key" = %s`, `"django_session"."session_key" = %s`; an unqualified column in a statement on such a table is masked too). Django's `auth_` app prefix is ignored, so `auth_user` stays visible | `[redacted]` |
| Cache keys (Django cache, redis) | the rest of the key after `django.contrib.sessions.cache`, `django.contrib.sessions.cached_db`, `session:`, `sess:`, or after a word matching the key pattern | `django.contrib.sessions.cache[redacted]` (the prefix is kept, so hit rates still group) |
| URL path | the value of a route parameter named `token`, `uidb64`, `key`, `signature`, `code` or matching the key pattern (`/reset/<uidb64>/<token>/` → `/reset/[redacted]/[redacted]/`); on unmatched routes, segments of 20+ token characters that mix letters and digits | `[redacted]` |
| Multipart file parts | the content | `[file: name, N bytes]` |
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
| A secret in the **URL path** under an innocent parameter name (`/users/<email>/`, `/invite/<slug>/`) | Only parameters named like a secret are masked | Put the path in `ignore_paths`, or avoid secrets in paths |
| **Personal data** under ordinary keys (`email`, `name`, `address`, `phone`, `dob`) | They do not all match the default credential pattern | Add reviewed profiles/custom patterns, and keep bodies off in production |
| **Bare secrets in text request bodies**, and anything in **HTML or other non-JSON response bodies** | Text request bodies only get the free-text rule (`key=value`, `<key>`, JWTs, cards); response text is not scanned | Turn off `capture_request_body` / `capture_response_body`, or `ignore_paths` |
| **Literal values inlined in SQL text** when SQL value capture is enabled | Key masking does not identify arbitrary statement literals | Use bound parameters and `capture_sql_params = false` in production |
| **SQL parameters** bound to columns *and* tables whose names look innocent (`settings.value`, `kv.data`) | Matching is on the column and table name | Add patterns, or accept it |
| **Bare secrets in exception messages** (`ValueError: bad abc123`) | Only the free-text rule applies (`token=abc123` and `token: abc123` *are* masked) | Do not put secrets in exception messages |
| **Source lines** in callsites | They are your code | n/a, but note they land on disk |
| **Other headers** that carry secrets under innocent names (`Referer` with a token in the URL, `X-Signature`) | Only the listed names and names matching `redact_keys` are masked | Extend `redact_headers` |
| **Multipart field values** under innocent names, and **file names** | Fields are parsed and masked by name; Django and Flask do not read multipart bodies at all, FastAPI and Litestar store the parsed fields (files as `[file: name, N bytes]`) | Turn off request body capture for upload routes with `ignore_paths` |

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
secret column or table (every query on `django_session`, `authtoken_token`…) runs with `[redacted]`
as the value. The plan may differ from the real one.
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

    **Default: active superusers** (staff before 0.2: staff status is often handed out for
    one model's admin, and Redline shows every user's requests). The check is
    `request.user.is_active and request.user.is_superuser`.

    - An **anonymous** visitor to the page is redirected to the admin login (or
      `LOGIN_URL`).
    - A **signed-in non-superuser** (staff included) gets `403`.
    - The **API** answers `403` JSON `{"error": {"code": "forbidden", ...}}`.

    Change it with `REDLINE["PERMISSION"]`, a callable `(request) -> bool` or a dotted
    path to one:

    ```python
    # myproject/redline.py
    def is_performance_team(request):
        user = request.user
        return user.is_active and user.groups.filter(name="performance").exists()

    # settings.py
    REDLINE = {"PERMISSION": "myproject.redline.is_performance_team"}
    # the 0.1 rule: "django_redline.conf.staff_permission"
    ```

    The callable is wrapped: an exception in it denies access (it **fails closed**).

=== "FastAPI, Flask and Litestar"

    **Default: `local`, loopback only, and only in debug mode until you set `access`.**
    While `access` is not set anywhere and the app is not in debug mode (`app.debug`),
    every Redline URL answers `404` and an error is logged at startup: a reverse proxy on
    the same host that adds no `X-Forwarded-For` would otherwise make every visitor
    local. Set `access = "local"` to accept that risk on a development machine, or
    `token`/`basic` on a server.

    With `local` in force and without `dependencies` (FastAPI), `permission` (Flask) or
    `guards` (Litestar), a request is served only when **both** hold, and a warning is
    logged at startup:

    - the client is loopback (`127.0.0.0/8`, `::1`; a unix-socket peer only with
      `trust_unix_socket = true`);
    - the `Host` header names loopback: `localhost`, `*.localhost`, `127.x.x.x` or
      `[::1]`, with any port. This defeats DNS rebinding (below).

    A loopback peer that sends `X-Forwarded-For`, `X-Real-IP` or `Forwarded` without being
    in `trusted_proxies` is refused too (it is almost certainly a reverse proxy on the same
    host). Pass your own hook (it implies `access = "framework"`; an empty list is no hook),
    or set `access`, for anything else. `access = "framework"` with no hook attached
    refuses every request (`403`) in all four adapters: there is nothing to delegate to. A FastAPI dependency should raise
    `HTTPException` to refuse; one that returns `False` is refused with `403` as well. A refusal is `403` JSON `{"error": {"code": "forbidden", ...}}`. See
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
  mount (`Secure` over https, see `cookie_secure`), so a cross-site request does not carry
  it. Logging out revokes the session in that process (in memory).
- **Replay** sends the recorded request **without your cookies** unless you tick "Send my
  cookies": the method, path and body were chosen by whoever made the recorded request,
  and replaying them with your session would be a stored CSRF.

### Forcing capture

`?_redline=profile` and `X-Redline-Profile: 1` start the profiler and make Redline keep the
trace whatever `sample_rate` says. They are honoured only in framework debug mode or when
the request itself carries valid Redline credentials (a Bearer token or Basic
credentials, a client the `local` policy lets in, or `access_check`); anyone else
could make you profile and store their requests at will. The login cookie is scoped to
the mount, so the browser never sends it to application URLs. With `access = "framework"` (a
hook that only runs inside Redline's own routes) only debug mode honours them. The
`production` preset turns profiling off. `X-Redline-Run` (the load tester's header) and `X-Redline-Check` (the refactor check's) tag a
trace only when that run exists in the store and is running (a check also for a minute after it ends).

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
  `127.0.0.1` and `[::1]`. The admin session cookie is not sent to the attacker's host name
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

### Refactor checks

Starting a [refactor check](refactor-check.md) from the UI is opt-in too: `checks_from_ui`
defaults to `"auto"`, which means **debug mode only**, and the `production` preset sets it to
`false` (`403 checks_from_ui_disabled`). It is also refused under `read_only` and when the
`checks` feature is off. A check is sequential `GET`, `HEAD` and `OPTIONS` requests: a suite
containing any other method is rejected when it is loaded, saved or started. Runs started
from the UI are limited to 200 requests, 5 000 calls and 15 minutes, and aim at the
`base_url` setting or the server's own listening port, never at the `Host` header.

What the feature stores, and what it does not:

- **`${env:NAME}` placeholders are never expanded in anything stored.** A suite keeps the
  placeholder. The value is read from the environment of the process that runs the check. For a
  run started from the UI, the headers are handed to the background process in a file with mode
  `0600` that is deleted when the run ends.
- **Secret header values are masked** (`[redacted]`) in stored suites, in `api/checks` responses
  and in exported TOML, unless they are only a placeholder (optionally after a scheme such as
  `Bearer ${env:API_TOKEN}`). Saving a suite with `[redacted]` back keeps the stored value.
- **Baselines are redacted.** Response headers and bodies go through the redactor before they
  are written (JSON keys at any depth, text through the text redactor), and again when a
  baseline is imported through the API, along with suite headers, URLs and request paths. The
  same redaction is applied to both sides of a comparison, so a masked secret never shows as a
  difference. A baseline is still a copy of your responses: keep it as private as a trace.
- **The check tag cannot force storage.** Every check call carries `X-Redline-Check: <run
  id>;req=<i>;n=<k>`. Redline honours the tag only when `<run id>` belongs to a check run that
  exists in the store and is running (or ended less than a minute ago). The ids are random
  128-bit values known only to Redline users. For any other request carrying the header, the
  `check_*` tags are removed and its normal sampling decision stands; a request that sampling
  would have dropped and that was only provisionally kept for the tag is dropped entirely.
  Profiling is never started by the tag.

### If you must expose it

In order of preference:

1. **Do not.** Use it in development and staging, and look at production traces by tunnelling
   (`ssh -L`) to a loopback-only instance.
2. Put it behind your SSO or an authenticating proxy **in addition to** the application's
   check, restricted to a small group.
3. Serve it only on an internal network or a VPN.

Never expose it with a dependency or permission that lets everyone in, or `access = "open"`
(which Redline refuses without `REDLINE_ALLOW_OPEN=1` and warns about on every request).

## Data on disk

- The file is **not encrypted**. On POSIX systems the store directory Redline creates is
  `0700`, and the database, its `-wal`/`-shm` files and the load-run files (`runs/`) are
  `0600`; an older, world-readable database is tightened when it is opened. A directory
  that already existed (for example when `storage_path` is in your project root) keeps its
  permissions. Nothing changes on Windows.
- **Never commit it.** A store directory Redline creates (and `.redline/`) gets a
  `.gitignore` containing `*`; an existing `.gitignore` is never overwritten. Check your
  Docker build context and your backups too.
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
| **Outbound network** | Recording starts no telemetry, update check or exporter. Explicit load/check, online SQL compilation and export operations contact their configured targets |
| **Dependencies** | `redline-core` has **no runtime dependencies** |
| **The profiler** | Optional; adds `pyinstrument` |

## Supply chain: published and source installs

Published prereleases are available from the project's PyPI packages; use the adapter
for your framework so its exact core dependency is resolved together. Pin the matching
adapter/core versions in a reviewed lock file. See [Getting started](getting-started.md#install).
For an unreleased source install, use adapter and core from the same reviewed Git commit,
not moving branches or unrelated versions. Verify package/project links and distribution
provenance rather than relying only on a similar package name.

## A short threat model

The full, structured version (assets, trust boundaries, STRIDE table and a checklist for an
independent reviewer) is on [Threat model](threat-model.md).

| Attacker | Wants | Stopped by |
|---|---|---|
| Anonymous internet user | Read traces | Django: login and superuser check. Others: `404` until `access` is set (outside debug mode), the loopback policy, a token or your own hook. **Not stopped** if you expose it open |
| Anonymous internet user | Force profiling and storage of their requests | `X-Redline-Profile` / `?_redline=profile` honoured only in debug mode or with Redline credentials; `production` turns profiling off |
| Logged-in non-admin user | Read other users' request data | `PERMISSION`, `dependencies`, `permission`, `guards` or a token |
| A client that sends a made-up `X-Redline-Check` header | Make Redline store its requests, or tag them as part of a check | The tag is honoured only for a check run that exists in the store and is running; otherwise it is stripped, and a request that was only kept for the tag is dropped |
| A logged-in user who may start load runs | Make the server hit someone else (or another local service), or overload it | Paths only (no host); the target is the server's own listening port, never the `Host` header; `load_from_ui` off by default, `read_only`, the `load` switch |
| Someone who guesses or sniffs a token | Read everything | A long random token, HTTPS, login rate limit, constant-time comparison. Use a VPN as well |
| A malicious website the admin visits | Delete traces, trigger EXPLAIN | CSRF protections; `X-Frame-Options: DENY` |
| A malicious website (DNS rebinding) | Read `GET` endpoints of a local instance | FastAPI: the `Host` check of the loopback default, or real auth. Django: `ALLOWED_HOSTS` (enforced on Redline's views) and the session cookie |
| Someone with read access to the disk or backups | Everything stored | redaction, retention, file permissions. **Not** encryption |
| An admin with the UI | Run arbitrary SQL | Not possible: EXPLAIN takes a trace and span id, never SQL, and is limited to **recorded single read-only `SELECT`s** (no locking clause, no data-modifying CTE, no second statement; see [EXPLAIN: safety](explain.md#safety)), in a rolled-back transaction. They can read what was recorded and, if `explain_analyze` is on, make the server re-run recorded queries |
| Someone who can write the store file | Plant SQL for EXPLAIN | The same `is_explainable` check runs again before EXPLAIN, on the stored text; side-effecting *functions* in a planted `SELECT` would only run under `explain_analyze` (off by default), in a rolled-back transaction |


## Shared-environment controls in 0.4

The [real-world guide](real-world-workflows.md#safer-shared-development-recording) describes
`capture_sql_params`, profiles, table patterns, route modes, logger filters and redaction
preview. The production recipe additionally disables log capture and active tools explicitly.
Route rules are applied after the route template is known and before persistence; `values`
preserves JSON structure while masking values, and `body` removes body contents. They also
restrict SQL values, query values, source snippets, log messages and exception detail for
that recording. Review source/fix snippets and nested issue evidence as capture surfaces too.

A user resolver is an explicit synchronous callable returning an identifier. Request-ID
headers are configurable, can be redacted, and are not proof of identity or authorization.
Do not use a client-supplied request ID to choose a store, authorize access or attach a trace
to another tenant. A person ID can still be personal data even when it is not a name.

For multiple codebases, separate stores, credentials and processes are the initial isolation
boundary. Secondary stores deliberately expose those files to the same authorized UI users;
read-only file access does not create per-codebase authorization. Configure `extra_stores`
only when that sharing is intended.

## Explicit outbound operations

Recording does not start an exporter automatically. Existing load/check commands contact
their configured target. Online SQL checks contact an explicitly supplied development DSN.
`redline export` previews summary JSON by default; `--send` and an explicit collector URL
are required to transmit it. The initial format omits bodies, SQL and arbitrary tags/error
messages and does not follow redirects. It is not a full production telemetry pipeline.
See the repository's `SECURITY.md` data-egress statement.


For recording in shared or production environments, `capture_error_details = false`
keeps exception types and structured database codes while suppressing free-text
messages, formatted tracebacks and exception locals. Set `capture_logs = false` as
well: warning/error severity does not make a log message free of personal data. These
are defaults of both `shared-dev` and `production`; explicit configuration can override
them. SQL failure messages also require that statement's SQL parameter capture policy
to allow capture. Counts and structured error categories remain useful when text is off.


Capture byte budgets reduce the amount of SQL, span data and tag text retained. They do
not replace redaction: a secret can fit inside a short value. The bounded value converter
uses standard JSON primitives and avoids arbitrary application object conversion methods.
Oversized, deep, cyclic or unsupported metadata is incomplete evidence, never a clean bill
of health. Validate secret canaries separately from size-limit tests.
