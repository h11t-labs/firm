"""Parity tests ported from rails/solid_queue covering concurrency-control edge cases.

These mirror behaviours exercised in solid_queue's ``test/models/solid_queue/job_test.rb`` and
``test/integration/concurrency_controls_test.rb``. Each test cites the upstream example it adapts.

This is a TEST-PORTING step: the goal is for every test to *run* against the real firm API. A
red assertion here means a genuine behaviour gap in the source (notably the dispatcher's failure
to honour ``on_conflict="discard"`` when promoting a scheduled job) — that is intentional and is
left failing rather than papered over.
"""

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

# ---------------------------------------------------------------------------
# Job definitions. Bodies append to a shared sink so tests can assert ordering.
# ---------------------------------------------------------------------------

_SINK: list[object] = []


@bq.job(concurrency={"key": lambda rid: f"rec/{rid}", "to": 1, "on_conflict": "discard"})
def discard_on_record(rid: int) -> None:
    _SINK.append(("discard", rid))


@bq.job(concurrency={"key": lambda rid: f"block/{rid}", "to": 1})
def block_on_record(rid: int) -> None:
    _SINK.append(("block", rid))


# Two DIFFERENT jobs that SHARE a concurrency group ("grp"). The shared key becomes
# ``grp/<variable>`` for both, so identical args collide across the two functions.
@bq.job(concurrency={"group": "grp", "key": lambda rid: f"{rid}", "to": 1})
def group_a(rid: int) -> None:
    _SINK.append(("a", rid))


@bq.job(concurrency={"group": "grp", "key": lambda rid: f"{rid}", "to": 1})
def group_b(rid: int) -> None:
    _SINK.append(("b", rid))


# Same shared-group setup, but discarding on conflict.
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


# A concurrency-limited job that always raises — used to prove a failed run still releases.
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


