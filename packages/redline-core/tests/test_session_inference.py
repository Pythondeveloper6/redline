from redline_core.analyze import analyze_source


def _query_loop(source):
    return [
        finding
        for finding in analyze_source(source)["findings"]
        if finding["kind"] == "query_in_loop"
    ]


def test_optional_union_annotated_and_quoted_session_annotations():
    source = """
from typing import Annotated, Optional
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

def optional_sync(session: Optional[Session], keys):
    for key in keys:
        session.execute(key)

async def optional_async(session: AsyncSession | None, keys):
    for key in keys:
        await session.execute(key)

def annotated(session: Annotated[Session, "db session"], keys):
    for key in keys:
        session.execute(key)

async def quoted(session: "Optional[AsyncSession]", keys):
    for key in keys:
        await session.execute(key)
"""

    findings = _query_loop(source)
    assert len(findings) == 4


def test_session_factory_assignment_and_sync_context_manager():
    source = """
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import async_sessionmaker

SyncFactory = sessionmaker(engine)
AsyncFactory = async_sessionmaker(engine)

def sync_work(keys):
    for key in keys:
        session = SyncFactory()
        session.execute(key)

async def async_work(keys):
    for key in keys:
        async with AsyncFactory() as session:
            await session.execute(key)

def sync_context(keys):
    for key in keys:
        with SyncFactory() as session:
            session.execute(key)
"""

    assert len(_query_loop(source)) == 3


def test_imported_factory_alias_and_annotated_session_call():
    source = """
from sqlalchemy.ext.asyncio import async_sessionmaker as make_async
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

Factory = make_async(engine)

async def work(keys):
    for key in keys:
        async with Factory() as session:
            await session.execute(key)

async def annotated(session: Annotated[AsyncSession, "metadata"], keys):
    for key in keys:
        await session.execute(key)
"""

    assert len(_query_loop(source)) == 2


def test_reassigned_factory_and_non_session_factory_are_not_inferred():
    source = """
from sqlalchemy.ext.asyncio import async_sessionmaker

Factory = async_sessionmaker(engine)
Factory = ordinary_factory()
OtherFactory = ordinary_factory()

def reassigned(keys):
    for key in keys:
        session = Factory()
        session.execute(key)

def ordinary(keys):
    for key in keys:
        session = OtherFactory()
        session.execute(key)
"""

    assert not _query_loop(source)


def test_factory_parameter_or_local_shadowing_suppresses_inference():
    source = """
from sqlalchemy.ext.asyncio import async_sessionmaker

Factory = async_sessionmaker(engine)

def parameter_shadow(Factory, keys):
    for key in keys:
        session = Factory()
        session.execute(key)

def local_shadow(keys):
    Factory = ordinary_factory()
    for key in keys:
        session = Factory()
        session.execute(key)
"""

    assert not _query_loop(source)


def test_custom_session_type_and_factory_names_are_not_guessed():
    source = """
class Session:
    def execute(self, query):
        pass

def sessionmaker(engine):
    return CustomFactory()

CustomFactory = sessionmaker(engine)

def ordinary(session: Session, keys):
    for key in keys:
        session.execute(key)

def ordinary_factory(keys):
    for key in keys:
        session = CustomFactory()
        session.execute(key)
"""

    assert not _query_loop(source)


def test_dynamic_application_session_factory_is_not_assumed():
    source = """
from app.database import get_sessionmaker

Factory = get_sessionmaker(engine)

def work(keys):
    for key in keys:
        session = Factory()
        session.execute(key)
"""

    assert not _query_loop(source)


def test_string_annotations_are_parsed_without_execution_or_unbounded_input():
    source = """
from sqlalchemy.ext.asyncio import AsyncSession

async def safe(session: "AsyncSession", keys):
    for key in keys:
        await session.execute(key)

async def dangerous(session: "__import__('os').system('false')", keys):
    for key in keys:
        await session.execute(key)
"""

    findings = _query_loop(source)
    assert len(findings) == 1
