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
