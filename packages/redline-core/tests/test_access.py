"""The shared access gate (CONTRACT.md section 18)."""

from __future__ import annotations

import base64
import json
import logging

import pytest

from redline_core import access as access_mod
from redline_core.access import (
    SESSION_COOKIE,
    Access,
    RequestInfo,
    check,
    get_access,
    hash_password,
    is_local_host,
    is_loopback,
    parse_cookies,
    route_policy,
    verify_password,
)
from redline_core.api import RedlineAPI
from redline_core.conf import Settings, SettingsError
from redline_core.model import LogRecord, Trace
from redline_core.storage import SQLiteStore

TOKEN = "s3cr3t-" + "x" * 30
HASH = hash_password("pw", iterations=1000)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def req(
    path="api/meta",
    *,
    method="GET",
    ip="127.0.0.1",
    host="localhost:8000",
    scheme="http",
    mount="/_redline",
    **headers,
):
    hdrs = {k.replace("_", "-"): v for k, v in headers.items()}
    if host is not None:
        hdrs["host"] = host
    return RequestInfo.build(
        method=method, path=path, client_ip=ip, headers=hdrs, scheme=scheme, mount=mount
    )


def gate(clock=None, environ=None, **settings) -> Access:
    return Access(Settings.from_mapping(settings), clock=clock or Clock(), environ=environ or {})


def basic(user="ann", password="pw"):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ── helpers ───────────────────────────────────────────────────────────────────────────


def test_loopback_and_host_helpers():
    for addr in ("127.0.0.1", "127.9.9.9", "::1", "::ffff:127.0.0.1", "[::1]:80"):
        assert is_loopback(addr), addr
    for addr in ("10.0.0.1", "2001:db8::1", "evil.com", "0.0.0.0", "::ffff:8.8.8.8", None, ""):
        assert not is_loopback(addr), addr
    # a unix-socket peer counts only when trusted (trust_unix_socket)
    assert is_loopback(None, unix_socket=True) and is_loopback("", unix_socket=True)
    for host in ("localhost", "LOCALHOST:1", "a.localhost", "127.0.0.1:9", "[::1]", None, ""):
        assert is_local_host(host), host
    for host in ("evil.example", "localhost.evil.com", "10.0.0.1", "[2001:db8::1]:80"):
        assert not is_local_host(host), host
    assert is_local_host("devbox:8000", ["DEVBOX"])


def test_parse_cookies_keeps_duplicates():
    c = parse_cookies(f'a=1; {SESSION_COOKIE}=x; junk; {SESSION_COOKIE}="y"')
    assert c["a"] == "1" and c[SESSION_COOKIE] == "x\x00y"


def test_request_info_build_merges_headers():
    info = RequestInfo.build(
        method="get",
        path="api/x",
        client_ip=None,
        headers=[(b"X-Forwarded-For", b"1.1.1.1"), (b"x-forwarded-for", b"2.2.2.2")],
    )
    assert info.method == "GET" and info.headers["x-forwarded-for"] == "1.1.1.1, 2.2.2.2"
    assert info.host is None and info.cookies == {}


def test_password_hashing():
    encoded = hash_password("correct horse", iterations=1000)
    assert encoded.startswith("pbkdf2_sha256$1000$")
    assert verify_password("correct horse", encoded)
    assert not verify_password("Correct horse", encoded)
    assert hash_password("x", iterations=1000) != hash_password("x", iterations=1000)  # salted
    with pytest.raises(ValueError):
        hash_password("")
    with pytest.raises(SettingsError):
        verify_password("x", "pbkdf2_sha256$10$bad")
    with pytest.raises(SettingsError):
        gate(access="basic", basic_users={"ann": "pbkdf2_sha256$1$a$b"})  # too few iterations


# ── local ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "::ffff:127.0.0.1", None, "fe80::1%lo0"])
def test_local_allows_loopback(ip):
    g = gate(trust_unix_socket=ip is None)
    d = g.check(req(ip=ip))
    if ip == "fe80::1%lo0":
        assert not d.allowed  # link-local is not loopback
    else:
        assert d.allowed and d.principal == "local"


