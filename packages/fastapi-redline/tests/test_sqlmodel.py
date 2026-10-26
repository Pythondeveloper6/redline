from typing import Optional

import pytest

sqlmodel = pytest.importorskip("sqlmodel")

from fastapi_redline.suggest import SQLAlchemyFixSuggester  # noqa: E402
from redline_core.model import Issue, Span  # noqa: E402


class Team(sqlmodel.SQLModel, table=True):
    id: int | None = sqlmodel.Field(default=None, primary_key=True)
    name: str
    heroes: list["Hero"] = sqlmodel.Relationship(back_populates="team")


class Hero(sqlmodel.SQLModel, table=True):
    id: int | None = sqlmodel.Field(default=None, primary_key=True)
    team_id: int | None = sqlmodel.Field(default=None, foreign_key="team.id")
    team: Optional["Team"] = sqlmodel.Relationship(back_populates="heroes")


def _suggest(sql):
    span = Span("a", None, "sql", "x", 0, 1, None, {"sql": sql})
    issue = Issue("i", "n_plus_one", "warning", "t", "d", ["a"])
    return SQLAlchemyFixSuggester(sqlmodel.SQLModel).suggest(issue, [span])


def test_sqlmodel_one_to_many():
    s = _suggest("SELECT hero.id FROM hero WHERE ? = hero.team_id")
    assert s.code == "select(Team).options(selectinload(Team.heroes))"


def test_sqlmodel_many_to_one():
    s = _suggest("SELECT team.id FROM team WHERE team.id = ?")
    assert s.code == "select(Hero).options(joinedload(Hero.team))"
