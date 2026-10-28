from redline_core.analyze import analyze_project, analyze_source


def kinds(source):
    return {f["kind"] for f in analyze_source(source)["findings"]}


def test_queryset_aggregation_and_filtering():
    source = """
def report():
    rows = Event.objects.filter(active=True)
    total = sum(row.duration for row in rows)
    selected = [row for row in rows if row.duration > 0]
"""
    assert kinds(source) == {"python_aggregation", "python_filtering"}
    assert not kinds("total = sum(x for x in [1, 2, 3])")


def test_query_in_loop_and_sorting():
    source = """
def report(ids, events):
    for id in ids:
        user = User.objects.get(id=id)
        ranked = sorted(events)
"""
    assert kinds(source) == {"query_in_loop", "sorting_in_loop"}


def test_async_alias_and_no_execution():
    assert "blocking_async_call" in kinds("import time as t\nasync def view():\n    t.sleep(1)")
    result = analyze_source('raise RuntimeError("must never run")')
    assert result == {"findings": [], "errors": []}


def test_loop_membership_and_repeated_passes():
    source = """
def report(events):
    allowed = [1, 2]
    for hour in range(24):
        for event in events:
            if event in allowed:
                pass
"""
    assert {"repeated_collection_scan", "list_membership_in_loop"} <= kinds(source)


def test_project_skips_symlinks_and_dependencies(tmp_path):
    (tmp_path / "ok.py").write_text("import time\nasync def f():\n    time.sleep(1)")
    (tmp_path / "link.py").symlink_to(tmp_path / "ok.py")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "bad.py").write_text("invalid source!")
    result = analyze_project(tmp_path)
    assert result["files_analyzed"] == 1
    assert not result["errors"]


def test_parse_failure_is_reported():
    assert analyze_source("def bad(")["errors"]


def test_sqlalchemy_session_annotations_and_async_results():
    source = """
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

def report(session: Session, ids):
    for id in ids:
        session.execute(select(Event).where(Event.id == id))

async def totals(session: AsyncSession):
    rows = await session.scalars(select(Event))
    return sum(row.duration for row in rows)
"""
    assert {"query_in_loop", "python_aggregation"} <= kinds(source)


def test_reassigned_collection_and_unrelated_all_method_are_not_querysets():
    source = """
def report():
    rows = Event.objects.all()
    rows = [1, 2, 3]
    allowed = [1, 2]
    allowed = set(allowed)
    for row in rows:
        if row in allowed:
            pass
    return sum(x for x in rows), unrelated.all()
"""
    assert not kinds(source)


def test_nested_function_definition_does_not_imply_repeated_execution():
    assert not kinds("for n in range(24):\n    def later(events):\n        return sorted(events)")


def test_conditional_loop_filtering_requires_row_filter_evidence():
    source = """
def report():
    rows = Event.objects.all()
    chosen = []
    for row in rows:
        if row.active:
            chosen.append(row)
    return chosen
"""
    assert "python_filtering" in kinds(source)
    assert "python_filtering" not in kinds(source.replace("chosen.append(row)", "print(row)"))
