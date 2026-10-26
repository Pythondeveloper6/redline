"""Http404 sampling, the shared ?_redline= parsing, REDLINE["STORE"] and ALLOWED_HOSTS."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_redline.runtime import get_runtime
from redline_core.storage import SQLiteStore

pytestmark = pytest.mark.django_db

_DROP_ALL = {"STORAGE_PATH": ":memory:", "SAMPLE_RATE": 0.0, "ALWAYS_KEEP_SLOWER_THAN_MS": 1e9}


@override_settings(REDLINE=_DROP_ALL)
def test_http404_is_not_an_error(client, latest):
    assert client.get("/missing/").status_code == 404
    assert latest() is None  # not kept by always_keep_errors


@override_settings(REDLINE={"STORAGE_PATH": ":memory:", "SAMPLE_RATE": 1.0})
def test_http404_trace_has_no_error(client, latest):
    assert client.get("/missing/").status_code == 404
    trace = latest()
    assert trace.status == 404 and trace.error is None and trace.tags["sampled_by"] == "all"


@override_settings(REDLINE=_DROP_ALL)
def test_unhandled_exception_still_kept(client, latest):
    client.raise_request_exception = False
    client.get("/boom/")
    trace = latest()
    assert trace.status == 500 and trace.error and trace.tags["sampled_by"] == "error"


@pytest.fixture
def fine_profiler(monkeypatch):
    from redline_core.profiler import RequestProfiler

    original = RequestProfiler.__init__
    monkeypatch.setattr(
        RequestProfiler,
        "__init__",
        lambda self, interval=0.0001, async_mode="enabled": original(self, interval, async_mode),
    )


@pytest.mark.parametrize("value", ["profile", "1", "true", "yes", "on"])
def test_profile_query_values_match_fastapi(client, latest, rt, fine_profiler, value):
    client.get(f"/plain/?_redline={value}")
    assert rt.store.has_profile(latest().id)


def test_profile_query_off_value(client, latest, rt):
    client.get("/plain/?_redline=0")
    assert not rt.store.has_profile(latest().id)


# ── REDLINE["STORE"] ──────────────────────────────────────────────────────────


class RecordingStore(SQLiteStore):
    instances: list = []

    def __init__(self, settings):
        super().__init__(":memory:", background=False)
        self.settings = settings
        RecordingStore.instances.append(self)


SHARED = SQLiteStore(":memory:", background=False)


def make_store(settings):
    return RecordingStore(settings)


@pytest.mark.parametrize(
    "spec",
    [
        RecordingStore,
        make_store,
        "tests.test_hardening.RecordingStore",
        "tests.test_hardening.make_store",
    ],
)
def test_custom_store_factory(client, spec):
    RecordingStore.instances.clear()
    with override_settings(REDLINE={"STORE": spec, "SAMPLE_RATE": 1.0}):
        rt = get_runtime()
        assert isinstance(rt.store, RecordingStore) and rt.owns_store
        assert rt.store.settings is rt.settings and rt.recorder.store is rt.store
        assert rt.api.store is rt.store
        client.get("/plain/")
        assert rt.store.count() == 1


@pytest.mark.parametrize("spec", [SHARED, "tests.test_hardening.SHARED"])
def test_custom_store_instance_survives_rebuilds(client, spec):
    SHARED.clear()
    with override_settings(REDLINE={"STORE": spec, "SAMPLE_RATE": 1.0}):
        rt = get_runtime()
        assert rt.store is SHARED and not rt.owns_store
        client.get("/plain/")
    # override_settings exit rebuilt the runtime; the shared instance is still open
    assert SHARED.count() == 1


@pytest.mark.parametrize("spec", ["tests.nope.Store", 42])
def test_bad_store_setting(spec):
    with override_settings(REDLINE={"STORE": spec}), pytest.raises(ImproperlyConfigured):
        get_runtime()


def test_store_factory_must_return_a_store():
    with (
        override_settings(REDLINE={"STORE": lambda settings: object()}),
        pytest.raises(ImproperlyConfigured, match="Store protocol"),
    ):
        get_runtime()


def test_store_appears_in_system_check():
    from django_redline.checks import check_redline

    with override_settings(REDLINE={"STORE": "tests.nope.Store"}):
        assert [e.id for e in check_redline()] == ["django_redline.E001"]


# ── PathLike settings ────────────────────────────────────────────────────────


def test_pathlib_storage_path(tmp_path):
    with override_settings(REDLINE={"STORAGE_PATH": tmp_path / "r.db", "PROJECT_ROOT": tmp_path}):
        rt = get_runtime()
        assert rt.settings.storage_path == str(tmp_path / "r.db")
        assert rt.store.path == str(tmp_path / "r.db")


# ── ALLOWED_HOSTS on Redline's views (DNS rebinding) ─────────────────────────


@override_settings(ALLOWED_HOSTS=["localhost"])
def test_redline_views_enforce_allowed_hosts(staff_client):
    assert staff_client.get("/redline/api/meta", HTTP_HOST="localhost").status_code == 200
    assert staff_client.get("/redline/api/meta", HTTP_HOST="evil.example").status_code == 400
    assert staff_client.get("/redline/", HTTP_HOST="evil.example").status_code == 400


def test_index_denies_framing(staff_client):
    assert staff_client.get("/redline/")["X-Frame-Options"] == "DENY"
