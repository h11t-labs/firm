"""Concurrency-control specs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import Engine, update

import firm.queue as bq
from firm._core.clock import now_utc
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.claim import claim_ready
from firm.queue.dispatcher import dispatch_once, run_maintenance
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


# Jobs whose bodies append to _SINK so tests can assert run ordering. Two DIFFERENT jobs that
# share a concurrency "group" collide on the same key for identical args.
@bq.job(concurrency={"key": lambda rid: f"rec/{rid}", "to": 1, "on_conflict": "discard"})
def discard_on_record(rid: int) -> None:
    _SINK.append(("discard", rid))


@bq.job(concurrency={"key": lambda rid: f"block/{rid}", "to": 1})
def block_on_record(rid: int) -> None:
    _SINK.append(("block", rid))


@bq.job(concurrency={"group": "grp", "key": lambda rid: f"{rid}", "to": 1})
def group_a(rid: int) -> None:
    _SINK.append(("a", rid))


@bq.job(concurrency={"group": "grp", "key": lambda rid: f"{rid}", "to": 1})
def group_b(rid: int) -> None:
    _SINK.append(("b", rid))


@bq.job(
    concurrency={"group": "dgrp", "key": lambda rid: f"{rid}", "to": 1, "on_conflict": "discard"}
)
def dgroup_a(rid: int) -> None:
    _SINK.append(("da", rid))


@bq.job(
    concurrency={"group": "dgrp", "key": lambda rid: f"{rid}", "to": 1, "on_conflict": "discard"}
)
def dgroup_b(rid: int) -> None:
    _SINK.append(("db", rid))


@bq.job(concurrency={"key": lambda rid: f"flaky/{rid}", "to": 1}, attempts=1)
def flaky_on_record(rid: int) -> None:
    _SINK.append(("flaky", rid))
    raise ValueError("boom")


def _make_scheduled_due(engine: Engine) -> None:
    """Backdate every scheduled execution so the dispatcher treats it as due."""
    with engine.begin() as conn:
        conn.execute(
            update(schema.scheduled_executions).values(
                scheduled_at=now_utc() - timedelta(seconds=1)
            )
        )


def test_scheduled_job_discarded_on_conflict_when_due(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # upstream: job_test.rb "enqueue scheduled job with concurrency controls and on_conflict set
    # to discard". An immediate job holds the key; a future job with the SAME key becomes due --
    # on dispatch it must be discarded, not blocked.
    _SINK.clear()
    first = discard_on_record.enqueue(1)  # immediate A acquires the discard key
    assert first is not None
    assert count(schema.ready_executions) == 1
    assert count(schema.semaphores) == 1

    second = discard_on_record.enqueue_in(timedelta(hours=1), 1)  # future B, SAME key
    assert second is not None
    assert count(schema.scheduled_executions) == 1

    _make_scheduled_due(engine)
    dispatch_once(runtime)

    # B is discarded: its job row gone, nothing blocked, no new ready row.
    assert count(schema.scheduled_executions) == 0
    assert count(schema.blocked_executions) == 0
    assert count(schema.jobs) == 1
    assert count(schema.ready_executions) == 1


def test_discard_on_conflict_then_semaphore_released(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    # upstream: concurrency_controls_test.rb "discard on conflict and release semaphore". After a
    # discard, the key's semaphore is released once the holder finishes, so a later same-key job
    # can acquire and run.
    _SINK.clear()
    first = discard_on_record.enqueue(7)
    second = discard_on_record.enqueue(7)  # same key -> discarded
    assert first is not None
    assert second is None
    assert count(schema.ready_executions) == 1
    assert count(schema.semaphores) == 1

    assert run_ready(runtime) == 1  # holder runs; success releases the semaphore
    assert _SINK == [("discard", 7)]
    assert count(schema.ready_executions) == 0

    third = discard_on_record.enqueue(7)  # slot is free now
    assert third is not None
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0
    assert run_ready(runtime) == 1
    assert _SINK == [("discard", 7), ("discard", 7)]


def test_same_group_second_job_blocks(runtime: Runtime, count: Callable[..., int]) -> None:
    # upstream: job_test.rb "block jobs in the same concurrency group when concurrency limits are
    # reached". Two DIFFERENT jobs sharing a group contend on one key; the second blocks.
    _SINK.clear()
    group_a.enqueue(1)  # acquires grp/1
    group_b.enqueue(1)  # same shared key -> blocked

    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1
    assert count(schema.semaphores) == 1

    assert run_ready(runtime) == 1  # draining the holder promotes the blocked member
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1
    assert run_ready(runtime) == 1
    assert _SINK == [("a", 1), ("b", 1)]


def test_same_group_discard_skips_second_job(runtime: Runtime, count: Callable[..., int]) -> None:
    # upstream: job_test.rb "skips jobs with on_conflict set to discard in the same concurrency
    # group". Same shared-group setup but discarding: the second is dropped (enqueue -> None).
    _SINK.clear()
    first = dgroup_a.enqueue(2)  # acquires dgrp/2
    second = dgroup_b.enqueue(2)  # same shared key, discard -> None

    assert first is not None
    assert second is None
    assert count(schema.jobs) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0


def test_several_scheduled_same_key_drain_sequentially(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # upstream: concurrency_controls_test.rb "schedule several conflicting jobs over the same
    # record sequentially". Several scheduled same-key jobs drain one-at-a-time.
    _SINK.clear()
    for _ in range(3):
        assert block_on_record.enqueue_in(timedelta(hours=1), 9) is not None
    assert count(schema.scheduled_executions) == 3

    _make_scheduled_due(engine)
    dispatch_once(runtime)  # all become due; only one acquires, the other two block
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 2

    assert run_ready(runtime) == 1  # each release promotes exactly the next blocked one
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1

    assert run_ready(runtime) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0

    assert run_ready(runtime) == 1
    assert count(schema.ready_executions) == 0
    assert count(schema.blocked_executions) == 0
    assert _SINK == [("block", 9), ("block", 9), ("block", 9)]


def test_failing_job_still_releases_semaphore(runtime: Runtime, count: Callable[..., int]) -> None:
    # upstream: concurrency_controls_test.rb "run several jobs over the same record sequentially,
    # with some failing". A concurrency-limited job that raises still releases its semaphore.
    _SINK.clear()
    flaky_on_record.enqueue(3)  # acquires flaky/3, will raise on run
    flaky_on_record.enqueue(3)  # same key -> blocked

    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1

    assert run_ready(runtime) == 1  # first run raises; failure path still releases + promotes
    assert count(schema.failed_executions) == 1
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1

    assert run_ready(runtime) == 1  # promoted job runs (and also fails)
    assert count(schema.failed_executions) == 2
    assert count(schema.blocked_executions) == 0
    assert _SINK == [("flaky", 3), ("flaky", 3)]


def test_recovered_claim_returns_to_ready_not_blocked(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    # upstream: concurrency_controls_test.rb "don't block claimed executions that get released". A
    # claim returned to ready by crash recovery must NOT be re-blocked by its own concurrency key
    # (it already holds the semaphore it acquired at enqueue time).
    from firm.queue.recovery import recover_orphaned_claims

    _SINK.clear()
    job_id = block_on_record.enqueue(11)  # acquires block/11, goes ready
    assert job_id is not None
    assert count(schema.ready_executions) == 1
    assert count(schema.semaphores) == 1

    # A worker claims it, then "dies": claim it under a process id that has no live row.
    claimed = claim_ready(runtime.engine, runtime.dialect, ["*"], 10, process_id=424242)
    assert claimed == [job_id]
    assert count(schema.claimed_executions) == 1

    assert recover_orphaned_claims(runtime, [424242]) == 1
    assert count(schema.claimed_executions) == 0
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1

    assert run_ready(runtime) == 1
    assert _SINK == [("block", 11)]
    assert count(schema.ready_executions) == 0
    assert run_maintenance(runtime) == 0  # recovery did not double-promote via maintenance
