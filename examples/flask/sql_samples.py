"""Offline SQL-checker examples. These functions are fixtures and must not be executed."""

from sqlalchemy import text


def comment_bind(conn):
    return conn.execute(text("SELECT :id -- documentation mentions :extra"), {"id": 1})


def bare_output(conn):
    return conn.execute(
        text("INSERT INTO dbo.products (name) OUTPUT inserted.id VALUES (:name)"),
        {"name": "invented example"},
    )


def unbalanced_session_option(conn):
    return conn.execute(text("SET NOCOUNT ON; SELECT 1"))


def missing_bind(conn):
    return conn.execute(text("SELECT :required"), {"different": 1})


def reset_in_same_statement(conn):
    return conn.execute(text("SET NOCOUNT ON; SELECT 1; SET NOCOUNT OFF"))
