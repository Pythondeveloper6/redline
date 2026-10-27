"""A Flask-SQLAlchemy app. Real source lines matter: the suggester reads this file."""

from __future__ import annotations

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class FsaBase(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=FsaBase)


class Author(db.Model):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    books: Mapped[list[Book]] = relationship(back_populates="author")


class Book(db.Model):
    __tablename__ = "books"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"))
    title: Mapped[str] = mapped_column(String(40))
    author: Mapped[Author] = relationship(back_populates="books")


class Log(db.Model):
    __bind_key__ = "audit"
    __tablename__ = "log"
    id: Mapped[int] = mapped_column(primary_key=True)
    line: Mapped[str] = mapped_column(String(40))


def create_app(db_uri: str, audit_uri: str) -> Flask:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SQLALCHEMY_BINDS"] = {"audit": audit_uri}

    @app.get("/authors")
    def authors() -> dict:
        authors = db.session.scalars(select(Author).order_by(Author.id)).all()
        return {"books": sum(len(a.books) for a in authors)}

    @app.get("/books")
    def books() -> dict:
        books = db.session.scalars(select(Book).order_by(Book.id)).all()
        return {"authors": [b.author.name for b in books]}

    @app.get("/audit")
    def audit() -> dict:
        db.session.add(Log(line="seen"))
        db.session.commit()
        return {"lines": len(db.session.scalars(select(Log)).all())}

    return app


def seed() -> None:
    authors = [Author(name=f"a{i}") for i in range(8)]
    db.session.add_all(authors)
    db.session.flush()
    for a in authors:
        db.session.add_all([Book(author_id=a.id, title=f"{a.name}-{j}") for j in range(2)])
    db.session.commit()