def test_local_refuses_remote_and_rebinding():
    g = gate()
    d = g.check(req(ip="203.0.113.9"))
    assert (d.status, d.code) == (403, "forbidden") and "localhost" in d.message
    for host in ("evil.example", "evil.example:8000", "localhost.evil.com", "10.0.0.1"):
        d = g.check(req(host=host))
        assert d.code == "forbidden" and "Host" in d.message
    assert g.check(req(path="", host="evil.example")).code == "forbidden"  # the page too
    assert g.check(req(path="assets/x.js", ip="10.0.0.2")).code == "forbidden"


def test_local_allowed_ips_and_hosts():
    g = gate(allowed_ips=["10.8.0.0/16", "fd00::/8"], allowed_hosts=["devbox"])
    assert g.check(req(ip="10.8.3.4", host="devbox:8000")).allowed
    assert g.check(req(ip="fd00::5", host="devbox")).allowed
    assert g.check(req(ip="10.9.0.1", host="devbox")).code == "forbidden"
    assert g.check(req(ip="10.8.3.4", host="evil.example")).code == "forbidden"  # rebinding


def test_local_refuses_requests_relayed_by_untrusted_proxy():
    g = gate()
    for header in ("x_forwarded_for", "x_real_ip", "forwarded"):
        d = g.check(req(**{header: "203.0.113.9"}))
        assert d.code == "forbidden" and "proxy" in d.message
    assert g.runtime_warnings() and "trusted_proxies" in g.runtime_warnings()[0]
    # a remote peer with a spoofed XFF claiming to be local is still remote
    assert g.check(req(ip="203.0.113.9", x_forwarded_for="127.0.0.1")).code == "forbidden"


# ── proxies ───────────────────────────────────────────────────────────────────────────


def test_trusted_proxy_xff_rightmost_untrusted():
    g = gate(trusted_proxies=["10.0.0.0/8"], allowed_ips=["198.51.100.0/24"])
    # client spoofs a leftmost entry; the proxy appends the real client address
    info = req(ip="10.0.0.2", x_forwarded_for="127.0.0.1, 198.51.100.7, 10.0.0.9")
    client = g.client(info)
    assert client.ip == "198.51.100.7"
    assert g.check(info).allowed  # the real client is in allowed_ips (local policy)
    assert not g.check(req(ip="10.0.0.2", x_forwarded_for="203.0.113.1")).allowed
    # all hops trusted → the leftmost
    assert g.client(req(ip="10.0.0.2", x_forwarded_for="10.1.1.1, 10.2.2.2")).ip == "10.1.1.1"
    # garbage hop stops the walk and is not an address
    c = g.client(req(ip="10.0.0.2", x_forwarded_for="1.1.1.1, nonsense"))
    assert c.ip == "nonsense" and c.addr is None


def test_forwarded_headers_ignored_from_untrusted_peers():
    g = gate(access="token", token=TOKEN, trusted_proxies=["10.0.0.1"])
    c = g.client(
        req(
            ip="203.0.113.5",
            x_forwarded_for="198.51.100.1",
            x_forwarded_proto="https",
            x_forwarded_host="evil",
        )
    )
    assert (c.ip, c.scheme, c.host) == ("203.0.113.5", "http", "localhost:8000")
    c = g.client(
        req(
            ip="10.0.0.1",
            x_forwarded_for="198.51.100.1",
            x_forwarded_proto="http, https",
            x_forwarded_host="a, redline.example",
        )
    )
    assert (c.ip, c.scheme, c.host) == ("198.51.100.1", "https", "redline.example")
    c = g.client(req(ip="10.0.0.1", x_forwarded_proto="gopher"))
    assert c.scheme == "http"


def test_ipv6_trusted_proxy():
    g = gate(trusted_proxies=["fd00::/8"], allowed_ips=["2001:db8::/32"])
    assert g.check(req(ip="fd00::1", x_forwarded_for="2001:db8::7")).allowed
    assert g.client(req(ip="[fd00::1]:443", x_forwarded_for="[2001:db8::7]:5")).ip == (
        "[2001:db8::7]:5"
    )


# ── token ─────────────────────────────────────────────────────────────────────────────