# ---------------------------------------------------------------------------
# 1. job_test.rb :: "enqueue scheduled job with concurrency controls and on_conflict
#    set to discard". An immediate job holds the key; a future job with the SAME key
#    becomes due — on dispatch it must be DISCARDED, not blocked.
#
#    KNOWN BUG: dispatcher._to_blocked ignores on_conflict, so B is blocked instead of
#    discarded. This assertion is expected to FAIL red, exposing that gap.
# ---------------------------------------------------------------------------
def test_scheduled_job_discarded_on_conflict_when_due(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    _SINK.clear()
    # Immediate A acquires the discard key.
    first = discard_on_record.enqueue(1)
    assert first is not None
    assert count(schema.ready_executions) == 1
    assert count(schema.semaphores) == 1

    # Future B with the SAME key goes to scheduled.
    second = discard_on_record.enqueue_in(timedelta(hours=1), 1)
    assert second is not None
    assert count(schema.scheduled_executions) == 1

    _make_scheduled_due(engine)
    dispatch_once(runtime)

    # B must be discarded: its job row gone, nothing blocked, no new ready row.
    assert count(schema.scheduled_executions) == 0
    assert count(schema.blocked_executions) == 0, (
        "scheduled discard-on-conflict job was blocked, not discarded"
    )
    assert count(schema.jobs) == 1, "discarded scheduled job row should not survive"
    assert count(schema.ready_executions) == 1


# ---------------------------------------------------------------------------
# 2. concurrency_controls_test.rb :: "discard on conflict and release semaphore".
#    After a discard, the key's semaphore is released once the holder finishes, so a
#    later same-key job can acquire and run.
# ---------------------------------------------------------------------------
def test_discard_on_conflict_then_semaphore_released(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    _SINK.clear()
    first = discard_on_record.enqueue(7)
    second = discard_on_record.enqueue(7)  # same key -> discarded
    assert first is not None
    assert second is None
    assert count(schema.ready_executions) == 1
    assert count(schema.semaphores) == 1

    # Run the holder: success releases the semaphore (and promotes nothing, nothing blocked).
    assert run_ready(runtime) == 1
    assert _SINK == [("discard", 7)]
    assert count(schema.ready_executions) == 0

    # A later same-key job must be able to acquire and run now the slot is free.
    third = discard_on_record.enqueue(7)
    assert third is not None
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0
    assert run_ready(runtime) == 1
    assert _SINK == [("discard", 7), ("discard", 7)]


# ---------------------------------------------------------------------------
# 3. job_test.rb :: "block jobs in the same concurrency group when concurrency limits
#    are reached". Two DIFFERENT jobs sharing a group contend on one key; the second
#    blocks behind the first.
# ---------------------------------------------------------------------------
def test_same_group_second_job_blocks(runtime: Runtime, count: Callable[..., int]) -> None:
    _SINK.clear()
    group_a.enqueue(1)  # acquires grp/1
    group_b.enqueue(1)  # same shared key -> blocked

    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1
    assert count(schema.semaphores) == 1

    # Draining the holder promotes the blocked group member.
    assert run_ready(runtime) == 1
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1
    assert run_ready(runtime) == 1
    assert _SINK == [("a", 1), ("b", 1)]


# ---------------------------------------------------------------------------
# 4. job_test.rb :: "skips jobs with on_conflict set to discard in the same concurrency
#    group". Same shared-group setup but discarding: the second is dropped (enqueue -> None).
# ---------------------------------------------------------------------------
def test_same_group_discard_skips_second_job(runtime: Runtime, count: Callable[..., int]) -> None:
    _SINK.clear()
    first = dgroup_a.enqueue(2)  # acquires dgrp/2
    second = dgroup_b.enqueue(2)  # same shared key, discard -> None

    assert first is not None
    assert second is None
    assert count(schema.jobs) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0


# ---------------------------------------------------------------------------
# 5. concurrency_controls_test.rb :: "schedule several conflicting jobs over the same
#    record sequentially". Several scheduled same-key jobs drain one-at-a-time:
#    dispatch -> execute -> release -> the next blocked one is promoted.
# ---------------------------------------------------------------------------
def test_several_scheduled_same_key_drain_sequentially(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    _SINK.clear()
    for _ in range(3):
        assert block_on_record.enqueue_in(timedelta(hours=1), 9) is not None
    assert count(schema.scheduled_executions) == 3

    _make_scheduled_due(engine)
    # All three become due at once; only one acquires, the other two block.
    dispatch_once(runtime)
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 2

    # Drain one job at a time; each release promotes exactly the next blocked one.
    assert run_ready(runtime) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1

    assert run_ready(runtime) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0

    assert run_ready(runtime) == 1
    assert count(schema.ready_executions) == 0
    assert count(schema.blocked_executions) == 0

    # Exactly three ran, never more than one concurrently (proven by the block counts above).
    assert _SINK == [("block", 9), ("block", 9), ("block", 9)]


# ---------------------------------------------------------------------------
# 6. concurrency_controls_test.rb :: "run several jobs over the same record sequentially,
#    with some failing". A concurrency-limited job that RAISES still releases its
#    semaphore, so the next blocked one proceeds (no deadlock).
# ---------------------------------------------------------------------------
def test_failing_job_still_releases_semaphore(runtime: Runtime, count: Callable[..., int]) -> None:
    _SINK.clear()
    flaky_on_record.enqueue(3)  # acquires flaky/3, will raise on run
    flaky_on_record.enqueue(3)  # same key -> blocked

    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1

    # First run raises; failure path must still release + promote the blocked one.
    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 1
    assert count(schema.blocked_executions) == 0, (
        "failed job did not release its semaphore (deadlock)"
    )
    assert count(schema.ready_executions) == 1

    # The promoted job runs (and also fails), with no lingering blocked entry.
    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 2
    assert count(schema.blocked_executions) == 0
    assert _SINK == [("flaky", 3), ("flaky", 3)]


# ---------------------------------------------------------------------------
# 7. concurrency_controls_test.rb :: "don't block claimed executions that get released".
#    A claim returned to ready by crash recovery must NOT be re-blocked by its own
#    concurrency key (it already holds the semaphore it acquired at enqueue time).
# ---------------------------------------------------------------------------
def test_recovered_claim_returns_to_ready_not_blocked(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    _SINK.clear()
    job_id = block_on_record.enqueue(11)  # acquires block/11, goes ready
    assert job_id is not None
    assert count(schema.ready_executions) == 1
    assert count(schema.semaphores) == 1

    # A worker claims it, then "dies": claim it under a process id that has no live row.
    claimed = claim_ready(runtime.engine, runtime.dialect, ["*"], 10, process_id=424242)
    assert claimed == [job_id]
    assert count(schema.ready_executions) == 0
    assert count(schema.claimed_executions) == 1

    # Recovery moves the orphaned claim straight back to ready — NOT to blocked, even
    # though the job's own concurrency key is still held.
    from firm.queue.recovery import recover_orphaned_claims

    assert recover_orphaned_claims(runtime, [424242]) == 1
    assert count(schema.claimed_executions) == 0
    assert count(schema.blocked_executions) == 0, (
        "recovered claim was wrongly blocked by its own key"
    )
    assert count(schema.ready_executions) == 1

    # It can now be picked up and run to completion.
    assert run_ready(runtime) == 1
    assert _SINK == [("block", 11)]
    assert count(schema.ready_executions) == 0
    # Recovery did not double-promote anything via maintenance either.
    assert run_maintenance(runtime) == 0
