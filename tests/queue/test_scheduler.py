"""Recurring-task specs — schedule math + (task_key, run_at) dedupe."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest
from sqlalchemy import Engine, select

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.scheduler import RecurringTask, Scheduler
from firm.queue.worker import run_ready

_SINK: list[int] = []


@bq.job()
def recurring_job() -> None:
    _SINK.append(1)


@bq.job(concurrency={"to": 1, "duration": 300})
def limited_recurring_job() -> None:
    _SINK.append(3)


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


@bq.job(queue="reports", priority=8)
def custom_queue_job() -> None:
    _SINK.append(2)


def test_valid_and_invalid_schedules(runtime: Runtime, count: Callable[..., int]) -> None:
    # upstream: recurring_task_test.rb "valid and invalid schedules". A valid 5-field cron is
    # accepted and fires; an invalid cron is rejected when the task computes its period
    # (croniter raises CroniterBadCronError, a ValueError subclass).
    valid = Scheduler(runtime, [_task()])
    valid.sync_tasks()
    assert valid.tick(at=datetime(2026, 6, 28, 12, 3, 0)) == 1
    assert count(schema.ready_executions) == 1

    invalid = RecurringTask(key="bad", schedule="not a cron", job=recurring_job)
    with pytest.raises(ValueError):
        invalid.current_period(datetime(2026, 6, 28, 12, 3, 0))
    with pytest.raises(ValueError):
        Scheduler(runtime, [invalid]).tick(at=datetime(2026, 6, 28, 12, 3, 0))


def test_recurring_task_custom_queue_and_priority(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # upstream: recurring_task_test.rb "task with custom queue and priority". A recurring run
    # enqueues onto the job's configured queue_name/priority -- both on the persisted jobs row
    # and on the ready_executions row the worker polls.
    task = RecurringTask(key="report", schedule="*/5 * * * *", job=custom_queue_job)
    scheduler = Scheduler(runtime, [task])
    scheduler.sync_tasks()

    assert scheduler.tick(at=datetime(2026, 6, 28, 12, 3, 0)) == 1

    with engine.connect() as conn:
        job_row = conn.execute(select(schema.jobs.c.queue_name, schema.jobs.c.priority)).one()
        ready_row = conn.execute(
            select(schema.ready_executions.c.queue_name, schema.ready_executions.c.priority)
        ).one()

    assert job_row.queue_name == "reports"
    assert job_row.priority == 8
    assert ready_row.queue_name == "reports"
    assert ready_row.priority == 8


def test_recurring_task_honors_concurrency(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    # A recurring job with a concurrency limit must not run unbounded: while the first fire
    # holds the only slot, the next period's fire lands in blocked_executions (not ready), and
    # is promoted once the slot frees. Matches solid_queue, where recurring tasks honor
    # concurrency controls.
    _SINK.clear()
    task = RecurringTask(key="limited", schedule="*/5 * * * *", job=limited_recurring_job)
    scheduler = Scheduler(runtime, [task])
    scheduler.sync_tasks()

    assert scheduler.tick(at=datetime(2026, 6, 28, 12, 3, 0)) == 1  # acquires the slot -> ready
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0
    with runtime.engine.connect() as conn:
        assert conn.execute(select(schema.jobs.c.concurrency_key)).scalar() is not None

    # Next period fires while the first still holds the slot -> blocked, not ready.
    assert scheduler.tick(at=datetime(2026, 6, 28, 12, 8, 0)) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1

    # Draining the holder releases the slot and promotes the blocked fire.
    assert run_ready(runtime) == 1
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1
    assert run_ready(runtime) == 1
    assert _SINK == [3, 3]


def test_sync_updates_changed_task(runtime: Runtime, count: Callable[..., int]) -> None:
    # sync_tasks upserts: a changed schedule for an existing key updates the stored row (a
    # documented read surface for the dashboard/CLI), rather than leaving it stale.
    Scheduler(runtime, [_task()]).sync_tasks()
    with runtime.engine.connect() as conn:
        before = conn.execute(
            select(schema.recurring_tasks.c.schedule, schema.recurring_tasks.c.class_name)
        ).one()
    assert before.schedule == "*/5 * * * *"

    changed = RecurringTask(key="cleanup", schedule="0 9 * * *", job=recurring_job)
    Scheduler(runtime, [changed]).sync_tasks()

    assert count(schema.recurring_tasks) == 1  # updated in place, not duplicated
    with runtime.engine.connect() as conn:
        after = conn.execute(
            select(schema.recurring_tasks.c.schedule, schema.recurring_tasks.c.class_name)
        ).one()
    assert after.schedule == "0 9 * * *"  # mutable column written
    assert after.class_name == before.class_name  # unchanged column untouched


def test_sync_persists_and_deletes_configured_tasks(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    # upstream: recurring_tasks_test.rb "persist and delete configured tasks". Syncing persists
    # the configured tasks AND deletes a previously-persisted task no longer in the config.
    stale = RecurringTask(key="stale", schedule="0 * * * *", job=recurring_job)
    Scheduler(runtime, [_task(), stale]).sync_tasks()
    assert count(schema.recurring_tasks) == 2

    Scheduler(runtime, [_task()]).sync_tasks()  # re-sync with only "cleanup"
    assert count(schema.recurring_tasks) == 1
    with runtime.engine.connect() as conn:
        keys = {row[0] for row in conn.execute(select(schema.recurring_tasks.c.key))}
    assert keys == {"cleanup"}
