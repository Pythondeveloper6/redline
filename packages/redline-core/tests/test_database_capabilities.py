"""SQL Server permission probe, permission-error messages, plan upload and explain_url."""

from __future__ import annotations

import pytest

from redline_core import database_capabilities as dc
from redline_core.api import RedlineAPI
from redline_core.conf import Settings, SettingsError
from redline_core.explain import ExplainError, run_explain
from redline_core.storage import SQLiteStore

PLAN = (
    '<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan" Version="1.5">'
    '<BatchSequence><Batch><Statements><StmtSimple StatementText="SELECT 1">'
    '<QueryPlan><RelOp NodeId="0" PhysicalOp="Table Scan" LogicalOp="Table Scan" '
    'EstimateRows="50000" EstimatedTotalSubtreeCost="12.5">'
    '<TableScan><Object Schema="[dbo]" Table="[orders]"/></TableScan></RelOp>'
    "</QueryPlan></StmtSimple></Statements></Batch></BatchSequence></ShowPlanXML>"
)


def probe_executor(*flags, user="app_user", edition=3):
    def run(sql, params):
        assert "HAS_PERMS_BY_NAME" in sql  # the probe only asks about permissions
        return [(user, *flags, edition)]

    return run


@pytest.fixture(autouse=True)
def _clear():
    dc.clear_cache()
    yield
    dc.clear_cache()


# --------------------------------------------------------------------- the probe
def test_all_granted():
    report = dc.probe_mssql(probe_executor(1, 1, 0, 1))
    assert report["principal"] == "app_user" and report["managed"] is False
    assert {k: v["status"] for k, v in report["capabilities"].items()} == dict.fromkeys(
        dc.CAPABILITIES, "granted"
    )
    assert not dc.partial_visibility(report)


def test_missing_permissions_name_the_exact_grant():
    report = dc.probe_mssql(probe_executor(0, 0, 0, 0, user="le]ss"))
    caps = report["capabilities"]
    assert {v["status"] for v in caps.values()} == {"missing"}
    assert caps["estimated_plan"]["grant"] == "GRANT SHOWPLAN TO [le]]ss];"
    assert caps["actual_plan"]["grant"].startswith("GRANT SHOWPLAN")
    assert "VIEW SERVER STATE" in caps["index_usage"]["grant"]
    assert caps["object_definitions"]["grant"] == "GRANT VIEW DEFINITION TO [le]]ss];"
    assert dc.partial_visibility(report)


def test_managed_instances_use_view_database_state():
    report = dc.probe_mssql(probe_executor(1, 0, 0, 1, edition=5))
    assert report["managed"] is True
    usage = report["capabilities"]["index_usage"]
    assert usage["status"] == "missing" and usage["grant"].startswith("GRANT VIEW DATABASE STATE")
    granted = dc.probe_mssql(probe_executor(1, 0, 1, 1, edition=5))
    assert granted["capabilities"]["index_usage"]["status"] == "granted"


def test_a_failing_probe_is_unknown_not_missing():
    def boom(sql, params):
        raise RuntimeError("connection reset")

    report = dc.probe_mssql(boom)
    assert {v["status"] for v in report["capabilities"].values()} == {"unknown"}
    assert not dc.partial_visibility(report)  # unknown never lowers confidence


def test_the_probe_is_cached():
    calls = []

    def counting(sql, params):
        calls.append(sql)
        return [("u", 1, 1, 0, 1, 3)]

    first = dc.capabilities_for("db1", counting, "mssql")
    assert dc.capabilities_for("db1", counting, "mssql") is first
    dc.capabilities_for("db1", counting, "mssql", refresh=True)
    assert len(calls) == 2
    assert dc.capabilities_for("db1", counting, "postgresql") is None


# --------------------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    ("message", "permission"),
    [
        ("(262, b'SHOWPLAN permission denied in database \\'shop\\'.')", "SHOWPLAN"),
        (
            "The user does not have permission: VIEW SERVER STATE permission was denied",
            "VIEW SERVER STATE",
        ),
        ("VIEW DATABASE STATE permission denied in database 'x'", "VIEW DATABASE STATE"),
        ("[42000] [Microsoft][ODBC Driver] ... (297) (SQLExecDirectW)", "VIEW SERVER STATE"),
    ],
)
def test_permission_errors_name_the_missing_permission(message, permission):
    hint = dc.permission_hint(RuntimeError(message), "svc")
    assert hint["permission"] == permission
    assert f"GRANT {permission} TO [svc];" == hint["grant"]
    assert "explain_url" in hint["message"] and "paste a plan" in hint["message"]


def test_other_errors_are_not_mistaken_for_permissions():
    assert dc.permission_hint(RuntimeError("Invalid object name 'orders'")) is None


def test_run_explain_turns_a_showplan_denial_into_a_clear_error():
    class Denied:
        def mssql_estimated_plan(self, sql, params):
            raise RuntimeError("SHOWPLAN permission denied in database 'shop'")

        def __call__(self, sql, params):  # pragma: no cover
            raise AssertionError

    with pytest.raises(ExplainError) as info:
        run_explain("SELECT 1", None, "mssql", Denied())
    assert info.value.code == "explain_permission" and info.value.status == 403
    assert "GRANT SHOWPLAN TO" in info.value.message


# --------------------------------------------------------------------- API
@pytest.fixture
def api(tmp_path):
    store = SQLiteStore(tmp_path / "c.db", background=False)
    yield RedlineAPI(
        store, Settings(), explain_executor=lambda alias, vendor: probe_executor(0, 1, 0, 1)
    )
    store.close()


