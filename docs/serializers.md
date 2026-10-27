# Slow serializers

[← Index](index.md)

Turning objects into a response is real work. A Django REST Framework serializer with
nested relations, or a Pydantic response model over a few thousand items, can take longer
than the query that fetched the data, and a profiler that only shows "the view" hides it.
Redline times serialization as its own span kind and reports it when it is slow.

## The `serializer` span

| Data key | Meaning |
|---|---|
| `library` | `drf`, `pydantic`, `marshmallow` or `other` |
| `serializer` | the class or model name |
| `items` | how many objects were serialized, when known |
| `many` | whether it was a list |

Where each comes from:

| Library | How it is recorded |
|---|---|
| **DRF** (Django) | `BaseSerializer.data` for `Serializer`, `ModelSerializer` and `ListSerializer`. Only the outermost call is a span; nested serializers run inside their parent's `to_representation` and are part of its time |
| **Pydantic** (FastAPI) | `fastapi.routing.serialize_response`: response-model validation plus the dump, nested models included |
| **Marshmallow** | `Schema.dump`, if you call [`instrument_marshmallow()`](integrations.md#marshmallow); the outermost dump only |

Serializers built by hand inside a `SerializerMethodField` are covered by the parent
serializer's span.

## The `slow_serializer` issue

`slow_serializer` fires when a serializer span takes at least `slow_serializer_ms`
(default 50 ms). It is `critical` at five times that. Spans of the same serializer class
from the same callsite are one issue, with the run count and the total.

```text
Slow serializer: OrderSerializer took 340 ms for 1,200 items   (shop/api.py:44)
```

The item count tells you whether the cost is **per item** or a fixed cost: 340 ms for 1,200
items is 0.28 ms each, and the fix is to return fewer items or cheaper ones; 340 ms for
three items is something pathological inside the serializer.

## Usual causes and fixes

| Cause | Fix |
|---|---|
| A nested serializer or a `SerializerMethodField` that runs a query per item (this will also be an [N+1](n-plus-one.md), and the serializer span *contains* those SQL spans) | `select_related` / `prefetch_related` on the view's queryset, or `selectinload` |
| Returning every row | paginate; see [Large and unbounded results](result-size.md) |
| Heavy per-item Python (date formatting, computed fields) | compute in the database (`annotate`), cache, or do it once outside the loop |
| Validating a response model for data you built yourself | `response_model_exclude_unset`, a lighter model, or return the model instance directly |

The static [N+1 scan](static-scan.md) also checks DRF serializers for nested relations whose
queryset lacks the matching `select_related` or `prefetch_related`, before you ever run the
code.

## In the UI

A serializer span appears in the Timeline in its own colour, between the view and the
response. Its drawer shows the library, the class and the item count. The span's SQL
children, if any, nest under it.

## Settings

| Setting | Default |
|---|---|
| `slow_serializer_ms` | `50` |
