from __future__ import annotations

import httpx
import pytest
import requests
from requests.adapters import BaseAdapter

from redline_core.contrib.httpx import instrument_httpx, uninstrument_httpx
from redline_core.contrib.requests import instrument_requests, uninstrument_requests


class FakeAdapter(BaseAdapter):
    def __init__(self, status=200):
        super().__init__()
        self.status = status

    def send(self, request, **kwargs):
        resp = requests.Response()
        resp.status_code = self.status
        resp.url = request.url
        return resp

    def close(self):
        pass


@pytest.fixture
def req_instrumented():
    assert instrument_requests() and instrument_requests()
    yield
    uninstrument_requests()


def test_requests_span(tracer, req_instrumented):
    s = requests.Session()
    s.mount("http://", FakeAdapter(404))
    b = tracer.start()
    s.get("http://api.example.com/v1/items?id=3&token=secret")
    (span,) = tracer.finish(b)
    assert span.kind == "http_out"
    assert span.name == "GET api.example.com/v1/items"
    assert span.data["method"] == "GET"
    assert span.data["status"] == 404
    assert "secret" not in span.data["url"]
    assert span.callsite is not None


def test_outbound_url_userinfo_password_is_masked(tracer, req_instrumented):
    s = requests.Session()
    s.mount("http://", FakeAdapter())
    assert instrument_httpx()
    try:
        b = tracer.start()
        s.get("http://bob:hunter2@api.example.com/v1")
        transport = httpx.MockTransport(lambda r: httpx.Response(200))
        with httpx.Client(transport=transport) as c:
            c.get("http://bob:hunter2@h.test/a")
        spans = tracer.finish(b)
    finally:
        uninstrument_httpx()
    assert len(spans) == 2
    for span in spans:
        assert "hunter2" not in span.data["url"] and "hunter2" not in span.name
        assert "bob:%5Bredacted%5D@" in span.data["url"]


def test_requests_exception_has_no_status(tracer, req_instrumented):
    class Boom(FakeAdapter):
        def send(self, request, **kwargs):
            raise requests.ConnectionError("down")

    s = requests.Session()
    s.mount("http://", Boom())
    b = tracer.start()
    with pytest.raises(requests.ConnectionError):
        s.get("http://x.test/")
    (span,) = tracer.finish(b)
    assert span.data["status"] is None


def test_requests_noop_without_trace_and_uninstrument():
    original = requests.Session.send
    instrument_requests()
    s = requests.Session()
    s.mount("http://", FakeAdapter())
    assert s.get("http://x.test/").status_code == 200
    uninstrument_requests()
    assert requests.Session.send is original


async def test_httpx_spans(tracer):
    assert instrument_httpx() and instrument_httpx()
    try:
        transport = httpx.MockTransport(lambda r: httpx.Response(201))
        b = tracer.start()
        with httpx.Client(transport=transport) as c:
            c.post("http://h.test/a?x=1")
        async with httpx.AsyncClient(transport=transport) as ac:
            await ac.get("http://h.test/b")
        spans = tracer.finish(b)
    finally:
        uninstrument_httpx()
    assert [s.name for s in spans] == ["POST h.test/a", "GET h.test/b"]
    assert all(s.data["status"] == 201 for s in spans)
