"""Retention + manual-retry specs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import Engine, select, update

import firm.queue as bq
from firm._core.clock import now_utc
from firm._core.config import Runtime
from firm.queue import config, maintenance, schema
from firm.queue.worker import run_ready


@bq.job()
def ok_job() -> None:
    pass


@bq.job(attempts=1)
def boom_job() -> None:
    raise ValueError("x")


@bq.job(concurrency={"limit": 1, "duration": 60})
def limited_job(n: int = 0) -> None:
    pass


def test_clear_finished_removes_preserved_jobs(runtime: Runtime, count: Callable[..., int]) -> None:
    ok_job.enqueue()
    run_ready(runtime)
    assert count(schema.jobs) == 1
    assert maintenance.clear_finished(runtime) == 1
    assert count(schema.jobs) == 0


def test_clear_finished_respects_older_than(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    ok_job.enqueue()
    run_ready(runtime)
    assert maintenance.clear_finished(runtime, older_than=timedelta(hours=1)) == 0
    with engine.begin() as conn:
        conn.execute(update(schema.jobs).values(finished_at=now_utc() - timedelta(hours=2)))
    assert maintenance.clear_finished(runtime, older_than=timedelta(hours=1)) == 1


def test_retry_failed_moves_back_to_ready(runtime: Runtime, count: Callable[..., int]) -> None:
    boom_job.enqueue()
    run_ready(runtime)
    assert count(schema.failed_executions) == 1

    with runtime.engine.connect() as conn:
        job_id = conn.execute(select(schema.failed_executions.c.job_id)).scalar()
    assert maintenance.retry_failed(runtime, job_id) is True
    assert count(schema.failed_executions) == 0
    assert count(schema.ready_executions) == 1


def test_retry_all_failed(runtime: Runtime, count: Callable[..., int]) -> None:
    boom_job.enqueue()
    boom_job.enqueue()
    run_ready(runtime)
    assert count(schema.failed_executions) == 2
    assert maintenance.retry_all_failed(runtime) == 2
    assert count(schema.failed_executions) == 0
    assert count(schema.ready_executions) == 2


def test_discard_job_deletes_job_and_executions(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    job_id = ok_job.enqueue()
    assert maintenance.discard_job(runtime, job_id) is True
    assert count(schema.jobs) == 0
    assert count(schema.ready_executions) == 0
    assert maintenance.discard_job(runtime, job_id) is False  # already gone


def test_discard_job_refuses_claimed(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    from sqlalchemy import insert

    job_id = ok_job.enqueue()
    with engine.begin() as conn:
        conn.execute(insert(schema.claimed_executions).values(job_id=job_id, process_id=1))
    assert maintenance.discard_job(runtime, job_id) is False
    assert count(schema.jobs) == 1


def test_discard_slot_holder_promotes_next_blocked(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    """Discarding the ready job that holds the only concurrency slot hands the slot to the
    next blocked job — no stranding until the semaphore-expiry failsafe."""
    first = limited_job.enqueue(1)  # acquires the slot -> ready
    limited_job.enqueue(2)  # blocked behind it
    assert count(schema.blocked_executions) == 1

    assert maintenance.discard_job(runtime, first) is True
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1  # the second job was promoted
    # The slot moved with the promotion: semaphore capacity is still exhausted.
    with runtime.engine.connect() as conn:
        value = conn.execute(select(schema.semaphores.c.value)).scalar()
    assert value == 0


def test_discard_slot_holder_releases_capacity_when_none_blocked(runtime: Runtime) -> None:
    job_id = limited_job.enqueue(1)
    assert maintenance.discard_job(runtime, job_id) is True
    with runtime.engine.connect() as conn:
        value = conn.execute(select(schema.semaphores.c.value)).scalar()
    assert value == 1  # the held unit was returned


def test_preserve_finished_false_deletes_on_finish(
    db_url: str, engine: Engine, count: Callable[..., int]
) -> None:
    rt = config.configure(database_url=db_url, preserve_finished_jobs=False)
    try:
        ok_job.enqueue()
        run_ready(rt)
        assert count(schema.jobs) == 0
    finally:
        config.set_runtime(None)
        rt.reset()
