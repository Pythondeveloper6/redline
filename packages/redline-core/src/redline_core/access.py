"""Access control shared by every adapter (``CONTRACT.md`` §18).

Adapters describe each request to Redline's own URLs (UI, assets, API, SSE, badge.js,
snapshot) as a framework-neutral :class:`RequestInfo` and ask :meth:`Access.check` for a
:class:`Decision`; ``POST api/login`` / ``POST api/logout`` are answered by
:meth:`Access.handle`. One implementation, so the four adapters cannot drift apart.

Order of the checks in :meth:`Access.check`:

1. ``ui_enabled`` off → 404 ``not_found``.
2. The client address, scheme and host: ``X-Forwarded-For`` (rightmost untrusted hop),
   ``X-Forwarded-Proto`` and ``X-Forwarded-Host`` are only believed when the TCP peer is in
   ``trusted_proxies``.
3. Network rules. ``local``: a loopback client (or one in ``allowed_ips``) addressing a
   loopback ``Host`` (or one in ``allowed_hosts``) – against DNS rebinding – and not
   relayed by an untrusted proxy on this machine. Other policies: when ``allowed_ips`` is
   non-empty the client must be in it.
4. The policy: ``token`` (Bearer header or login cookie), ``basic`` (Basic header or login
   cookie), ``framework`` (the adapter's native hook), ``custom`` (``access_check``),
   ``open`` (only with ``REDLINE_ALLOW_OPEN=1``). With ``token``/``basic`` the SPA shell
   (``index``, ``assets/``, top-level files such as ``badge.js``) is public – it holds no
   data – so the UI can show its login screen; every ``api/`` URL needs credentials.
   ``api/login`` / ``api/logout`` pass here and are answered by :meth:`Access.handle`.
5. :func:`route_policy`: disabled features (404 ``feature_disabled``) and ``read_only``
   (403 ``read_only``).
6. CSRF for every state-changing method (anything but GET/HEAD/OPTIONS, login included):
   ``Sec-Fetch-Site: cross-site``/``same-site`` and an ``Origin`` that differs from the
   request's host are refused (403 ``csrf_failed``). No ``Origin``: not a browser, allowed.
   It runs last so that an outsider gets the policy's refusal, not a CSRF hint.

Login cookies are ``v1.<expiry>.<subject>.<mac>`` signed with HMAC-SHA256 under
``secret_key`` (or a random per-process key), bound to the policy and to a fingerprint of
the credential (rotating the token or a password logs those sessions out). Failed
credentials are rate-limited per client IP (in memory, per process).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .conf import PASSWORD_HASH_PREFIX, Settings, SettingsError, check_token_strength

__all__ = [
    "ALLOW_OPEN_ENV",
    "LOCAL_HOST_NAMES",
    "LOGIN_WINDOW_S",
    "SESSION_COOKIE",
    "UI_HEADER",
    "Access",
    "Decision",
    "RequestInfo",
    "check",
    "get_access",
    "hash_password",
    "host_name",
    "is_local_host",
    "is_loopback",
    "parse_cookies",
    "route_policy",
    "verify_password",
]

logger = logging.getLogger("redline")

SESSION_COOKIE = "redline_session"
#: Sent by the Redline UI on every fetch: a 401 then carries no ``WWW-Authenticate: Basic``
#: (which would pop the browser's own password dialog over the login screen).
UI_HEADER = "x-redline-ui"
ALLOW_OPEN_ENV = "REDLINE_ALLOW_OPEN"
LOGIN_WINDOW_S = 300.0
#: ``Host`` names the loopback check accepts (plus loopback IPs and ``*.localhost``).
LOCAL_HOST_NAMES = frozenset({"localhost"})
PBKDF2_ITERATIONS = 600_000
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_FORWARD_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")
_MAX_LOGIN_BODY = 4096
_OPEN_WARN_EVERY_S = 60.0
_MAX_REVOKED = 10_000

_ID = r"[A-Za-z0-9_-]{1,64}"
_PROFILE = re.compile(rf"^api/traces/{_ID}/profile$")
_SNAPSHOT = re.compile(rf"^api/traces/{_ID}/snapshot$")
_RUNS = re.compile(r"^api/runs(/.*)?$")
_CHECKS = re.compile(r"^api/checks(/.*)?$")


# ── request & decision ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RequestInfo:
    """What the access check needs to know about one request, framework-neutral.

    ``path`` is relative to Redline's mount (``""``, ``"assets/x.js"``, ``"api/traces"``)
    and must be the very string the adapter routes on. ``headers`` has lower-case names
    (repeated headers joined with ``", "``). ``client_ip`` is the TCP peer (``None`` for a
    unix socket); ``mount`` is the mount path as the browser sees it (the login cookie's
    ``Path``).
    """

    method: str
    path: str
    client_ip: str | None
    headers: Mapping[str, str]
    cookies: Mapping[str, str] = field(default_factory=dict)
    host: str | None = None
    scheme: str = "http"
    mount: str = "/"

    @classmethod
    def build(
        cls,
        *,
        method: str,
        path: str,
        client_ip: str | None,
        headers: Mapping[str, str] | Iterable[tuple[Any, Any]],
        cookies: Mapping[str, str] | None = None,
        host: str | None = None,
        scheme: str = "http",
        mount: str = "/",
    ) -> RequestInfo:
        """Normalise header names (``str``/``bytes`` pairs or a mapping) and parse cookies."""
        items = headers.items() if isinstance(headers, Mapping) else headers
        merged: dict[str, str] = {}
        for key, value in items:
            k = (key.decode("latin-1") if isinstance(key, bytes) else str(key)).lower()
            v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
            merged[k] = f"{merged[k]}, {v}" if k in merged else v
        if cookies is None:
            cookies = parse_cookies(merged.get("cookie", ""))
        if host is None:
            host = merged.get("host")
        return cls(
            method=method.upper(),
            path=path,
            client_ip=client_ip,
            headers=merged,
            cookies=cookies,
            host=host,
            scheme=(scheme or "http").lower(),
            mount=mount,
        )


@dataclass(frozen=True)
class Decision:
    """The answer of :meth:`Access.check` / :meth:`Access.handle`.

    Refused (``allowed`` False): send ``status`` with :meth:`body` and ``headers``.
    Allowed: go on, adding ``headers`` (empty unless a stale cookie is being cleared).
    Login/logout answers are allowed decisions with a ``payload`` to send as JSON.
    """

    allowed: bool
    status: int = 200
    code: str = "ok"
    message: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    payload: Any = None
    #: Who got in: ``"token"``, a basic user name, ``"local"``…
    principal: str | None = None
    #: Extra keys of the error object (``login`` for ``login_required``).
    extra: Mapping[str, Any] = field(default_factory=dict)

    def body(self) -> Any:
        """The JSON body: ``payload`` when allowed, else the API's error shape."""
        if self.allowed:
            return self.payload
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


