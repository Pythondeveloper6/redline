# Security policy

Redline records what your application does with each request: headers, query strings,
bodies, SQL statements and their parameters, and the code locations that issued them.
Its UI and JSON API expose all of that. A defect here can be a defect in someone's access
control, a leak of credentials, or a denial of service, so reports are taken seriously
and answered.

This policy covers all the distributions in this repository:
`redline-core`, `django-redline`, `fastapi-redline`, `flask-redline` and
`litestar-redline`.

## Reporting a vulnerability

**Do not open a public issue.**

Report privately by email to <pythondeveloper6@gmail.com>, with `redline security` in the
subject line.

Please include, as far as you have it:

- which package and version, plus Python and framework versions;
- what an attacker gains: which traces, headers, bodies or SQL they can read or change,
  and as whom;
- the smallest reproduction you can manage (a failing test is ideal);
- whether the deployment needs anything unusual to be vulnerable.

## What to expect

| | |
| --- | --- |
| Acknowledgement | within **3 working days** |
| Initial assessment | within **7 days** |
| Fix or a dated plan | within **30 days** for high and critical, best effort otherwise |
| Public disclosure | after a fix ships, coordinated with you |

This is a small project maintained by volunteers. If you have not heard back in a week,
please chase.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.2.x (pre-release) | ✅ current development line |
| 0.1.x (pre-release) | ⚠️ fixes only for serious issues, please upgrade |
| < 0.1 | ❌ never released |

The packages are versioned in lockstep; a fix ships as a new version of all of them.

## Scope

**In scope:**

- the Redline UI or JSON API being reachable, or returning data, without the permission
  the configured access policy requires (`local`, `token`, `basic`, the framework hook, or
  your own check, including IP allow-lists, trusted proxies and `read_only`);
- a bypass of the login (the token or basic credentials), of its rate limit, or of the
  cookie's scoping;
- the load-from-UI feature being usable to target anything other than the application that
  serves it, or while `load_from_ui` is off;
- secrets (Authorization, Cookie, Set-Cookie, API keys, passwords, tokens) appearing in
  stored traces, the UI or the API when the default redaction is on;
- a stored-XSS path through captured paths, headers, bodies, SQL or source snippets;
- EXPLAIN execution that can modify data, or run anything other than a read-only SELECT
  inside a rolled-back transaction;
- a denial of service reachable by an unauthenticated request, such as unbounded
  capture or storage growth.

**Out of scope:**

- the UI being reachable because you deliberately configured it open (`access = "open"`
  with `REDLINE_ALLOW_OPEN=1`, or empty `dependencies` / `guards`), or because a reverse
  proxy you configured makes every client look local. The documentation says so
  prominently. Report it as an ordinary issue if the documentation is unclear;
- anything requiring a Django superuser or someone already allowed to read traces;
- findings from automated scanners with no demonstrated impact.

## Hardening notes

- **Redline captures request data.** Bodies, query strings and SQL parameters can contain
  personal data and credentials. Redaction is on by default and masks common secret
  headers and keys (Authorization, Cookie, Set-Cookie, X-Api-Key, and body or query keys
  matching password, token, secret or key). Extend the patterns in your settings for your
  own field names; do not turn redaction off in shared environments.
- **Never expose the Redline UI publicly without authentication.** Keep it behind a token,
  staff login or your own check, preferably also behind a VPN, and do not mount it on a
  public host in production. Use `preset = "production"`. Treat the stored trace database
  as sensitive data.
- Logs and exception locals are captured too. Redaction is key-based and does not catch a
  secret held under an innocent name; the `staging` and `production` presets turn locals
  off.
- Run `redline load` only against systems you own or are authorised to test.
- Use the sampling policy in production (a sample rate, plus slow and 5xx requests only)
  rather than capturing everything.
- Client addresses come from the connection. `X-Forwarded-For` is honoured only from
  `trusted_proxies`; list only proxies you operate.

## Data egress

Importing and using the recorder does not start an exporter or send recordings to an
external collector. Optional instrumentations observe application operations; existing
load/check commands contact their explicitly configured targets. Online SQL compilation
contacts only the explicitly supplied development DSN.

`redline export` previews a bounded OTLP summary locally by default. Transmission requires
`--send` and an explicit collector endpoint. Its initial summary format excludes bodies,
SQL values/text, arbitrary tags and exception messages, and does not follow redirects.
There is no automatic background export. Review configuration, logging and redaction
before recording shared-environment data; key-pattern profiles are not a compliance
certification.