def test_token_policy_header_and_shell():
    g = gate(access="token", token=TOKEN)
    d = g.check(req(ip="203.0.113.9", host="redline.example"))
    assert (d.status, d.code) == (401, "login_required")
    assert d.body()["error"]["login"] == "token"
    assert d.headers["WWW-Authenticate"].startswith("Bearer")
    assert "WWW-Authenticate" not in g.check(req(x_redline_ui="1")).headers
    ok = g.check(req(ip="203.0.113.9", authorization=f"Bearer {TOKEN}"))
    assert ok.allowed and ok.principal == "token"
    assert g.check(req(authorization=f"bearer {TOKEN}")).allowed  # scheme is case-insensitive
    bad = g.check(req(authorization="Bearer nope"))
    assert (bad.status, bad.code) == (401, "invalid_credentials")
    assert g.check(req(authorization=basic())).code == "login_required"  # wrong scheme
    # the SPA shell and static files are public (no data), the API is not
    for path in ("", "assets/index-abc.js", "badge.js"):
        assert g.check(req(path, ip="203.0.113.9")).allowed, path
    assert g.check(req("api/traces/abc/snapshot", ip="203.0.113.9")).code == "login_required"
    assert g.check(req("api/stream", ip="203.0.113.9")).code == "login_required"


def test_token_file(tmp_path):
    f = tmp_path / "token"
    f.write_text(TOKEN + "\n")
    g = gate(access="token", token_file=str(f))
    assert g.check(req(authorization=f"Bearer {TOKEN}")).allowed
    with pytest.raises(SettingsError, match="token_file"):
        gate(access="token", token_file=str(tmp_path / "missing"))
    f.write_text("two words\n")
    with pytest.raises(SettingsError, match="without whitespace"):
        gate(access="token", token_file=str(f))
    f.write_text("short\n")
    with pytest.raises(SettingsError, match="at least 24"):
        gate(access="token", token_file=str(f), preset="staging")


def login(g, body, **kw):
    info = req("api/login", method="POST", **kw)
    assert g.check(info).allowed
    return g.handle(info, json.dumps(body).encode())


def session_of(decision) -> str:
    cookie = decision.headers["Set-Cookie"]
    return cookie.split(";", 1)[0].split("=", 1)[1]


def test_token_login_cookie_flow():
    clock = Clock()
    g = gate(access="token", token=TOKEN, session_ttl_s=60, clock=clock)
    d = login(g, {"token": TOKEN})
    assert d.allowed and d.status == 200 and d.body() == {"ok": True, "expires_at": 1_000_060}
    cookie = d.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/_redline;" in cookie
    assert "Max-Age=60" in cookie and "Secure" not in cookie
    value = session_of(d)
    assert TOKEN not in value
    assert g.check(req(cookie=f"{SESSION_COOKIE}={value}")).allowed
    # expiry
    clock.t += 61
    assert g.check(req(cookie=f"{SESSION_COOKIE}={value}")).code == "login_required"


def test_cookie_secure_on_https_and_via_trusted_proxy():
    g = gate(access="token", token=TOKEN, trusted_proxies=["10.0.0.1"])
    assert "; Secure" in login(g, {"token": TOKEN}, scheme="https").headers["Set-Cookie"]
    d = login(g, {"token": TOKEN}, ip="10.0.0.1", x_forwarded_proto="https")
    assert "; Secure" in d.headers["Set-Cookie"]
    d = login(g, {"token": TOKEN}, mount="/a;b")  # unsafe mount → Path=/
    assert "Path=/;" in d.headers["Set-Cookie"]


def test_cookie_tampering():
    g = gate(access="token", token=TOKEN)
    value = session_of(login(g, {"token": TOKEN}))
    v, expiry, subject, mac = value.split(".")
    forged = [
        f"{v}.{int(expiry) + 1000}.{subject}.{mac}",  # extended expiry
        f"{v}.{expiry}.{subject}.{mac[:-2]}AA",  # bad mac
        f"v2.{expiry}.{subject}.{mac}",
        f"{v}.{expiry}.{base64.urlsafe_b64encode(b'admin').decode().rstrip('=')}.{mac}",
        "garbage",
        f"{v}.-5.{subject}.{mac}",
        "",
    ]
    for bad in forged:
        assert g.check(req(cookie=f"{SESSION_COOKIE}={bad}")).code == "login_required", bad
    # another process's key (no secret_key) does not validate it
    other = gate(access="token", token=TOKEN)
    assert other.check(req(cookie=f"{SESSION_COOKIE}={value}")).code == "login_required"
    # a tossed cookie next to the real one does not hide it
    tossed = f"{SESSION_COOKIE}=junk; {SESSION_COOKIE}={value}"
    assert g.check(req(cookie=tossed)).allowed


