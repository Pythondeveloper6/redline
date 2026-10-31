"""SQL Server with engines built per request (token-style), found by auto-discovery."""

from __future__ import annotations

import os

import pytest
from live_database_helpers import database_url

pytestmark = pytest.mark.skipif(
    os.environ.get("REDLINE_TEST_COMPOSE") != "1",
    reason="Requires explicit disposable Compose integration environment",
)

#: ODBC pre-connect attribute SQL_ATTR_LOGIN_TIMEOUT: stands in for the access-token
#: attribute (1256) an app would pass per user, without needing an identity provider.
SQL_ATTR_LOGIN_TIMEOUT = 103


def _odbc_connection_string(url):
    query = dict(url.query)
    driver = query.get("driver", "ODBC Driver 18 for SQL Server")
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={url.host},{url.port or 1433}",
        f"DATABASE={url.database}",
        f"UID={url.username}",
        f"PWD={url.password}",
        f"TrustServerCertificate={query.get('TrustServerCertificate', 'yes')}",
    ]
    return ";".join(parts)


@pytest.mark.parametrize("style", ("connect_args", "creator"))
def test_per_request_token_style_engines_are_all_captured(style):
    import pyodbc
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL

    from redline_core import Recorder, Settings

    url = database_url("mssql")
    recorder = Recorder(Settings(access="local", instrument_engines="all"))
    engines = []

    def build(user_index):
        if style == "connect_args":
            # attrs_before is how the Microsoft guidance passes an access token to pyodbc.
            return create_engine(
                url,
                connect_args={"attrs_before": {SQL_ATTR_LOGIN_TIMEOUT: 10 + user_index}},
                hide_parameters=True,
            )
        conn_str = _odbc_connection_string(url)
        return create_engine(
            URL.create("mssql+pyodbc"),
            creator=lambda: pyodbc.connect(
                conn_str, attrs_before={SQL_ATTR_LOGIN_TIMEOUT: 10 + user_index}
            ),
            hide_parameters=True,
        )

    try:
        with recorder.recording(method="GET", path="/per-user"):
            for user_index in range(3):
                engine = build(user_index)
                engines.append(engine)
                with engine.connect() as connection:
                    assert (
                        connection.exec_driver_sql("SELECT DB_NAME()")
                        .scalar_one()
                        .startswith("redline_test_")
                    )
                    connection.exec_driver_sql("SELECT 1")
        spans = [s for s in recorder.last_trace.spans if s.kind == "sql"]
        assert len(spans) == 6
        assert all(s.data["vendor"] == "mssql" and s.data["auto_engine"] for s in spans)
        assert len({s.data["db_alias"] for s in spans}) == 3  # one alias per engine
    finally:
        for engine in engines:
            engine.dispose()
