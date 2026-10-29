"""SQL column and table privacy policies apply across supported bind styles."""

from __future__ import annotations

import pytest

from redline_core.conf import Settings
from redline_core.redact import MASK, Redactor


@pytest.mark.parametrize(
    "table",
    ["medical.records", '"medical"."records"', "[medical].[records]", "`medical` . `records`"],
)
@pytest.mark.parametrize("operation", ["update", "insert", "select"])
def test_schema_table_policy_survives_identifier_quoting(table, operation):
    redactor = Redactor(Settings(redact_tables=(r"(?i)^medical\.",)))
    sql = {
        "update": f"UPDATE {table} SET value=:payload WHERE id=:id",
        "insert": f"INSERT INTO {table} (value, id) VALUES (:payload, :id)",
        "select": f"SELECT r.value FROM {table} AS r WHERE r.value=:payload AND r.id=:id",
    }[operation]
    assert redactor.sql_params(sql, {"payload": "private", "id": 7}) == {
        "payload": MASK,
        "id": MASK,
    }
    positional = sql.replace(":payload", "?").replace(":id", "?")
    assert redactor.sql_params(positional, ["private", 7]) == [MASK, MASK]


@pytest.mark.parametrize("placeholder", [":payload", "%(payload)s"])
def test_named_binding_uses_inferred_column_and_key_rules(placeholder):
    redactor = Redactor(Settings(redact_profiles=("health",)))
    sql = f"UPDATE records SET diagnosis={placeholder}, title=:title WHERE id=:id"
    assert redactor.sql_params(sql, {"payload": "private", "title": "public", "id": 7}) == {
        "payload": MASK,
        "title": "public",
        "id": 7,
    }
    assert redactor.sql_params("SELECT :token, :id", {"token": "private", "id": 7}) == {
        "token": MASK,
        "id": 7,
    }


def test_repeated_bind_is_masked_if_any_use_is_sensitive():
    redactor = Redactor()
    assert redactor.sql_params(
        "SELECT * FROM users WHERE name=:value OR password=:value", {"value": "private"}
    ) == {"value": MASK}


@pytest.mark.parametrize("qualifier", ["r", "records"])
def test_schema_policy_resolves_aliases_and_basename_qualifiers(qualifier):
    redactor = Redactor(Settings(redact_tables=(r"^medical\.",)))
    sql = f"SELECT * FROM medical.records r WHERE {qualifier}.value=?"
    assert redactor.sql_params(sql, ["private"]) == [MASK]


def test_legacy_basename_policy_and_auth_user_exception_survive_schema_prefix():
    redactor = Redactor(Settings(redact_tables=(r"^records$",)))
    assert redactor.sql_params("UPDATE medical.records SET value=?", ["private"]) == [MASK]
    assert Redactor().sql_params("UPDATE public.auth_user SET username=?", ["alice"]) == ["alice"]


def test_named_parameter_rows_are_masked_independently_by_the_caller():
    redactor = Redactor(Settings(redact_profiles=("health",)))
    sql = "INSERT INTO records (diagnosis, title) VALUES (:payload, :title)"
    rows = [{"payload": "private-a", "title": "a"}, {"payload": "private-b", "title": "b"}]
    assert [redactor.sql_params(sql, row) for row in rows] == [
        {"payload": MASK, "title": "a"},
        {"payload": MASK, "title": "b"},
    ]


def test_unaliased_join_does_not_consume_the_next_table_keyword():
    redactor = Redactor(Settings(redact_tables=(r"^medical\.",)))
    sql = "SELECT * FROM users JOIN medical.records ON users.id=records.id WHERE records.value=?"
    assert redactor.sql_params(sql, ["private"]) == [MASK]


def test_catalog_schema_table_is_preserved():
    redactor = Redactor(Settings(redact_tables=(r"^catalog\.medical\.",)))
    assert redactor.sql_params("UPDATE [catalog].[medical].[records] SET value=?", ["private"]) == [
        MASK
    ]