def test_secret_key_shares_sessions_and_token_rotation_revokes():
    a = gate(access="token", token=TOKEN, secret_key="k" * 32)
    b = gate(access="token", token=TOKEN, secret_key="k" * 32)
    value = session_of(login(a, {"token": TOKEN}))
    assert b.check(req(cookie=f"{SESSION_COOKIE}={value}")).allowed  # another worker
    rotated = gate(access="token", token=TOKEN + "2", secret_key="k" * 32)
    assert rotated.check(req(cookie=f"{SESSION_COOKIE}={value}")).code == "login_required"
    other_key = gate(access="token", token=TOKEN, secret_key="j" * 32)
    assert not other_key.check(req(cookie=f"{SESSION_COOKIE}={value}")).allowed


def test_logout_clears_cookie():
    g = gate(access="token", token=TOKEN)
    info = req("api/logout", method="POST")
    assert g.check(info).allowed
    d = g.handle(info, b"")
    assert d.body() == {"ok": True}
    assert (
        d.headers["Set-Cookie"].startswith(f"{SESSION_COOKIE}=;")
        and "Max-Age=0" in (d.headers["Set-Cookie"])
    )
    assert g.handle(req("api/login", method="GET"), None).status == 405
    assert g.handle(req("api/meta"), None) is None


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"{", "invalid_json"),
        (b"[]", "invalid_body"),
        (b'{"token": 5}', "invalid_body"),
        (b'{"token": ""}', "invalid_body"),
        (b'{"token": "' + b"x" * 5000 + b'"}', "invalid_body"),
        (b'{"token": "wrong"}', "invalid_credentials"),
    ],
)
def test_login_body_validation(body, code):
    g = gate(access="token", token=TOKEN)
    assert g.handle(req("api/login", method="POST"), body).code == code


def test_login_not_available_without_login_policy():
    g = gate()
    info = req("api/login", method="POST")
    assert g.check(info).allowed
    assert g.handle(info, b"{}").status == 404


# ── basic ─────────────────────────────────────────────────────────────────────────────


def test_basic_policy():
    g = gate(access="basic", basic_users={"ann": HASH})
    d = g.check(req())
    assert d.code == "login_required" and d.headers["WWW-Authenticate"].startswith("Basic")
    assert d.body()["error"]["login"] == "basic"
    assert g.check(req(x_redline_ui="1")).headers == {}  # no native browser dialog for the UI
    ok = g.check(req(authorization=basic()))
    assert ok.allowed and ok.principal == "ann"
    assert g.check(req(authorization=basic()))  # cached verification
    assert g.check(req(authorization=basic(password="nope"))).code == "invalid_credentials"
    assert g.check(req(authorization=basic(user="bob"))).code == "invalid_credentials"
    assert g.check(req(authorization="Basic !!!")).code == "invalid_credentials"
    assert g.check(req(authorization="Basic " + base64.b64encode(b"nocolon").decode())).code == (
        "invalid_credentials"
    )
    d = login(g, {"username": "ann", "password": "pw"})
    assert d.allowed and d.principal == "ann"
    assert g.check(req(cookie=f"{SESSION_COOKIE}={session_of(d)}")).principal == "ann"
    assert login(g, {"username": "ann"}).code == "invalid_body"
    assert login(g, {"username": "ann", "password": "x"}).code == "invalid_credentials"


def test_basic_password_change_revokes_sessions():
    a = gate(access="basic", basic_users={"ann": HASH}, secret_key="k" * 32)
    value = session_of(login(a, {"username": "ann", "password": "pw"}))
    b = gate(
        access="basic",
        basic_users={"ann": hash_password("new", iterations=1000)},
        secret_key="k" * 32,
    )
    assert b.check(req(cookie=f"{SESSION_COOKIE}={value}")).code == "login_required"
    removed = gate(access="basic", basic_users={"bob": HASH}, secret_key="k" * 32)
    assert not removed.check(req(cookie=f"{SESSION_COOKIE}={value}")).allowed


# ── rate limit ────────────────────────────────────────────────────────────────────────


def test_login_rate_limit_per_ip():
    clock = Clock()
    g = gate(access="token", token=TOKEN, max_login_attempts=3, clock=clock)
    for _ in range(3):
        assert login(g, {"token": "bad"}).code == "invalid_credentials"
    limited = login(g, {"token": TOKEN})  # even the right token waits
    assert (limited.status, limited.code) == (429, "too_many_requests")
    assert 1 <= int(limited.headers["Retry-After"]) <= 300
    # bearer headers count and are limited too
    assert g.check(req(authorization=f"Bearer {TOKEN}")).status == 429
    # another IP is not affected
    assert login(g, {"token": TOKEN}, ip="::1").allowed
    # a valid session is not affected
    clock.t += 301
    assert login(g, {"token": TOKEN}).allowed


