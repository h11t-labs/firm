"""Engine-creation checks for firm-core's ``create_engine_for`` — in particular the in-memory
SQLite path, which must not crash and must share one database across threads."""

from __future__ import annotations

import threading

from sqlalchemy import Column, Integer, MetaData, Table, insert, select
from sqlalchemy.pool import StaticPool

from firm._core.database import (
    create_engine_for,
    is_memory_sqlite_url,
    transaction,
)


def test_is_memory_sqlite_url_detects_the_ram_forms() -> None:
    for url in ("sqlite://", "sqlite:///:memory:", "sqlite:///file:x?mode=memory&uri=true"):
        assert is_memory_sqlite_url(url), url
    for url in ("sqlite:///data.db", "sqlite:////abs/path.db", "postgresql://h/db"):
        assert not is_memory_sqlite_url(url), url


def test_create_engine_for_bare_memory_url_succeeds() -> None:
    # Regression: bare in-memory SQLite used SingletonThreadPool, which rejects the
    # pool_size/max_overflow kwargs we always passed → TypeError at engine creation.
    engine = create_engine_for("sqlite://")
    try:
        assert isinstance(engine.pool, StaticPool)
    finally:
        engine.dispose()


def test_create_engine_for_explicit_memory_url_succeeds() -> None:
    engine = create_engine_for("sqlite:///:memory:")
    try:
        assert isinstance(engine.pool, StaticPool)
    finally:
        engine.dispose()


def test_memory_engine_is_shared_across_threads() -> None:
    # StaticPool hands the one connection to every thread, so a row written by one thread is
    # visible to another. SingletonThreadPool would give each thread its own empty database.
    engine = create_engine_for("sqlite:///:memory:")
    md = MetaData()
    t = Table("t", md, Column("id", Integer, primary_key=True))
    md.create_all(engine)
    try:
        with transaction(engine) as conn:
            conn.execute(insert(t).values(id=1))

        seen: list[int] = []

        def _read() -> None:
            with engine.connect() as conn:
                seen.extend(conn.execute(select(t.c.id)).scalars().all())

        reader = threading.Thread(target=_read)
        reader.start()
        reader.join()

        assert seen == [1]
    finally:
        engine.dispose()
