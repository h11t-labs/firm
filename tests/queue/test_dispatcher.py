"""Dispatcher specs — due scheduled jobs promote to ready; maintenance is the failsafe."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import Engine, insert, update

import firm.queue as bq
from firm._core.clock import now_utc
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.dispatcher import dispatch_once, run_maintenance
from firm.queue.serialization import serialize


@bq.job(concurrency={"key": lambda: "one", "to": 1})
def limited_future() -> None:
    pass


def _add_scheduled(engine: Engine, scheduled_at, queue: str = "default", priority: int = 0) -> int:
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(
                queue_name=queue, class_name="J", priority=priority, scheduled_at=scheduled_at
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(schema.scheduled_executions).values(
                job_id=job_id, queue_name=queue, priority=priority, scheduled_at=scheduled_at
            )
        )
    return job_id


def test_due_job_promoted_to_ready(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    _add_scheduled(engine, now_utc() - timedelta(minutes=1))
    assert dispatch_once(runtime) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.scheduled_executions) == 0


def test_future_job_not_dispatched(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    _add_scheduled(engine, now_utc() + timedelta(hours=1))
    assert dispatch_once(runtime) == 0
    assert count(schema.scheduled_executions) == 1
    assert count(schema.ready_executions) == 0


def test_batch_size_limits_dispatch(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    for _ in range(5):
        _add_scheduled(engine, now_utc() - timedelta(seconds=1))
    assert dispatch_once(runtime, batch_size=2) == 2
    assert count(schema.ready_executions) == 2
    assert count(schema.scheduled_executions) == 3


def test_dispatch_applies_concurrency(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    limited_future.enqueue_in(timedelta(hours=1))
    limited_future.enqueue_in(timedelta(hours=1))
    assert count(schema.scheduled_executions) == 2

    with engine.begin() as conn:
        conn.execute(
            update(schema.scheduled_executions).values(
                scheduled_at=now_utc() - timedelta(seconds=1)
            )
        )

    dispatch_once(runtime)
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1


def test_maintenance_promotes_expired_blocked(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(queue_name="default", class_name="J", concurrency_key="k/x")
        ).inserted_primary_key[0]
        conn.execute(
            insert(schema.blocked_executions).values(
                job_id=job_id,
                queue_name="default",
                priority=0,
                concurrency_key="k/x",
                expires_at=now_utc() - timedelta(seconds=1),
            )
        )
    assert run_maintenance(runtime) == 1
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1


def test_two_dispatch_passes_do_not_double_promote(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # upstream: dispatcher_test.rb "run more than one instance of the dispatcher". Two dispatch
    # passes over the same due scheduled jobs promote each to ready exactly once.
    for _ in range(4):
        _add_scheduled(engine, now_utc() - timedelta(minutes=1))

    first = dispatch_once(runtime)
    second = dispatch_once(runtime)

    assert first == 4
    assert second == 0
    assert count(schema.ready_executions) == 4
    assert count(schema.scheduled_executions) == 0


def test_dispatch_missing_class_with_concurrency_key_skips_controls(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # upstream: claimed_execution_test.rb "dispatch job with missing class and concurrency key
    # skips concurrency controls". A scheduled job whose class_name is unregistered but which
    # carries a concurrency_key must NOT acquire/leak a semaphore: promote it straight to ready.
    due = now_utc() - timedelta(seconds=1)
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(
                queue_name="default",
                class_name="nope.MissingWithKey",
                arguments=serialize((), {}),
                concurrency_key="MissingWithKey/abc",
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(schema.scheduled_executions).values(
                job_id=job_id,
                queue_name="default",
                priority=0,
                scheduled_at=due,
            )
        )

    assert dispatch_once(runtime) == 1

    # Promoted to ready, not parked as blocked, and no semaphore was created.
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0
    assert count(schema.semaphores) == 0


def test_maintenance_deletes_expired_semaphores(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(schema.semaphores).values(
                key="dead", value=0, expires_at=now_utc() - timedelta(seconds=1)
            )
        )
    run_maintenance(runtime)
    assert count(schema.semaphores) == 0


def test_maintenance_promotes_blocked_when_capacity_is_free(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(queue_name="default", class_name="J", concurrency_key="k/y")
        ).inserted_primary_key[0]
        conn.execute(
            insert(schema.blocked_executions).values(
                job_id=job_id,
                queue_name="default",
                priority=0,
                concurrency_key="k/y",
                expires_at=now_utc() + timedelta(hours=1),
            )
        )
    assert run_maintenance(runtime) == 1
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1
