# Threat model

[← Index](index.md) · [Security](security.md)

This is the structured threat model behind [Security](security.md). It exists so that a
reviewer (internal or an independent third party) can check each claim against the code.
[Security](security.md) explains the controls to users; this page lists assets, trust
boundaries, entry points and residual risks. Redline has **not** had an independent
security review yet.

## Scope and assumptions

- Redline runs **inside** the application process (middleware/plugin + database hooks) and
  serves its UI and JSON API under a mount path (default `/_redline/`) of the same server.
- It targets development and staging. Production use is limited to the recording-only
  preset in [Production](production.md).
- The host, its filesystem, the Python environment and the application code are trusted.
  An attacker who can change them owns the application anyway.

## Assets

| Asset | Where | Why it matters |
|---|---|---|
| Captured request data: headers, query strings, bodies, SQL text and parameters, logs, exception locals | the trace store (SQLite file or shared PostgreSQL store) | Personal data, credentials and business data can leak through it |
| Source callsites and snippets | the store | Reveal code structure and literals |
| Access credentials: Redline token, basic-auth hashes, session cookie, `REDLINE_SECRET_KEY` | settings, environment, cookies | Grant UI/API access |
| Database connections of the app | in-process engines | EXPLAIN re-runs recorded reads on them |
| The app's availability | the process | Recording costs CPU, memory and disk |

## Trust boundaries and entry points

| # | Boundary | Entry points | Main controls |
|---|---|---|---|
| B1 | Browser / network → Redline UI and API | `GET/POST {mount}/api/*`, login, static assets, SSE stream | access policy (`local`, `token`, `basic`, framework hook, `open` only by opt-in), CSRF checks on mutations, `Host` check against DNS rebinding, `X-Frame-Options: DENY`, rate-limited login, constant-time comparison, `read_only` and per-feature switches |
| B2 | Any client → recorded application | every request the app serves | redaction before persistence, capture byte budgets, sampling, route rules; profiling and check tags honoured only for authorised callers |
| B3 | Redline → the app's database | EXPLAIN, index advice, catalog reads | only recorded single read-only `SELECT`s, re-validated on the stored text; rolled-back transaction; `explain_analyze` off by default |
| B4 | Redline → disk | store file(s), WAL, profile blobs | private file permissions, retention/pruning, WAL check, network-filesystem refusal; **no encryption at rest** |
| B5 | Redline → other services | OTLP export, load and refactor-check targets, webhooks | nothing is sent by default; every egress needs an explicit command or setting; load targets are path-only on the server's own port |
| B6 | Developer machine / CI → Redline CLI | `redline check`, `check-sql`, `analyze`, `load` | static commands parse source and never import it; `check-imports` imports in an isolated subprocess only when asked |

## Threats (STRIDE) and mitigations

| Threat | Category | Mitigation | Residual risk |
|---|---|---|---|
| Unauthenticated read of traces | Information disclosure | access policy; `404` until configured outside debug; doctor `--production` flags `open` | an operator can still choose `access="open"` |
| Secrets stored in clear | Information disclosure | key-based redaction of headers, query, bodies, logs and locals; SQL parameter policies; route rules | free text and values under innocent names are not caught ([what redaction misses](security.md#what-redaction-does-not-catch)) |
| Cross-site request deletes or triggers EXPLAIN | Tampering | CSRF token / same-origin checks on every mutation | depends on the browser enforcing SameSite and Origin |
| DNS rebinding against a loopback instance | Spoofing | `Host` allow-list for the loopback policy; `ALLOWED_HOSTS` on Django | another app on the same origin is trusted |
| Forced recording of attacker requests | Tampering / DoS | profile and check headers honoured only for authorised callers; bounded queue drops instead of blocking | high-cardinality traffic still costs CPU before sampling |
| Arbitrary SQL through EXPLAIN | Elevation of privilege | EXPLAIN takes trace/span ids, never SQL; `is_explainable` on stored text; rolled back | under `explain_analyze` a planted `SELECT` calling a side-effecting function would run (in a rolled-back transaction) |
| Store file stolen from disk or backups | Information disclosure | redaction, retention, file permissions | no at-rest encryption |
| Recorder exhausts memory/disk | Denial of service | span/SQL/body/trace byte budgets, bounded writer queue, retention | budgets do not bound total process memory; see [capacity](production.md#capacity-and-failure-behavior) |
| Exported telemetry leaks data | Information disclosure | OTLP export omits bodies, SQL text and free-text tags; preview by default | collector security is the operator's |
| Malicious dependency or tampered release | Tampering | trusted publishing, PEP 740 attestations, pinned CI actions, Dependabot | users must verify attestations themselves |

## Review checklist for an independent reviewer

1. Access policy: every route under the mount goes through the same guard (`access.py`), including static assets, the SSE stream and the login form.
2. CSRF: every non-GET API route checks the token/origin (`access.py`, adapter routes).
3. EXPLAIN: `is_explainable` cannot be bypassed via comments, CTEs, multiple statements or vendor-specific syntax (`explain.py`, `explain_mssql.py`, `sql.py`).
4. Redaction runs before *every* persistence path, including jobs, logs, exception locals and refactor-check results (`redact.py`, `privacy.py`, `recorder.py`).
5. No egress without explicit opt-in (`otlp.py`, `load/`, `check/`, webhook commands).
6. File permissions and path placeholders of the store (`storage/safety.py`).
7. The bundled UI renders recorded data without HTML injection (React escaping; no `dangerouslySetInnerHTML` on recorded values).

## Needs a human

- An independent third-party security review against the checklist above.
- Validation of the deployment topology (proxy, TLS, filesystem, process model) in each
  environment where Redline is enabled; `redline doctor --production` covers what can be
  checked from inside the process.
