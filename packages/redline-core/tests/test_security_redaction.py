"""Regression tests for the redaction gaps found in the 0.2 security review (H1, H2, M1, M2)."""

from __future__ import annotations

import json

from redline_core.conf import Settings
from redline_core.recorder import Recorder, record_span
from redline_core.redact import MASK, Redactor
from redline_core.storage.derive import derive

DRF_TOKEN = "9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"


def r(**kw) -> Redactor:
    return Redactor(Settings.from_mapping(kw))


# ── H1(a): cache keys ────────────────────────────────────────────────────────────────


def test_cache_key_session_prefixes_masked_but_prefix_kept():
    red = r()
    sid = "q1w2e3r4t5y6u7i8o9p0asdfghjklzxc"
    assert red.cache_key(f"django.contrib.sessions.cache{sid}") == (
        f"django.contrib.sessions.cache{MASK}"
    )
    assert red.cache_key(f"django.contrib.sessions.cached_db{sid}") == (
        f"django.contrib.sessions.cached_db{MASK}"
    )
    assert red.cache_key(f"myapp:session:{sid}") == f"myapp:session:{MASK}"
    assert red.cache_key(f"sess:{sid}") == f"sess:{MASK}"
    assert red.cache_key(f"user:1:api_key:{sid}") == f"user:1:api_key:{MASK}"
    # Innocent keys are untouched; ``assess:`` is not ``sess:``.
    assert red.cache_key("views.page:/products/42") == "views.page:/products/42"
    assert red.cache_key("assess:12") == "assess:12"
    assert red.cache_key("") == ""


def test_cache_key_masking_keeps_hit_rate_grouping():
    rec = Recorder(Settings(storage_path=":memory:"))
    with rec.recording(method="GET", path="/"):
        for sid in ("a" * 32, "b" * 32):
            key = rec.redactor.cache_key(f"django.contrib.sessions.cache{sid}")
            record_span("cache", f"get {key}", 0.0, 0.001, {"op": "get", "key": key, "hit": True})
    trace = rec.last_trace
    assert trace is not None
    rows = derive([trace]).cache
    assert len(rows) == 1  # both session reads land in one key pattern
    assert rows[0][3] == f"django.contrib.sessions.cache{MASK}"
    assert rows[0][4] == 2 and rows[0][5] == 2


# ── H1(b): SQL params by table qualifier, functions, INSERT/UPSERT ───────────────────


def test_sql_params_masked_by_secret_table_qualifier():
    red = r()
    drf = (
        'SELECT "authtoken_token"."key", "authtoken_token"."user_id" FROM "authtoken_token" '
        'WHERE "authtoken_token"."key" = %s LIMIT 21'
    )
    assert red.sql_params(drf, [DRF_TOKEN]) == [MASK]
    session = (
        'SELECT "django_session"."session_key", "django_session"."session_data" '
        'FROM "django_session" WHERE ("django_session"."expire_date" > %s '
        'AND "django_session"."session_key" = %s) LIMIT 21'
    )
    assert red.sql_params(session, ["2026-01-01", "abc123sessionkey"])[1] == MASK
    # An alias resolves to its table.
    aliased = "SELECT t.key FROM oauth2_provider_accesstoken AS t WHERE t.key = ?"
    assert red.sql_params(aliased, ["tok"]) == [MASK]
    # Unqualified column in a statement on a secret table: masked (conservative).
    assert red.sql_params("SELECT 1 FROM api_keys WHERE prefix = %s", ["pk_live"]) == [MASK]


def test_sql_params_innocent_tables_and_django_auth_tables_stay_visible():
    red = r()
    sql = 'SELECT "auth_user"."id" FROM "auth_user" WHERE "auth_user"."username" = %s LIMIT 21'
    assert red.sql_params(sql, ["alice"]) == ["alice"]
    join = (
        'SELECT "shop_order"."id" FROM "shop_order" INNER JOIN "authtoken_token" '
        'ON ("shop_order"."user_id" = "authtoken_token"."user_id") '
        'WHERE "shop_order"."id" = %s'
    )
    assert red.sql_params(join, [7]) == [7]
    assert red.sql_params("SELECT * FROM products WHERE id = %s", [3]) == [3]


