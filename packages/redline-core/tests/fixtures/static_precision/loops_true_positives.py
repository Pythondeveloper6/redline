"""Labelled corpus. ``# expect: <kind>`` marks a line that should be reported."""

import time

from sqlalchemy.orm import Session


def customers_for(session: Session, order_ids):
    rows = []
    for order_id in order_ids:
        rows.append(session.execute(order_id))  # expect: query_in_loop
    return rows


def django_lookup(orders):
    names = []
    for order in orders:
        names.append(Customer.objects.get(pk=order.customer_id))  # expect: query_in_loop
    return names


def fetch_one(session: Session, key):
    return session.get(Customer, key)


def helper_in_loop(session: Session, keys):
    return [fetch_one(session, key) for key in keys]  # expect: query_in_loop


def total_hours():
    return sum(e.hours for e in Event.objects.filter(active=True))  # expect: python_aggregation


def active_only():
    return [e for e in Event.objects.all() if e.active]  # expect: python_filtering


async def slow_handler():
    time.sleep(1)  # expect: blocking_async_call


def resort(batches, scores):
    for batch in batches:
        ordered = sorted(scores)  # expect: sorting_in_loop
        print(batch, ordered)