def test_capabilities_endpoint(api):
    status, payload = api.dispatch("GET", "api/database/capabilities", {"db": "default"})
    assert status == 200 and payload["supported"] is True
    assert payload["capabilities"]["estimated_plan"]["status"] == "missing"
    assert payload["capabilities"]["index_usage"]["status"] == "granted"


def test_capabilities_without_a_connection_say_so(tmp_path):
    store = SQLiteStore(tmp_path / "c.db", background=False)
    status, payload = RedlineAPI(store, Settings()).dispatch("GET", "api/database/capabilities")
    assert status == 200 and payload["supported"] is False
    store.close()


def test_a_pasted_plan_is_analysed_without_a_database(api):
    status, payload = api.dispatch(
        "POST", "api/explain/plan", body={"vendor": "mssql", "plan": PLAN}
    )
    assert status == 200 and payload["uploaded"] is True
    assert isinstance(payload["findings"], list) and payload["analyzed"] is False
    assert "Table Scan" in payload["text"]


@pytest.mark.parametrize(
    "body",
    [
        {"plan": ""},
        {"plan": "<not-a-plan/>" + "x" * 10, "vendor": "postgresql"},
        {"plan": "<!DOCTYPE x [<!ENTITY a 'b'>]><x/>"},
        {"plan": "x" * 5_000_100},
    ],
)
def test_plan_upload_validates_and_refuses_entities(api, body):
    status, _ = api.dispatch("POST", "api/explain/plan", body=body)
    assert status in (400, 422)


def test_plan_upload_is_allowed_when_read_only(tmp_path):
    from redline_core.access import route_policy

    assert route_policy("POST", "api/explain/plan", Settings(read_only=True)) is None


# --------------------------------------------------------------------- explain_url
def test_explain_url_validation_and_masking():
    with pytest.raises(SettingsError):
        Settings(explain_url="not a url").validate() if hasattr(
            Settings, "validate"
        ) else Settings.from_mapping({"explain_url": "not a url"})
    ok = Settings.from_mapping({"explain_url": "mssql+pyodbc://svc:pw-9@host/db?driver=x"})
    assert ok.explain_url.startswith("mssql+pyodbc://")
    from redline_core.config import mask_secrets

    masked = mask_secrets({"explain_url": "mssql+pyodbc://svc:pw-9@host/db"})
    assert "pw-9" not in str(masked)


def test_explain_url_routes_plans_to_the_separate_engine(tmp_path, monkeypatch):
    from redline_core import api as api_module

    seen = {}

    class FakeEngine:
        pass

    monkeypatch.setattr(
        api_module, "_make_explain_engine", lambda url: seen.setdefault("url", url) and FakeEngine()
    )
    import redline_core.contrib.sqlalchemy as sa_contrib

    monkeypatch.setattr(
        sa_contrib, "build_executor", lambda engine, vendor, loop: ("executor", vendor)
    )
    store = SQLiteStore(tmp_path / "e.db", background=False)
    settings = Settings.from_mapping({"explain_url": "mssql+pyodbc://svc:pw@host/db?driver=x"})
    app_executor = lambda alias, vendor: ("app", vendor)  # noqa: E731
    api = RedlineAPI(store, settings, explain_executor=app_executor)
    assert api.explain_executor("default", "mssql") == ("executor", "mssql")
    assert api.explain_executor("default", "postgresql") == ("app", "postgresql")
    assert "pw" not in repr(settings.public_dict())
    store.close()


# --------------------------------------------------------------------- doctor
def test_doctor_lists_the_four_capabilities_as_granted_or_missing(monkeypatch):
    from redline_core import cli
    from redline_core import database_capabilities as caps

    class Resolved:
        settings = Settings.from_mapping({"explain_url": "mssql+pyodbc://svc:pw-9@host/db"})

    monkeypatch.setattr(
        caps,
        "probe_mssql",
        lambda executor: (
            caps.probe_mssql.__wrapped__(executor)
            if False
            else {
                "principal": "svc",
                "capabilities": {
                    "estimated_plan": {
                        "status": "missing",
                        "detail": "No SHOWPLAN.",
                        "grant": "GRANT SHOWPLAN TO [svc];",
                    },
                    "actual_plan": {
                        "status": "missing",
                        "detail": "No SHOWPLAN.",
                        "grant": "GRANT SHOWPLAN TO [svc];",
                    },
                    "index_usage": {"status": "granted", "detail": "Usage ok.", "grant": None},
                    "object_definitions": {
                        "status": "granted",
                        "detail": "Defs ok.",
                        "grant": None,
                    },
                },
            }
        ),
    )
    import sqlalchemy

    monkeypatch.setattr(
        sqlalchemy, "create_engine", lambda *a, **k: type("E", (), {"dispose": lambda self: None})()
    )
    lines = cli._database_checks(Resolved())
    text = "\n".join(f"{s} {c} {d}" for s, c, d in lines)
    assert "pw-9" not in text
    statuses = [s for s, c, _ in lines if c.startswith("db:")]
    assert statuses == ["warn", "warn", "ok", "ok"]
    assert "GRANT SHOWPLAN TO [svc];" in text


def test_doctor_without_a_database_url_adds_nothing():
    from redline_core import cli

    class Resolved:
        settings = Settings()

    assert cli._database_checks(Resolved(), environ={}) == []
