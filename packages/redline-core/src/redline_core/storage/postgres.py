"""A shared store: the same engine as :class:`~redline_core.storage.SQLiteStore`, writing
to a PostgreSQL database that several worker processes and hosts share.

Install the driver with ``pip install "redline-core[postgres]"`` (psycopg 3; imported only
when this store is used) and set ``storage_url`` (``REDLINE_STORAGE_URL``) to a
``postgresql://`` URL. Everything else is :class:`~redline_core.storage.sqlite.SQLStore`:

* **Same queries.** A small dialect layer turns the engine's SQL into PostgreSQL's: ``?``
  placeholders become ``%s``, ``INSERT OR REPLACE`` an ``ON CONFLICT ... DO UPDATE`` on the
  table's primary key, ``LIMIT -1`` ``LIMIT ALL``; the DDL maps ``REAL`` to ``DOUBLE
  PRECISION``, ``INTEGER`` to ``BIGINT``, ``BLOB`` to ``BYTEA`` and ``TEXT`` to ``TEXT
  COLLATE "C"`` (byte order, like SQLite, so cursors and sorting agree).
* **Same write semantics.** ``save()`` still only enqueues; the writer thread writes a
  batch per transaction. ``BEGIN IMMEDIATE`` becomes ``BEGIN`` plus a transaction-scoped
  advisory lock, so writers from every process and host are serialized exactly as SQLite
  serializes them (the insertion sequence and the merged hourly histograms stay exact).
  Reads never take the lock.
* **Schema versioning.** The ``meta`` table holds ``schema_version`` as in SQLite; the
  migration runs under the same lock, so concurrent processes create the schema once.
* **Fork-safe.** The connection is opened lazily, per process. A connection inherited
  across ``fork()`` is never used and its socket is pointed at ``/dev/null`` in the child,
  so tearing it down there cannot end the parent's session.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from typing import Any

from .checks import SCHEMA_V4
from .sqlite import _SCHEMA, _SCHEMA_V2, _STORES, SCHEMA_VERSION, SQLStore

__all__ = ["PostgresStore", "is_postgres_url", "mask_url", "ping"]

_URL_SCHEMES = ("postgresql://", "postgres://")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_INSTALL_HINT = 'the PostgreSQL store needs psycopg 3: pip install "redline-core[postgres]"'


def is_postgres_url(value: Any) -> bool:
    """Whether ``value`` is a ``postgresql://`` (or ``postgres://``) URL."""
    return isinstance(value, str) and value.strip().lower().startswith(_URL_SCHEMES)


