# Server-Timing and the badge

[← Index](index.md)

Two small features put Redline's numbers where you are already looking: in the browser's
developer tools, and in the corner of the page you are developing.

## Server-Timing

Every recorded response carries a standard
[`Server-Timing`](https://developer.mozilla.org/docs/Web/HTTP/Headers/Server-Timing)
header. Chrome, Firefox and Safari show it in the Network panel's Timing tab, and
`curl -i` shows it in plain text:

```text
Server-Timing: total;dur=84.2, db;dur=51.7;desc="12 queries", cache;dur=3.1, http;dur=18.0, tpl;dur=6.4, redline;desc="9d4c1f0e7a2b4c6f8e1d3a5b7c9e0f21"
```

| Metric | Meaning |
|---|---|
| `total` | the whole request, in ms |
| `db` | time in SQL, with the query count in `desc` |
| `cache` | time in cache operations |
| `http` | time in outbound HTTP calls |
| `tpl` | time rendering templates |
| `redline` | the **trace id**, so you can paste it into `#/requests/<id>` |

A metric appears only if the request had that kind of work. The header is added to
**recorded** responses only: a request that is ignored (`ignore_paths`) does not get one.
If your app already sets a `Server-Timing` header, Redline appends its metrics to it.

The header is useful in two ways. In the browser it gives a per-request breakdown without
opening Redline. In scripts and CI, `curl -sI` plus a one-line parse is a cheap performance
assertion.

!!! info "It is information about your app"

    `Server-Timing` is visible to anyone who can read the response, including the user's
    browser. It contains timings, a query *count* and a trace id, never SQL or data. Even so
    it is a side channel for anyone who can make requests, anonymous clients included
    (query counts can reveal, for example, whether an account exists). That is why it is
    on only in debug mode by default; the [production preset](configuration.md#presets)
    turns it, and the badge, off.

| `server_timing` | Behaviour |
|---|---|
| `"auto"` (default) | on when the framework is in debug mode (Django `DEBUG`, `app.debug` in Flask, FastAPI and Litestar), off otherwise |
| `True` | always on (Redline warns outside the `dev` preset) |
| `False` | never |

Upgrading from 0.1: the header used to be on whenever recording was on. Set
`server_timing = True` to keep that.

## The floating badge

In development, a small pill appears in the corner of your HTML pages:

```text
 84 ms · 12 q · 2 issues
```

It shows the request's duration, query count and issue count. Click it to jump straight to
that trace in the Redline UI.  You do not edit any template.

How it works: after the response is built, Redline inserts one tag before `</body>`:

```html
<script src="/_redline/badge.js" defer data-redline-trace="..." data-redline-mount="/_redline" data-redline-summary="..."></script>
```

The tag carries `data-*` attributes with the trace id, the mount URL and the summary.

`badge.js` is a small dependency-free script shipped in the UI bundle and served from
Redline's own mount. It is public under `token` and `basic` access (it holds no data); it
sends `X-Redline-UI: 1` when it fetches the trace (for the issue count) on opening.

### When it is injected

The badge is injected only when **all** of these hold:

- the `badge` setting allows it (see below);
- the response is HTML (`text/html`), not compressed, and not a streaming response (on
  ASGI only small pages are held back for injection: at most 2 MiB with a known length, or
  a single body message);
- the status is 200 to 299;
- the response has a `</body>`;
- the request was recorded;
- the page's `Content-Security-Policy`, if any, allows the script (below).

JSON responses, redirects, downloads and error pages are untouched.

| `badge` setting | Behaviour |
|---|---|
| `"auto"` (default) | on when the framework is in debug mode (for example `DEBUG = True` in Django), off otherwise |
| `True` | always on |
| `False` | never |

### Content-Security-Policy

If your pages send a strict `Content-Security-Policy`, the badge script must be allowed by
`script-src`. Because it is served from your own origin under Redline's mount, `'self'`
covers it. When the effective policy (`script-src`, else `default-src`) allows neither
`'self'` nor `*`, for example a policy that uses **nonces or hashes only**, Redline skips
the injection rather than cause a console violation. Allow `'self'` in development if you
want the badge. Redline does not rewrite your CSP header.

## Live mode and snapshots

Two other features are wired by the same adapter work:

- `GET api/stream` is a Server-Sent Events feed of new traces, used by
  [Live mode](the-ui.md#live-mode). It resumes by `seq` (`after=` or `Last-Event-ID`),
  and the server closes it so clients reconnect: after five minutes on ASGI, about 25
  seconds on WSGI servers.
- `GET api/traces/{id}/snapshot` downloads one trace as a standalone HTML file
  ([Snapshots](the-ui.md#snapshots)).

Both are served by every adapter and are subject to the same
[access control](access-control.md) as the rest of the API. The `sse` and `snapshot`
[feature switches](access-control.md#feature-switches) disable them individually.