def test_sql_params_inside_function_calls():
    red = r()
    sql = "SELECT id FROM users WHERE email = %s AND password = crypt(%s, password)"
    assert red.sql_params(sql, ["a@b.c", "hunter2"]) == ["a@b.c", MASK]
    sql = "SELECT id FROM users WHERE lower(api_token) = lower(%s) AND name = upper(%s)"
    assert red.sql_params(sql, ["tok", "bob"]) == [MASK, "bob"]


def test_sql_params_insert_upsert_and_update():
    red = r()
    upsert = (
        "INSERT INTO creds (user_id, api_key) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET api_key = %s"
    )
    assert red.sql_params(upsert, [1, "k1", "k1"]) == [1, MASK, MASK]
    mysql = (
        "INSERT INTO creds (user_id, label) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE secret_value = %s"
    )
    assert red.sql_params(mysql, [1, "x", "s"]) == [1, "x", MASK]
    multi = 'INSERT INTO "u" ("name", "password") VALUES (%s, %s), (%s, %s) RETURNING "id"'
    assert red.sql_params(multi, ["a", "p1", "b", "p2"]) == ["a", MASK, "b", MASK]
    update = 'UPDATE "u" SET "name" = %s, "password" = %s WHERE "u"."id" = %s'
    assert red.sql_params(update, ["a", "p", 3]) == ["a", MASK, 3]
    # INSERT into a secret table: every column is masked.
    ins = 'INSERT INTO "django_session" ("session_key", "session_data") VALUES (%s, %s)'
    assert red.sql_params(ins, ["k", "d"]) == [MASK, MASK]


# ── H2: exception message / traceback ───────────────────────────────────────────────


def _charge(card_number):
    api_key = "sk_live_abcdef"  # noqa: F841 - shows up in the traceback source line
    raise RuntimeError(
        "Stripe refused https://user:pw@api.stripe.com/v1/charges?api_key=sk_live_abcdef "
        f"card={card_number} token=tok_123 number {card_number}"
    )


def test_exception_message_and_traceback_are_redacted():
    rec = Recorder(Settings(storage_path=":memory:"))
    try:
        with rec.recording(method="POST", path="/pay"):
            try:
                _charge("4242424242424242")
            except RuntimeError as exc:
                raise ValueError("payment failed for token=tok_chain") from exc
    except ValueError:
        pass
    trace = rec.last_trace
    assert trace is not None and trace.error is not None
    blob = json.dumps(trace.error)
    for secret in ("sk_live_abcdef", ":pw@", "4242424242424242", "tok_123", "tok_chain"):
        assert secret not in blob, secret
    assert trace.error["type"] == "ValueError"
    assert "api.stripe.com" in trace.error["traceback"]  # still useful


def test_mapping_error_is_redacted():
    rec = Recorder(Settings(storage_path=":memory:"))
    b = rec.start_trace(method="GET", path="/")
    t = rec.finish_trace(
        b,
        status=500,
        error={"type": "X", "message": "password=hunter2", "traceback": "token: abc"},
    )
    assert t.error == {"type": "X", "message": f"password={MASK}", "traceback": f"token: {MASK}"}


def test_text_masks_cards_userinfo_query_and_jwt():
    red = r()
    out = red.text("pay 4242 4242 4242 4242 and 4000-0566-5566-5556 order 1234567890123")
    assert "4242" not in out and "5556" not in out
    assert "1234567890123" in out  # not Luhn-valid: an id, not a card
    out = red.text("GET https://bob:s3cret@h.example/x?api_key=k1&page=2 failed")
    assert "s3cret" not in out and "k1" not in out and "page=2" in out
    out = red.text("decoded eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl for user")
    assert "eyJ" not in out and "for user" in out
    out = red.text("HTTPSConnectionPool: Max retries exceeded with url: /v1/charge?api_key=sk_1")
    assert "sk_1" not in out


# ── M1: headers and bodies ───────────────────────────────────────────────────────────


def test_headers_with_secret_looking_names_are_masked():
    red = r()
    out = red.headers(
        {
            "X-Auth-Token": "s3cr3t",
            "X-CSRFToken": "csrf123",
            "X-Amz-Security-Token": "aws",
            "X-Session-Id": "sess",
            "Accept": "*/*",
            "Content-Type": "application/json",
        }
    )
    assert out["X-Auth-Token"] == out["X-CSRFToken"] == MASK
    assert out["X-Amz-Security-Token"] == out["X-Session-Id"] == MASK
    assert out["Accept"] == "*/*" and out["Content-Type"] == "application/json"


