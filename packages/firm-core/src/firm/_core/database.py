"""Engine creation, SQLite PRAGMA wiring, and transaction context managers.

The important SQLite-specific behaviour lives here:

* WAL + ``busy_timeout`` so readers never block the single writer and competing writers
  block-and-retry instead of immediately raising ``SQLITE_BUSY``.
* ``isolation_level=None`` (we drive transactions ourselves) so we can emit
  ``BEGIN IMMEDIATE`` for the claim path. ``BEGIN IMMEDIATE`` takes SQLite's write lock up
  front, which is how we get the "only one worker wins a row" guarantee that PostgreSQL/MySQL
  get from ``FOR UPDATE SKIP LOCKED``. This is the documented SQLAlchemy pysqlite recipe.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection

_IMMEDIATE_KEY = "firm_begin_immediate"


def is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


def normalize_url(url: str) -> str:
    """Point bare ``postgresql://`` / ``mysql://`` URLs at the drivers we ship, so users don't
    have to remember the ``+psycopg`` / ``+pymysql`` suffix. Explicit drivers are left alone."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("mysql://"):
        return "mysql+pymysql://" + url[len("mysql://") :]
    return url


def _require_driver(url: str) -> None:
    """Raise a clear error if the URL needs a database-driver extra that isn't installed."""
    import importlib.util

    for prefix, module, extra in (
        ("postgresql+psycopg", "psycopg", "postgres"),
        ("mysql+pymysql", "pymysql", "mysql"),
    ):
        if url.startswith(prefix) and importlib.util.find_spec(module) is None:
            raise ImportError(
                f'The {extra} driver "{module}" is not installed. Install the {extra} extra: '
                f'pip install "firm-core[{extra}]"'
            )


def create_engine_for(
    url: str,
    *,
    busy_timeout_ms: int = 5000,
    pool_size: int = 20,
    max_overflow: int = 40,
    echo: bool = False,
) -> Engine:
    """Create an :class:`~sqlalchemy.Engine` configured for firm's access patterns."""
    url = normalize_url(url)
    _require_driver(url)
    connect_args: dict[str, object] = {}
    # Plenty of headroom for many worker threads + dispatcher/scheduler/heartbeat loops.
    kwargs: dict[str, object] = {"pool_size": pool_size, "max_overflow": max_overflow}
    if is_sqlite_url(url):
        # Connections are checked out of the pool by whichever worker thread needs them;
        # SQLAlchemy's pool still hands a connection to one thread at a time.
        connect_args["check_same_thread"] = False
    else:
        # Recover transparently from server-dropped / idle-timed-out connections (PG/MySQL).
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 3600

    engine = create_engine(url, echo=echo, connect_args=connect_args, **kwargs)

    if is_sqlite_url(url):
        _install_sqlite_pragmas(engine, busy_timeout_ms)
    return engine


def _install_sqlite_pragmas(engine: Engine, busy_timeout_ms: int) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        # Hand transaction control to us (see module docstring).
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        # With isolation_level=None pysqlite emits no implicit BEGIN, so we emit our own —
        # IMMEDIATE when the caller asked for the claim path, plain otherwise.
        if conn.info.get(_IMMEDIATE_KEY):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.exec_driver_sql("BEGIN")


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    """Run a block inside an ordinary transaction, committing on success."""
    with engine.connect() as conn, conn.begin():
        yield conn


@contextmanager
def immediate_transaction(engine: Engine) -> Iterator[Connection]:
    """Run a block inside ``BEGIN IMMEDIATE`` (SQLite) / an ordinary transaction elsewhere.

    Used by the claim path so concurrent claimers serialize on SQLite's write lock instead of
    double-claiming a job.
    """
    with engine.connect() as conn:
        # conn.info lives on the pooled DBAPI connection and survives check-in, so the flag
        # must be cleared on the way out — otherwise every later plain transaction() on this
        # pooled connection would also BEGIN IMMEDIATE, needlessly serializing reads.
        conn.info[_IMMEDIATE_KEY] = True
        try:
            with conn.begin():
                yield conn
        finally:
            conn.info.pop(_IMMEDIATE_KEY, None)


@contextmanager
def snapshot_transaction(engine: Engine, *, write: bool = False) -> Iterator[Connection]:
    """A transaction that sees a **consistent snapshot** for its whole span — so a multi-statement
    read (audit verification) or a check-then-mutate (retention's aligned prune) is never fooled by
    another transaction committing in the middle.

    Dialect-aware isolation, since only Postgres/MySQL default to ``READ COMMITTED`` where that
    interleaving is visible:

    * **SQLite** — a plain (``write=False``) deferred ``BEGIN`` already reads a stable WAL snapshot
      from its first statement; ``write=True`` upgrades to ``BEGIN IMMEDIATE`` (the write lock) so a
      re-verify-then-delete holds off any concurrent writer for the whole transaction.
    * **Postgres / MySQL** — ``REPEATABLE READ`` for a read snapshot; ``SERIALIZABLE`` for a
      read-write prune, so a row modified and committed by another session between the pre-prune
      re-verify and the delete makes the prune fail (and retry) rather than launder the change.
    """
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            conn.info[_IMMEDIATE_KEY] = write
            try:
                with conn.begin():
                    yield conn
            finally:
                conn.info.pop(_IMMEDIATE_KEY, None)
        return
    level = "SERIALIZABLE" if write else "REPEATABLE READ"
    with engine.connect() as raw:
        conn = raw.execution_options(isolation_level=level)
        with conn.begin():
            yield conn


def dispose_engine(engine: Engine, *, close: bool = True) -> None:
    """Dispose an engine's pool.

    Pass ``close=False`` from a forked child: the child must *drop* the pooled connections it
    inherited without closing them — they are the parent's live sockets (SQLAlchemy's
    documented post-fork recipe). ``close=True`` (the default) is for genuine shutdown, where
    the connections belong to us and must be closed.
    """
    engine.dispose(close=close)
