"""Clean shapes (no marker, must stay silent) and known false-positive shapes.

``# fp: <kind>`` marks a line the analyzer is known to report although the code is fine.
They are counted as false positives, so the floor in the test documents the real rate.
"""

from sqlalchemy.orm import Session


def batched(orders):
    ids = [o.customer_id for o in orders]
    by_id = {c.pk: c for c in Customer.objects.filter(pk__in=ids)}
    return [by_id[o.customer_id] for o in orders]


def query_before_loop(session: Session, order_ids):
    rows = session.execute(order_ids)
    return [row for row in rows]


def plain_python_loop(values):
    out = []
    for value in values:
        out.append(value * 2)
    return out


def sorted_once(scores, batches):
    ordered = sorted(scores)
    return [(b, ordered) for b in batches]


def sort_changes_each_pass(batches):
    for batch in batches:
        batch = list(batch)
        print(sorted(batch))


def membership_on_set(items, allowed):
    allowed = set(allowed)
    return [i for i in items if i in allowed]


def sync_in_sync(session: Session):
    import time

    time.sleep(1)


def feature_flags():
    flags = {}
    for name in ("search", "billing"):
        flags[name] = Setting.objects.get(name=name)  # fp: query_in_loop
    return flags


def cached_identity_lookup(session: Session, keys):
    return [session.get(Customer, key) for key in keys]  # fp: query_in_loop