def test_multipart_body_fields_masked_and_files_dropped():
    red = r()
    body = (
        b'------b\r\nContent-Disposition: form-data; name="username"\r\n\r\nbob\r\n'
        b'------b\r\nContent-Disposition: form-data; name="password"\r\n\r\nhunter2\r\n'
        b'------b\r\nContent-Disposition: form-data; name="avatar"; filename="me.png"\r\n'
        b"Content-Type: image/png\r\n\r\n\x89PNG\x00\x01secretbytes\r\n------b--\r\n"
    )
    text, truncated = red.body(body, "multipart/form-data; boundary=----b")
    assert not truncated
    data = json.loads(text)
    assert data == {"username": "bob", "password": MASK, "avatar": "[file: me.png, 17 bytes]"}
    # No boundary / garbage: never stored raw.
    text, _ = red.body(b"password=hunter2", "multipart/form-data")
    assert "hunter2" not in text


def test_text_bodies_fall_back_to_free_text_rule():
    red = r()
    for body, ctype in (
        (b'{"password":"hunter2"}', "text/plain;charset=UTF-8"),
        (b'{"password":"hunter2",}', "application/json"),
        (b'mutation { login(password: "hunter2") }', "application/graphql"),
        (b"<login><password>hunter2</password></login>", "application/xml"),
        (b"password=hunter2", "text/plain"),
    ):
        text, _ = red.body(body, ctype)
        assert "hunter2" not in text, (ctype, text)


def test_graphql_json_variables_and_inline_literals_masked():
    red = r()
    body = json.dumps(
        {
            "query": 'mutation { login(user: "bob", password: "hunter2") { token } }',
            "variables": {"input": {"apiKey": "k", "api_key": "k2", "name": "x"}},
        }
    )
    text, _ = red.body(body, "application/json")
    assert "hunter2" not in text and "k2" not in text
    assert json.loads(text)["variables"]["input"]["name"] == "x"


# ── M2: path segments ───────────────────────────────────────────────────────────────


def test_path_params_with_secret_names_are_masked():
    red = r()
    path = "/reset/MQ/c9x1k2-5f0d7a8e3b2c1d4e5f6a7b8c9d0e1f2a/"
    params = {"uidb64": "MQ", "token": "c9x1k2-5f0d7a8e3b2c1d4e5f6a7b8c9d0e1f2a"}
    assert red.path(path, params, resolved=True) == f"/reset/{MASK}/{MASK}/"
    assert red.path("/orders/42", {"order_id": 42}, resolved=True) == "/orders/42"
    assert red.path("/k/abc", {"api_key": "abc"}, resolved=True) == f"/k/{MASK}"
    assert red.path("/s/x%2Fy", {"signature": "x/y"}, resolved=True) == f"/s/{MASK}"


def test_unresolved_path_masks_high_entropy_segments():
    red = r()
    tok = "c9x1k2-5f0d7a8e3b2c1d4e5f6a7b8c9d0e1f2a"
    assert red.path(f"/reset/MQ/{tok}/", None, resolved=False) == f"/reset/MQ/{MASK}/"
    assert red.path("/blog/a-short-post", None, resolved=False) == "/blog/a-short-post"
    assert red.path("/blog/introducing-our-new-feature", None, resolved=False) == (
        "/blog/introducing-our-new-feature"
    )


def test_recorder_masks_path_params_and_unresolved_paths():
    rec = Recorder(Settings(storage_path=":memory:"))
    b = rec.start_trace(method="GET", path="/reset/MQ/tok-abcdefghijklmnopqrstuvwxyz0123/")
    t = rec.finish_trace(
        b,
        status=200,
        route="/reset/<uidb64>/<token>/",
        path_params={"uidb64": "MQ", "token": "tok-abcdefghijklmnopqrstuvwxyz0123"},
    )
    assert t.path == f"/reset/{MASK}/{MASK}/"
    b = rec.start_trace(method="GET", path="/x/a8Fk2LmQ9zX0pR7tY4wB1nC6/")
    t = rec.finish_trace(b, status=404)
    assert t.path == f"/x/{MASK}/"
