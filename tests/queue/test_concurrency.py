"""Concurrency-control specs."""

from __future__ import annotations

from collections.abc import Callable

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.worker import run_ready

_SINK: list[int] = []


@bq.job(concurrency={"key": lambda x: f"k{x}", "to": 1, "duration": 300})
def limited(x: int) -> None:
    _SINK.append(x)


@bq.job(concurrency={"key": lambda x: f"d{x}", "to": 1, "on_conflict": "discard"})
def discardable(x: int) -> None:
    _SINK.append(x)


@bq.job(concurrency={"key": lambda x: f"t{x}", "to": 2})
def throttled(x: int) -> None:
    _SINK.append(x)


def test_first_acquires_second_blocks(runtime: Runtime, count: Callable[..., int]) -> None:
    limited.enqueue(1)
    limited.enqueue(1)
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1
    assert count(schema.semaphores) == 1


def test_different_keys_do_not_block(runtime: Runtime, count: Callable[..., int]) -> None:
    limited.enqueue(1)
    limited.enqueue(2)
    assert count(schema.ready_executions) == 2
    assert count(schema.blocked_executions) == 0


def test_discard_on_conflict(runtime: Runtime, count: Callable[..., int]) -> None:
    first = discardable.enqueue(5)
    second = discardable.enqueue(5)
    assert first is not None
    assert second is None
    assert count(schema.jobs) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0


def test_throttle_limit_two(runtime: Runtime, count: Callable[..., int]) -> None:
    throttled.enqueue(1)
    throttled.enqueue(1)
    throttled.enqueue(1)
    assert count(schema.ready_executions) == 2
    assert count(schema.blocked_executions) == 1


def test_release_promotes_next_blocked(runtime: Runtime, count: Callable[..., int]) -> None:
    _SINK.clear()
    limited.enqueue(7)
    limited.enqueue(7)
    assert count(schema.blocked_executions) == 1

    run_ready(runtime)
    assert _SINK == [7]
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1

    run_ready(runtime)
    assert _SINK == [7, 7]


def test_release_during_block_decision_cannot_strand_the_job(
    runtime: Runtime, engine, count: Callable[..., int]
) -> None:
    """Lost-wakeup regression (Q-F6 / PLAN 2.3+2.5): a release that runs between a failed
    acquire and the blocked-row insert used to see no blocked row and leave the slot free —
    the job then waited for the maintenance pass (10 minutes by default). acquire() now
    holds the semaphore row lock, so the release must land before the re-check (freed slot
    taken) or after the blocked row is visible (job promoted)."""
    import threading
    import time

    from sqlalchemy import insert as sa_insert

    from firm._core.clock import now_utc
    from firm._core.database import immediate_transaction
    from firm.queue import semaphore

    key = "strand-key"
    # Someone else holds the only slot.
    with immediate_transaction(engine) as conn:
        assert semaphore.acquire(conn, key, limit=1, duration_s=60) is True

    with engine.begin() as conn:
        job_id = conn.execute(
            sa_insert(schema.jobs).values(
                queue_name="default", class_name="J", priority=0, concurrency_key=key
            )
        ).inserted_primary_key[0]

    released = threading.Event()

    def _releaser() -> None:
        with runtime.dialect.begin_claim_tx(engine) as conn:
            semaphore.release_and_promote(conn, runtime.dialect, key, limit=1, duration_s=60)
        released.set()

    releaser = threading.Thread(target=_releaser)
    with immediate_transaction(engine) as conn:
        # Failed acquire takes the row lock and decides to block...
        assert semaphore.acquire(conn, key, limit=1, duration_s=60) is False
        # ...while a concurrent release fires. It must not complete before our blocked row
        # commits: pre-fix it slipped through here, promoted nothing, and the job stranded.
        releaser.start()
        time.sleep(0.3)
        assert not released.is_set(), "release completed inside the block-decision window"
        conn.execute(
            sa_insert(schema.blocked_executions).values(
                job_id=job_id,
                queue_name="default",
                priority=0,
                concurrency_key=key,
                expires_at=now_utc(),
            )
        )
    releaser.join(10)
    assert released.is_set()

    # The freed slot went to the blocked job instead of stranding it.
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1
