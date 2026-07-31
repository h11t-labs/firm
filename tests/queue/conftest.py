"""Shared pytest fixtures for the queue tests.

By default the suite runs against an on-disk SQLite database. When ``FIRM_TEST_PG_URL``
and/or ``FIRM_TEST_MYSQL_URL`` are set, every database-touching test *also* runs against
those live backends (fresh schema per test). Fork-mode tests stay SQLite-only (see ``is_sqlite``).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, Table, func, insert, select

from firm._core.clock import now_utc
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


class Seeder:
    """Inserts rows directly, so read-layer tests can set up each job state without running a
    worker. Plain Core inserts, so it works on every backend the suite parametrizes."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _job(self, *, queue: str = "default", class_name: str = "app.task", finished: bool = False):
        with self.engine.begin() as conn:
            return conn.execute(
                insert(schema.jobs).values(
                    queue_name=queue,
                    class_name=class_name,
                    finished_at=now_utc() if finished else None,
                )
            ).inserted_primary_key[0]

    def ready(self, *, queue: str = "default") -> int:
        job_id = self._job(queue=queue)
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.ready_executions).values(job_id=job_id, queue_name=queue, priority=0)
            )
        return job_id

    def scheduled(self, *, queue: str = "default") -> int:
        job_id = self._job(queue=queue)
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.scheduled_executions).values(
                    job_id=job_id, queue_name=queue, priority=0, scheduled_at=now_utc()
                )
            )
        return job_id

    def claimed(self, *, process_id: int = 1) -> int:
        job_id = self._job()
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.claimed_executions).values(job_id=job_id, process_id=process_id)
            )
        return job_id

    def failed(self, *, error: str = "Traceback...\nValueError: boom") -> int:
        job_id = self._job()
        with self.engine.begin() as conn:
            conn.execute(insert(schema.failed_executions).values(job_id=job_id, error=error))
        return job_id

    def finished(self) -> int:
        return self._job(finished=True)

    def process(self, *, kind: str = "worker", name: str = "w1", age_seconds: float = 0.0) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.processes).values(
                    kind=kind,
                    name=name,
                    pid=4242,
                    hostname="testhost",
                    last_heartbeat_at=now_utc() - timedelta(seconds=age_seconds),
                )
            )

    def recurring_task(
        self,
        *,
        key: str = "cleanup",
        schedule: str = "*/10 * * * *",
        class_name: str = "app.task",
        queue: str = "default",
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.recurring_tasks).values(
                    key=key, schedule=schedule, class_name=class_name, queue_name=queue
                )
            )


@pytest.fixture
def seed(engine: Engine) -> Seeder:
    return Seeder(engine)


@pytest.fixture
def runtime(db_url: str, engine: Engine) -> Iterator[Runtime]:
    rt = config.configure(database_url=db_url)
    try:
        yield rt
    finally:
        config.set_runtime(None)
        rt.reset()
