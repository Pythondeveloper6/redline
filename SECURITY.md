# Security policy

Redline records what your application does with each request: headers, query strings,
bodies, SQL statements and their parameters, and the code locations that issued them.
Its UI and JSON API expose all of that. A defect here can be a defect in someone's access
control, a leak of credentials, or a denial of service, so reports are taken seriously
and answered.

This policy covers all three distributions in this repository:
`redline-core`, `django-redline` and `fastapi-redline`.

## Reporting a vulnerability

**Do not open a public issue.**

Report privately by email to <pythondeveloper6@gmail.com>, with `redline security` in the
subject line.

Please include, as far as you have it:

- which package and version, plus Python and Django/FastAPI versions;
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
| 0.1.x (pre-release) | ✅ current development line |
| < 0.1 | ❌ never released |

The three packages are versioned in lockstep; a fix ships as a new version of all three.

## Scope

**In scope:**

- the Redline UI or JSON API being reachable, or returning data, without the permission
  the adapter documents as required (Django staff/permission hook; the auth dependency
  you attach to the FastAPI mount);
- secrets (Authorization, Cookie, Set-Cookie, API keys, passwords, tokens) appearing in
  stored traces, the UI or the API when the default redaction is on;
- a stored-XSS path through captured paths, headers, bodies, SQL or source snippets;
- EXPLAIN execution that can modify data, or run anything other than a read-only SELECT
  inside a rolled-back transaction;
- a denial of service reachable by an unauthenticated request, such as unbounded
  capture or storage growth.

**Out of scope:**

- the FastAPI mount being reachable when you attached it without an auth dependency.
  Redline ships without authentication by design (it cannot know your auth), and the
  documentation says so prominently. Report it as an ordinary issue if the documentation
  is unclear;
- anything requiring a Django superuser or someone already allowed to read traces;
- findings from automated scanners with no demonstrated impact.

## Hardening notes

- **Redline captures request data.** Bodies, query strings and SQL parameters can contain
  personal data and credentials. Redaction is on by default and masks common secret
  headers and keys (Authorization, Cookie, Set-Cookie, X-Api-Key, and body or query keys
  matching password, token, secret or key). Extend the patterns in your settings for your
  own field names; do not turn redaction off in shared environments.
- **Never expose the Redline UI publicly without authentication.** Keep it behind staff
  login or an auth dependency, and do not mount it on a public host in production. Treat
  the stored trace database as sensitive data.
- Use the sampling policy in production (a sample rate, plus slow and 5xx requests only)
  rather than capturing everything.
- Targeting and request attribution use what the framework gives you; configure your
  proxy-header handling rather than trusting raw forwarding headers.
