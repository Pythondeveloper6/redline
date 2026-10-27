"""Django end-to-end checks for the redaction fixes of the 0.2 security review."""

from __future__ import annotations

import json

import pytest
from django.test import override_settings

from redline_core.redact import MASK

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.security_urls")]


def _blob(trace) -> str:
    return json.dumps(trace.to_dict())


def test_drf_token_lookup_param_is_masked(client, latest, django_user_model):
    from rest_framework.authtoken.models import Token

    user = django_user_model.objects.create_user("alice", password="pw")
    key = Token.objects.create(user=user).key
    response = client.get("/me", HTTP_AUTHORIZATION=f"Token {key}")
    assert response.json() == {"user": "alice"}
    trace = latest()
    assert key not in _blob(trace)
    lookups = [s for s in trace.spans if s.kind == "sql" and "authtoken_token" in s.data["sql"]]
    assert lookups and all(s.data["params"] == [MASK] for s in lookups)


@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.cache")
def test_session_id_not_stored_in_cache_keys(client, latest):
    client.get("/s")
    sid = client.cookies["sessionid"].value
    trace = latest()
    assert sid not in _blob(trace)
    keys = [s.data["key"] for s in trace.spans if s.kind == "cache"]
    assert f"django.contrib.sessions.cache{MASK}" in keys


@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.db")
def test_session_id_not_stored_in_session_table_params(client, latest):
    client.get("/s")
    sid = client.cookies["sessionid"].value
    assert sid not in _blob(latest())


def test_password_reset_path_is_masked(client, latest):
    token = "c9x1k2-5f0d7a8e3b2c1d4e5f6a7b8c9d0e1f2a"
    client.get(f"/reset/MQ/{token}/?token=abc")
    trace = latest()
    assert trace.path == f"/reset/{MASK}/{MASK}/"
    assert trace.route == "/reset/<uidb64>/<token>/"
    assert token not in _blob(trace) and "abc" not in json.dumps(trace.request)


def test_unmatched_path_with_token_segment_is_masked(client, latest):
    client.get("/nope/a8Fk2LmQ9zX0pR7tY4wB1nC6/")
    trace = latest()
    assert trace.status == 404 and trace.path == f"/nope/{MASK}/"


def test_custom_auth_headers_are_masked(client, latest):
    client.get("/reset/MQ/x/", HTTP_X_AUTH_TOKEN="s3cr3t", HTTP_X_CSRFTOKEN="csrfvalue")
    headers = latest().request["headers"]
    assert headers["X-Auth-Token"] == MASK and headers["X-Csrftoken"] == MASK


def test_exception_message_and_traceback_are_masked(client, latest):
    client.raise_request_exception = False
    assert client.get("/boom").status_code == 500
    trace = latest()
    assert trace.error is not None
    blob = json.dumps(trace.error)
    for secret in ("pw-in-url", "sk_live_django", "4242424242424242"):
        assert secret not in blob, secret
    assert "upstream failed" in trace.error["message"]
