"""SQLAlchemy: rows counted from fetches, wide columns, and the result-size suggestions.

The suggester reads *this file*: the callsite frames point at the test functions below.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import JSON, LargeBinary, String, Text, create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from redline_core.conf import Settings
from redline_core.contrib.sqlalchemy import SQLAlchemyFixSuggester, instrument_engine
from redline_core.detect import Detector

TESTS = os.path.dirname(os.path.dirname(__file__))


class Base(DeclarativeBase):
    pass


class Doc(Base):
    __tablename__ = "docs"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(30))
    body: Mapped[str] = mapped_column(Text, default="")
    blob: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'd.db'}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add_all([Doc(title=f"t{i}", body="x" * 20) for i in range(250)])
        s.commit()
    instrument_engine(eng)
    yield eng
    eng.dispose()


def selects(spans):
    return [s for s in spans if s.kind == "sql" and s.data["sql"].lstrip().startswith("SELECT")]


def run_detect(tracer, builder, **settings):
    trace = tracer.recorder.finish_trace(builder, status=200)
    suggester = SQLAlchemyFixSuggester(Base, project_root=TESTS)
    return Detector(Settings(**settings), [suggester]).detect(trace)


def test_rows_counted_for_orm_and_core(tracer, engine):
    b = tracer.start()
    with Session(engine) as s:
        s.scalars(select(Doc)).all()
        s.scalars(select(Doc).limit(7)).all()
        s.scalars(select(Doc).limit(3)).first()
        s.execute(text("SELECT id FROM docs WHERE id < 0")).fetchall()
    with engine.connect() as conn:
        list(conn.execute(select(Doc.id).execution_options(yield_per=40)))
    spans = selects(tracer.finish(b))
    assert [s.data["rows"] for s in spans] == [
        250,
        7,
        3,
        0,
        250,
    ]  # first() still reads its 3 limited rows
    assert [s.data["has_limit"] for s in spans] == [False, True, True, False, False]


def test_non_select_rows_untouched(tracer, engine):
    b = tracer.start()
    with engine.begin() as conn:
        conn.execute(text("UPDATE docs SET title = 'z' WHERE id <= 3"))
    (span,) = [s for s in tracer.finish(b) if s.kind == "sql" and "UPDATE" in s.data["sql"]]
    assert span.data["rows"] == 3


async def test_async_rows(tracer, tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    instrument_engine(eng)
    async with AsyncSession(eng) as s:
        s.add_all([Doc(title=f"t{i}") for i in range(12)])
        await s.commit()
    b = tracer.start()
    async with AsyncSession(eng) as s:
        assert len((await s.scalars(select(Doc))).all()) == 12
    spans = selects(tracer.finish(b))
    assert spans[-1].data["rows"] == 12
    await eng.dispose()


def test_unbounded_with_wide_columns_defer_diff(tracer, engine):
    b = tracer.start()
    with Session(engine) as s:
        stmt = select(Doc).order_by(Doc.id)
        docs = s.scalars(stmt).all()
    issues = run_detect(tracer, b, unbounded_rows=200)
    unbounded = next(i for i in issues if i.kind == "unbounded_query")
    assert len(docs) == 250
    assert unbounded.extra["wide_columns"] == ["body", "blob", "extra"]
    s = unbounded.suggestion
    assert s.code == "select(Doc).options(defer(Doc.body), defer(Doc.blob), defer(Doc.extra))"
    assert s.confidence == "high"
    assert "+from sqlalchemy.orm import" in s.diff and "defer" in s.diff
    assert "defer(Doc.body), defer(Doc.blob), defer(Doc.extra))" in s.diff
    assert "test_sqlalchemy_data.py" in s.diff


def test_large_result_without_wide_columns_hint(tracer, engine):
    b = tracer.start()
    with Session(engine) as s:
        ids = s.execute(select(Doc.id)).all()
    issues = run_detect(tracer, b, large_result_rows=100, unbounded_rows=100)
    assert len(ids) == 250
    large = next(i for i in issues if i.kind == "large_result")
    assert large.extra["wide_columns"] == []
    assert large.suggestion is not None  # generic fallback: no mapper for a bare column select


def test_len_of_query_all(tracer, engine):
    b = tracer.start()
    with Session(engine) as s:
        n = len(s.query(Doc).all())
    issues = run_detect(tracer, b, unbounded_rows=100)
    assert n == 250
    issue = next(i for i in issues if i.kind == "inefficient_count")
    sug = issue.suggestion
    assert sug.code == "n = s.query(Doc).count()"
    assert "-        n = len(s.query(Doc).all())" in sug.diff
    assert "+        n = s.query(Doc).count()" in sug.diff


def test_count_greater_than_zero(tracer, engine):
    b = tracer.start()
    with Session(engine) as s:
        if s.query(Doc).filter(Doc.id > 0).count() > 0:
            pass
    issue = next(i for i in run_detect(tracer, b) if i.kind == "inefficient_count")
    assert issue.extra["pattern"] == "count_cmp"
    assert issue.suggestion.code == "if s.query(Doc).filter(Doc.id > 0).first() is not None:"
    assert (
        "+        if s.query(Doc).filter(Doc.id > 0).first() is not None:" in issue.suggestion.diff
    )


def test_truthy_all_result(tracer, engine):
    b = tracer.start()
    with Session(engine) as s:
        if s.query(Doc).filter(Doc.id > 0).all():
            pass
    issue = next(i for i in run_detect(tracer, b) if i.kind == "inefficient_count")
    assert issue.suggestion.code == "if s.query(Doc).filter(Doc.id > 0).first():"
    assert issue.suggestion.diff


def test_len_of_core_result_gets_hint_only(tracer, engine):
    b = tracer.start()
    with Session(engine) as s:
        n = len(s.scalars(select(Doc)).all())
    issue = next(i for i in run_detect(tracer, b) if i.kind == "inefficient_count")
    assert n == 250
    assert issue.suggestion.diff is None
    assert "func.count()" in issue.suggestion.summary


def test_raw_text_n_plus_one_gets_a_batching_hint():
    from redline_core.model import Callsite, Issue

    from ..conftest import sql_span

    callsite = Callsite(
        file="shop/raw.py",
        line=7,
        function="list_books",
        code='session.execute(text("SELECT * FROM books WHERE author_id = :id"), {"id": i})',
        stack=(),
    )
    spans = [
        sql_span("SELECT * FROM books WHERE author_id = ?", [i], callsite=callsite)
        for i in range(5)
    ]
    issue = Issue(
        id="n1", kind="n_plus_one", severity="warning", title="t", detail="d", callsite=callsite
    )
    suggestion = SQLAlchemyFixSuggester(Base, project_root=TESTS).suggest(issue, spans)
    assert suggestion is not None
    assert "Batch the repeated SELECT on books.author_id" in suggestion.summary
    assert "raw SQL" in suggestion.summary
    assert 'bindparam("ids", expanding=True)' in suggestion.code
    assert suggestion.confidence == "medium" and suggestion.diff is None
