"""Regression tests from the security review (M4, L4) through the Django adapter.

Each one failed before its fix and passes after it.
"""

from __future__ import annotations

import pytest
from django.test import Client, RequestFactory

from django_redline.middleware import _profile_requested
from django_redline.runtime import get_runtime

TOKEN = "tok-" + "x" * 30
REMOTE = {"REMOTE_ADDR": "203.0.113.9"}


def flagged(**extra):
    return RequestFactory().get("/plain/?_redline=profile", **REMOTE, **extra)


def test_m4_profile_flag_needs_debug_or_credentials(settings):
    settings.REDLINE = {"STORAGE_PATH": ":memory:"}
    settings.DEBUG = False
    assert not _profile_requested(flagged())  # anonymous: ignored
    settings.DEBUG = True
    assert _profile_requested(flagged())
    settings.DEBUG = False
    settings.REDLINE = {"STORAGE_PATH": ":memory:", "ACCESS": "token", "TOKEN": TOKEN}
    get_runtime()
    assert not _profile_requested(flagged())
    assert not _profile_requested(flagged(HTTP_AUTHORIZATION="Bearer wrong"))
    assert _profile_requested(flagged(HTTP_AUTHORIZATION=f"Bearer {TOKEN}"))
    no_flag = RequestFactory().get("/plain/", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert not _profile_requested(no_flag)


@pytest.mark.django_db
def test_l4_staff_without_superuser_is_refused_by_default(settings, django_user_model):
    settings.REDLINE = {"STORAGE_PATH": ":memory:"}
    staff = django_user_model.objects.create_user("s", password="pw", is_staff=True)
    boss = django_user_model.objects.create_user("b", password="pw", is_superuser=True)
    c = Client()
    c.force_login(staff)
    assert c.get("/redline/api/meta").status_code == 403
    c.force_login(boss)
    assert c.get("/redline/api/meta").status_code == 200
    settings.REDLINE = {
        "STORAGE_PATH": ":memory:",
        "PERMISSION": "django_redline.conf.staff_permission",
    }
    c.force_login(staff)
    assert c.get("/redline/api/meta").status_code == 200  # the old behaviour, opted in
