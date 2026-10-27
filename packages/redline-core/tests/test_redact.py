from __future__ import annotations

import json

from redline_core.conf import Settings
from redline_core.redact import MASK, Redactor, json_safe


def r(**kw) -> Redactor:
    return Redactor(Settings.from_mapping(kw))


def test_headers_masked_case_insensitively_and_asgi_pairs():
    red = r()
    out = red.headers({"Authorization": "Bearer x", "Accept": "*/*", "COOKIE": "a=b"})
    assert out == {"Authorization": MASK, "Accept": "*/*", "COOKIE": MASK}
    out = red.headers([(b"x-api-key", b"k"), (b"set-cookie", b"a"), (b"set-cookie", b"b")])
    assert out == {"x-api-key": MASK, "set-cookie": f"{MASK}, {MASK}"}


def test_query_keys_masked_including_raw_string_and_repeats():
    red = r()
    assert red.query("token=abc&page=2&page=3") == {"token": MASK, "page": ["2", "3"]}
    assert red.query({"author": "x", "auth": "y"}) == {"author": "x", "auth": MASK}


def test_json_body_masked_recursively_before_truncation():
    red = r(max_body_bytes=40)
    body = json.dumps({"user": {"password": "p" * 100, "name": "bob"}, "items": [{"api_key": 1}]})
    text, truncated = red.body(body.encode(), "application/json")
    assert "ppp" not in text
    assert truncated is True and len(text.encode()) <= 40
    full, cut = r().body(body, "application/json; charset=utf-8")
    data = json.loads(full)
    assert not cut and data["user"]["password"] == MASK and data["items"][0]["api_key"] == MASK


def test_form_body_and_binary_and_empty():
    red = r()
    text, _ = red.body(b"username=a&password=b", "application/x-www-form-urlencoded")
    assert "password=%5Bredacted%5D" in text and "username=a" in text
    assert red.body(b"\xff\xfe\x00", "application/octet-stream") == ("<3 bytes binary>", False)
    assert red.body(None) == (None, False)
    assert red.body(b"") == ("", False)


def test_url_query_masked():
    assert r().url("https://x.io/a?token=1&q=2") == "https://x.io/a?token=[redacted]&q=2"
    assert r().url("https://x.io/a") == "https://x.io/a"


def test_sql_params_masked_by_column():
    red = r()
    sql = 'UPDATE auth_user SET "password" = %s, "name" = %s WHERE "id" = %s'
    assert red.sql_params(sql, ["hash", "bob", 3]) == [MASK, "bob", 3]
    ins = 'INSERT INTO users ("email", "api_key") VALUES (%s, %s), (%s, %s)'
    assert red.sql_params(ins, ["a@x", "k1", "b@x", "k2"]) == ["a@x", MASK, "b@x", MASK]
    assert red.sql_params("SELECT 1", {"token": "x", "id": 1}) == {"token": MASK, "id": 1}
    assert red.sql_params("SELECT 1", None) is None


def test_custom_patterns():
    red = r(redact_keys=[r"(?i)^email$"], redact_headers=["x-custom"])
    assert red.query({"email": "a", "password": "b"}) == {"email": MASK, "password": "b"}
    assert red.headers({"X-Custom": "1", "Authorization": "a"}) == {
        "X-Custom": MASK,
        "Authorization": "a",
    }


def test_json_safe():
    import datetime
    import decimal
    import uuid

    u = uuid.uuid4()
    assert json_safe([b"abc", decimal.Decimal("1.5"), u, datetime.date(2020, 1, 2), None]) == [
        "<3 bytes>",
        "1.5",
        str(u),
        "2020-01-02",
        None,
    ]
    assert json_safe("x" * 1000).endswith("…")


def test_default_keys_mask_secrets_but_not_auth_lookalikes():
    red = r()
    for key in (
        "auth", "Authorization", "auth_token", "x-auth", "password", "passwd", "secret",
        "token", "api_key", "apikey", "session", "sessionid", "csrf", "card", "card_number",
        "cvv", "ssn", "private_key",
    ):  # fmt: skip
        assert red.is_secret_key(key), key
    for key in ("authenticated", "author", "author_id", "authority", "oauth_provider", "name"):
        assert not red.is_secret_key(key), key


TEXT_CASES = [
    # (input, expected) – every secret value masked
    (
        "calling url=https://api/v1?api_key=SECRET123",
        "calling url=https://api/v1?api_key=[redacted]",
    ),
    ("Cookie: sessionid=abc123", "Cookie: sessionid=[redacted]"),
    ("redirect: /cb?token=XYZ", "redirect: /cb?token=[redacted]"),
    ("user=bob:password=hunter2", "user=bob:password=[redacted]"),
    ('{"password": "x", "name": "bob"}', '{"password": "[redacted]", "name": "bob"}'),
    ("{'token': 'x', 'n': 1}", "{'token': '[redacted]', 'n': 1}"),
    ("Authorization: Bearer x", "Authorization: [redacted]"),
    ("authorization=Basic dXNlcjpwYXNz ok", "authorization=[redacted] ok"),
    ("password=a&b=c", "password=[redacted]&b=c"),
    ("password=ab:cd&e=f", "password=[redacted]&e=f"),
    ("login password=hunter2 user=bob", "login password=[redacted] user=bob"),
    ("token='s3cr3t' ok", "token='[redacted]' ok"),
    ("next=/a?b=1&csrf_token=t1", "next=/a?b=1&csrf_token=[redacted]"),
    ("db=postgres://bob:pw@db:5432/app", "db=postgres://bob:%5Bredacted%5D@db:5432/app"),
    ("GET https://x.io/a?q=1&secret=s", "GET https://x.io/a?q=1&secret=[redacted]"),
    ("api_key=[redacted] again", "api_key=[redacted] again"),
    # negatives – nothing secret, text unchanged
    ("author=bob", "author=bob"),
    ("authenticated=true", "authenticated=true"),
    ("author=bob authenticated=true oauth_provider=github", None),
    ("time=12:30:00 status: ok", None),
    ("see https://x.io/a?q=1&b=2 now", None),
    ("plain message without pairs", None),
    ("", None),
]


def test_text_masks_secret_pairs_table():
    red = r()
    for text, expected in TEXT_CASES:
        assert red.text(text) == (text if expected is None else expected), text
    leaks = ("SECRET123", "abc123", "XYZ", "hunter2", "dXNlcjpwYXNz", "t1", ":pw@")
    for text, _ in TEXT_CASES:
        out = red.text(text)
        assert not [s for s in leaks if s in out], (text, out)


def test_url_masks_userinfo_password():
    red = r()
    assert red.url("https://bob:pw@h.io/x") == "https://bob:%5Bredacted%5D@h.io/x"
    assert red.url("https://bob:pw@h.io/x?token=1&q=2") == (
        "https://bob:%5Bredacted%5D@h.io/x?token=[redacted]&q=2"
    )
    assert red.url("https://bob@h.io/x") == "https://bob@h.io/x"  # no password
    assert red.url("https://h.io/x?q=a%20b") == "https://h.io/x?q=a%20b"  # untouched
    from urllib.parse import urlsplit

    assert urlsplit(red.url("https://bob:pw@h.io:8443/x")).hostname == "h.io"  # still parses
