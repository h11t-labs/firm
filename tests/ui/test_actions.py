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


def test_trim_channel_honors_retention_override(runtime, seed) -> None:
    """UL-1: the trim button silently used the 1-day default even when the app runs a longer
    Channel(message_retention=...). The action now accepts the operator's retention."""
    from datetime import timedelta

    from sqlalchemy import update

    from firm._core.clock import now_utc
    from firm.channel import schema as channel_schema
    from firm.ui import actions

    seed.channel_message(channel=b"room", payload=b"old")
    seed.channel_message(channel=b"room", payload=b"fresh")
    with runtime.engine.begin() as conn:
        conn.execute(
            update(channel_schema.messages)
            .where(channel_schema.messages.c.payload == b"old")
            .values(created_at=now_utc() - timedelta(hours=2))
        )

    # 7-day retention: the 2-hour-old message must survive (the old code deleted it).
    assert actions.trim_channel(runtime.engine, retention=7 * 24 * 3600.0) == 0
    # 1-hour retention: now it goes.
    assert actions.trim_channel(runtime.engine, retention=3600.0) == 1
