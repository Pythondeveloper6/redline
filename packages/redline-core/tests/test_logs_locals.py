# ruff: noqa: F841
from __future__ import annotations

import logging
import os
import re
import time

import pytest

from redline_core.conf import Settings
from redline_core.recorder import (
    Recorder,
    _capture_log,
    _uninstall_log_capture,
    configure,
    record_query,
    record_span,
)

HERE = os.path.dirname(__file__)
log = logging.getLogger("shop.orders")


def make(**settings) -> Recorder:
    return Recorder(Settings.from_mapping({"project_root": HERE, **settings}))


@pytest.fixture(autouse=True)
def _root_level():
    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.DEBUG)
    yield
    root.setLevel(prev)
    _uninstall_log_capture()


def run(rec: Recorder, fn, **fields):
    with rec.recording(method="GET", path="/x", **fields):
        fn()
    return rec.last_trace


# ── logs ───────────────────────────────────────────────────────────────────────


def test_captures_logs_with_level_and_callsite():
    rec = make()

    def work():
        log.info("order %s created", 42)
        log.warning("slow")
        log.debug("too quiet")

    trace = run(rec, work)
    assert [(r.level, r.message) for r in trace.logs] == [
        ("INFO", "order 42 created"),
        ("WARNING", "slow"),
    ]
    first = trace.logs[0]
    assert first.logger == "shop.orders"
    assert first.t_ms >= 0
    assert first.callsite is not None
    assert first.callsite.file == "test_logs_locals.py"
    assert first.callsite.code == 'log.info("order %s created", 42)'
    assert trace.logs_dropped == 0


def test_log_level_setting_case_insensitive():
    rec = make(log_level="error")

    def work():
        log.warning("w")
        log.error("e")
        log.critical("c")

    assert [r.level for r in run(rec, work).logs] == ["ERROR", "CRITICAL"]


def test_log_cap_counts_dropped():
    rec = make(max_logs_per_trace=3)

    def work():
        for i in range(10):
            log.info("n%d", i)

    trace = run(rec, work)
    assert [r.message for r in trace.logs] == ["n0", "n1", "n2"]
    assert trace.logs_dropped == 7


def test_message_capped_at_2000():
    trace = run(make(), lambda: log.info("x" * 5000))
    assert len(trace.logs[0].message) == 2000


def test_messages_are_redacted():
    def work():
        log.info("login password=hunter2 user=bob")
        log.info('payload {"api_key": "abc123", "name": "bob"}')
        log.info("Authorization: Bearer abcdefghij")
        log.info("token='s3cr3t' ok")
        log.info("author=jane authenticated=yes")

    msgs = [r.message for r in run(make(), work).logs]
    assert msgs[0] == "login password=[redacted] user=bob"
    assert msgs[1] == 'payload {"api_key": "[redacted]", "name": "bob"}'
    assert msgs[2] == "Authorization: [redacted]"
    assert msgs[3] == "token='[redacted]' ok"
    assert msgs[4] == "author=jane authenticated=yes"
    assert all("hunter2" not in m and "abc123" not in m and "s3cr3t" not in m for m in msgs)


def test_no_capture_outside_a_trace_or_when_disabled():
    rec = make()
    log.info("outside")  # no trace: nothing to attach to, nothing raised
    trace = run(make(capture_logs=False), lambda: log.info("inside"))
    assert trace.logs == []
    assert rec.last_trace is None


def test_redline_logger_ignored():
    def work():
        logging.getLogger("redline").warning("internal")
        logging.getLogger("redline.sub").warning("internal2")
        log.warning("mine")

    assert [r.message for r in run(make(), work).logs] == ["mine"]


def test_capture_install_is_idempotent_and_removed_on_reconfigure():
    from redline_core import recorder as recmod

    root = logging.getLogger()
    handlers = list(root.handlers)
    original = logging.getLogRecordFactory()
    make()
    installed = logging.getLogRecordFactory()
    make()
    assert logging.getLogRecordFactory() is installed is recmod._log_factory
    assert installed.previous is original  # one wrapper, not two
    assert root.handlers == handlers  # no handler is ever added
    configure(Settings(project_root=HERE, capture_logs=False))
    assert logging.getLogRecordFactory() is original
    configure(Settings(project_root=HERE))
    assert logging.getLogRecordFactory().previous is original


