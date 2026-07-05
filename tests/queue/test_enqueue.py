"""Enqueue routing specs (immediate -> ready, future -> scheduled)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import Engine, select

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema


@bq.job(queue="mailers", priority=5)
def sample_job(x: int, y: int = 0) -> int:
    return x + y


def test_enqueue_immediate_creates_job_and_ready(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    sample_job.enqueue(1, y=2)
    assert count(schema.jobs) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.scheduled_executions) == 0


def test_enqueued_job_records_queue_and_priority(runtime: Runtime, engine: Engine) -> None:
    sample_job.enqueue(1)
    with engine.connect() as conn:
        row = conn.execute(select(schema.jobs)).one()
    assert row.queue_name == "mailers"
    assert row.priority == 5
    assert row.class_name.endswith("sample_job")


def test_enqueue_in_future_creates_scheduled(runtime: Runtime, count: Callable[..., int]) -> None:
    sample_job.enqueue_in(timedelta(hours=1), 5)
    assert count(schema.ready_executions) == 0
    assert count(schema.scheduled_executions) == 1


def test_scheduled_at_is_filled_for_immediate(runtime: Runtime, engine: Engine) -> None:
    sample_job.enqueue(1)
    with engine.connect() as conn:
        scheduled_at = conn.execute(select(schema.jobs.c.scheduled_at)).scalar()
    assert scheduled_at is not None


def test_enqueue_at_accepts_timezone_aware_datetimes(runtime, count) -> None:
    """QL-1: docs say naive UTC, but the idiomatic datetime.now(UTC) + timedelta must
    schedule correctly instead of raising TypeError on aware-vs-naive comparison."""
    from datetime import UTC, datetime, timedelta

    import firm.queue as bq
    from firm.queue import schema

    @bq.job()
    def aware_job() -> None:
        pass

    aware_job.enqueue_at(datetime.now(UTC) + timedelta(hours=1))
    assert count(schema.scheduled_executions) == 1
    assert count(schema.ready_executions) == 0

    # An aware time in the past behaves like any past time: ready immediately.
    aware_job.enqueue_at(datetime.now(UTC) - timedelta(hours=1))
    assert count(schema.ready_executions) == 1
