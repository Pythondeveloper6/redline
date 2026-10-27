# Access control

[← Index](index.md)

<!-- verify: access control (redline_core.access, policies, login, read_only, features) -->

The Redline UI shows SQL, request and response bodies, headers, source lines and exception
locals. Treat it like a database console. Who may open it is decided by **one policy**,
the same for every framework: `redline_core.access.check(request_info)` returns a decision,
and each adapter just asks it.

Two things always apply, whatever policy you pick: the **CSRF check** (`Origin` and
`Sec-Fetch-Site` on `DELETE` and `POST`) and **`X-Frame-Options: DENY`** on Redline's own
responses.

## Policies

Set `access` to one of:

| `access` | Who gets in | Typical use |
|---|---|---|
| `local` (default) | loopback clients using a loopback `Host`, plus anything in `allowed_ips` | your laptop |
| `token` | a request with `Authorization: Bearer <token>`, or a browser that logged in with the token | staging, production, a shared server |
| `basic` | a user from `basic_users` | a small team, behind a proxy that cannot do SSO |
| `framework` | the framework's own check: Django `is_staff` or `PERMISSION`, FastAPI `dependencies`, Flask `permission`, Litestar `guards` | you already have an admin login |
| `custom` | whatever your `access_check(request_info) -> bool` returns | anything else |
| `open` | everyone | **refused** unless `REDLINE_ALLOW_OPEN=1` is also set |

The default is `local` for FastAPI, Flask and Litestar. In Django the default is
`framework`, meaning active **staff** users, exactly as in 0.1.

### Local

Only loopback clients (`127.0.0.0/8`, `::1`, unix sockets) that address the server by a
loopback name (`localhost`, `*.localhost`, `127.0.0.1`, `[::1]`, any port) are served. The
`Host` check defeats DNS rebinding: a malicious page cannot make your browser talk to
`evil.example`, resolve it to `127.0.0.1`, and read your traces. `allowed_ips` adds client
networks, and `allowed_hosts` adds accepted `Host` values.

### Token

```bash
REDLINE_ACCESS=token
REDLINE_TOKEN=$(openssl rand -hex 32)       # or token_file=/run/secrets/redline_token
```

Clients send `Authorization: Bearer <token>`. In a browser, the UI shows a **login screen**
when the API answers `401 login_required`; you paste the token once, and Redline sets an
HttpOnly, SameSite=Strict cookie scoped to its own path, valid for `session_ttl_s` (12 hours
by default). The token is compared in constant time and is never logged or shown, not even
on the Settings page.

```bash
curl -H "Authorization: Bearer $REDLINE_TOKEN" https://staging.example.com/_redline/api/meta
```

Use a long random token. `redline doctor` warns about short or guessable ones. Login
attempts are rate-limited per client address (10 per 5 minutes by default) and answer
`429 too_many_requests`.

### Basic

```bash
redline hash-password            # prompts, prints a PBKDF2-SHA256 hash
```

```toml
# redline.toml
access = "basic"

[basic_users]
alice = "pbkdf2_sha256$..."      # the output of redline hash-password
```

Passwords are stored only as hashes. Use it behind HTTPS only: basic credentials are sent on
every request.

### Framework

Use your application's existing authentication and let Redline defer to it. Each framework
exposes its native hook:

=== "Django"

    ```python
    REDLINE = {"ACCESS": "framework", "PERMISSION": "myproject.redline.can_view"}
    ```

    Default: `user.is_active and user.is_staff`. See [Django: access control](django.md#access-control).

=== "FastAPI"

    ```python
    install(app, engine=engine, dependencies=[Depends(require_admin)])
    ```

    See [FastAPI: access control](fastapi.md#access-control).

=== "Flask"

    ```python
    Redline(app, permission=lambda request: current_user.is_admin)
    ```

=== "Litestar"

    ```python
    RedlinePlugin(guards=[require_admin])
    ```

### Custom

```python
def can_view(request_info) -> bool:
    return request_info.headers.get("x-internal-user") in ALLOWED

REDLINE = {"ACCESS": "custom", "ACCESS_CHECK": can_view}
```

### Open

`access = "open"` serves the UI to everyone. Redline refuses to start that way unless you
also set `REDLINE_ALLOW_OPEN=1`, and logs a warning on **every request**. It exists for
sealed environments (a throwaway container, a network you control completely). Do not use
it on anything reachable from the internet.

## Network restrictions

These apply **in addition to** the policy:

| Setting | Effect |
|---|---|
| `allowed_ips` | IPs and CIDRs allowed extra access. With `local`, loopback plus these. Example: a VPN range `10.8.0.0/16` |
| `allowed_hosts` | extra `Host` values accepted by the DNS-rebinding check |
| `trusted_proxies` | CIDRs of reverse proxies. **Only** requests arriving from these have their `X-Forwarded-For` honoured for the client IP |

`trusted_proxies` matters. If you do not set it, `X-Forwarded-For` is ignored, so a client
cannot claim to be `127.0.0.1` by sending the header. If you do set it, list only the
proxies you operate.

## Read-only mode

`read_only = True` refuses every state-changing call with `403 read_only`: `DELETE` on
traces and runs, `POST api/runs` and `EXPLAIN ANALYZE`. Reads, plain EXPLAIN, search and
exports still work. The `production` [preset](configuration.md#presets) turns it on.

## Feature switches

`features` turns individual capabilities off. All default to on.

| Switch | Turns off |
|---|---|
| `explain` | `POST api/explain` and the EXPLAIN drawer |
| `profile` | capturing and viewing flamegraphs |
| `replay` | the Replay button |
| `snapshot` | the snapshot download |
| `load` | the Load page and run endpoints |
| `logs` | log capture and the Logs tab |
| `locals` | exception local variables |
| `sse` | `api/stream`, so Live mode falls back to polling |

```toml
[features]
explain = false
replay = false
```

## Other access settings

| Setting | Default | Meaning |
|---|---|---|
| `ui_enabled` | `True` | If false, Redline **captures but does not serve** the UI or API, for shipping data elsewhere |
| `mount_path` | adapter default | Where the UI and API are served |
| `session_ttl_s` | `43200` | Login cookie lifetime |
| `max_login_attempts` | 10 per 5 minutes per IP | Rate limit for `POST api/login` |

## Login and API

| Endpoint | Behaviour |
|---|---|
| `POST api/login` | body `{"token": "..."}` or `{"username": "...", "password": "..."}`; answers `204` and sets the cookie |
| `POST api/logout` | clears the cookie |
| `GET api/config` | the effective configuration with secrets as `***` and where each value came from |

An unauthenticated request to a `token` or `basic` instance gets `401 login_required`. The
UI reacts by showing its login screen. See [HTTP API](http-api.md#authentication-and-csrf).

## Recipes

### Local development

Nothing to do. The default `local` policy serves `http://localhost:8000/_redline/` to you
and nobody else.

### Behind a VPN

You run a shared dev or staging box that only VPN users can reach. Add the VPN range:

```toml
access = "local"
allowed_ips = ["10.8.0.0/16"]
allowed_hosts = ["devbox.internal"]
```

Anyone on the VPN can open the UI with no login. If the VPN is wider than the people who
should see request bodies, use a token as well.

### Token in staging

```bash
REDLINE_PRESET=staging          # token access, 25% sampling, no locals
REDLINE_TOKEN=...               # from your secret store
```

Share the token with the team through your password manager. Rotate it by changing the
variable and restarting.

### Production, read-only

```bash
REDLINE_PRESET=production
REDLINE_TOKEN=...
```

Read-only, token-protected, 5% sampling plus slow requests and errors, no bodies, no
locals, no badge. Reach it through your VPN or an SSH tunnel as well; a token is a second
lock, not the only one. See [Production](production.md).

### Behind a reverse proxy

The proxy terminates TLS and forwards to the app. Without help, every request appears to
come from the proxy. Tell Redline which proxy to believe:

```toml
access = "token"
trusted_proxies = ["10.0.0.0/8"]       # your load balancer's network, not "0.0.0.0/0"
```

The proxy must set `X-Forwarded-For` and overwrite any value the client sent. Also set
`X-Forwarded-Proto` and the right `Host` so the CSRF `Origin` comparison sees the public
host name. Optionally, restrict `/_redline/` at the proxy to your office or VPN range, so the
app's check is the second line of defence:

```nginx
location /_redline/ {
    allow 10.8.0.0/16;
    deny  all;
    proxy_pass http://app;
}
```

### Kubernetes

Do not expose Redline through a public Ingress. Options, safest first:

1. **`kubectl port-forward`**. Reach it from your machine only, with the default `local`
   policy plus a token:

   ```bash
   kubectl port-forward deploy/shop 8000:8000
   # http://localhost:8000/_redline/
   ```

   `port-forward` makes the connection arrive from the pod's own loopback side, so check
   with `redline doctor` that the `local` policy lets you in; if your server binds a
   different address, add the forwarding source to `allowed_ips`.

2. **An internal Ingress or Service** reachable only from the cluster or VPN, with
   `access = "token"` and `trusted_proxies` set to the ingress controller's pod CIDR.

Keep the token in a `Secret`, mounted as `token_file`:

```yaml
env:
  - name: REDLINE_PRESET
    value: production
  - name: REDLINE_TOKEN_FILE
    value: /run/secrets/redline/token
volumeMounts:
  - { name: redline-token, mountPath: /run/secrets/redline, readOnly: true }
```

Remember that the store is a SQLite file **per pod**: use a volume if you want it to
survive restarts, and note that each replica has its own data
([Production](production.md#process-models)).

## What to read next

[Security](security.md) covers what is stored and the full threat model;
[Configuration](configuration.md) covers how these settings are supplied.