def test_uninstall_restores_the_previous_factory_and_is_chain_safe():
    original = logging.getLogRecordFactory()
    seen = []

    def app_factory(*args, **kwargs):
        record = original(*args, **kwargs)
        record.app_tag = "x"
        return record

    logging.setLogRecordFactory(app_factory)
    try:
        rec = make()
        assert logging.getLogRecordFactory() is not app_factory
        trace = run(rec, lambda: seen.append(log.makeRecord("shop", 30, "f", 1, "m", (), None)))
        assert seen[0].app_tag == "x"  # the previous factory still runs
        assert [r.message for r in trace.logs] == ["m"]
        _uninstall_log_capture()
        assert logging.getLogRecordFactory() is app_factory
        # wrapped by the app after Redline: Redline stays in the chain as a pass-through
        make()
        ours = logging.getLogRecordFactory()

        def outer(*args, **kwargs):
            return ours(*args, **kwargs)

        logging.setLogRecordFactory(outer)
        _uninstall_log_capture()
        assert logging.getLogRecordFactory() is outer
        trace = run(make(capture_logs=True), lambda: log.warning("after"))
        assert logging.getLogRecordFactory() is not outer  # re-installed around ``outer``
        assert [r.message for r in trace.logs] == ["after"]  # captured exactly once
    finally:
        _uninstall_log_capture()
        logging.setLogRecordFactory(original)


def test_library_log_keeps_raw_path():
    rec = make()
    record = logging.LogRecord("lib", logging.WARNING, "/site/lib.py", 7, "msg", None, None)

    def work():
        from redline_core.recorder import _trace

        _capture_log(_trace.get(), record)

    trace = run(rec, work)
    assert trace.logs[0].callsite.file == "/site/lib.py" and trace.logs[0].callsite.line == 7


def test_logs_per_request_are_isolated_between_threads():
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    rec = make()
    results = {}

    def one(n):
        with rec.recording(method="GET", path=f"/{n}") as b:
            log.info("req %d", n)
            results[n] = b

    with ThreadPoolExecutor(4) as pool:
        for n in range(8):
            pool.submit(contextvars.copy_context().run, one, n)
    for n in range(8):
        assert [r.message for r in results[n].logs] == [f"req {n}"]


def test_logs_serialised_on_trace_roundtrip():
    from redline_core.model import Trace

    trace = run(make(), lambda: log.info("hello"))
    back = Trace.from_dict(trace.to_dict())
    assert back.logs[0].message == "hello"


def test_unused_handler_costs_little():
    make()
    n = 20_000
    t0 = time.perf_counter()
    for _ in range(n):
        log.info("nothing active")
    per_call_us = (time.perf_counter() - t0) / n * 1e6
    assert per_call_us < 50  # generous: one contextvar read + logging's own overhead


# ── release, replay header ─────────────────────────────────────────────────────


def test_trace_release_resolved_once():
    rec = make(release="v9")
    assert rec.release == "v9"
    assert run(rec, lambda: None).release == "v9"


def test_replay_header_becomes_tag():
    rid = "a1" * 16
    rec = make()
    for hdrs in ({"X-Redline-Replay-Of": rid}, [(b"x-redline-replay-of", rid.encode())]):
        with rec.recording(method="GET", path="/", headers=hdrs):
            pass
        assert rec.last_trace.tags["replay_of"] == rid


@pytest.mark.parametrize("bad", ["", "xyz", "a" * 31, "a" * 33, "g" * 32, "../etc"])
def test_replay_header_invalid_ignored(bad):
    rec = make()
    with rec.recording(method="GET", path="/", headers={"X-Redline-Replay-Of": bad}):
        pass
    assert "replay_of" not in rec.last_trace.tags


def test_replay_and_run_headers_together():
    rid = "b" * 32
    rec = make()
    hdrs = {"X-Redline-Run": "run1;step=2", "X-Redline-Replay-Of": rid}
    with rec.recording(method="GET", path="/", headers=hdrs):
        pass
    assert rec.last_trace.tags["run_id"] == "run1" and rec.last_trace.tags["replay_of"] == rid


# ── server timing ──────────────────────────────────────────────────────────────

PART = re.compile(r"^[a-z]+(;[a-z]+=(\"[^\"]*\"|[0-9.]+))+$")


def test_server_timing_format():
    rec = make()
    b = rec.start_trace(method="GET", path="/x")
    t = time.perf_counter()
    for _ in range(3):
        record_query("SELECT 1", [], start=t, duration=0.004, vendor="sqlite")
    record_span("cache", "get k", t, 0.0021)
    record_span("http_out", "GET x", t, 0.03)
    record_span("template", "t.html", t, 0.0051)
    header = b.server_timing()
    parts = header.split(", ")
    assert parts[0].startswith("total;dur=")
    assert parts[1] == 'db;dur=12.0;desc="3 queries"'
    assert parts[2:5] == ["cache;dur=2.1", "http;dur=30.0", "tpl;dur=5.1"]
    assert parts[-1] == f'redline;desc="{b.id}"'
    assert all(PART.match(p) for p in parts)
    assert re.fullmatch(r"total;dur=\d+\.\d", parts[0])
    rec.finish_trace(b, status=200)


