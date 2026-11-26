"""The client-span helper and the data-store, queue and object-storage integrations.

Third-party libraries are replaced by small fakes in ``sys.modules``; nothing connects to a
service.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from redline_core import client_span, instrument_client_method
from redline_core.extending import payload_size


class AcmeClient:
    def put(self, table, blob):
        return len(blob)

    def boom(self, table):
        raise ValueError("secret message with token=abc123")

    async def aget(self, table):
        return b"x" * 7


def test_payload_size_only_measures_bytes_and_text():
    assert payload_size(b"abc") == 3
    assert payload_size(bytearray(4)) == 4
    assert payload_size(memoryview(b"abcde")) == 5
    assert payload_size("hé") == 3
    assert payload_size({"a": 1}) is None
    assert payload_size(None) is None


def test_helper_records_operation_target_payload_and_duration(tracer):
    restore = instrument_client_method(
        AcmeClient,
        "put",
        client="acme",
        operation="write",
        target=lambda self, args, kwargs: args[0],
        payload_bytes=lambda self, args, kwargs: payload_size(args[1]),
        result_bytes=lambda result: result,
    )
    try:
        builder = tracer.start()
        assert AcmeClient().put("orders", b"12345") == 5
        (span,) = tracer.finish(builder)
    finally:
        restore()
    assert span.kind == "custom"
    assert span.name == "acme write orders"
    assert span.data == {
        "client": "acme",
        "op": "write",
        "target": "orders",
        "payload_bytes": 5,
        "error": None,
        "result_bytes": 5,
    }
    assert span.duration_ms >= 0
    assert span.callsite is not None


def test_error_is_recorded_by_class_and_reraised(tracer):
    restore = instrument_client_method(AcmeClient, "boom", client="acme", target="t")
    try:
        builder = tracer.start()
        with pytest.raises(ValueError):
            AcmeClient().boom("orders")
        (span,) = tracer.finish(builder)
    finally:
        restore()
    assert span.data["error"] == "ValueError"
    assert "secret" not in repr(span.data) and "abc123" not in repr(span.name)


def test_idempotent_reversible_and_free_outside_a_trace(tracer):
    original = AcmeClient.__dict__["put"]
    restore = instrument_client_method(AcmeClient, "put", client="acme")
    again = instrument_client_method(AcmeClient, "put", client="acme")
    assert AcmeClient().put("t", b"ab") == 2  # no trace: plain call
    again()  # the duplicate undo does nothing
    assert AcmeClient.__dict__["put"] is not original
    restore()
    assert AcmeClient.__dict__["put"] is original
    restore()  # harmless twice


def test_a_failing_hook_never_breaks_the_call(tracer):
    def bad(self, args, kwargs):
        raise RuntimeError("hook bug")

    restore = instrument_client_method(
        AcmeClient, "put", client="acme", target=bad, payload_bytes=bad, operation=bad
    )
    try:
        builder = tracer.start()
        assert AcmeClient().put("orders", b"1") == 1
        (span,) = tracer.finish(builder)
    finally:
        restore()
    assert span.data["op"] == "put" and span.data["target"] is None
    assert span.data["payload_bytes"] is None


def test_target_is_redacted_and_capped(tracer):
    restore = instrument_client_method(
        AcmeClient,
        "put",
        client="acme",
        target=lambda self, args, kwargs: "https://user:hunter2@host/" + "x" * 500,
    )
    try:
        builder = tracer.start()
        AcmeClient().put("t", b"")
        (span,) = tracer.finish(builder)
    finally:
        restore()
    assert "hunter2" not in span.data["target"]
    assert len(span.data["target"]) <= 200


def test_wraps_coroutines_and_single_objects(tracer):
    client = AcmeClient()
    restore = instrument_client_method(
        client, "aget", client="acme", result_bytes=payload_size, target="things"
    )
    try:
        builder = tracer.start()
        assert asyncio.run(client.aget("x")) == b"x" * 7
        (span,) = tracer.finish(builder)
        assert "aget" in client.__dict__
    finally:
        restore()
    assert "aget" not in client.__dict__
    assert span.data["result_bytes"] == 7 and span.data["target"] == "things"
    assert asyncio.run(AcmeClient().aget("x")) == b"x" * 7  # the class was never touched


def test_missing_method_raises_attribute_error():
    with pytest.raises(AttributeError):
        instrument_client_method(AcmeClient, "nope", client="acme")


def test_client_span_context_manager(tracer):
    builder = tracer.start()
    with client_span("acme", "scan", target="orders") as info:
        info["payload_bytes"] = 12
    with pytest.raises(KeyError), client_span("acme", "scan"):
        raise KeyError("x")
    ok, failed = tracer.finish(builder)
    assert ok.name == "acme scan orders" and ok.data["payload_bytes"] == 12
    assert failed.data["error"] == "KeyError"
    with client_span("acme", "scan") as info:  # outside a trace: nothing happens
        assert info["target"] is None


# first-party integrations, against fake libraries ----------------------------------------
def _module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


@pytest.fixture
def fake_pymongo(monkeypatch):
    class Cursor:
        def __init__(self, collection):
            self.collection = collection

        def _refresh(self):
            return 3

    class Collection:
        full_name = "app.orders"

        def insert_one(self, doc):
            return "id"

        def find_one(self, flt=None):
            return {"secret": "value"}

        def find(self, flt=None):
            return Cursor(self)

    _module(monkeypatch, "pymongo")
    _module(monkeypatch, "pymongo.collection", Collection=Collection)
    _module(monkeypatch, "pymongo.cursor", Cursor=Cursor)
    from redline_core.contrib import pymongo as integration

    integration.uninstrument_pymongo()
    yield Collection, Cursor
    integration.uninstrument_pymongo()


def test_pymongo_collection_and_cursor_spans(tracer, fake_pymongo):
    from redline_core.contrib import pymongo as integration

    Collection, _ = fake_pymongo
    assert integration.instrument_pymongo() and integration.instrument_pymongo()
    builder = tracer.start()
    coll = Collection()
    coll.insert_one({"password": "hunter2"})
    coll.find_one({"email": "a@b.example"})
    coll.find({"x": 1})._refresh()
    spans = tracer.finish(builder)
    assert [(s.data["client"], s.data["op"], s.data["target"]) for s in spans] == [
        ("mongodb", "insert_one", "app.orders"),
        ("mongodb", "find_one", "app.orders"),
        ("mongodb", "find", "app.orders"),
    ]
    assert "hunter2" not in repr([s.data for s in spans])
    integration.uninstrument_pymongo()
    assert not integration._originals
    assert not hasattr(Collection.find_one, "__redline_client_span__")
    builder = tracer.start()
    Collection().insert_one({})
    assert tracer.finish(builder) == []


@pytest.fixture
def fake_kafka(monkeypatch):
    class KafkaProducer:
        def send(self, topic, value=None, key=None, headers=None, partition=None):
            return "future"

    _module(monkeypatch, "kafka", KafkaProducer=KafkaProducer)
    from redline_core.contrib import kafka as integration

    integration.uninstrument_kafka()
    yield KafkaProducer
    integration.uninstrument_kafka()


def test_kafka_publish_span_has_topic_and_size_only(tracer, fake_kafka):
    from redline_core.contrib import kafka as integration

    assert integration.instrument_kafka()
    builder = tracer.start()
    assert fake_kafka().send("orders.created", value=b"x" * 40, key=b"secret-key") == "future"
    fake_kafka().send("audit", b"abc")
    first, second = tracer.finish(builder)
    assert first.data["op"] == "publish" and first.data["client"] == "kafka"
    assert (first.data["target"], first.data["payload_bytes"]) == ("orders.created", 40)
    assert (second.data["target"], second.data["payload_bytes"]) == ("audit", 3)
    assert "secret-key" not in repr(first.data)


@pytest.fixture
def fake_pika(monkeypatch):
    class BlockingChannel:
        def basic_publish(self, exchange, routing_key, body, properties=None, mandatory=False):
            return None

    _module(monkeypatch, "pika")
    _module(monkeypatch, "pika.adapters")
    _module(monkeypatch, "pika.adapters.blocking_connection", BlockingChannel=BlockingChannel)
    from redline_core.contrib import pika as integration

    integration.uninstrument_pika()
    yield BlockingChannel
    integration.uninstrument_pika()


def test_pika_publish_span(tracer, fake_pika):
    from redline_core.contrib import pika as integration

    assert integration.instrument_pika()
    builder = tracer.start()
    fake_pika().basic_publish("", "emails", b"hello")
    fake_pika().basic_publish(exchange="events", routing_key="user.new", body=b"12")
    one, two = tracer.finish(builder)
    assert one.data["target"] == "(default)/emails" and one.data["payload_bytes"] == 5
    assert two.data["target"] == "events/user.new" and two.data["client"] == "amqp"


@pytest.fixture
def fake_gcs(monkeypatch):
    class Blob:
        def __init__(self, bucket, name):
            self.bucket = types.SimpleNamespace(name=bucket)
            self.name = name

        def upload_from_string(self, data, content_type="text/plain"):
            return None

        def download_as_bytes(self):
            return b"z" * 9

    _module(monkeypatch, "google")
    _module(monkeypatch, "google.cloud")
    _module(monkeypatch, "google.cloud.storage", Blob=Blob)
    from redline_core.contrib import google_cloud_storage as integration

    integration.uninstrument_google_cloud_storage()
    yield Blob
    integration.uninstrument_google_cloud_storage()


def test_gcs_records_bucket_not_object_name(tracer, fake_gcs):
    from redline_core.contrib import google_cloud_storage as integration

    assert integration.instrument_google_cloud_storage()
    builder = tracer.start()
    blob = fake_gcs("media-bucket", "users/42/passport-scan.png")
    blob.upload_from_string(b"12345678")
    assert blob.download_as_bytes() == b"z" * 9
    up, down = tracer.finish(builder)
    assert (up.data["op"], up.data["target"], up.data["payload_bytes"]) == (
        "upload",
        "media-bucket",
        8,
    )
    assert (down.data["op"], down.data["result_bytes"]) == ("download", 9)
    assert "passport" not in repr([up.data, down.data, up.name, down.name])


@pytest.fixture
def fake_azure(monkeypatch):
    class BlobClient:
        container_name = "scans"

        def upload_blob(self, data, overwrite=False):
            return None

        def download_blob(self):
            return object()

        def delete_blob(self):
            return None

    _module(monkeypatch, "azure")
    _module(monkeypatch, "azure.storage")
    _module(monkeypatch, "azure.storage.blob", BlobClient=BlobClient)
    monkeypatch.delitem(sys.modules, "azure.storage.blob.aio", raising=False)
    from redline_core.contrib import azure_blob as integration

    integration.uninstrument_azure_blob()
    yield BlobClient
    integration.uninstrument_azure_blob()


def test_azure_blob_upload_download_delete(tracer, fake_azure):
    from redline_core.contrib import azure_blob as integration

    assert integration.instrument_azure_blob()
    builder = tracer.start()
    client = fake_azure()
    client.upload_blob(b"abcd")
    client.download_blob()
    client.delete_blob()
    spans = tracer.finish(builder)
    assert [(s.data["op"], s.data["target"]) for s in spans] == [
        ("upload", "scans"),
        ("download", "scans"),
        ("delete", "scans"),
    ]
    assert spans[0].data["payload_bytes"] == 4


@pytest.mark.parametrize(
    "module,function",
    [
        ("pymongo", "instrument_pymongo"),
        ("kafka", "instrument_kafka"),
        ("pika", "instrument_pika"),
        ("google_cloud_storage", "instrument_google_cloud_storage"),
        ("azure_blob", "instrument_azure_blob"),
    ],
)
def test_missing_library_returns_false_and_never_raises(monkeypatch, module, function):
    from importlib import import_module

    for name in ("pymongo", "kafka", "pika", "google.cloud.storage", "azure.storage.blob"):
        monkeypatch.setitem(sys.modules, name, None)  # makes ``import name`` raise ImportError
    mod = import_module(f"redline_core.contrib.{module}")
    assert getattr(mod, function)() is False
    assert not mod._originals
