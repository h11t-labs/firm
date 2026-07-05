"""Parity specs ported from rails/solid_queue.

These mirror upstream behaviours that firm's own suite did not yet cover, around
claiming (queue ordering / prefixes / mixed selectors) and the claimed-execution
lifecycle (failed-execution dedup, retry-then-succeed, huge errors, and the
missing-class + concurrency-key edge cases).

Each test cites the upstream Ruby test it is adapted from. Every test runs against
SQLite via the shared fixtures in ``conftest.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

import firm.queue as bq
from firm._core.config import Runtime
from firm._core.dialects import get_dialect
from firm.queue import dispatcher, schema
from firm.queue.claim import claim_ready
from firm.queue.results import execute_claimed
from firm.queue.serialization import serialize
from firm.queue.worker import run_ready


def _claim_all(engine: Engine, queues: list[str], limit: int = 100) -> list[int]:
    return claim_ready(engine, get_dialect(engine), queues, limit, None)


# ---------------------------------------------------------------------------
# 1. ready_execution_test.rb
#    "claim jobs using both exact names and a prefix"
# ---------------------------------------------------------------------------
def test_claim_using_both_exact_names_and_a_prefix(
    engine: Engine, add_ready: Callable[..., int]
) -> None:
    # An exact name AND a "prefix*" selector in one spec list claim from both.
    backend = add_ready(queue="backend")
    mailers_high = add_ready(queue="mailers_high")
    mailers_low = add_ready(queue="mailers_low")
    add_ready(queue="reports")  # not selected

    claimed = _claim_all(engine, ["backend", "mailers*"])

    assert set(claimed) == {backend, mailers_high, mailers_low}


# ---------------------------------------------------------------------------
# 2. ready_execution_test.rb
#    "queue order and then priority is respected when using a list of queues"
# ---------------------------------------------------------------------------
def test_queue_order_then_priority_with_list_of_queues(
    engine: Engine, add_ready: Callable[..., int]
) -> None:
    # Jobs in the earlier-listed queue are claimed before later ones, then by
    # priority within a queue -- even when a later queue holds higher-priority work.
    # queue2 is listed first; queue1 is listed second but its jobs carry the
    # "better" (lower-number == higher) priority. Order-by-queue must win.
    q1_high = add_ready(queue="queue1", priority=1)
    q1_low = add_ready(queue="queue1", priority=5)
    q2_high = add_ready(queue="queue2", priority=1)
    q2_low = add_ready(queue="queue2", priority=5)

    claimed = _claim_all(engine, ["queue2", "queue1"])

    # queue2 first (by priority within), then queue1 (by priority within).
    assert claimed == [q2_high, q2_low, q1_high, q1_low]


# ---------------------------------------------------------------------------
# 3a. ready_execution_test.rb
#     "queue order is respected when using prefixes"
# ---------------------------------------------------------------------------
def test_queue_order_respected_when_using_prefixes(
    engine: Engine, add_ready: Callable[..., int]
) -> None:
    # With prefix selectors, queues are still polled in the order their selectors
    # are listed: "b*" before "a*", so the beta_* jobs precede the alpha_* ones.
    alpha = add_ready(queue="alpha_one", priority=1)
    beta = add_ready(queue="beta_one", priority=5)

    claimed = _claim_all(engine, ["beta*", "alpha*"])

    assert claimed == [beta, alpha]


# ---------------------------------------------------------------------------
# 3b. ready_execution_test.rb
#     "queue order is respected when mixing exact names with prefixes"
# ---------------------------------------------------------------------------
def test_queue_order_respected_when_mixing_names_and_prefixes(
    engine: Engine, add_ready: Callable[..., int]
) -> None:
    # An exact name listed first is polled before a later prefix selector, even
    # when the prefix-matched queue holds higher-priority work.
    exact = add_ready(queue="urgent", priority=5)
    prefixed = add_ready(queue="mailers_high", priority=1)

    claimed = _claim_all(engine, ["urgent", "mailers*"])

    assert claimed == [exact, prefixed]


# ---------------------------------------------------------------------------
# 4. claimed_execution_test.rb
#    "fail with error when a failed execution already exists updates the
#     existing one"
# ---------------------------------------------------------------------------
@bq.job(attempts=1)
def parity_always_fails() -> None:
    raise ValueError("kaboom")


def test_fail_with_existing_failed_execution_updates_not_inserts(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # A job that already has a failed_executions row, failed again, must update the
    # existing row rather than insert a duplicate: the count stays 1. _finalize_failure
    # updates the existing row in place when one exists (the at-least-once duplicate-run
    # case), so the UNIQUE(job_id) index is never violated.
    parity_always_fails.enqueue()
    run_ready(runtime)
    assert count(schema.failed_executions) == 1

    with engine.connect() as conn:
        job_id = conn.execute(select(schema.failed_executions.c.job_id)).scalar()

    # Re-claim the same job (simulate it being picked up and failing once more) and
    # run it through the failure path a second time.
    with engine.begin() as conn:
        conn.execute(insert(schema.claimed_executions).values(job_id=job_id, process_id=None))

    raised: Exception | None = None
    try:
        execute_claimed(runtime, job_id)
    except IntegrityError as exc:  # firm's current (buggy) behaviour
        raised = exc

    # solid_queue updates the existing failed_executions row in place: no second
    # row, and no constraint violation.
    assert raised is None, (
        "failing an already-failed job should update the existing "
        f"failed_executions row, but firm raised: {raised!r}"
    )
    assert count(schema.failed_executions) == 1


# ---------------------------------------------------------------------------
# 5. jobs_lifecycle_test.rb
#    "enqueue and run jobs that fail and succeed after retrying"
# ---------------------------------------------------------------------------
_ATTEMPTS: list[int] = []


@bq.job(attempts=3, backoff=0.0)
def parity_fail_then_succeed() -> None:
    _ATTEMPTS.append(1)
    if len(_ATTEMPTS) < 2:
        raise ValueError("first attempt fails")


def test_fail_then_succeed_after_retry(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # Raises on attempt 1, succeeds on attempt 2 -> ends finished, no lingering
    # failed_executions row.
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


# ---------------------------------------------------------------------------
# 6. failed_execution_test.rb
#    "run job that fails with a SystemStackError (stack level too deep)"
# ---------------------------------------------------------------------------
@bq.job(attempts=1)
def parity_recurses_to_death() -> None:
    def _recurse(n: int) -> int:
        return _recurse(n + 1)

    _recurse(0)


def test_run_job_that_fails_with_recursion_error(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # A job raising a deep recursion error (Python's analogue of Ruby's
    # SystemStackError) is recorded as failed without blowing up serialization.
    parity_recurses_to_death.enqueue()

    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 1
    assert count(schema.claimed_executions) == 0

    with engine.connect() as conn:
        error = conn.execute(select(schema.failed_executions.c.error)).scalar()
    assert error is not None
    assert "RecursionError" in error


# ---------------------------------------------------------------------------
# 7. claimed_execution_test.rb
#    "dispatch job with missing class and concurrency key skips concurrency
#     controls"
# ---------------------------------------------------------------------------
def test_dispatch_missing_class_with_concurrency_key_skips_controls(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # A scheduled job whose class_name is unregistered but which carries a
    # concurrency_key must NOT acquire/leak a semaphore: the dispatcher skips
    # concurrency controls and promotes it straight to ready.
    from datetime import timedelta

    from firm._core.clock import now_utc

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

    assert dispatcher.dispatch_once(runtime) == 1

    # Promoted to ready, not parked as blocked, and no semaphore was created.
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0
    assert count(schema.semaphores) == 0


# ---------------------------------------------------------------------------
# 8. claimed_execution_test.rb
#    "perform concurrency-controlled job with missing class fails gracefully"
# ---------------------------------------------------------------------------
def test_perform_missing_class_with_concurrency_key_fails_gracefully(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # An unregistered class on the perform path -- even with a concurrency_key set
    # on the job row -- is recorded as failed (mirrors test_execute.py's
    # unregistered-class test) without deadlocking or leaking the semaphore.
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(
                queue_name="default",
                class_name="nope.MissingPerform",
                arguments=serialize((), {}),
                concurrency_key="MissingPerform/abc",
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(schema.ready_executions).values(job_id=job_id, queue_name="default", priority=0)
        )

    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 1
    assert count(schema.claimed_executions) == 0
    # The missing-class failure path must not have touched the semaphore table.
    assert count(schema.semaphores) == 0


# Guard: the recursion test relies on Python actually raising RecursionError.
assert sys.getrecursionlimit() > 0
