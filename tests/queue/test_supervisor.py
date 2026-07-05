"""Thread-mode supervisor end-to-end: enqueue -> dispatch -> worker -> finished."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import Engine, func, select

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.supervisor import (
    DispatcherConfig,
    SupervisorConfig,
    ThreadSupervisor,
    WorkerConfig,
)

_SINK: list[int] = []


@bq.job()
def e2e_job(x: int) -> None:
    _SINK.append(x)


def _finished_jobs(engine: Engine) -> int:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(func.count())
                .select_from(schema.jobs)
                .where(schema.jobs.c.finished_at.is_not(None))
            ).scalar()
            or 0
        )


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_thread_supervisor_runs_immediate_and_scheduled(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    _SINK.clear()
    config = SupervisorConfig(
        workers=[WorkerConfig(poll_interval=0.02)],
        dispatchers=[DispatcherConfig(poll_interval=0.05)],
    )

    e2e_job.enqueue(1)
    e2e_job.enqueue_in(timedelta(seconds=0.1), 2)

    with ThreadSupervisor(runtime, config):
        assert _wait_until(lambda: _finished_jobs(engine) == 2)

    assert sorted(_SINK) == [1, 2]
    assert count(schema.claimed_executions) == 0
    assert count(schema.scheduled_executions) == 0
    assert count(schema.processes) == 0
