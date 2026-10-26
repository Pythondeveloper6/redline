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
| Outbound **URLs** (`http_out`) | URL | query-string values whose key matches `redact_keys` are masked |
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
| Headers | `authorization`, `cookie`, `set-cookie`, `x-api-key`, `proxy-authorization` (case-insensitive) | `[redacted]` |
| Query strings, form bodies, JSON bodies (any depth) | any key matching `(?i)pass(word)?\|secret\|token\|api[_-]?key\|auth(?!or)\|session\|csrf\|card\|cvv\|ssn` | `[redacted]` |
| Outbound URLs | the same key rule on the query string | `[redacted]` |
| SQL parameters | values bound to a column whose name matches the same pattern (`SET "password" = %s`, `INSERT ... ("token") VALUES (%s)`) | `[redacted]` |
| Bodies | cut at `max_body_bytes`, **after** masking, so a cut never exposes half a secret | |
| Binary bodies | replaced by `<N bytes binary>` | |

`auth(?!or)` is deliberate: it catches `auth`, `authorization` and `authentication` but not
`author` or `author_id`, which would otherwise hide half a blog's data.

### Customising

```python
REDLINE = {
    "REDACT_HEADERS": ["authorization", "cookie", "set-cookie", "x-api-key",
                       "proxy-authorization", "x-internal-token"],
    "REDACT_KEYS": [
        r"(?i)pass(word)?|secret|token|api[_-]?key|auth(?!or)|session|csrf|card|cvv|ssn",
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

### The SQL EXPLAIN consequence

Because masked parameter values are what is stored, EXPLAIN of a query filtered on a
secret column runs with `[redacted]` as the value. The plan may differ from the real one.
See [EXPLAIN](explain.md#safety).

## Who can open the UI

The UI and API expose SQL, bodies and EXPLAIN. Treat access to them as access to a database
console with read-only privileges *and* to the traffic.

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

=== "FastAPI"

    **Default: loopback only.** Without `dependencies`, a request is served only when
    **both** hold, and a warning is logged at startup:

    - the client is loopback (`127.0.0.0/8`, `::1`, unix sockets);
    - the `Host` header names loopback: `localhost`, `*.localhost`, `127.x.x.x` or
      `[::1]`, with any port. This defeats DNS rebinding (below).

    Pass your own dependencies for anything else. A refusal is `403` JSON
    `{"error": {"code": "forbidden", ...}}`. See
    [FastAPI: access control](fastapi.md#access-control).

### Cross-site request forgery

Reads are `GET`. The two mutating calls, `DELETE` (traces, **Clear all**) and `POST
api/explain`, are protected:

- **Django** runs Django's own CSRF machinery on every API request (token in the CSRF
  header, plus the Origin and Referer checks on HTTPS), even if `CsrfViewMiddleware` is not
  installed, and answers `403 csrf_failed` as JSON.
- **FastAPI** rejects cross-site browser requests via `Origin` and `Sec-Fetch-Site`,
  with `403 csrf_failed` in the same JSON shape.

### DNS rebinding

A web page on `evil.example` can re-point its own DNS name at `127.0.0.1`. Your browser
then sends that page's requests to your local server, as same-origin requests, so CORS and
the CSRF checks do not stop it reading `GET` endpoints. What gives it away is the `Host`
header, which still says `evil.example`.

- **FastAPI:** the default `local_only` guard refuses any `Host` that is not a loopback
  name or address (`403 forbidden`). If you reach the app through a hosts-file alias, allow
  it explicitly:
  `install(app, dependencies=[Depends(loopback_guard(allowed_hosts=["devbox"]))])`.
  With your own `dependencies` (real authentication), the attacker's page has no
  credentials for your host (cookies are bound to `evil.example`), so it is refused too.
- **Django:** every Redline view calls `request.get_host()`, so `ALLOWED_HOSTS` is enforced
  on them even without `CommonMiddleware` (an unknown host gets Django's `400`). With
  `DEBUG = True` and an empty `ALLOWED_HOSTS`, Django only accepts `localhost`,
  `127.0.0.1` and `[::1]`. The staff session cookie is not sent to the attacker's host name
  either. Do not combine `ALLOWED_HOSTS = ["*"]` with a `PERMISSION` that returns `True`.

### Framing

The UI page is served with `X-Frame-Options: DENY` by both adapters, so another site
cannot frame it and trick a click on **Clear all**.

### Putting it behind a proxy

!!! warning "Loopback is not a security boundary behind a proxy"

    If a reverse proxy forwards to your app on `127.0.0.1`, FastAPI's default allows
    **everyone** the proxy lets through whose `Host` the proxy rewrites to `localhost`
    (many do). Pass `dependencies`, or run uvicorn with
    `--proxy-headers --forwarded-allow-ips=<proxy ip>` so the real client address is seen.

### If you must expose it

In order of preference:

1. **Do not.** Use it in development and staging, and look at production traces by tunnelling
   (`ssh -L`) to a loopback-only instance.
2. Put it behind your SSO or an authenticating proxy **in addition to** the application's
   check, restricted to a small group.
3. Serve it only on an internal network or a VPN.

Never expose it with `dependencies=[]` or a permission that returns `True`.

## Data on disk

- The file is **not encrypted** and has the default permissions of the process that
  created it (your umask). On a shared machine restrict the directory
  (`chmod 700 .redline`).
- **Never commit it.** Add `.redline/` to `.gitignore`. Check your Docker build context
  and your backups too.
- It is **not your application database**: separate file, separate lifetime. Deleting it is
  always safe (`manage.py redline_clear` or **Clear all** in the UI).
- It lives as long as `max_age_days` and `max_rows` say, **and no longer**: retention is
  the main control on how much data you hold. Keep both low in production.
- It is a **GDPR-style** concern as much as a security one: recorded bodies may be personal
  data. Retention, access control and the redaction patterns are your levers. Redline has
  no per-user erase; the nearest equivalent is `DELETE api/traces/<id>` or clearing
  everything.

## Other surfaces

| Surface | Behaviour |
|---|---|
| **Responses** | Both adapters: the page sets `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY` and `Cache-Control: no-store`. API responses are `no-store` and `nosniff` |
| **Static assets** | Served through the same access check. Hashed asset names are cached for a year; the path is resolved (symlinks and `..` included) and refused unless it stays inside the bundle directory, so `assets/../../conf.py` is a `404` |
| **`X-Redline-Trace` header** | Django only, and only when `DEBUG` is true: the id of the trace is added to the response, so you can find it. It is not sent in production |
| **Rendering** | The UI renders stored text through React, which escapes it: there is no `dangerouslySetInnerHTML` or `innerHTML` anywhere. The SQL highlighter returns tokens rendered as text nodes, the diff view renders each line as text, and a suggestion's docs link is only rendered when it is `http(s)` (never `javascript:`). Trace ids are URL-encoded in API calls |
| **Outbound network** | Redline makes **no** network calls of its own: no telemetry, no update check |
| **Dependencies** | `redline-core` has **no runtime dependencies** |
| **The profiler** | Optional; adds `pyinstrument` |

## Supply chain: installing from Git

Redline is not on PyPI. See
[Getting started](getting-started.md#install) for why you must name `redline-core` in the
same install command. Do not run an unqualified `pip install redline-core` against PyPI:
the project does not own that name there, and a third party could.

## A short threat model

| Attacker | Wants | Stopped by |
|---|---|---|
| Anonymous internet user | Read traces | Django: login and staff check. FastAPI: loopback default or your dependency. **Not stopped** if you expose it open |
| Logged-in non-admin user | Read other users' request data | `PERMISSION` / `dependencies` |
| A malicious website the admin visits | Delete traces, trigger EXPLAIN | CSRF protections; `X-Frame-Options: DENY` |
| A malicious website (DNS rebinding) | Read `GET` endpoints of a local instance | FastAPI: the `Host` check of the loopback default, or real auth. Django: `ALLOWED_HOSTS` (enforced on Redline's views) and the session cookie |
| Someone with read access to the disk or backups | Everything stored | redaction, retention, file permissions. **Not** encryption |
| An admin with the UI | Run arbitrary SQL | Not possible: EXPLAIN takes a trace and span id, never SQL, and is limited to **recorded single read-only `SELECT`s** (no locking clause, no data-modifying CTE, no second statement; see [EXPLAIN: safety](explain.md#safety)), in a rolled-back transaction. They can read what was recorded and, if `explain_analyze` is on, make the server re-run recorded queries |
| Someone who can write the store file | Plant SQL for EXPLAIN | The same `is_explainable` check runs again before EXPLAIN, on the stored text; side-effecting *functions* in a planted `SELECT` would only run under `explain_analyze` (off by default), in a rolled-back transaction |