def test_rate_limit_counts_header_failures():
    g = gate(access="basic", basic_users={"ann": HASH}, max_login_attempts=2)
    for _ in range(2):
        g.check(req(authorization=basic(password="x")))
    assert g.check(req(authorization=basic())).status == 429


def test_rate_limit_key_is_the_real_client_behind_trusted_proxy():
    g = gate(access="token", token=TOKEN, max_login_attempts=1, trusted_proxies=["10.0.0.1"])
    assert login(g, {"token": "x"}, ip="10.0.0.1", x_forwarded_for="198.51.100.1").status == 401
    assert login(g, {"token": "x"}, ip="10.0.0.1", x_forwarded_for="198.51.100.1").status == 429
    assert login(g, {"token": TOKEN}, ip="10.0.0.1", x_forwarded_for="198.51.100.2").allowed


# ── allowed_ips as an extra requirement ───────────────────────────────────────────────


def test_allowed_ips_restrict_other_policies():
    g = gate(access="token", token=TOKEN, allowed_ips=["192.168.0.0/16"])
    auth = {"authorization": f"Bearer {TOKEN}"}
    assert g.check(req(ip="192.168.1.2", **auth)).allowed
    assert g.check(req(ip="127.0.0.1", **auth)).code == "forbidden"  # loopback is not implicit
    assert g.check(req("", ip="8.8.8.8")).code == "forbidden"  # even the shell


# ── framework / custom / open ─────────────────────────────────────────────────────────


def test_framework_policy():
    g = gate(access="framework")
    assert g.check(req()).code == "forbidden"  # no hook given
    assert g.check(req(ip="8.8.8.8", host="x"), framework_ok=lambda: True).allowed
    assert g.check(req(), framework_ok=lambda: False).code == "forbidden"

    def boom():
        raise RuntimeError("framework says 401")

    with pytest.raises(RuntimeError):
        g.check(req(), framework_ok=boom)  # adapters render their own exceptions
    hooked = Access(Settings(access="framework"), framework_ok=lambda: True)
    assert hooked.check(req()).allowed


def allow_admins(info):
    return info.headers.get("x-admin") == "yes"


def test_custom_policy(caplog):
    g = gate(access="custom", access_check=allow_admins)
    assert g.check(req(x_admin="yes")).allowed
    assert g.check(req()).code == "forbidden"
    dotted = gate(access="custom", access_check=f"{__name__}:allow_admins")
    assert dotted.check(req(x_admin="yes")).allowed
    dotted = gate(access="custom", access_check=f"{__name__}.allow_admins")
    assert dotted.check(req(x_admin="yes")).allowed
    with pytest.raises(SettingsError):
        gate(access="custom", access_check="no.such.module:fn")

    def broken(info):
        raise ValueError("bug")

    with caplog.at_level(logging.ERROR, logger="redline"):
        assert gate(access="custom", access_check=broken).check(req()).code == "forbidden"
    assert "access_check raised" in caplog.text


def test_open_is_refused_without_env(caplog):
    with caplog.at_level(logging.ERROR, logger="redline"):
        g = gate(access="open")
    assert "REDLINE_ALLOW_OPEN" in caplog.text
    d = g.check(req(ip="8.8.8.8", host="anything"))
    assert (d.status, d.code) == (403, "open_refused")
    clock = Clock()
    g = gate(access="open", environ={"REDLINE_ALLOW_OPEN": "1"}, clock=clock)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="redline"):
        assert g.check(req(ip="8.8.8.8", host="anything")).allowed
        assert g.check(req(ip="8.8.8.8", host="anything")).allowed
    assert caplog.text.count("access='open'") == 1  # throttled to once a minute
    clock.t += 61
    with caplog.at_level(logging.WARNING, logger="redline"):
        g.check(req(ip="8.8.8.8", host="anything"))
    assert caplog.text.count("access='open'") == 2


# ── CSRF ──────────────────────────────────────────────────────────────────────────────


