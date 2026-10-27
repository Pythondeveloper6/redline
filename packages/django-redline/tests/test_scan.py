"""``redline_scan``: template loops filtered by the app registry, and DRF serializer checks
(DRF is not a test dependency; serializers/views are stand-ins recognised by class name)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db.models import Prefetch

from django_redline.scan import RegistryResolver, drf_findings, scan_project, template_dirs
from tests.testapp.models import Order, OrderItem, Product

TEMPLATES = Path(__file__).parent / "testapp" / "templates" / "testapp"
N1 = TEMPLATES / "n1_scan.html"


def test_resolver_knows_every_relation_kind():
    r = RegistryResolver()
    assert r.is_relation("customer").kind in ("fk", "o2o")  # Order.customer and Profile.customer
    assert r.is_relation("customer").method == "select_related"
    assert r.is_relation("product").kind == "fk"
    assert r.is_relation("profile").kind == "o2o"  # reverse one-to-one is select_related-able
    assert r.is_relation("tags").kind == "m2m"
    assert r.is_relation("items").kind == "reverse_fk"
    assert r.is_relation("items").method == "prefetch_related"
    assert r.is_relation("items").model == "testapp.OrderItem"
    assert r.is_relation("status") is None
    assert r.is_relation("created") is None


def test_template_dirs_include_app_dirs_not_site_packages():
    dirs = [d for _, d in template_dirs()]
    assert str(TEMPLATES.parent) in dirs
    assert not any("site-packages" in d for d in dirs)


def test_scan_project_explicit_path_filters_through_registry():
    fs = scan_project([str(N1)])
    chains = {f.chain: f for f in fs}
    # order.status / order.created.year are not relations; item.product.name nests under items
    assert set(chains) == {"order.customer.name", "order.items.all", "item.product.name"}
    assert chains["order.customer.name"].suggestion == 'select_related("customer")'
    assert chains["order.items.all"].suggestion == 'prefetch_related("items")'
    assert chains["item.product.name"].suggestion == 'prefetch_related("items__product")'
    assert chains["order.customer.name"].line == 4
    assert chains["order.customer.name"].for_line == 3


def test_default_scan_covers_app_templates():
    files = {Path(f.file).name for f in scan_project(drf=False)}
    assert "n1_scan.html" in files
    assert "orders.html" in files


def test_command_text_output():
    out = io.StringIO()
    call_command("redline_scan", str(N1), stdout=out)
    text = out.getvalue()
    assert "LOCATION" in text and "order.customer.name" in text
    assert 'select_related("customer")' in text
    assert "3 possible N+1" in text


def test_command_json_and_fail():
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("redline_scan", str(N1), "--json", "--fail", stdout=out)
    assert exc.value.code == 1
    data = json.loads(out.getvalue())
    assert data["warnings"] == []
    assert {f["chain"] for f in data["findings"]} == {
        "order.customer.name",
        "order.items.all",
        "item.product.name",
    }
    first = data["findings"][0]
    assert {"file", "line", "loop_var", "chain", "kind", "severity", "suggestion"} <= set(first)


def test_command_clean_path_exits_zero(tmp_path):
    f = tmp_path / "ok.html"
    f.write_text("{% for o in x %}{{ o.status }}{{ o.customer }}{% endfor %}")
    out = io.StringIO()
    call_command("redline_scan", str(f), "--fail", stdout=out)
    assert "No N+1" in out.getvalue()


def test_command_missing_path_is_a_warning(tmp_path):
    err = io.StringIO()
    call_command("redline_scan", str(tmp_path / "nope"), stdout=io.StringIO(), stderr=err)
    assert "no such file" in err.getvalue()


def test_command_jinja_dialect(tmp_path):
    f = tmp_path / "j.html"
    f.write_text("{% for o in x %}{{ o['customer'].name }}{% endfor %}")
    out = io.StringIO()
    call_command("redline_scan", str(f), "--dialect=jinja", "--json", stdout=out)
    assert [x["chain"] for x in json.loads(out.getvalue())["findings"]] == ["o.customer.name"]


# ---- DRF stand-ins -----------------------------------------------------------------------


class BaseSerializer:
    def __init__(self, many=False, source=None, **kw):
        self.many = many
        self.source = source


class Serializer(BaseSerializer):
    _declared_fields: dict = {}


class ModelSerializer(Serializer):
    pass


class RelatedField:
    def __init__(self, many=False, source=None, **kw):
        self.many = many
        self.source = source


class PrimaryKeyRelatedField(RelatedField):
    pass


class StringRelatedField(RelatedField):
    pass


class GenericAPIView:
    pass


class ProductSerializer(ModelSerializer):
    _declared_fields = {}

    class Meta:
        model = Product
        fields = ["name"]


class ItemSerializer(ModelSerializer):
    _declared_fields = {"product": ProductSerializer()}

    class Meta:
        model = OrderItem
        fields = ["qty", "product"]


class OrderSerializer(ModelSerializer):
    _declared_fields = {
        "items": ItemSerializer(many=True),
        "customer_name": StringRelatedField(source="customer.name"),
        "status": PrimaryKeyRelatedField(),
    }
    items = ItemSerializer(many=True)

    class Meta:
        model = Order
        fields = ["id", "items", "customer_name"]


def _view(queryset, serializer=OrderSerializer):
    return type("V", (GenericAPIView,), {"queryset": queryset, "serializer_class": serializer})


def _paths(fs):
    return sorted(f.suggestion.rsplit("(", 1)[1].rstrip(")").strip('"') for f in fs)


def test_drf_flags_unoptimised_queryset():
    fs = drf_findings([_view(Order.objects.all())])
    assert _paths(fs) == ["customer", "items", "items__product"]
    assert all(f.kind == "serializer" for f in fs)
    items = next(f for f in fs if f.suggestion.endswith('("items")'))
    assert items.file == __file__
    assert items.chain == "OrderSerializer.items"
    assert items.severity == "high"
    assert "V.queryset" in items.message
    assert items.suggestion.startswith("V.queryset = ")


def test_drf_field_line_points_at_declaration():
    fs = drf_findings([_view(Order.objects.all())])
    items = next(f for f in fs if f.chain == "OrderSerializer.items")
    assert (
        "items = ItemSerializer(many=True)"
        in Path(__file__).read_text().splitlines()[items.line - 1]
    )


def test_drf_satisfied_by_select_and_prefetch():
    qs = Order.objects.select_related("customer").prefetch_related("items__product")
    assert drf_findings([_view(qs)]) == []


def test_drf_prefetch_object_and_partial():
    qs = Order.objects.select_related("customer").prefetch_related(
        Prefetch("items", queryset=OrderItem.objects.all())
    )
    assert _paths(drf_findings([_view(qs)])) == ["items__product"]


def test_drf_ignores_views_without_class_queryset():
    view = type("V", (GenericAPIView,), {"serializer_class": OrderSerializer})
    assert drf_findings([view]) == []
    assert drf_findings([_view(None)]) == []


def test_drf_without_drf_installed_discovers_nothing():
    assert drf_findings() == []


def test_drf_bad_view_is_a_warning():
    class Boom(GenericAPIView):
        serializer_class = OrderSerializer

        @property
        def queryset(self):  # pragma: no cover - accessed on the class, returns property
            raise RuntimeError

    warnings: list[str] = []
    assert drf_findings([Boom], warnings=warnings) == []
