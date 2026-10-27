from __future__ import annotations

import pytest
from marshmallow import Schema, fields

from redline_core.contrib.marshmallow import instrument_marshmallow, uninstrument_marshmallow


class AddressSchema(Schema):
    city = fields.Str()


class UserSchema(Schema):
    name = fields.Str()
    address = fields.Nested(AddressSchema)


@pytest.fixture(autouse=True)
def patched():
    assert instrument_marshmallow() is True
    assert instrument_marshmallow() is True  # idempotent
    yield
    uninstrument_marshmallow()


def spans_of(tracer, builder):
    return [s for s in tracer.finish(builder) if s.kind == "serializer"]


def test_outermost_dump_only(tracer):
    b = tracer.start()
    out = UserSchema().dump({"name": "a", "address": {"city": "c"}})
    assert out == {"name": "a", "address": {"city": "c"}}
    (span,) = spans_of(tracer, b)
    assert span.data == {
        "library": "marshmallow",
        "serializer": "UserSchema",
        "items": None,
        "many": False,
    }
    assert span.callsite is not None and span.callsite.file.endswith("test_marshmallow.py")


def test_many_counts_items(tracer):
    b = tracer.start()
    UserSchema(many=True).dump([{"name": "a"}, {"name": "b"}])
    UserSchema().dump([{"name": "c"}], many=True)
    first, second = spans_of(tracer, b)
    assert first.data["items"] == 2 and first.data["many"] is True
    assert second.data["items"] == 1 and second.data["many"] is True


def test_no_trace_is_a_passthrough():
    assert UserSchema().dump({"name": "x"}) == {"name": "x"}


def test_uninstrument_restores_dump(tracer):
    uninstrument_marshmallow()
    b = tracer.start()
    UserSchema().dump({"name": "a"})
    assert spans_of(tracer, b) == []
    assert instrument_marshmallow() is True