def _deny(status: int, code: str, message: str, **kw: Any) -> Decision:
    return Decision(False, status, code, message, **kw)


_ALLOW = Decision(True)


# ── addresses & hosts ─────────────────────────────────────────────────────────────────

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network
_Addr = ipaddress.IPv4Address | ipaddress.IPv6Address


def _parse_ip(text: str | None) -> _Addr | None:
    """An address from a peer/XFF entry (``[::1]:80``, ``1.2.3.4:5``, ``fe80::1%eth0``)."""
    if text is None:
        return None
    value = text.strip().strip('"')
    if value.startswith("["):
        end = value.find("]")
        value = value[1:end] if end != -1 else value[1:]
    elif value.count(":") == 1:  # IPv4 with port
        value = value.split(":", 1)[0]
    value = value.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _networks(entries: Iterable[str]) -> tuple[_Net, ...]:
    return tuple(ipaddress.ip_network(e.strip(), strict=False) for e in entries)


def _in(addr: _Addr | None, nets: tuple[_Net, ...]) -> bool:
    return addr is not None and any(addr.version == n.version and addr in n for n in nets)


def is_loopback(client: str | None, *, unix_socket: bool = False) -> bool:
    """Whether a client address is loopback.

    ``None``/``""`` – a unix-socket peer – counts only with ``unix_socket=True`` (the
    ``trust_unix_socket`` setting): a reverse proxy on this machine talking to the app over
    a unix socket would otherwise make every visitor local.
    """
    if client is None or client == "":
        return unix_socket
    if client == "localhost":
        return True
    addr = _parse_ip(client)
    return addr is not None and addr.is_loopback


def host_name(header: str) -> str:
    """``Host`` header → lower-cased name without port (``[::1]:8000`` → ``::1``)."""
    host = header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host[1:]
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host.rstrip(".")


def is_local_host(header: str | None, extra: Iterable[str] = ()) -> bool:
    """Whether a ``Host`` header names this machine's loopback interface.

    Accepted: ``localhost``, ``*.localhost``, loopback IP literals – any port – and the
    names in ``extra``. A missing/empty ``Host`` (HTTP/1.0, raw ASGI) is accepted: a
    browser always sends one, so it cannot be a rebinding attack.
    """
    if header is None:
        return True
    name = host_name(header)
    if not name:
        return True
    if name in LOCAL_HOST_NAMES or name.endswith(".localhost"):
        return True
    if name in {host_name(e) for e in extra}:
        return True
    addr = _parse_ip(name if ":" not in name else f"[{name}]")
    return addr is not None and addr.is_loopback


