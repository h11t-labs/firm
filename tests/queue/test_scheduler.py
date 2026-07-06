"""Recurring-task specs — schedule math + (task_key, run_at) dedupe."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.scheduler import RecurringTask, Scheduler

_SINK: list[int] = []


@bq.job()
def recurring_job() -> None:
    _SINK.append(1)


def _task() -> RecurringTask:
    return RecurringTask(key="cleanup", schedule="*/5 * * * *", job=recurring_job)


def test_sync_persists_tasks(runtime: Runtime, count: Callable[..., int]) -> None:
    Scheduler(runtime, [_task()]).sync_tasks()
    assert count(schema.recurring_tasks) == 1


def test_tick_requires_recorded_task(runtime: Runtime, count: Callable[..., int]) -> None:
    """Upstream: recurring_task_test.rb::"error when enqueuing the job before the task has been
    recorded". A scheduler that has not synced enqueues nothing; after sync the same tick fires."""
    scheduler = Scheduler(runtime, [_task()])
    assert count(schema.recurring_tasks) == 0
    assert scheduler.tick(at=datetime(2026, 6, 28, 12, 3, 0)) == 0
    assert count(schema.ready_executions) == 0

    scheduler.sync_tasks()
    assert scheduler.tick(at=datetime(2026, 6, 28, 12, 3, 0)) == 1
    assert count(schema.ready_executions) == 1


def test_tick_enqueues_once_per_period(runtime: Runtime, count: Callable[..., int]) -> None:
    scheduler = Scheduler(runtime, [_task()])
    scheduler.sync_tasks()

    assert scheduler.tick(at=datetime(2026, 6, 28, 12, 3, 0)) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.recurring_executions) == 1

    assert scheduler.tick(at=datetime(2026, 6, 28, 12, 4, 30)) == 0
    assert count(schema.ready_executions) == 1

    assert scheduler.tick(at=datetime(2026, 6, 28, 12, 6, 0)) == 1
    assert count(schema.ready_executions) == 2
    assert count(schema.recurring_executions) == 2


def test_dedupe_across_two_schedulers(runtime: Runtime, count: Callable[..., int]) -> None:
    task = _task()
    first = Scheduler(runtime, [task])
    second = Scheduler(runtime, [task])
    first.sync_tasks()  # record the shared task so either scheduler may enqueue it
    at = datetime(2026, 6, 28, 12, 3, 0)

    assert first.tick(at=at) == 1
    assert second.tick(at=at) == 0
    assert count(schema.recurring_executions) == 1
