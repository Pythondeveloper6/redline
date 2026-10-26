from __future__ import annotations

import pytest
from django.conf import settings
from django.db import connections

from django_redline.explain import executor_factory
from redline_core.explain import run_explain


@pytest.mark.django_db
def test_sqlite_explain_rolls_back(shop):
    from tests.testapp.models import Order

    execute = executor_factory("default", "sqlite")
    sql = 'SELECT * FROM "testapp_order" WHERE "testapp_order"."status" = %s'
    result = run_explain(sql, ["new"], "sqlite", execute)
    assert result.vendor == "sqlite"
    assert "SCAN" in result.text.upper()
    assert Order.objects.count() == 6


@pytest.mark.django_db
def test_factory_rejects_unknown_alias_and_vendor():
    assert executor_factory("nope", "sqlite") is None
    assert executor_factory("default", "postgresql") is None
    assert executor_factory("default", "other") is None


@pytest.mark.django_db
def test_explain_is_not_recorded(client, rt, latest):
    from redline_core.recorder import Recorder

    rec = Recorder(rt.settings)
    with rec.recording(method="GET", path="/x"):
        executor_factory("default", "sqlite")("EXPLAIN QUERY PLAN SELECT 1", None)
    assert rec.last_trace.query_count == 0


@pytest.mark.skipif("pg" not in settings.DATABASES, reason="set REDLINE_TEST_PG to run")
@pytest.mark.django_db(databases=["default", "pg"])
def test_postgres_explain_json():  # pragma: no cover - needs a server
    execute = executor_factory("pg", "postgresql")
    with connections["pg"].cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE IF NOT EXISTS t (id int, v text)")
    result = run_explain("SELECT * FROM t WHERE v = %s", ["x"], "postgresql", execute)
    assert result.vendor == "postgresql"
    assert result.tree
