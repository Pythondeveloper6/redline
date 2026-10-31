from __future__ import annotations

import sys
import types

import pytest

from redline_core.contrib import boto3 as integration


class FakeBaseClient:
    def __init__(self, method):
        self.meta = types.SimpleNamespace(service_model=types.SimpleNamespace(service_name="s3"))
        self.method = method

    def _make_api_call(self, operation_name, api_params):
        return self.method(operation_name, api_params)


@pytest.fixture
def fake_botocore(monkeypatch):
    integration.uninstrument_boto3()
    package = types.ModuleType("botocore")
    package.__path__ = []
    client = types.ModuleType("botocore.client")
    client.BaseClient = FakeBaseClient
    monkeypatch.setitem(sys.modules, "botocore", package)
    monkeypatch.setitem(sys.modules, "botocore.client", client)
    yield
    integration.uninstrument_boto3()


def test_success_records_only_bounded_operation_and_status(tracer, fake_botocore):
    assert integration.instrument_boto3() and integration.instrument_boto3()
    client = FakeBaseClient(
        lambda operation, params: {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "echo": params,
        }
    )
    secret = "bucket-name/private/key?X-Amz-Signature=secret"
    builder = tracer.start()
    result = client._make_api_call("GetObject", {"Bucket": secret, "Token": "secret"})
    (span,) = tracer.finish(builder)
    assert result["echo"]["Bucket"] == secret
    assert span.kind == "http_out"
    assert span.name == "AWS s3 GetObject"
    assert span.data == {
        "method": "SDK:GetObject",
        "url": "https://s3.amazonaws.com/__sdk__/GetObject",
        "status": 200,
        "service": "s3",
        "sdk": "boto3",
    }
    assert secret not in repr(span.data)


def test_error_preserved_and_does_not_record_error_message(tracer, fake_botocore):
    assert integration.instrument_boto3()
    error = RuntimeError("secret bucket and presigned URL")

    def fail(*args):
        raise error

    client = FakeBaseClient(fail)
    builder = tracer.start()
    with pytest.raises(RuntimeError) as raised:
        client._make_api_call("PutObject", {"Key": "private"})
    (span,) = tracer.finish(builder)
    assert raised.value is error
    assert span.data["status"] is None
    assert "secret" not in repr(span.data)


def test_outside_trace_and_uninstall(fake_botocore):
    original = FakeBaseClient._make_api_call
    assert integration.instrument_boto3()
    client = FakeBaseClient(lambda *_: {"ok": True})
    assert client._make_api_call("ListBuckets", {}) == {"ok": True}
    integration.uninstrument_boto3()
    assert FakeBaseClient._make_api_call is original
    assert integration.instrument_boto3()


def test_recording_failure_does_not_change_sdk_result(tracer, fake_botocore, monkeypatch):
    assert integration.instrument_boto3()
    monkeypatch.setattr(
        integration, "record_span", lambda *a, **k: (_ for _ in ()).throw(ValueError())
    )
    client = FakeBaseClient(lambda *_: {"ResponseMetadata": {"HTTPStatusCode": 204}})
    builder = tracer.start()
    assert client._make_api_call("ListBuckets", {})["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert tracer.finish(builder) == []


def test_outside_trace_failure_calls_sdk_exactly_once(fake_botocore):
    assert integration.instrument_boto3()
    calls = []
    error = RuntimeError("application failure")

    def fail(*args):
        calls.append(args)
        raise error

    client = FakeBaseClient(fail)
    with pytest.raises(RuntimeError) as raised:
        client._make_api_call("PutObject", {})
    assert raised.value is error
    assert len(calls) == 1


def test_response_metadata_failure_cannot_replace_success(tracer, fake_botocore):
    assert integration.instrument_boto3()

    class Response(dict):
        def __getitem__(self, key):
            raise RuntimeError("metadata unavailable")

    result = Response(ok=True)
    client = FakeBaseClient(lambda *_: result)
    builder = tracer.start()
    assert client._make_api_call("ListBuckets", {}) is result
    (span,) = tracer.finish(builder)
    assert span.data["status"] is None


def test_distinct_sdk_resources_do_not_become_duplicate_or_retry_findings(
    tracer, fake_botocore, store
):
    assert integration.instrument_boto3()
    client = FakeBaseClient(lambda *_: {"ResponseMetadata": {"HTTPStatusCode": 200}})
    builder = tracer.start()
    for key in ("first", "second", "third"):
        client._make_api_call("GetObject", {"Key": key})
    trace = tracer.recorder.finish_trace(builder, status=200)
    assert not {"duplicate_external", "retry_storm"} & {i.kind for i in trace.issues}
    store.save(trace)
    store.flush()
    (stat,) = store.external_stats(3600)
    assert stat.count == 3
    assert stat.duplicates == stat.retries == 0
