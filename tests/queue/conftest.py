"""Shared pytest fixtures for the queue tests.

By default the suite runs against an on-disk SQLite database. When ``FIRM_TEST_PG_URL``
and/or ``FIRM_TEST_MYSQL_URL`` are set, every database-touching test *also* runs against
those live backends (fresh schema per test). Fork-mode tests stay SQLite-only (see ``is_sqlite``).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, Table, func, insert, select

from firm._core.config import Runtime
from firm._core.database import create_engine_for
from firm.queue import config, schema


def _backend_params() -> list:
    params = [pytest.param("sqlite", id="sqlite")]
    if pg := os.environ.get("FIRM_TEST_PG_URL"):
        params.append(pytest.param(pg, id="postgres"))
    if my := os.environ.get("FIRM_TEST_MYSQL_URL"):
        params.append(pytest.param(my, id="mysql"))
    return params


@pytest.fixture(params=_backend_params())
def backend(request) -> str:
    """Either the literal ``"sqlite"`` or a live database URL."""
    return request.param


@pytest.fixture
def db_url(backend: str, tmp_path) -> str:
    if backend == "sqlite":
        return f"sqlite:///{tmp_path / 'queue.db'}"
    eng = create_engine_for(backend)
    schema.drop_all(eng)
    schema.create_all(eng)
    eng.dispose()
    return backend


@pytest.fixture
def is_sqlite(db_url: str) -> bool:
    return db_url.startswith("sqlite")


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    eng = create_engine_for(db_url)
    schema.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def add_ready(engine: Engine) -> Callable[..., int]:
    """Insert a job + a ready_execution; return the new job id."""

    def _add(
        queue: str = "default",
        priority: int = 0,
        class_name: str = "J",
        arguments: str | None = None,
    ) -> int:
        with engine.begin() as conn:
            job_id = conn.execute(
                insert(schema.jobs).values(
                    queue_name=queue,
                    class_name=class_name,
                    priority=priority,
                    arguments=arguments,
                )
            ).inserted_primary_key[0]
            conn.execute(
                insert(schema.ready_executions).values(
                    job_id=job_id, queue_name=queue, priority=priority
                )
            )
        return job_id

    return _add


@pytest.fixture
def count(engine: Engine) -> Callable[[Table], int]:
    def _count(table: Table) -> int:
        with engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(table)).scalar() or 0

    return _count


@pytest.fixture
def runtime(db_url: str, engine: Engine) -> Iterator[Runtime]:
    rt = config.configure(database_url=db_url)
    try:
        yield rt
    finally:
        config.set_runtime(None)
        rt.reset()