def test_csrf_on_every_mutation_including_login():
    g = gate()
    post = {"method": "POST"}
    assert g.check(req("api/explain", **post, origin="http://evil.example")).code == "csrf_failed"
    assert g.check(req("api/explain", **post, origin="null")).code == "csrf_failed"
    assert g.check(req("api/explain", **post, sec_fetch_site="cross-site")).code == "csrf_failed"
    assert g.check(req("api/explain", **post, sec_fetch_site="same-site")).code == "csrf_failed"
    assert g.check(req("api/login", **post, origin="http://evil.example")).code == "csrf_failed"
    assert g.check(req("api/logout", **post, origin="http://evil.example")).code == "csrf_failed"
    assert g.check(req("api/explain", **post, origin="http://localhost:8000")).allowed
    assert g.check(req("api/explain", **post)).allowed  # no Origin: not a browser
    assert g.check(req("api/traces", method="DELETE", origin="http://evil.example")).code == (
        "csrf_failed"
    )
    # GET/HEAD/OPTIONS are never CSRF-checked
    for method in ("GET", "HEAD", "OPTIONS"):
        assert g.check(req(method=method, origin="http://evil.example")).allowed


def test_csrf_default_ports_and_proxy_host():
    g = gate(access="token", token=TOKEN, trusted_proxies=["10.0.0.1"])
    auth = {"authorization": f"Bearer {TOKEN}"}
    info = req(
        "api/explain",
        method="POST",
        ip="10.0.0.1",
        host="10.0.0.5:8000",
        x_forwarded_host="redline.example",
        x_forwarded_proto="https",
        origin="https://redline.example",
        **auth,
    )
    assert g.check(info).allowed
    info = req(
        "api/explain",
        method="POST",
        host="example.com:443",
        scheme="https",
        origin="https://example.com",
        **auth,
    )
    assert g.check(info).allowed


def test_policy_refusal_before_csrf():
    g = gate()
    d = g.check(req("api/traces", method="DELETE", ip="8.8.8.8", origin="http://evil"))
    assert d.code == "forbidden"


# ── route policy ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "feature"),
    [
        ("POST", "api/explain", "explain"),
        ("GET", "api/stream", "sse"),
        ("GET", "api/traces/abc/profile", "profile"),
        ("GET", "/api/traces/abc/snapshot/", "snapshot"),
        ("GET", "api/runs", "load"),
        ("POST", "api/runs", "load"),
        ("GET", "api/runs/r1/bottlenecks", "load"),
    ],
)
def test_features_disable_routes(method, path, feature):
    s = Settings(features={feature: False})
    d = route_policy(method, path, s)
    assert d is not None and (d.status, d.code) == (404, "feature_disabled")
    assert route_policy(method, path, Settings()) is None


@pytest.mark.parametrize(
    ("method", "path", "refused"),
    [
        ("DELETE", "api/traces", True),
        ("DELETE", "api/traces/abc", True),
        ("POST", "api/runs", True),
        ("POST", "api/runs/r1/stop", True),
        ("DELETE", "api/runs/r1", True),
        ("PUT", "api/traces", True),
        ("POST", "api/explain", False),
        ("POST", "api/login", False),
        ("POST", "api/logout", False),
        ("GET", "api/traces", False),
        ("HEAD", "api/traces", False),
    ],
)
def test_read_only(method, path, refused):
    d = route_policy(method, path, Settings(read_only=True))
    assert (d is not None and d.code == "read_only") is refused


def test_check_applies_route_policy_and_ui_enabled():
    g = gate(read_only=True, features={"sse": False})
    assert g.check(req("api/traces", method="DELETE")).code == "read_only"
    assert g.check(req("api/stream")).code == "feature_disabled"
    off = gate(ui_enabled=False)
    for path in ("", "api/meta", "badge.js"):
        assert off.check(req(path)).status == 404


def test_module_level_check_reuses_the_gate():
    s = Settings(access="token", token=TOKEN, max_login_attempts=1)
    assert get_access(s) is get_access(s)
    assert check(req(), s).code == "login_required"
    assert check(req(authorization="Bearer x"), s).code == "invalid_credentials"
    assert check(req(authorization="Bearer x"), s).status == 429  # limiter state kept
    assert access_mod.get_access(Settings()) is not get_access(s)


# ── the API side ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def store():
    st = SQLiteStore(":memory:", background=False)
    yield st
    st.close()


