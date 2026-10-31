from __future__ import annotations

import io
import logging
import sys
import types

from redline_core import correlation
from redline_core.conf import Settings
from redline_core.correlation import (
    TraceIdFilter,
    chain_before_send,
    install_log_filter,
    redline_url,
    sentry_before_send,
    tag_sentry_scope,
)
from redline_core.recorder import Recorder


def _logger(stream):
    handler = logging.StreamHandler(stream)
    handler.addFilter(TraceIdFilter())
    handler.setFormatter(logging.Formatter("[%(redline_trace_id)s] %(message)s"))
    log = logging.getLogger("test.correlation." + str(id(stream)))
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    return log


def test_filter_stamps_inside_a_recording_and_a_dash_outside():
    stream = io.StringIO()
    log = _logger(stream)
    recorder = Recorder(Settings())
    log.info("before")
    builder = recorder.start_trace(method="GET", path="/orders")
    log.info("inside")
    recorder.finish_trace(builder, status=200)
    log.info("after")
    lines = stream.getvalue().splitlines()
    assert lines == ["[-] before", f"[{builder.id}] inside", "[-] after"]


def test_filter_covers_job_recordings_and_never_drops_records():
    from redline_core.jobs import job

    stream = io.StringIO()
    log = _logger(stream)
    with job("send_mail", recorder=Recorder(Settings())):
        log.warning("in job")
    assert "[-]" not in stream.getvalue() and "in job" in stream.getvalue()
    assert TraceIdFilter().filter(logging.makeLogRecord({})) is True


def test_install_log_filter_is_idempotent():
    handler = logging.StreamHandler(io.StringIO())
    install_log_filter([handler])
    install_log_filter([handler])
    assert sum(isinstance(f, TraceIdFilter) for f in handler.filters) == 1


def test_redline_url(monkeypatch):
    monkeypatch.delenv("REDLINE_PUBLIC_URL", raising=False)
    assert redline_url("abc") == "/_redline/#/requests/abc"
    assert redline_url("abc", base_url="https://dev.example.com/", mount="/rl/") == (
        "https://dev.example.com/rl/#/requests/abc"
    )
    monkeypatch.setenv("REDLINE_PUBLIC_URL", "https://env.example")
    assert redline_url("abc").startswith("https://env.example/_redline/")


def test_sentry_hook_adds_tag_and_context_only_inside_a_recording():
    recorder = Recorder(Settings())
    assert sentry_before_send({"tags": {"a": "b"}}) == {"tags": {"a": "b"}}
    builder = recorder.start_trace(method="GET", path="/orders")
    try:
        event = sentry_before_send({"tags": {"a": "b"}}, None, base_url="https://x.example")
        as_list = sentry_before_send({"tags": [["a", "b"]]})
        bare = sentry_before_send({})
    finally:
        recorder.finish_trace(builder, status=200)
    assert event["tags"] == {"a": "b", "redline_trace_id": builder.id}
    assert event["contexts"]["redline"]["open_in_redline"] == (
        f"https://x.example/_redline/#/requests/{builder.id}"
    )
    assert ["redline_trace_id", builder.id] in as_list["tags"]
    assert bare["tags"]["redline_trace_id"] == builder.id


def test_chain_runs_the_previous_hook_and_respects_drops():
    recorder = Recorder(Settings())
    builder = recorder.start_trace(method="GET", path="/x")
    try:
        hook = chain_before_send(lambda e, h: {**e, "prev": True})
        assert hook({}, None)["prev"] is True
        assert chain_before_send(lambda e, h: None)({}, None) is None
        assert "tags" in chain_before_send(None)({}, None)
    finally:
        recorder.finish_trace(builder, status=200)


def test_tag_sentry_scope_with_and_without_sdk(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(
        set_tag=lambda k, v: calls.append(("tag", k, v)),
        set_context=lambda k, v: calls.append(("ctx", k, v)),
    )
    recorder = Recorder(Settings())
    assert tag_sentry_scope() is False  # no recording
    builder = recorder.start_trace(method="GET", path="/x")
    try:
        monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
        assert tag_sentry_scope() is True
        assert calls[0] == ("tag", "redline_trace_id", builder.id)
        assert calls[1][1] == "redline" and calls[1][2]["trace_id"] == builder.id
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # import raises ImportError
        assert tag_sentry_scope() is False
    finally:
        recorder.finish_trace(builder, status=200)


def test_module_does_not_import_sentry():
    assert "sentry_sdk" not in vars(correlation)
