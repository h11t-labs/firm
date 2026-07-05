"""Queue management API specs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import Engine, update

import firm.queue as bq
from firm._core.clock import now_utc
from firm._core.config import Runtime
from firm.queue import queues, schema


@bq.job(queue="reports")
def report_job() -> None:
    pass


def test_pause_resume(runtime: Runtime) -> None:
    assert queues.is_paused(runtime, "reports") is False
    queues.pause(runtime, "reports")
    queues.pause(runtime, "reports")  # idempotent
    assert queues.is_paused(runtime, "reports") is True
    queues.resume(runtime, "reports")
    assert queues.is_paused(runtime, "reports") is False


def test_size_and_all_queues(runtime: Runtime) -> None:
    report_job.enqueue()
    report_job.enqueue()
    assert queues.size(runtime, "reports") == 2
    assert "reports" in queues.all_queues(runtime)


def test_clear_discards_ready_jobs(runtime: Runtime, count: Callable[..., int]) -> None:
    report_job.enqueue()
    report_job.enqueue()
    assert queues.clear(runtime, "reports") == 2
    assert queues.size(runtime, "reports") == 0
    assert count(schema.jobs) == 0


def test_latency_reflects_oldest_job(runtime: Runtime, engine: Engine) -> None:
    report_job.enqueue()
    with engine.begin() as conn:
        conn.execute(
            update(schema.ready_executions).values(created_at=now_utc() - timedelta(seconds=60))
        )
    assert queues.latency(runtime, "reports") >= 59
    assert queues.latency(runtime, "empty") == 0.0
