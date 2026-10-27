"""DRF serializers (real DRF) become ``serializer`` spans; slow ones raise an issue."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


def _spans(trace):
    return [s for s in trace.spans if s.kind == "serializer"]


def test_many_serializer_is_one_span(client, shop, latest):
    assert client.get("/drf/orders/").status_code == 200
    (span,) = _spans(latest())
    assert span.data == {
        "library": "drf",
        "serializer": "OrderSerializer",
        "items": 6,
        "many": True,
    }
    assert span.duration_ms > 0
    assert span.callsite is not None and span.callsite.file.endswith("drf_views.py")


def test_single_serializer_with_nested_is_one_span(client, shop, latest):
    assert client.get("/drf/order/").status_code == 200
    (span,) = _spans(latest())
    assert span.data["serializer"] == "OrderSerializer"
    assert span.data["many"] is False and span.data["items"] is None


def test_slow_serializer_issue(client, shop, latest):
    assert client.get("/drf/order/?slow=1").status_code == 200
    trace = latest()
    issue = next(i for i in trace.issues if i.kind == "slow_serializer")
    assert issue.severity == "warning"
    assert issue.extra["serializer"] == "OrderSerializer" and issue.extra["library"] == "drf"
    assert "OrderSerializer" in issue.title
    assert issue.suggestion is not None


def test_install_is_idempotent_and_reversible():
    from rest_framework.serializers import BaseSerializer

    from django_redline._drf import install_drf, uninstall_drf

    wrapped = BaseSerializer.__dict__["data"]
    assert install_drf() is True and BaseSerializer.__dict__["data"] is wrapped
    uninstall_drf()
    assert BaseSerializer.__dict__["data"] is not wrapped
    assert install_drf() is True