def test_server_timing_minimal_and_singular():
    rec = make()
    b = rec.start_trace(method="GET", path="/x")
    assert b.server_timing().split(", ")[1:] == [f'redline;desc="{b.id}"']
    record_query("SELECT 1", [], start=time.perf_counter(), duration=0.001)
    assert 'desc="1 query"' in b.server_timing()
    rec.finish_trace(b, status=200)
    assert b.server_timing()  # still safe after finishing


def test_server_timing_empty_for_inert_builder():
    rec = make()
    b = rec.start_trace(method="GET", path="/static/app.css")
    assert b.server_timing() == ""
    rec.finish_trace(b, status=200)


# ── exception locals ───────────────────────────────────────────────────────────


class Unrepr:
    def __repr__(self):
        raise RuntimeError("no repr")


def _inner(order_id, password):
    secret_thing = "shh"
    payload = {"user": "bob", "password": "pw", "n": 1}
    big = "z" * 1000
    bad = Unrepr()
    zero = 0
    return 1 / zero, (order_id, password, secret_thing, payload, big, bad)


def _outer():
    local_in_outer = "visible"
    _inner(7, "pw")
    return local_in_outer


def fail():
    _outer()


def test_exception_locals_captured_and_redacted():
    rec = make()
    with pytest.raises(ZeroDivisionError):
        with rec.recording(method="GET", path="/boom"):
            fail()
    err = rec.last_trace.error
    assert err["type"] == "ZeroDivisionError"
    frames = err["frames"]
    assert [f["function"] for f in frames][-3:] == ["fail", "_outer", "_inner"]
    assert all(f["file"] == "test_logs_locals.py" for f in frames)
    inner = frames[-1]
    assert inner["code"] == "return 1 / zero, (order_id, password, secret_thing, payload, big, bad)"
    loc = inner["locals"]
    assert loc["order_id"] == "7"
    assert loc["password"] == "[redacted]"
    assert loc["secret_thing"] == "[redacted]"
    assert "pw" not in loc["payload"] and "bob" in loc["payload"]
    assert len(loc["big"]) <= 301 and loc["big"].endswith("…")
    assert loc["bad"] == "<unrepresentable>"
    assert frames[-2]["locals"]["local_in_outer"] == "'visible'"


def test_locals_off():
    rec = make(capture_locals=False)
    with pytest.raises(ZeroDivisionError):
        with rec.recording(method="GET", path="/boom"):
            fail()
    assert "frames" not in rec.last_trace.error


def test_locals_limits():
    def many():
        (
            v0,
            v1,
            v2,
            v3,
            v4,
            v5,
            v6,
            v7,
            v8,
            v9,
            v10,
            v11,
            v12,
            v13,
            v14,
            v15,
            v16,
            v17,
            v18,
            v19,
            v20,
            v21,
            v22,
            v23,
            v24,
            v25,
            v26,
            v27,
            v28,
            v29,
            v30,
            v31,
            v32,
            v33,
            v34,
            v35,
            v36,
            v37,
            v38,
            v39,
        ) = (
            0,
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            16,
            17,
            18,
            19,
            20,
            21,
            22,
            23,
            24,
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            33,
            34,
            35,
            36,
            37,
            38,
            39,
        )
        raise ValueError("x")

    rec = make()
    with pytest.raises(ValueError):
        with rec.recording(method="GET", path="/"):
            many()
    assert len(rec.last_trace.error["frames"][-1]["locals"]) == 30


def test_frames_capped_at_twenty():
    def recurse(n):
        if n == 0:
            raise RuntimeError("deep")
        recurse(n - 1)

    rec = make()
    with pytest.raises(RuntimeError):
        with rec.recording(method="GET", path="/"):
            recurse(60)
    frames = rec.last_trace.error["frames"]
    assert len(frames) == 20 and frames[-1]["locals"]["n"] == "0"


def test_non_project_frames_excluded():
    rec = make(project_root=os.path.join(HERE, "fixtures"))
    with pytest.raises(ZeroDivisionError):
        with rec.recording(method="GET", path="/"):
            fail()
    assert rec.last_trace.error["frames"] == []


def test_mapping_error_has_no_frames():
    rec = make()
    b = rec.start_trace(method="GET", path="/")
    trace = rec.finish_trace(b, status=500, error={"type": "X", "message": "m", "traceback": ""})
    assert "frames" not in trace.error


class FakeRequest:  # what a Django HttpRequest repr looks like
    def __repr__(self):
        return "<WSGIRequest: GET '/cb?code=1&token=SECRET-R'>"


