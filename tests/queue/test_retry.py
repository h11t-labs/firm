"""Retry specs — a failing job is re-scheduled until attempts are exhausted, then fails."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine, select

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import dispatcher, schema
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


_ATTEMPTS: list[int] = []


@bq.job(attempts=3, backoff=0.0)
def parity_fail_then_succeed() -> None:
    _ATTEMPTS.append(1)
    if len(_ATTEMPTS) < 2:
        raise ValueError("first attempt fails")


def test_fail_then_succeed_after_retry(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # upstream: jobs_lifecycle_test.rb "enqueue and run jobs that fail and succeed after retrying".
    # Raises on attempt 1, succeeds on attempt 2 -> ends finished, no lingering failed row.
    _ATTEMPTS.clear()
    parity_fail_then_succeed.enqueue()

    # Attempt 1: fails and reschedules (backoff 0 -> due immediately).
    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 0
    assert count(schema.scheduled_executions) == 1

    # Promote the rescheduled execution back to ready, then run attempt 2.
    assert dispatcher.dispatch_once(runtime) == 1
    assert count(schema.ready_executions) == 1
    assert run_ready(runtime) == 1

    assert _ATTEMPTS == [1, 1]
    assert count(schema.failed_executions) == 0
    assert count(schema.scheduled_executions) == 0
    assert count(schema.claimed_executions) == 0

    # preserve_finished_jobs defaults to True -> the job lingers as finished.
    with engine.connect() as conn:
        finished_at = conn.execute(select(schema.jobs.c.finished_at)).scalar()
    assert finished_at is not None