def parse_cookies(header: str) -> dict[str, str]:
    """A ``Cookie`` header → ``{name: value}``; repeated names keep every value joined by
    ``"\\x00"`` (see :meth:`Access._session`)."""
    out: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if not sep or not name:
            continue
        value = value.strip().strip('"')
        out[name] = f"{out[name]}\x00{value}" if name in out else value
    return out


def _last(value: str | None) -> str | None:
    if not value:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts[-1] if parts else None


@dataclass(frozen=True)
class _Client:
    ip: str | None
    addr: _Addr | None
    scheme: str
    host: str | None
    #: The peer is loopback but the request carries forwarding headers from a peer that is
    #: not trusted: a proxy on this machine relays it.
    relayed_untrusted: bool


# ── passwords ─────────────────────────────────────────────────────────────────────────


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(
    password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None
) -> str:
    """``pbkdf2_sha256$<iterations>$<salt>$<hash>`` (base64url) for ``basic_users``."""
    if not password:
        raise ValueError("The password must not be empty.")
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PASSWORD_HASH_PREFIX}{iterations}${_b64e(salt)}${_b64e(digest)}"


def _parse_hash(encoded: str) -> tuple[int, bytes, bytes]:
    try:
        algo, iterations, salt, digest = encoded.split("$")
        if algo != PASSWORD_HASH_PREFIX.rstrip("$"):
            raise ValueError
        n = int(iterations)
        if not 1_000 <= n <= 100_000_000:
            raise ValueError
        return n, _b64d(salt), _b64d(digest)
    except (ValueError, binascii.Error):
        raise SettingsError(
            "Invalid password hash in 'basic_users' (make one with `redline hash-password`)."
        ) from None


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a :func:`hash_password` hash."""
    iterations, salt, expected = _parse_hash(encoded)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# ── rate limit ────────────────────────────────────────────────────────────────────────


class _RateLimiter:
    """Failed credential attempts per client IP in a sliding window (in memory)."""

    MAX_KEYS = 10_000

    def __init__(self, limit: int, window: float, clock: Callable[[], float]) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float] | None:
        hits = self._hits.get(key)
        if hits is None:
            return None
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits

    def blocked(self, key: str) -> float:
        """Seconds until ``key`` may try again (0: not blocked)."""
        with self._lock:
            now = self.clock()
            hits = self._trim(key, now)
            if hits is None or len(hits) < self.limit:
                return 0.0
            return max(1.0, hits[0] + self.window - now)

    def fail(self, key: str) -> None:
        with self._lock:
            now = self.clock()
            if key not in self._hits and len(self._hits) >= self.MAX_KEYS:
                for k in list(self._hits)[: self.MAX_KEYS // 10]:  # drop the oldest keys
                    self._trim(k, now)
                    self._hits.pop(k, None)
            self._hits.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# ── route policy ──────────────────────────────────────────────────────────────────────


def _feature_for(method: str, path: str) -> str | None:
    if path == "api/explain":
        return "explain"
    if path == "api/stream":
        return "sse"
    if _PROFILE.match(path):
        return "profile"
    if _SNAPSHOT.match(path):
        return "snapshot"
    if _RUNS.match(path):
        return "load"
    if _CHECKS.match(path):
        return "checks"
    return None


def route_policy(method: str, path: str, settings: Settings) -> Decision | None:
    """Feature switches and ``read_only`` for one mount-relative ``path``.

    ``None``: allowed. A disabled feature's routes answer 404 ``feature_disabled``;
    ``read_only`` refuses every state-changing API call (DELETE, ``POST api/runs``, stop)
    with 403 ``read_only`` – except ``POST api/explain`` (EXPLAIN ANALYZE is refused by the
    API itself, which sees the body), login and logout.
    """
    method = method.upper()
    p = path.strip("/")
    feature = _feature_for(method, p)
    if feature is not None and not settings.feature(feature):
        return _deny(
            404,
            "feature_disabled",
            f"The {feature!r} feature is switched off on this Redline instance.",
        )
    if (
        settings.read_only
        and method not in _SAFE_METHODS
        and p.startswith("api/")
        and p not in ("api/explain", "api/login", "api/logout")
    ):
        return _deny(403, "read_only", "This Redline instance is read-only.")
    return None


# ── the gate ──────────────────────────────────────────────────────────────────────────


def _load_callable(spec: Any) -> Callable[[RequestInfo], Any]:
    if callable(spec):
        return spec
    module, _, attr = str(spec).replace(":", ".").rpartition(".")
    try:
        target = getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError, ValueError) as exc:
        raise SettingsError(f"access_check {spec!r} could not be imported: {exc}.") from None
    if not callable(target):
        raise SettingsError(f"access_check {spec!r} is not callable.")
    return target


def _read_token(settings: Settings) -> str | None:
    if settings.token is not None:
        return settings.token
    if settings.token_file is None:
        return None
    path = Path(settings.token_file).expanduser()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SettingsError(f"Cannot read the Redline token_file {str(path)!r}: {exc}.") from None
    if not token or any(c.isspace() or not c.isprintable() for c in token):
        raise SettingsError(
            f"The Redline token_file {str(path)!r} must hold one token without whitespace."
        )
    return token


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


class Access:
    """The access gate for one Redline mount (one per adapter instance).

    Raises :class:`SettingsError` when the policy cannot work (no token, unreadable
    ``token_file``, a weak token outside ``dev``, a bad ``access_check`` path).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        framework_ok: Callable[[], Any] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        access_explicit: bool = True,
        debug: Callable[[], Any] | None = None,
    ) -> None:
        environ = os.environ if environ is None else environ
        self.settings = settings
        self.policy = settings.access
        self.framework_ok = framework_ok
        self.clock = clock
        self._allowed = _networks(settings.allowed_ips)
        self._proxies = _networks(settings.trusted_proxies)
        self._extra_hosts = tuple(settings.allowed_hosts)
        self._limiter = _RateLimiter(settings.max_login_attempts, LOGIN_WINDOW_S, clock)
        if settings.secret_key:
            self._key = hmac.new(
                settings.secret_key.encode("utf-8"), b"redline-session-v1", hashlib.sha256
            ).digest()
        else:
            self._key = secrets.token_bytes(32)
        self._token: str | None = None
        self._users: dict[str, str] = {}
        self._custom: Callable[[RequestInfo], Any] | None = None
        self._verified: dict[bytes, float] = {}
        self._dummy_hash: str | None = None
        self._lock = threading.Lock()
        self._last_open_warning = 0.0
        self._notes: set[str] = set()
        #: ``access`` was set explicitly (file, env or framework layer). When it was not and
        #: the policy is ``local``, Redline only answers in framework debug mode (H3: a
        #: same-host reverse proxy that adds no forwarding headers makes everyone local).
        self.access_explicit = access_explicit
        #: The framework's debug flag, read on each Redline request (``app.debug``…).
        self.debug: Callable[[], Any] = debug or (lambda: False)
        self._startup_logged = False
        #: Logged-out session MACs → their expiry (per process, see :meth:`logout`).
        self._revoked: dict[str, float] = {}
        if self.policy == "token":
            self._token = _read_token(settings)
            if self._token is None:  # pragma: no cover - Settings validates it
                raise SettingsError("access='token' needs 'token' or 'token_file'.")
            check_token_strength(self._token, settings.preset)
        elif self.policy == "basic":
            self._users = dict(settings.basic_users)
            for encoded in self._users.values():
                _parse_hash(encoded)
        elif self.policy == "custom":
            self._custom = _load_callable(settings.access_check)
        self.open_allowed = environ.get(ALLOW_OPEN_ENV, "").strip() == "1"
        if self.policy == "open":
            if self.open_allowed:
                logger.warning(
                    "Redline access='open': the UI and API are reachable by anyone who can "
                    "reach this application. Never do this on a public server."
                )
            else:
                logger.error(
                    "Redline access='open' is refused: set %s=1 as well to really expose "
                    "the UI and API to everyone. Until then every request is refused.",
                    ALLOW_OPEN_ENV,
                )

    # -- helpers ---------------------------------------------------------------
    @property
    def login_kind(self) -> str | None:
        """``"token"`` / ``"basic"`` when the UI can log in, else ``None``."""
        return self.policy if self.policy in ("token", "basic") else None

    @property
    def implicit_local(self) -> bool:
        """``local`` only because nothing set ``access``: served in debug mode only."""
        return self.policy == "local" and not self.access_explicit

    def locked(self) -> bool:
        """Whether every Redline URL answers 404 because ``access`` was never set and the
        framework is not in debug mode (see :attr:`implicit_local`)."""
        if not self.implicit_local:
            return False
        try:
            return not self.debug()
        except Exception:  # pragma: no cover - a broken debug flag counts as "not debug"
            return True

    def log_startup(self, mount: str, hook: str = "") -> None:
        """Log once how the gate is set up: an error when Redline is locked (``access`` not
        set and no debug mode), a warning for ``access='local'``."""
        if self._startup_logged:
            return
        self._startup_logged = True
        if self.locked():
            logger.error(
                "Redline at %s answers 404 to everyone: the 'access' setting is not set and "
                "the application is not in debug mode. Set access explicitly to enable the "
                "UI – access='token' (with a token) or 'basic' to reach it on a server, or "
                "access='local' to accept that a reverse proxy on this machine (one that "
                "adds no X-Forwarded-For) would make every visitor local. "
                "In debug mode (app.debug) the 'local' default applies.",
                mount,
            )
        elif self.policy == "local":
            logger.warning(
                "Redline is mounted at %s with access='local': only loopback clients "
                "addressing localhost are allowed. Set access='token' or 'basic'%s before "
                "exposing it.",
                mount,
                f", or {hook}," if hook else "",
            )

    def runtime_warnings(self) -> list[str]:
        """Things noticed while serving requests (e.g. an untrusted proxy in front)."""
        return sorted(self._notes)

    def client(self, info: RequestInfo) -> _Client:
        """The effective client address, scheme and host (``trusted_proxies`` applied)."""
        peer = _parse_ip(info.client_ip)
        headers = info.headers
        forwarded = any(h in headers for h in _FORWARD_HEADERS)
        if self._proxies and _in(peer, self._proxies):
            ip, addr = info.client_ip, peer
            hops = [h.strip() for h in headers.get("x-forwarded-for", "").split(",") if h.strip()]
            for hop in reversed(hops):
                hop_addr = _parse_ip(hop)
                ip, addr = hop, hop_addr
                if hop_addr is None or not _in(hop_addr, self._proxies):
                    break
            scheme = (_last(headers.get("x-forwarded-proto")) or info.scheme).lower()
            if scheme not in ("http", "https"):
                scheme = info.scheme
            host = _last(headers.get("x-forwarded-host")) or info.host
            return _Client(ip, addr, scheme, host, relayed_untrusted=False)
        # A loopback (or unix-socket) peer that forwards a client address is a proxy on
        # this machine: without trusted_proxies every visitor would look local.
        relayed = forwarded and (peer.is_loopback if peer is not None else not info.client_ip)
        return _Client(info.client_ip, peer, info.scheme, info.host, relayed_untrusted=relayed)

    def _csrf(self, info: RequestInfo, client: _Client) -> Decision | None:
        if info.method in _SAFE_METHODS:
            return None
        site = info.headers.get("sec-fetch-site", "").strip().lower()
        if site in ("cross-site", "same-site"):
            return _deny(403, "csrf_failed", "Cross-site request blocked.")
        origin = info.headers.get("origin")
        if origin is None:
            return None  # not a browser: a web page cannot forge this request
        hosts = {_netloc(h, client.scheme) for h in (client.host, info.host) if h}
        parts = urlsplit(origin.strip()) if origin.strip() != "null" else None
        if parts is None or not parts.netloc or _netloc(parts.netloc, parts.scheme) not in hosts:
            return _deny(403, "csrf_failed", "Cross-origin request blocked.")
        return None

    def _network(self, info: RequestInfo, client: _Client) -> Decision | None:
        if self.policy == "local":
            if client.relayed_untrusted:
                self._notes.add(
                    "access='local' refused requests relayed by a proxy on this machine "
                    "(X-Forwarded-For from an untrusted peer). Set trusted_proxies to the "
                    "proxy's address, and use access='token' or 'basic' to reach Redline "
                    "through it."
                )
                return _deny(
                    403,
                    "forbidden",
                    "Redline refused a request relayed by a proxy (access='local'): set "
                    "trusted_proxies, and use access='token' or 'basic' to expose it.",
                )
            if client.addr is not None:
                local = client.addr.is_loopback
            else:
                local = is_loopback(client.ip, unix_socket=self.settings.trust_unix_socket)
            local = local or _in(client.addr, self._allowed)
            if not local:
                return _deny(
                    403,
                    "forbidden",
                    "Redline is only reachable from localhost (access='local'). Set "
                    "access='token' or 'basic' (or allowed_ips) to reach it from elsewhere.",
                )
            if not is_local_host(client.host, self._extra_hosts):
                return _deny(
                    403,
                    "forbidden",
                    "Redline only answers requests addressed to localhost, 127.0.0.1 or "
                    "[::1] (this request's Host header names another site, as in a "
                    "DNS-rebinding attack). Add the name to allowed_hosts to allow it.",
                )
            return None
        if self._allowed and not _in(client.addr, self._allowed):
            return _deny(403, "forbidden", "Your address is not in Redline's allowed_ips.")
        return None

    # -- the check -------------------------------------------------------------
    def check(
        self, info: RequestInfo, *, framework_ok: Callable[[], Any] | None = None
    ) -> Decision:
        """May ``info`` proceed? See the module docstring for the order of the checks.

        ``framework_ok`` (zero-argument, closing over the request) is the adapter's native
        hook for ``access="framework"``; exceptions it raises (e.g. a framework's
        ``HTTPException``) propagate to the adapter.
        """
        s = self.settings
        if not s.ui_enabled or self.locked():
            return _deny(404, "not_found", "Not found.")
        client = self.client(info)
        decision = self._decide(info, client, framework_ok)
        if decision.allowed and (refusal := self._csrf(info, client)):
            return refusal  # after the policy: an outsider learns nothing from a CSRF error
        return decision

    def _decide(
        self, info: RequestInfo, client: _Client, framework_ok: Callable[[], Any] | None
    ) -> Decision:
        s = self.settings
        path = info.path.strip("/")
        if refusal := self._network(info, client):
            return refusal
        principal: str | None = None
        if self.policy == "open":
            if not self.open_allowed:
                return _deny(
                    403,
                    "open_refused",
                    f"access='open' is refused unless {ALLOW_OPEN_ENV}=1 is set too.",
                )
            now = self.clock()
            if now - self._last_open_warning >= _OPEN_WARN_EVERY_S:
                self._last_open_warning = now
                logger.warning("Redline is serving %s with access='open'.", info.path or "/")
            principal = "open"
        elif path in ("api/login", "api/logout"):
            return _ALLOW  # answered by handle(), which rate-limits and verifies
        elif self.policy in ("token", "basic"):
            if not path.startswith("api/"):
                return _ALLOW  # the SPA shell and static files: no data in them
            decision = self._credentials(info, client)
            if not decision.allowed:
                return decision
            principal = decision.principal
        elif self.policy == "framework":
            hook = framework_ok or self.framework_ok
            if hook is None or not hook():
                return _deny(403, "forbidden", "You do not have access to Redline.")
            principal = "framework"
        elif self.policy == "custom":
            assert self._custom is not None
            try:
                ok = self._custom(info)
            except Exception:
                logger.exception("Redline access_check raised; refusing the request")
                ok = False
            if not ok:
                return _deny(403, "forbidden", "You do not have access to Redline.")
            principal = "custom"
        else:  # local – the network rules above did the work
            principal = "local"
        if refusal := route_policy(info.method, path, s):
            return refusal
        return Decision(True, principal=principal)

    def may_force(self, info: RequestInfo, *, debug: bool = False) -> bool:
        """May this request force Redline's capture (``X-Redline-Profile: 1`` /
        ``?_redline=profile``)?

        Yes in framework debug mode; otherwise only when the request itself would be let
        into Redline's API: a valid login cookie or ``Authorization`` header (``token`` /
        ``basic``, failures count towards the rate limit), a loopback client (``local``),
        ``access_check`` (``custom``) or ``open`` with ``REDLINE_ALLOW_OPEN=1``.
        ``framework`` hooks cannot run outside the framework's own routing, so with that
        policy only debug mode forces. Adapters call it only when a flag is present: the
        normal request path never pays for it.
        """
        if debug:
            return True
        if not self.settings.ui_enabled or self.locked():
            return False
        probe = RequestInfo(
            method="GET",
            path="api/meta",
            client_ip=info.client_ip,
            headers=info.headers,
            cookies=info.cookies,
            host=info.host,
            scheme=info.scheme,
            mount=info.mount,
        )
        try:
            return self._decide(probe, self.client(probe), lambda: False).allowed
        except Exception:  # pragma: no cover - never break the application request
            return False

    # -- credentials -----------------------------------------------------------
    def _login_required(self, info: RequestInfo, message: str | None = None) -> Decision:
        headers: dict[str, str] = {}
        if UI_HEADER not in info.headers:
            if self.policy == "basic":
                headers["WWW-Authenticate"] = 'Basic realm="Redline", charset="UTF-8"'
            else:
                headers["WWW-Authenticate"] = 'Bearer realm="Redline"'
        return _deny(
            401,
            "login_required",
            message or "Log in to Redline.",
            headers=headers,
            extra={"login": self.policy},
        )

    def _rate_limited(self, client: _Client) -> Decision | None:
        wait = self._limiter.blocked(client.ip or "-")
        if wait:
            return _deny(
                429,
                "too_many_requests",
                "Too many failed logins from your address; try again later.",
                headers={"Retry-After": str(int(wait + 0.999))},
            )
        return None

    def _credentials(self, info: RequestInfo, client: _Client) -> Decision:
        subject = self._session(info)
        if subject is not None:
            return Decision(True, principal=subject)
        scheme, _, value = info.headers.get("authorization", "").strip().partition(" ")
        scheme = scheme.lower()
        wanted = "bearer" if self.policy == "token" else "basic"
        if scheme != wanted or not value.strip():
            return self._login_required(info)
        if limited := self._rate_limited(client):
            return limited
        if self.policy == "token":
            ok, who = self._token_ok(value.strip()), "token"
        else:
            ok, who = self._basic_header_ok(value.strip())
        if ok:
            return Decision(True, principal=who)
        self._limiter.fail(client.ip or "-")
        refused = self._login_required(info, "Invalid credentials.")
        return _deny(
            401,
            "invalid_credentials",
            refused.message,
            headers=refused.headers,
            extra={"login": self.policy},
        )

    def _token_ok(self, candidate: str) -> bool:
        assert self._token is not None
        return hmac.compare_digest(_digest(candidate), _digest(self._token))

    def _basic_header_ok(self, value: str) -> tuple[bool, str | None]:
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False, None
        user, sep, password = decoded.partition(":")
        if not sep:
            return False, None
        return self._password_ok(user, password), user

    def _password_ok(self, user: str, password: str) -> bool:
        """PBKDF2 check, cached per (user, password) under the process key so a Basic
        header sent with every API call does not cost a full hash each time."""
        encoded = self._users.get(user)
        cache_key = hmac.new(
            self._key, f"{user}\x00{password}\x00{encoded}".encode(), hashlib.sha256
        ).digest()
        now = self.clock()
        with self._lock:
            expiry = self._verified.get(cache_key)
            if expiry is not None and expiry > now:
                return True
        if encoded is None:  # same cost as a real check: no user-name oracle
            if self._dummy_hash is None:
                self._dummy_hash = hash_password(secrets.token_urlsafe(16))
            verify_password(password, self._dummy_hash)
            return False
        if not verify_password(password, encoded):
            return False
        with self._lock:
            if len(self._verified) > 256:
                self._verified.clear()
            self._verified[cache_key] = now + 300.0
        return True

    # -- sessions --------------------------------------------------------------
    def _binding(self, subject: str) -> str:
        if self.policy == "token":
            secret = self._token or ""
        else:
            secret = self._users.get(subject, "")
        return hashlib.sha256(f"{self.policy}\x00{subject}\x00{secret}".encode()).hexdigest()

    def _sign(self, payload: str) -> str:
        return _b64e(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def make_session(self, subject: str, now: float | None = None) -> str:
        """A signed cookie value for ``subject`` (``"token"`` or a basic user name)."""
        now = self.clock() if now is None else now
        expiry = int(now + self.settings.session_ttl_s)
        payload = f"v1.{expiry}.{_b64e(subject.encode('utf-8'))}"
        return f"{payload}.{self._sign(payload + '.' + self._binding(subject))}"

    def verify_session(self, value: str) -> str | None:
        """The subject of a valid, unexpired cookie value, else ``None``."""
        parts = value.split(".")
        if len(parts) != 4 or parts[0] != "v1" or not parts[1].isdigit() or len(parts[1]) > 12:
            return None
        try:
            subject = _b64d(parts[2]).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        if self.policy == "token" and subject != "token":
            return None
        if self.policy == "basic" and subject not in self._users:
            return None
        payload = ".".join(parts[:3])
        expected = self._sign(payload + "." + self._binding(subject))
        if not hmac.compare_digest(expected.encode("ascii"), parts[3].encode("ascii", "replace")):
            return None
        expiry = int(parts[1])
        now = self.clock()
        if expiry <= now or expiry > now + self.settings.session_ttl_s + 60:
            return None
        if self._revoked and parts[3] in self._revoked:
            return None
        return subject

    def revoke_session(self, value: str) -> bool:
        """Refuse a valid cookie value from now on, until it would expire anyway (in this
        process only). Returns whether it was valid."""
        if self.verify_session(value) is None:
            return False
        parts = value.split(".")
        now = self.clock()
        with self._lock:
            if len(self._revoked) >= _MAX_REVOKED:
                for mac, expiry in list(self._revoked.items()):
                    if expiry <= now:
                        del self._revoked[mac]
                while len(self._revoked) >= _MAX_REVOKED:  # still full: forget the oldest
                    del self._revoked[min(self._revoked, key=self._revoked.__getitem__)]
            self._revoked[parts[3]] = float(parts[1])
        return True

    def _session(self, info: RequestInfo) -> str | None:
        raw = info.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        for value in raw.split("\x00"):  # a tossed cookie must not hide the real one
            subject = self.verify_session(value)
            if subject is not None:
                return subject
        return None

    def _cookie(self, info: RequestInfo, client: _Client, value: str, max_age: int) -> str:
        path = info.mount if _safe_cookie_path(info.mount) else "/"
        cookie = (
            f"{SESSION_COOKIE}={value}; Path={path}; Max-Age={max_age}; HttpOnly; SameSite=Strict"
        )
        secure = self.settings.cookie_secure
        if secure is True or (secure == "auto" and client.scheme == "https"):
            cookie += "; Secure"
        return cookie

    # -- login / logout --------------------------------------------------------
    def handle(self, info: RequestInfo, body: bytes | str | None) -> Decision | None:
        """Answer ``POST api/login`` / ``POST api/logout`` (``None`` for other paths).

        Call only after :meth:`check` allowed the request (CSRF and network rules).
        """
        path = info.path.strip("/")
        if path not in ("api/login", "api/logout"):
            return None
        if info.method != "POST":
            return _deny(405, "method_not_allowed", "Use POST.", headers={"Allow": "POST"})
        if path == "api/logout":
            return self.logout(info)
        return self.login(info, body)

    def login(self, info: RequestInfo, body: bytes | str | None) -> Decision:
        if self.login_kind is None:
            return _deny(404, "not_found", f"Redline has no login with access={self.policy!r}.")
        client = self.client(info)
        if limited := self._rate_limited(client):
            return limited
        raw = body.encode("utf-8") if isinstance(body, str) else (body or b"")
        if len(raw) > _MAX_LOGIN_BODY:
            return _deny(400, "invalid_body", "The login body is too large.")
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return _deny(400, "invalid_json", "The request body is not valid JSON.")
        if not isinstance(data, dict):
            return _deny(400, "invalid_body", "Expected a JSON object.")
        if self.policy == "token":
            token = data.get("token")
            if not isinstance(token, str) or not token:
                return _deny(400, "invalid_body", 'Expected {"token": "..."}.')
            ok, subject = self._token_ok(token.strip()), "token"
        else:
            user, password = data.get("username"), data.get("password")
            if not isinstance(user, str) or not isinstance(password, str) or not user:
                return _deny(400, "invalid_body", 'Expected {"username": ..., "password": ...}.')
            ok, subject = self._password_ok(user, password), user
        if not ok:
            self._limiter.fail(client.ip or "-")
            return _deny(
                401, "invalid_credentials", "Invalid credentials.", extra={"login": self.policy}
            )
        self._limiter.reset(client.ip or "-")
        now = self.clock()
        value = self.make_session(subject, now)
        ttl = self.settings.session_ttl_s
        return Decision(
            True,
            200,
            headers={"Set-Cookie": self._cookie(info, client, value, ttl)},
            payload={"ok": True, "expires_at": int(now + ttl)},
            principal=subject,
        )

    def logout(self, info: RequestInfo) -> Decision:
        """Clear the cookie and revoke the session it carried.

        Revocation is kept in memory by this process until the cookie would have expired:
        with several worker processes another worker still accepts the cookie (until it
        expires, or ``secret_key``/the token/the password is rotated).
        """
        raw = info.cookies.get(SESSION_COOKIE)
        if raw:
            for value in raw.split("\x00"):
                self.revoke_session(value)
        client = self.client(info)
        return Decision(
            True,
            200,
            headers={"Set-Cookie": self._cookie(info, client, "", 0)},
            payload={"ok": True},
        )

    def meta(self) -> dict[str, Any]:
        """``GET api/meta``'s ``access`` block."""
        return {
            "policy": self.policy,
            "login": self.login_kind,
            "read_only": self.settings.read_only,
            "preset": self.settings.preset,
        }


def _netloc(value: str, scheme: str) -> str:
    """``host[:port]`` lower-cased, the default port of ``scheme`` dropped."""
    v = value.strip().lower().rstrip("/")
    default = {"http": "80", "https": "443"}.get(scheme.lower())
    if default and v.endswith(f":{default}") and not v.endswith("]"):
        v = v[: -(len(default) + 1)]
    return v


def _safe_cookie_path(path: str) -> bool:
    return (
        path.startswith("/")
        and len(path) < 512
        and not any(c in path for c in ';,"\\')
        and all(33 <= ord(c) < 127 for c in path)
    )


# ── module-level convenience ──────────────────────────────────────────────────────────

_gates: dict[int, tuple[weakref.ref[Any] | None, Access]] = {}
_gates_lock = threading.Lock()


def get_access(settings: Settings) -> Access:
    """A process-wide :class:`Access` for ``settings`` (cached per settings object, so the
    rate limiter and the session key persist)."""
    key = id(settings)
    with _gates_lock:
        entry = _gates.get(key)
        if entry is not None and entry[1].settings is settings:
            return entry[1]
        gate = Access(settings)
        try:
            ref: weakref.ref[Any] | None = weakref.ref(
                settings, lambda _r, k=key: _gates.pop(k, None)
            )
        except TypeError:  # pragma: no cover
            ref = None
        _gates[key] = (ref, gate)
        return gate


def check(
    info: RequestInfo, settings: Settings, *, framework_ok: Callable[[], Any] | None = None
) -> Decision:
    """:meth:`Access.check` with the process-wide gate for ``settings``."""
    return get_access(settings).check(info, framework_ok=framework_ok)
