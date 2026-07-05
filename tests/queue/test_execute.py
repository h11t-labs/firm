"""End-to-end execute specs: enqueue -> claim -> run -> finish/fail."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine, insert, select

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.worker import run_ready

_SINK: list[int] = []


@bq.job()
def record_job(value: int) -> None:
    _SINK.append(value)


@bq.job()
def boom_job() -> None:
    raise ValueError("boom")


def test_execute_success_finishes_job(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    _SINK.clear()
    record_job.enqueue(42)

    assert run_ready(runtime) == 1
    assert _SINK == [42]
    assert count(schema.claimed_executions) == 0
    assert count(schema.failed_executions) == 0

    with engine.connect() as conn:
        finished_at = conn.execute(select(schema.jobs.c.finished_at)).scalar()
    assert finished_at is not None


def test_execute_failure_records_failed_execution(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    boom_job.enqueue()

    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 1
    assert count(schema.claimed_executions) == 0

    with engine.connect() as conn:
        error = conn.execute(select(schema.failed_executions.c.error)).scalar()
    assert error is not None
    assert "ValueError" in error
    assert "boom" in error


def test_unregistered_class_records_failed(runtime: Runtime, count: Callable[..., int]) -> None:
    with runtime.engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(queue_name="default", class_name="nope.NotReal")
        ).inserted_primary_key[0]
        conn.execute(
            insert(schema.ready_executions).values(job_id=job_id, queue_name="default", priority=0)
        )

    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 1
