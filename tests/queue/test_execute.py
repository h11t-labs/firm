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


@bq.job()
def exit_job() -> None:
    raise SystemExit(3)


def test_base_exception_from_job_is_finalized_as_failure(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """SystemExit/KeyboardInterrupt from a job body must not strand the claim: the poll
    thread would die while the heartbeat keeps the process row fresh, so the claim would be
    neither finalized nor recovered (the Q-F2 zombie-worker regression)."""
    exit_job.enqueue()

    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 1
    assert count(schema.claimed_executions) == 0

    with engine.connect() as conn:
        error = conn.execute(select(schema.failed_executions.c.error)).scalar()
    assert error is not None
    assert "SystemExit" in error


def test_worker_keeps_processing_after_base_exception_job(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    import time

    from firm.queue.worker import Worker

    _SINK.clear()
    exit_job.enqueue()

    worker = Worker(runtime, poll_interval=0.02)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and count(schema.failed_executions) < 1:
            time.sleep(0.02)
        assert count(schema.failed_executions) == 1

        record_job.enqueue(7)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _SINK != [7]:
            time.sleep(0.02)
        assert _SINK == [7]
    finally:
        worker.stop()