def _trace_with_logs_and_locals() -> Trace:
    t = Trace(
        id="t1",
        framework="other",
        method="GET",
        path="/x",
        route=None,
        view=None,
        status=500,
        started_at=1.0,
        duration_ms=1.0,
        query_count=0,
        query_ms=0.0,
        request={},
        response={},
        spans=[],
        issues=[],
        tags={},
        profile={"shared": {"frames": []}, "profiles": []},
        error={
            "type": "E",
            "message": "m",
            "traceback": "",
            "frames": [
                {"file": "a.py", "line": 1, "function": "f", "code": "", "locals": {"x": "1"}}
            ],
        },
        truncated=None,
    )
    t.logs = [LogRecord(t_ms=0.0, level="INFO", logger="app", message="hello", callsite=None)]
    return t


def test_api_hides_logs_and_locals(store):
    store.save(_trace_with_logs_and_locals())
    api = RedlineAPI(store, Settings(features={"logs": False, "locals": False, "profile": False}))
    status, data = api.dispatch("GET", "api/traces/t1")
    assert status == 200 and data["logs"] == [] and "locals" not in data["error"]["frames"][0]
    assert api.dispatch("GET", "api/traces/t1/profile")[0] == 404
    status, page = api.dispatch("GET", "api/traces/t1/snapshot")
    body = page.body.decode()
    assert "hello" not in body and '"profile": null' in body.replace(
        '"profile":null', '"profile": null'
    )
    full = RedlineAPI(store, Settings())
    _, data = full.dispatch("GET", "api/traces/t1")
    assert data["logs"][0]["message"] == "hello" and data["error"]["frames"][0]["locals"]


def test_api_route_policy_and_meta(store):
    api = RedlineAPI(store, Settings(read_only=True, features={"explain": False}))
    assert api.dispatch("DELETE", "api/traces") == (
        403,
        {"error": {"code": "read_only", "message": "This Redline instance is read-only."}},
    )
    assert api.dispatch("POST", "api/explain", body={})[1]["error"]["code"] == "feature_disabled"
    _, meta = api.dispatch("GET", "api/meta")
    assert meta["features"]["explain"] is False and meta["features"]["load_from_ui"] is False
    assert meta["access"] == {"policy": "local", "login": None, "read_only": True, "preset": "dev"}


def test_api_explain_analyze_refused_when_read_only(store):
    api = RedlineAPI(store, Settings(read_only=True, explain_analyze=True))
    status, body = api.dispatch(
        "POST", "api/explain", body={"trace_id": "x", "span_id": "y", "analyze": True}
    )
    assert status == 403 and body["error"]["code"] == "read_only"


def test_api_config_endpoint(store, tmp_path):
    from redline_core.config import resolve_settings

    resolved = resolve_settings(
        {"access": "token", "token": TOKEN},
        environ={"REDLINE_SLOW_QUERY_MS": "5"},
        project_root=tmp_path,
    )
    g = Access(resolved.settings)
    api = RedlineAPI(store, resolved.settings, config=resolved, access=g)
    status, data = api.dispatch("GET", "api/config")
    assert status == 200
    assert data["settings"]["token"] == "***" and TOKEN not in json.dumps(data)
    assert data["sources"]["slow_query_ms"] == "env" and data["sources"]["token"] == "framework"
    assert data["preset"] == "dev" and any("secret_key" in w for w in data["warnings"])
    assert api.dispatch("POST", "api/config")[0] == 405
    _, meta = api.dispatch("GET", "api/meta")
    assert meta["access"]["login"] == "token"
    # without a ResolvedConfig the API still answers
    bare = RedlineAPI(store, Settings(token=TOKEN))
    _, data = bare.dispatch("GET", "api/config")
    assert data["settings"]["token"] == "***" and data["sources"]["token"] == "framework"


def test_recorder_does_not_capture_logs_or_locals_when_disabled():
    from redline_core.recorder import Recorder

    rec = Recorder(Settings(features={"logs": False, "locals": False}), store=None)
    assert rec._capture_logs is False
    with rec.recording(method="GET", path="/x"):
        logging.getLogger("app").warning("not captured")
    assert rec.last_trace is not None and rec.last_trace.logs == []
    with pytest.raises(ValueError), rec.recording(method="GET", path="/y"):
        secret_local = 1  # noqa: F841
        raise ValueError("boom")
    assert rec.last_trace.error is not None and "frames" not in rec.last_trace.error
