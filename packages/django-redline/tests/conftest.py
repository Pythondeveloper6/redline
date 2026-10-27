from __future__ import annotations

import pytest
from django.contrib.contenttypes.models import ContentType

from django_redline.runtime import get_runtime, reset_runtime
from redline_core.model import Trace


@pytest.fixture(autouse=True)
def fresh_runtime():
    reset_runtime()
    yield
    reset_runtime()


@pytest.fixture
def rt():
    return get_runtime()


def _latest(rt) -> Trace | None:
    rt.store.flush(5.0)
    page = rt.store.list(None, limit=1)
    if not page.items:
        return None
    return rt.store.get(page.items[0]["id"])


@pytest.fixture
def latest(rt):
    return lambda: _latest(get_runtime())


@pytest.fixture
def shop(db):
    from tests.testapp.models import (
        Category,
        Comment,
        Customer,
        Order,
        OrderItem,
        Product,
        Profile,
        Tag,
    )

    cat = Category.objects.create(name="c")
    tags = [Tag.objects.create(name=f"t{i}") for i in range(6)]
    products = []
    for i in range(6):
        p = Product.objects.create(name=f"p{i}", price=i + 1, category=cat)
        p.tags.set(tags[: i + 1])
        products.append(p)
    ct = ContentType.objects.get_for_model(Product)
    for p in products:
        Comment.objects.create(content_type=ct, object_id=p.pk, text="nice")
    for i in range(6):
        c = Customer.objects.create(name=f"cust{i}")
        Profile.objects.create(customer=c, bio=f"bio{i}")
        o = Order.objects.create(customer=c)
        for j in range(2):
            OrderItem.objects.create(order=o, product=products[(i + j) % 6], qty=j + 1)
    return {"products": products, "tags": tags}


@pytest.fixture
def staff(db, django_user_model):
    """A user the default ``PERMISSION`` lets in (a superuser since 0.2)."""
    return django_user_model.objects.create_user(
        "staff", password="pw", is_staff=True, is_superuser=True
    )


@pytest.fixture
def staff_only(db, django_user_model):
    """Staff without superuser status: refused by the default ``PERMISSION`` since 0.2."""
    return django_user_model.objects.create_user("staffonly", password="pw", is_staff=True)


@pytest.fixture
def staff_client(client, staff):
    client.force_login(staff)
    return client
