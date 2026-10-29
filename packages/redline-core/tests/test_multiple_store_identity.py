"""Cross-store identities must not mix trace bodies, profiles or execution provenance."""

from __future__ import annotations

import json
import sqlite3

import pytest

from redline_core.api import RedlineAPI
from redline_core.model import Span, Trace
from redline_core.storage import Filters, SQLiteStore
from redline_core.storage.multiple import MultipleStores


def _trace(trace_id, source, *, started_at=100, profile=False, method="GET"):
    return Trace(
        trace_id,
        "other",
        method,
        f"/source-{source}",
        started_at=started_at,
        spans=[
            Span(
                "query",
                None,
                "sql",
                "SELECT private",
                0,
                1,
                data={"sql": f"SELECT 'PRIVATE_BODY_{source}'", "vendor": "sqlite"},
            )
        ],
        profile={"private_profile": source} if profile else None,
    )


def _open(tmp_path, traces):
    paths = [tmp_path / f"source-{index}.db" for index in range(3)]
    for index, path in enumerate(paths):
        store = SQLiteStore(str(path), background=False)
        try:
            for trace in traces.get(index, []):
                store.save(trace)
        finally:
            store.close()
    primary = SQLiteStore(str(paths[0]), background=False)
    return MultipleStores(primary, paths[1:]), paths


@pytest.mark.parametrize("sources", [(0, 1), (1, 2), (0, 1, 2)])
def test_duplicate_trace_ids_withhold_all_bodies_and_profiles(tmp_path, monkeypatch, sources):
    merged, _ = _open(
        tmp_path, {index: [_trace("collision", index, profile=index > 0)] for index in sources}
    )

    def must_not_load(*args, **kwargs):
        raise AssertionError("ambiguous body/profile was loaded")

    try:
        for store in (merged.primary, *merged.readers):
            monkeypatch.setattr(store, "get", must_not_load)
            monkeypatch.setattr(store, "get_profile", must_not_load)
            monkeypatch.setattr(store, "has_profile", must_not_load)
        assert merged.get("collision") is None
        assert merged.get("collision", include_profile=False) is None
        assert merged.get_profile("collision") is None
        assert merged.has_profile("collision") is False
        assert merged.delete("collision") == 0
        assert merged.count() == len(sources)
        items = merged.list().items
        assert len(items) == len(sources)
        for item in items:
            assert item["ambiguous_trace_id"] is True
            assert item["tags"]["ambiguous_trace_id"] == "1"
            assert item["has_profile"] is False
    finally:
        merged.close()


def test_duplicate_outside_filters_and_page_is_still_marked(tmp_path):
    merged, _ = _open(
        tmp_path,
        {
            0: [_trace("collision", 0, started_at=100, profile=True)],
            1: [_trace("collision", 1, started_at=1, method="TASK")],
        },
    )
    try:
        for filters in (None, Filters(method="GET")):
            page = merged.list(filters, limit=1)
            assert len(page.items) == 1
            assert page.items[0]["ambiguous_trace_id"] is True
            assert page.items[0]["has_profile"] is False
        tail = merged.traces_since(50)
        assert len(tail) == 1
        assert tail[0]["ambiguous_trace_id"] is True
        assert tail[0]["has_profile"] is False
    finally:
        merged.close()


@pytest.mark.parametrize("source", [0, 1, 2])
@pytest.mark.parametrize("profile", [False, True])
def test_unique_trace_profile_and_presence_use_the_same_source(tmp_path, source, profile):
    merged, _ = _open(tmp_path, {source: [_trace("unique", source, profile=profile)]})
    try:
        trace = merged.get("unique")
        expected_profile = {"private_profile": source} if profile else None
        assert trace.path == f"/source-{source}"
        assert trace.profile == expected_profile
        assert merged.get("unique", include_profile=False).profile is None
        assert merged.get_profile("unique") == expected_profile
        assert merged.has_profile("unique") is profile
        assert merged.list().items[0]["has_profile"] is profile
        assert "ambiguous_trace_id" not in merged.list().items[0]
        assert trace.tags.get("secondary_store") == (str(source) if source else None)
        api = RedlineAPI(merged)
        status, data = api.get_trace("unique")
        assert status == 200 and data["has_profile"] is profile
        assert data["diagnostics"]["profile_available"] is profile
    finally:
        merged.close()


def test_newly_written_duplicate_is_not_hidden_by_cached_source_resolution(tmp_path):
    merged, paths = _open(tmp_path, {0: [_trace("collision", 0)]})
    try:
        assert merged.get("collision").path == "/source-0"
        writer = SQLiteStore(str(paths[1]), background=False)
        try:
            writer.save(_trace("collision", 1, profile=True))
        finally:
            writer.close()
        assert merged.get("collision") is None
        assert merged.get_profile("collision") is None
        assert merged.has_profile("collision") is False
        assert merged.delete("collision") == 0
        assert merged.primary.get("collision") is not None
        assert merged.readers[0].get("collision") is not None
    finally:
        merged.close()


def test_orphan_profile_in_another_store_cannot_attach_to_a_primary_trace(tmp_path):
    merged, paths = _open(tmp_path, {0: [_trace("unique", 0)]})
    try:
        with sqlite3.connect(paths[1]) as connection:
            connection.execute(
                "INSERT INTO profiles (trace_id, data) VALUES (?, ?)",
                ("unique", json.dumps({"private_profile": "wrong-source"})),
            )
        assert merged.get("unique").profile is None
        assert merged.get_profile("unique") is None
        assert merged.has_profile("unique") is False
        assert merged.get_profile("missing") is None
        assert merged.has_profile("missing") is False
    finally:
        merged.close()


def test_ambiguous_api_inspection_cannot_return_or_execute_evidence(tmp_path):
    merged, _ = _open(
        tmp_path,
        {0: [_trace("collision", 0)], 1: [_trace("collision", 1, profile=True)]},
    )

    def executor(*args, **kwargs):
        raise AssertionError("ambiguous trace reached the database executor")

    try:
        api = RedlineAPI(merged, explain_executor=executor)
        for response in (
            api.get_trace("collision"),
            api.get_profile("collision"),
            api.snapshot("collision"),
            api.compare({"a": "collision", "b": "collision"}),
            api.dispatch("POST", "api/explain", body={"trace_id": "collision", "span_id": "query"}),
        ):
            status, body = response
            assert status == 404
            assert "PRIVATE_BODY" not in json.dumps(body)
            assert "private_profile" not in json.dumps(body)
    finally:
        merged.close()


def test_unreadable_secondary_cannot_be_treated_as_absent_for_identity(tmp_path, monkeypatch):
    merged, _ = _open(tmp_path, {0: [_trace("unique", 0)]})

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("secondary unavailable")

    try:
        monkeypatch.setattr(merged.readers[0], "_query", unavailable)
        with pytest.raises(sqlite3.OperationalError):
            merged.get("unique")
        with pytest.raises(sqlite3.OperationalError):
            merged.get_profile("unique")
        with pytest.raises(sqlite3.OperationalError):
            merged.has_profile("unique")
    finally:
        merged.close()