def mask_url(url: str) -> str:
    """``url`` without its password (userinfo and a ``password=`` query parameter)."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    netloc, slash, tail = rest.partition("/")
    if "@" in netloc:
        user, _, host = netloc.rpartition("@")
        name = user.split(":", 1)[0]
        netloc = f"{name}:***@{host}" if ":" in user else f"{name}@{host}"
    tail = re.sub(r"(?i)(password=)[^&]*", r"\1***", tail)
    return f"{scheme}{sep}{netloc}{slash}{tail}"


def _import_psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised through ping()/the store
        raise ImportError(_INSTALL_HINT) from exc
    return psycopg


# dialect ------------------------------------------------------------------------------
_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \(")
_INSERT_OR_REPLACE = re.compile(r"^\s*INSERT OR REPLACE INTO (\w+)\s*(\(([^)]*)\))?\s*VALUES", re.I)
#: Columns added by SQLite migrations (``ALTER TABLE``) rather than in the DDL.
_EXTRA_COLUMNS = {"traces": ["saved_seq"]}


def _ddl_statements() -> list[str]:
    """The SQLite schema (every version) as PostgreSQL statements, in order."""
    out: list[str] = []
    for script in (_SCHEMA, _SCHEMA_V2, SCHEMA_V4):
        for statement in script.split(";"):
            if not statement.strip():
                continue
            statement = re.sub(r"\bREAL\b", "DOUBLE PRECISION", statement)
            statement = re.sub(r"\bINTEGER\b", "BIGINT", statement)
            statement = re.sub(r"\bBLOB\b", "BYTEA", statement)
            statement = re.sub(r"\bTEXT\b", 'TEXT COLLATE "C"', statement)
            out.append(statement.strip())
    out.append("ALTER TABLE traces ADD COLUMN IF NOT EXISTS saved_seq BIGINT")
    out.append("CREATE INDEX IF NOT EXISTS traces_seq ON traces (saved_seq)")
    return out


def _split_top(text: str) -> list[str]:
    """``text`` split on the commas outside parentheses."""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


@functools.lru_cache(maxsize=1)
def _tables() -> dict[str, tuple[list[str], list[str]]]:
    """``{table: (columns in order, primary key columns)}`` read from the SQLite DDL."""
    out: dict[str, tuple[list[str], list[str]]] = {}
    for script in (_SCHEMA, _SCHEMA_V2, SCHEMA_V4):
        for statement in script.split(";"):
            match = _CREATE.match(statement.strip())
            if not match:
                continue
            body = statement[statement.index("(") + 1 : statement.rindex(")")]
            columns: list[str] = []
            key: list[str] = []
            for item in _split_top(body):
                if item.upper().startswith("PRIMARY KEY"):
                    inner = item[item.index("(") + 1 : item.rindex(")")]
                    key = [c.strip() for c in inner.split(",")]
                    continue
                column = item.split()[0]
                columns.append(column)
                if "PRIMARY KEY" in item.upper():
                    key = [column]
            name = match.group(1)
            out[name] = (columns + _EXTRA_COLUMNS.get(name, []), key)
    return out


def _upsert_clause(table: str, columns: Sequence[str]) -> str:
    known = _tables().get(table)
    if known is None or not known[1]:
        raise ValueError(f"INSERT OR REPLACE into {table!r}: no primary key known")
    key = known[1]
    update = [c for c in columns if c not in key]
    if not update:
        return f" ON CONFLICT ({', '.join(key)}) DO NOTHING"
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in update)
    return f" ON CONFLICT ({', '.join(key)}) DO UPDATE SET {sets}"


@functools.lru_cache(maxsize=512)
def translate(sql: str) -> str:
    """The engine's SQLite statement ``sql`` in PostgreSQL's dialect (psycopg ``%s``)."""
    suffix = ""
    match = _INSERT_OR_REPLACE.match(sql)
    if match:
        table = match.group(1)
        columns = (
            [c.strip() for c in match.group(3).split(",")]
            if match.group(3)
            else _tables()[table][0]
        )
        sql = f"INSERT INTO {table} {match.group(2) or ''} VALUES" + sql[match.end() :]
        suffix = _upsert_clause(table, columns)
    sql = re.sub(r"\bLIMIT -1\b", "LIMIT ALL", sql)
    out: list[str] = []
    quoted = False
    for ch in sql:
        if ch == "'":
            quoted = not quoted
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?" and not quoted:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out) + suffix


def _param(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)  # the schema stores flags as integers
    if isinstance(value, str) and "\x00" in value:
        return value.replace("\x00", "�")  # PostgreSQL text cannot hold NUL
    return value


def _params(params: Any) -> list[Any]:
    return [_param(v) for v in (params or ())]


def _plain(value: Any) -> Any:
    if type(value) is Decimal:  # sum() of BIGINT is NUMERIC
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class Row:
    """A result row indexable by position and by column name, like ``sqlite3.Row``."""

    __slots__ = ("_index", "_values")

    def __init__(self, index: dict[str, int], values: Sequence[Any]) -> None:
        self._index = index
        self._values = tuple(_plain(v) for v in values)

    def __getitem__(self, key: int | str | slice) -> Any:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Any:
        return iter(self._values)

    def keys(self) -> list[str]:
        return list(self._index)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._index, self._values, strict=True))!r})"


def _row_factory(cursor: Any) -> Callable[[Sequence[Any]], Row]:
    index = {c.name: i for i, c in enumerate(cursor.description or ())}
    return lambda values: Row(index, values)


class _Connection:
    """A psycopg connection behind the subset of the sqlite3 API the engine uses.

    It connects lazily (and again after the server dropped the connection, between
    transactions), runs in autocommit mode so the engine's explicit ``BEGIN`` / ``COMMIT``
    delimit transactions, and turns ``BEGIN IMMEDIATE`` into ``BEGIN`` plus
    ``pg_advisory_xact_lock``.
    """

    def __init__(self, connect: Callable[[], Any], lock_key: int) -> None:
        self._connect = connect
        self._lock_key = lock_key
        self._raw: Any = None
        self._in_tx = False

    def _live(self) -> Any:
        raw = self._raw
        if raw is None or raw.closed or raw.broken:
            if self._in_tx:
                psycopg = _import_psycopg()
                raise psycopg.OperationalError("the connection was lost inside a transaction")
            raw = self._raw = self._connect()
        return raw

    def execute(self, sql: str, params: Any = ()) -> Any:
        head = sql.strip().upper()
        if head in ("BEGIN", "BEGIN IMMEDIATE"):
            raw = self._live()
            raw.execute("BEGIN")
            self._in_tx = True
            try:
                raw.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key,))
            except BaseException:
                self.execute("ROLLBACK")
                raise
            return None
        if head in ("COMMIT", "ROLLBACK"):
            self._in_tx = False
            raw = self._raw
            if raw is None or raw.closed or raw.broken:
                if head == "ROLLBACK":
                    return None  # nothing to roll back on a dead connection
                psycopg = _import_psycopg()
                raise psycopg.OperationalError("the connection was lost before COMMIT")
            return raw.execute(head)
        return self._live().execute(translate(sql), _params(params))

    def executemany(self, sql: str, seq: Iterable[Any]) -> Any:
        rows = [_params(p) for p in seq]
        cur = self._live().cursor()
        if rows:
            cur.executemany(translate(sql), rows)
        return cur

    def close(self) -> None:
        raw, self._raw = self._raw, None
        if raw is not None and not raw.closed:
            raw.close()

    def detach_after_fork(self) -> None:
        """In a forked child: point the inherited socket at ``/dev/null`` so that tearing
        the connection down here (libpq sends a terminate message) cannot end the parent's
        session, then forget it."""
        raw, self._raw = self._raw, None
        self._in_tx = False
        if raw is None or raw.closed:
            return
        try:
            fd = raw.pgconn.socket
            null = os.open(os.devnull, os.O_RDWR)
            try:
                os.dup2(null, fd)
            finally:
                os.close(null)
        except Exception:  # pragma: no cover - already unusable; nothing to protect
            pass
        _PARKED.append(raw)


#: Connections inherited across fork(), kept alive (never used) in the child.
_PARKED: list[Any] = []


def _lock_key(schema: str | None) -> int:
    digest = hashlib.sha256(f"redline-store:{schema or ''}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def ping(url: str, timeout: float = 5.0) -> str:
    """Connect to ``url`` and return the server version; raises on failure (the doctor)."""
    psycopg = _import_psycopg()
    with psycopg.connect(url, connect_timeout=max(1, int(timeout)), autocommit=True) as conn:
        row = conn.execute("SHOW server_version").fetchone()
    return str(row[0]) if row else "?"


class PostgresStore(SQLStore):
    """A :class:`~redline_core.storage.Store` in a shared PostgreSQL database.

    :param url: a libpq connection URL (``postgresql://user:pw@host:5432/db``).
    :param schema: optional schema for Redline's tables (created when missing); without
        it the tables go to the first schema of the connection's ``search_path``.
    :param connect_kwargs: extra keyword arguments for ``psycopg.connect``.

    The other parameters are :class:`~redline_core.storage.SQLiteStore`'s. Load tests and
    refactor checks started from the UI need a local SQLite store (their background
    process writes into the store file), so the UI answers 501 for them here; runs recorded
    by ``redline load`` / ``redline check`` into a SQLite store are unaffected.
    """

    _SAMPLES_SQL = (
        "SELECT DISTINCT ON (fingerprint) fingerprint, trace_id FROM trace_sql "
        "WHERE {where} ORDER BY fingerprint, started_at DESC"
    )

    def __init__(
        self,
        url: str,
        *,
        schema: str | None = None,
        background: bool = True,
        queue_size: int = 1_000,
        max_rows: int | None = 10_000,
        max_age_s: float | None = 7 * 86_400.0,
        prune_every: int = 200,
        batch_size: int = 50,
        history_retention_s: float | None = 90 * 86_400.0,
        connect_kwargs: dict[str, Any] | None = None,
    ) -> None:
        if not is_postgres_url(url):
            raise ValueError("PostgresStore needs a postgresql:// URL")
        if schema is not None and not _IDENT.match(schema):
            raise ValueError("schema must be a plain identifier (letters, digits, _)")
        psycopg = _import_psycopg()
        self._db_errors = (psycopg.Error,)
        self._init_common(
            background=background,
            queue_size=queue_size,
            max_rows=max_rows,
            max_age_s=max_age_s,
            prune_every=prune_every,
            batch_size=batch_size,
            history_retention_s=history_retention_s,
        )
        self.url = url.strip()
        #: Shown wherever a store is identified; never carries the password.
        self.path = mask_url(self.url)
        self.schema = schema
        self._connect_kwargs = dict(connect_kwargs or {})
        self._conn = self._connect()
        self._migrate()
        # Do not keep the setup connection: a store built before fork() (gunicorn
        # --preload) should not hand a live socket to every worker.
        self._conn.close()
        _STORES.add(self)

    @classmethod
    def from_settings(cls, settings: Any, **kwargs: Any) -> PostgresStore:
        """Build from a :class:`~redline_core.conf.Settings` with ``storage_url`` set."""
        days = getattr(settings, "history_retention_days", 90)
        kwargs.setdefault("history_retention_s", float(days) * 86_400.0 if days else None)
        return cls(
            settings.storage_url,
            queue_size=settings.writer_queue_size,
            max_rows=settings.max_rows,
            max_age_s=settings.max_age_s,
            **kwargs,
        )

    def _open(self) -> Any:
        psycopg = _import_psycopg()
        kwargs: dict[str, Any] = {
            "autocommit": True,
            "row_factory": _row_factory,
            # No server-side prepared statements: they break behind PgBouncer's
            # transaction pooling, and the store's statements are cheap to plan.
            "prepare_threshold": None,
            "connect_timeout": 10,
        }
        if "application_name" not in self.url and "application_name" not in self._connect_kwargs:
            kwargs["application_name"] = "redline"
        if sys.platform == "darwin" and "gssencmode" not in self.url:
            # macOS: libpq's GSSAPI probe crashes in a forked child (gunicorn workers,
            # multiprocessing); Kerberos-encrypted connections are opt-in there.
            kwargs["gssencmode"] = "disable"
        if self.schema:
            kwargs["options"] = f"-c search_path={self.schema}"
        kwargs.update(self._connect_kwargs)
        return psycopg.connect(self.url, **kwargs)

    def _connect(self) -> _Connection:
        return _Connection(self._open, _lock_key(self.schema))

    def _park_inherited(self, conn: Any) -> None:
        if isinstance(conn, _Connection):
            conn.detach_after_fork()

    def _migrate(self) -> None:
        """Create or upgrade the schema to :data:`SCHEMA_VERSION` (idempotent, additive,
        serialized across processes by the advisory lock)."""
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            if self.schema:
                c.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            c.execute(
                'CREATE TABLE IF NOT EXISTS meta (key TEXT COLLATE "C" PRIMARY KEY, '
                'value TEXT COLLATE "C" NOT NULL)'
            )
            row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            try:
                version = int(row[0]) if row else 0
            except ValueError:
                version = 0
            if version < SCHEMA_VERSION:
                # Every statement is IF NOT EXISTS: a partly created schema is completed.
                for statement in _ddl_statements():
                    c.execute(statement)
                c.execute(
                    "INSERT INTO meta (key, value) VALUES ('last_seq', "
                    "(SELECT CAST(COALESCE(MAX(saved_seq), 0) AS TEXT) FROM traces)) "
                    "ON CONFLICT (key) DO NOTHING"
                )
                c.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def schema_version(self) -> int:
        rows = self._query("SELECT value FROM meta WHERE key = 'schema_version'")
        return int(rows[0][0]) if rows else 0

    # Load and check runs launched from the UI are tracked by pid files beside a SQLite
    # store; there is no such directory here, so there is nothing to reap.
    def _reap_dead_runs(self) -> None:
        return None

    def _reap_dead_check_runs(self) -> None:
        return None

    def __repr__(self) -> str:
        return f"PostgresStore({self.path!r})"
