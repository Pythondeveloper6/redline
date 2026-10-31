import pytest

from redline_core.analyze import analyze_source


def findings(source: str) -> list[dict]:
    return analyze_source(source)["findings"]


def query_findings(source: str) -> list[dict]:
    return [finding for finding in findings(source) if finding["kind"] == "query_in_loop"]


@pytest.mark.parametrize(
    "statement",
    [
        "for row in (await session.execute(stmt)).all():\n        consume(row)",
        "rows = [row for row in (await session.execute(stmt)).all()]",
        "rows = {row.id for row in (await session.execute(stmt)).all()}",
        "rows = {row.id: row for row in (await session.execute(stmt)).all()}",
        "rows = (row for row in (await session.execute(stmt)).all())",
    ],
)
def test_query_used_as_first_iterable_runs_once(statement: str) -> None:
    source = f"""
from sqlalchemy.ext.asyncio import AsyncSession

async def load(session: AsyncSession, stmt):
    {statement}
"""

    assert not query_findings(source)


def test_query_in_nested_comprehension_iterable_is_repeated() -> None:
    source = """
from sqlalchemy.ext.asyncio import AsyncSession

async def load(session: AsyncSession, stmt, outer_rows):
    return [
        (outer, inner)
        for outer in outer_rows
        for inner in (await session.execute(stmt)).all()
    ]
"""

    matches = query_findings(source)
    assert len(matches) == 1
    assert "selectinload" in matches[0]["suggestion"] or "SQLAlchemy" in matches[0]["suggestion"]


@pytest.mark.parametrize(
    ("operation", "imports"),
    [
        ("await session.get(Item, item_id)", ""),
        (
            "await session.scalar(select(Item.id).where(Item.id == item_id))",
            "from sqlalchemy import select",
        ),
    ],
)
def test_sqlalchemy_session_get_and_scalar_in_loop_are_reported(
    operation: str, imports: str
) -> None:
    source = f"""
{imports}
from sqlalchemy.ext.asyncio import AsyncSession

async def load(session: AsyncSession, ids):
    for item_id in ids:
        {operation}
"""

    matches = query_findings(source)
    assert len(matches) == 1
    assert "selectinload" in matches[0]["suggestion"] or "SQLAlchemy" in matches[0]["suggestion"]


def test_dict_get_in_loop_is_not_reported_as_a_query() -> None:
    source = """
def load(rows, values):
    for row in rows:
        values.get(row.id)
"""

    assert not query_findings(source)


def test_async_session_helper_called_in_loop_is_reported() -> None:
    source = """
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

async def fetch_one(session: AsyncSession, item_id):
    return await session.execute(select(Item).where(Item.id == item_id))

async def load(session: AsyncSession, ids):
    for item_id in ids:
        await fetch_one(session, item_id)
"""

    matches = query_findings(source)
    assert len(matches) == 1
    assert "selectinload" in matches[0]["suggestion"] or "SQLAlchemy" in matches[0]["suggestion"]


def test_transitive_async_session_helper_called_in_loop_is_reported() -> None:
    source = """
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

async def execute_query(session: AsyncSession, item_id):
    return await session.execute(select(Item).where(Item.id == item_id))

async def fetch_one(session: AsyncSession, item_id):
    return await execute_query(session, item_id)

async def load(session: AsyncSession, ids):
    for item_id in ids:
        await fetch_one(session, item_id)
"""

    matches = query_findings(source)
    assert len(matches) == 1
    assert "selectinload" in matches[0]["suggestion"] or "SQLAlchemy" in matches[0]["suggestion"]


@pytest.mark.parametrize(
    "source",
    [
        """
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

async def fetch_one(session: AsyncSession, item_id):
    return await session.execute(select(Item).where(Item.id == item_id))

async def load(session: AsyncSession, ids):
    for item_id in ids:
        pass
    await fetch_one(session, 1)
""",
        """
from sqlalchemy.ext.asyncio import AsyncSession

async def fetch_one(session: AsyncSession, item_id):
    return item_id

async def load(session: AsyncSession, ids):
    for item_id in ids:
        await fetch_one(session, item_id)
""",
    ],
)
def test_helper_calls_without_repeated_sql_are_not_reported(source: str) -> None:
    assert not query_findings(source)


@pytest.mark.parametrize(
    "shadow",
    [
        "fetch_one = lambda *args: None",
        "import unrelated as fetch_one",
        "def fetch_one(*args): return None",
    ],
)
def test_local_shadowing_does_not_reuse_module_query_helper(shadow):
    source = f"""
from sqlalchemy.orm import Session

def fetch_one(session: Session, key):
    return session.get(Item, key)

def report(session: Session, keys):
    {shadow}
    for key in keys:
        fetch_one(session, key)
"""
    assert not query_findings(source)
