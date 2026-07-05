"""Retry specs — a failing job is re-scheduled until attempts are exhausted, then fails."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine, select

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.worker import run_ready


@bq.job(attempts=1)
def fail_once() -> None:
    raise ValueError("nope")


@bq.job(attempts=3)
def fail_thrice() -> None:
    raise ValueError("nope")


def test_no_retry_records_failed(runtime: Runtime, count: Callable[..., int]) -> None:
    fail_once.enqueue()
    run_ready(runtime)
    assert count(schema.failed_executions) == 1
    assert count(schema.scheduled_executions) == 0


def test_retry_reschedules_and_counts_attempts(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    fail_thrice.enqueue()
    run_ready(runtime)

    assert count(schema.failed_executions) == 0
    assert count(schema.scheduled_executions) == 1
    assert count(schema.ready_executions) == 0

    with engine.connect() as conn:
        attempts = conn.execute(select(schema.jobs.c.attempts)).scalar()
    assert attempts == 1
