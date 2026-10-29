from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import override_settings

from django_redline.checks import check_redline


def _ids(**kw):
    return [m.id for m in check_redline(None)]


def test_clean_configuration_with_production_warning():
    # tests run with DEBUG=False and SAMPLE_RATE=1.0
    assert _ids() == ["django_redline.W005"]


@override_settings(REDLINE={"STORAGE_PATH": ":memory:"})
def test_production_default_sample_rate():
    from django_redline.runtime import get_runtime

    assert get_runtime().settings.sample_rate == 0.1
    assert _ids() == []


@override_settings(DEBUG=True, REDLINE={"STORAGE_PATH": ":memory:"})
def test_debug_default_sample_rate():
    from django_redline.runtime import get_runtime

    assert get_runtime().settings.sample_rate == 1.0


@override_settings(REDLINE={"SAMPL_RATE": 1})
def test_unknown_key():
    messages = check_redline(None)
    assert messages[0].id == "django_redline.E001"
    assert "sample_rate" in messages[0].msg


@override_settings(REDLINE={"STORAGE_PATH": ":memory:", "PERMISSION": "nope.nothing"})
def test_bad_permission():
    assert _ids() == ["django_redline.E001"]


@override_settings(MIDDLEWARE=[], REDLINE={"STORAGE_PATH": ":memory:", "SAMPLE_RATE": 0.5})
def test_middleware_missing():
    assert _ids() == ["django_redline.W001"]


@override_settings(
    MIDDLEWARE=[
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django_redline.middleware.RedlineMiddleware",
        "django.middleware.gzip.GZipMiddleware",
    ],
    REDLINE={"STORAGE_PATH": ":memory:", "SAMPLE_RATE": 0.5},
)
def test_middleware_misordered():
    assert sorted(_ids()) == ["django_redline.W002", "django_redline.W003"]


@override_settings(
    ROOT_URLCONF="tests.no_urls", REDLINE={"STORAGE_PATH": ":memory:", "SAMPLE_RATE": 0.5}
)
def test_urls_not_mounted():
    assert _ids() == ["django_redline.W004"]


def test_storage_not_writable(tmp_path, monkeypatch):
    import tempfile

    def denied(*args, **kwargs):
        raise PermissionError("fixture filesystem denies creation")

    # Root can write through chmod(0500); model an actual refused operation instead.
    monkeypatch.setattr(tempfile, "TemporaryFile", denied)
    with override_settings(
        REDLINE={"STORAGE_PATH": str(tmp_path / "sub" / "r.db"), "SAMPLE_RATE": 0.5}
    ):
        assert _ids() == ["django_redline.E002"]


def test_storage_checks_effective_access_under_current_user(tmp_path):
    import tempfile

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        try:
            with tempfile.TemporaryFile(dir=locked) as probe:
                probe.write(b"x")
                probe.flush()
        except OSError:
            expected = ["django_redline.E002"]
        else:
            expected = []
        with override_settings(
            REDLINE={"STORAGE_PATH": str(locked / "sub" / "r.db"), "SAMPLE_RATE": 0.5}
        ):
            assert _ids() == expected
    finally:
        locked.chmod(0o700)


def test_storage_target_is_directory(tmp_path):
    with override_settings(REDLINE={"STORAGE_PATH": str(tmp_path), "SAMPLE_RATE": 0.5}):
        assert _ids() == ["django_redline.E002"]


def test_storage_check_preserves_database_and_cleans_probe(tmp_path):
    database = tmp_path / "recordings.db"
    database.write_bytes(b"existing recording")
    before = set(tmp_path.iterdir())
    with override_settings(REDLINE={"STORAGE_PATH": str(database), "SAMPLE_RATE": 0.5}):
        assert _ids() == []
    assert database.read_bytes() == b"existing recording"
    assert set(tmp_path.iterdir()) == before


@pytest.mark.django_db
def test_clear_and_prune_commands(client, latest, rt):
    for _ in range(3):
        client.get("/plain/")
    latest()
    out = io.StringIO()
    call_command("redline_prune", "--max-rows", "0", "--max-age-days", "0", stdout=out)
    assert "Pruned 0 trace(s); 3 left." in out.getvalue()  # 0 disables, never "delete all"
    out = io.StringIO()
    call_command("redline_prune", "--max-rows", "1", stdout=out)
    assert "Pruned 2 trace(s); 1 left." in out.getvalue()
    out = io.StringIO()
    call_command("redline_clear", "--noinput", stdout=out)
    assert "Deleted 1 trace(s)." in out.getvalue()
    assert rt.store.count() == 0