def test_bounded_sql_params_preserves_database_scalar_spellings():
    import datetime
    from decimal import Decimal
    from uuid import UUID

    values = [
        datetime.date(2026, 10, 28),
        datetime.datetime(2026, 10, 28, 12, 30, tzinfo=datetime.timezone.utc),
        datetime.time(12, 30),
        Decimal("12.345"),
        UUID("12345678-1234-5678-1234-567812345678"),
    ]
    safe, cut = Redactor().bounded_sql_params("SELECT ?, ?, ?, ?, ?", values, max_bytes=32768)
    assert safe == [str(value) for value in values]
    assert cut is False


@pytest.mark.parametrize("max_bytes", [4, 32, 512, 32768])
def test_bounded_sql_params_limits_utf8_and_marks_all_shortening(max_bytes):
    import json

    safe, cut = Redactor().bounded_sql_params("SELECT ?", ["🙂" * 100_000], max_bytes=max_bytes)
    assert len(json.dumps(safe, ensure_ascii=True, separators=(",", ":"))) <= max_bytes
    assert cut is True


def test_bounded_sql_params_handles_cycles_depth_and_large_input():
    redactor = Redactor()
    cyclic = []
    cyclic.append(cyclic)
    assert redactor.bounded_sql_params("SELECT ?", cyclic, max_bytes=32768) == ([None], True)
    safe, cut = redactor.bounded_sql_params("SELECT ?", [1] * 1_000_000, max_bytes=32768)
    assert len(safe) == 200 and cut is True
    nested = 1
    for _ in range(100):
        nested = [nested]
    safe, cut = redactor.bounded_sql_params("SELECT ?", nested, max_bytes=32768)
    assert cut is True
    assert len(str(safe)) < 30


def test_bounded_sql_params_does_not_call_application_object_methods():
    import datetime

    class Hostile:
        def __str__(self):
            raise AssertionError("application conversion called")

        __repr__ = __str__

    class HostileDict(dict):
        def items(self):
            raise AssertionError("application mapping traversed")

    class HostileList(list):
        def __iter__(self):
            raise AssertionError("application sequence traversed")

    class HostileTimezone(datetime.tzinfo):
        def utcoffset(self, value):
            raise AssertionError("application timezone called")

    values = [
        Hostile(),
        HostileDict(value=Hostile()),
        HostileList([Hostile()]),
        datetime.datetime(2026, 10, 28, tzinfo=HostileTimezone()),
    ]
    safe, cut = Redactor().bounded_sql_params("SELECT ?, ?, ?, ?", values, max_bytes=32768)
    assert safe == [None] * 4 and cut is True


def test_bounded_sql_params_keeps_inferred_masks_and_explicit_truncation():
    redactor = Redactor(Settings(redact_profiles=("health",)))
    safe, cut = redactor.bounded_sql_params(
        "UPDATE records SET diagnosis=:payload, title=:title",
        {"payload": "private", "title": "visible"},
        max_bytes=32768,
    )
    assert safe == {"payload": MASK, "title": "visible"} and cut is False
    safe, cut = redactor.bounded_sql_params(
        "UPDATE records SET diagnosis=:payload", {"payload": "private" * 1000}, max_bytes=32768
    )
    assert safe == {"payload": MASK} and cut is True
    safe, cut = Redactor(Settings(redact_tables=(r"^medical\.",))).bounded_sql_params(
        "UPDATE medical.records SET value=?", ["private"], max_bytes=32768
    )
    assert safe == [MASK] and cut is False


def test_bounded_sql_params_limits_mapping_and_large_decimal_before_conversion():
    from decimal import Decimal

    safe, cut = Redactor().bounded_sql_params(
        "SELECT 1", {str(i): i for i in range(10_000)}, max_bytes=32768
    )
    assert len(safe) == 200 and cut is True
    safe, cut = Redactor().bounded_sql_params("SELECT ?", [Decimal("1" * 100_000)], max_bytes=1024)
    assert safe == [None] and cut is True