def _leaky():
    body = b'{"user": "bob", "password": "SECRET-B"}'
    form = "user=bob&password=SECRET-F"
    next_url = "/cb?token=SECRET-U"
    dsn = "postgres://app:SECRET-D@db/app"
    nested = [{"name": "x", "auth": ("bob", "SECRET-N")}, ({"api_key": "SECRET-K"},)]
    request = FakeRequest()
    credentials_token = {"anything": "SECRET-T"}
    plain = {"user": "bob", "n": 1}
    raise ValueError("boom")


def test_exception_locals_values_are_redacted_not_only_names():
    rec = make()
    with pytest.raises(ValueError):
        with rec.recording(method="GET", path="/boom"):
            _leaky()
    loc = rec.last_trace.error["frames"][-1]["locals"]
    assert "SECRET" not in repr(loc), loc
    assert loc["credentials_token"] == "[redacted]"  # secret name: fully masked
    assert loc["body"] == """b'{"user": "bob", "password": "[redacted]"}'"""
    assert loc["form"] == "'user=bob&password=[redacted]'"
    assert loc["next_url"] == "'/cb?token=[redacted]'"
    assert loc["dsn"] == "'postgres://app:%5Bredacted%5D@db/app'"
    assert loc["nested"] == "[{'name': 'x', 'auth': '[redacted]'}, ({'api_key': '[redacted]'},)]"
    assert loc["request"] == "<WSGIRequest: GET '/cb?code=1&token=[redacted]'>"
    assert loc["plain"] == "{'user': 'bob', 'n': 1}"


def test_log_level_follows_a_configured_app():
    """Capture follows the app's levels; Redline never touches them."""
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    rec = make(log_level="INFO")

    def work():
        log.info("quiet")
        log.warning("loud")

    assert [r.message for r in run(rec, work).logs] == ["loud"]
    assert root.level == logging.WARNING


def _script(body: str):
    import subprocess
    import sys

    return subprocess.run([sys.executable, "-c", body], capture_output=True, text=True, check=True)


UNCONFIGURED = """
import logging
from redline_core.conf import Settings
from redline_core.recorder import Recorder

root = logging.getLogger()
assert not root.handlers and root.level == logging.WARNING
rec = Recorder(Settings.from_mapping({"log_level": "INFO"}))
log = logging.getLogger("shop")
log.warning("outside-warning")
with rec.recording(method="GET", path="/x"):
    log.info("in-info")
    log.warning("in-warning")
print([r.message for r in rec.last_trace.logs])
print(root.level, len(root.handlers))
"""


def test_unconfigured_app_captures_warning_without_changing_stderr():
    out = _script(UNCONFIGURED)
    captured, state = out.stdout.strip().splitlines()
    assert captured == "['in-warning']"  # INFO is not enabled by the app: never created
    assert state == f"{logging.WARNING} 0"  # no level change, no handler
    # stderr is exactly what Python prints with no handler at all (logging.lastResort)
    assert out.stderr == "outside-warning\nin-warning\n"


BASIC_CONFIG_AFTER = """
import logging
from redline_core.conf import Settings
from redline_core.recorder import Recorder

rec = Recorder(Settings.from_mapping({"log_level": "INFO"}))
logging.basicConfig(level=logging.INFO, format="APP %(levelname)s %(message)s")
log = logging.getLogger("shop")
with rec.recording(method="GET", path="/x"):
    log.info("hello")
print([r.message for r in rec.last_trace.logs])
"""


def test_basic_config_after_redline_still_prints():
    out = _script(BASIC_CONFIG_AFTER)
    assert out.stdout.strip() == "['hello']"
    assert out.stderr == "APP INFO hello\n"


DICT_CONFIG_AFTER = """
import logging, logging.config, sys
from redline_core.conf import Settings
from redline_core.recorder import Recorder

rec = Recorder(Settings.from_mapping({"log_level": "INFO"}))
logging.config.dictConfig({
    "version": 1,
    "handlers": {"h": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout"}},
    "root": {"level": "INFO", "handlers": ["h"]},
})
log = logging.getLogger("shop")
with rec.recording(method="GET", path="/x"):
    log.info("kept")
print([r.message for r in rec.last_trace.logs])
logging.basicConfig(force=True, level=logging.INFO, stream=sys.stdout)
with rec.recording(method="GET", path="/y"):
    log.info("forced")
print([r.message for r in rec.last_trace.logs])
"""


def test_dict_config_and_force_after_redline_keep_capture():
    out = _script(DICT_CONFIG_AFTER)
    assert out.stdout.strip().splitlines() == [
        "kept",
        "['kept']",
        "INFO:shop:forced",
        "['forced']",
    ]
