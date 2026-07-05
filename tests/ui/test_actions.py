"""Action specs — pause/resume, retry, discard."""

from __future__ import annotations

from sqlalchemy import func, select

from firm.queue import queues, schema
from firm.ui import actions


def test_pause_and_resume(runtime, seed) -> None:
    seed.ready(queue="default")
    actions.pause(runtime, "default")
    assert queues.is_paused(runtime, "default") is True
    actions.resume(runtime, "default")
    assert queues.is_paused(runtime, "default") is False


def test_retry_moves_failed_back_to_ready(runtime, seed) -> None:
    job_id = seed.failed()
    assert actions.retry(runtime, job_id) is True
    with runtime.engine.connect() as conn:
        failed = conn.execute(select(func.count()).select_from(schema.failed_executions)).scalar()
        ready = conn.execute(
            select(func.count())
            .select_from(schema.ready_executions)
            .where(schema.ready_executions.c.job_id == job_id)
        ).scalar()
    assert failed == 0
    assert ready == 1


def test_discard_deletes_the_job(runtime, seed) -> None:
    job_id = seed.failed()
    assert actions.discard(runtime, job_id) is True
    with runtime.engine.connect() as conn:
        remaining = conn.execute(
            select(func.count()).select_from(schema.jobs).where(schema.jobs.c.id == job_id)
        ).scalar()
        failed = conn.execute(select(func.count()).select_from(schema.failed_executions)).scalar()
    assert remaining == 0
    assert failed == 0  # FK cascade removed the failed-execution row too


def test_discard_missing_job_is_false(runtime) -> None:
    assert actions.discard(runtime, 999) is False
