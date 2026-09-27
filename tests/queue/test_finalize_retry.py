"""A transient database error while recording a job's outcome must not strand its claim.

After ``perform()`` returns or raises, the finalize transaction (delete our claim, stamp
``finished_at`` / schedule a retry / write ``failed_executions``, release the semaphore) is the
only thing standing between "the body ran" and "the queue knows it ran". If that transaction
fails with a transient error — a PostgreSQL failover at COMMIT, a MySQL lock-wait/deadlock
abort, SQLite ``database is locked`` past ``busy_timeout`` — the claim row used to stay owned
by this live, heartbeating process: no recovery sweep touches a live process's claims, so the
job showed as "running" until the process restarted, and then ran again.

Pinned here: a transient error is retried and converges (job finished or failed, slot released,
blocked sibling promoted); an error that outlasts the in-thread retry budget is handed back to
the :class:`~firm.queue.worker.Worker`, which reports it once and re-tries the finalize at the
top of its next poll before claiming new work.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

import firm.queue as bq
from firm._core import process as process_registry
from firm._core.config import Runtime
from firm._core.database import immediate_transaction as _real_immediate_transaction
from firm._core.process import ProcessInfo
from firm.queue import results, schema
from firm.queue.claim import claim_ready
from firm.queue.hooks import HOOKS
from firm.queue.recovery import recover_orphaned_claims
from firm.queue.results import execute_claimed
from firm.queue.worker import Worker

_SINK: list[int] = []


@bq.job(concurrency={"key": lambda x: f"finalize-retry/{x}", "to": 1, "duration": 300})
def finalize_retry_limited(x: int) -> None:
    _SINK.append(x)


@bq.job(
    concurrency={"key": lambda x: f"finalize-retry-failing/{x}", "to": 1, "duration": 300},
    attempts=1,
)
def finalize_retry_failing(x: int) -> None:
    _SINK.append(x)
    raise ValueError("boom")


class _Fault:
    """Make the first ``failures`` finalize transactions fail *at commit time*: the body runs,
    then the transaction rolls back with an ``OperationalError`` (a failover at COMMIT). Later
    calls are untouched, so the error is genuinely transient."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    @contextmanager
    def __call__(self, engine: Engine) -> Iterator[Connection]:
        self.calls += 1
        with _real_immediate_transaction(engine) as conn:
            yield conn
            if self.calls <= self.failures:
                raise OperationalError("COMMIT", None, Exception("simulated failover at commit"))


@pytest.fixture
def fault(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], _Fault]:
    """Inject ``n`` commit-time failures into ``results.immediate_transaction``; no backoff."""
    monkeypatch.setattr(results, "_FINALIZE_BACKOFF_S", (0.0, 0.0, 0.0), raising=False)

    def _inject(n: int) -> _Fault:
        fault = _Fault(n)
        monkeypatch.setattr(results, "immediate_transaction", fault)
        return fault

    return _inject


@pytest.fixture
def live_process(engine: Engine) -> int:
    return process_registry.register(
        engine, ProcessInfo(kind="worker", name="finalize-retry", pid=os.getpid())
    )


def _claims_owned_by(engine: Engine, job_id: int, pid: int) -> int:
    with engine.connect() as conn:
        return len(
            conn.execute(
                select(schema.claimed_executions.c.id).where(
                    schema.claimed_executions.c.job_id == job_id,
                    schema.claimed_executions.c.process_id == pid,
                )
            ).all()
        )


def _finished_at(engine: Engine, job_id: int):
    with engine.connect() as conn:
        return conn.execute(
            select(schema.jobs.c.finished_at).where(schema.jobs.c.id == job_id)
        ).scalar()


def _ready_job_ids(engine: Engine) -> list[int]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(select(schema.ready_executions.c.job_id)).all()]


def test_transient_finalize_error_after_success_is_retried(
    runtime: Runtime,
    engine: Engine,
    count: Callable[..., int],
    fault: Callable[[int], _Fault],
    live_process: int,
) -> None:
    _SINK.clear()
    first = finalize_retry_limited.enqueue(1)
    second = finalize_retry_limited.enqueue(1)  # same key, limit 1 -> blocked behind `first`
    assert first is not None and second is not None
    assert count(schema.blocked_executions) == 1
    assert claim_ready(engine, runtime.dialect, ["*"], 5, live_process) == [first]

    injected = fault(1)
    assert execute_claimed(runtime, first, live_process) is True

    assert _SINK == [1], "job body must run exactly once"
    assert injected.calls == 2, "one failed finalize, one retry"
    assert _claims_owned_by(engine, first, live_process) == 0
    assert _finished_at(engine, first) is not None
    # The slot was released and the blocked sibling promoted.
    assert count(schema.blocked_executions) == 0
    assert _ready_job_ids(engine) == [second]


def test_transient_finalize_error_after_failure_is_retried(
    runtime: Runtime,
    engine: Engine,
    count: Callable[..., int],
    fault: Callable[[int], _Fault],
    live_process: int,
) -> None:
    _SINK.clear()
    first = finalize_retry_failing.enqueue(7)
    second = finalize_retry_failing.enqueue(7)
    assert first is not None and second is not None
    assert count(schema.blocked_executions) == 1
    assert claim_ready(engine, runtime.dialect, ["*"], 5, live_process) == [first]

    injected = fault(1)
    assert execute_claimed(runtime, first, live_process) is False

    assert _SINK == [7], "job body must run exactly once"
    assert injected.calls == 2
    assert _claims_owned_by(engine, first, live_process) == 0
    assert count(schema.failed_executions) == 1
    assert count(schema.blocked_executions) == 0
    assert _ready_job_ids(engine) == [second]


def test_finalize_error_past_retry_budget_is_retried_by_worker_before_claiming(
    runtime: Runtime,
    engine: Engine,
    count: Callable[..., int],
    fault: Callable[[int], _Fault],
    live_process: int,
) -> None:
    """An outage longer than the in-thread retry budget hands the finalize to the Worker: the
    claim stays ours (nobody else may run the job), the error is reported once through the
    thread_error hook, and the next poll records the outcome before claiming new work — so the
    claim never sits under a live process for the rest of its lifetime."""
    from firm.queue.results import FinalizeError

    _SINK.clear()
    errors: list[BaseException] = []
    HOOKS.register_error(errors.append)
    first = finalize_retry_limited.enqueue(3)
    second = finalize_retry_limited.enqueue(3)
    assert first is not None and second is not None

    budget = len(results._FINALIZE_BACKOFF_S) + 1
    injected = fault(budget + 2)  # still failing after the whole in-thread budget
    worker = Worker(runtime, threads=1, poll_interval=0.01, process_id=live_process)
    worker.on_start()
    try:
        assert worker.poll() == 1
        assert _SINK == [3]
        assert injected.calls == budget, "the whole in-thread budget was spent"
        # Not recovered elsewhere, not finalized: still ours, and reported exactly once.
        assert _claims_owned_by(engine, first, live_process) == 1
        assert recover_orphaned_claims(runtime) == 0
        assert _finished_at(engine, first) is None
        assert [type(e) for e in errors] == [FinalizeError]
        assert f"job {first}" in str(errors[0])
        assert isinstance(errors[0].__cause__, OperationalError)

        # The database is back: the pending finalize lands *before* this poll claims, so the
        # freed slot lets `second` through in time to be claimed and run by the same poll. Two
        # body runs in total means `first` was not run a second time.
        injected.failures = 0
        worker.poll()
        assert _SINK == [3, 3]
        assert _claims_owned_by(engine, first, live_process) == 0
        assert _finished_at(engine, first) is not None
        assert _finished_at(engine, second) is not None
        assert count(schema.blocked_executions) == 0
        assert count(schema.claimed_executions) == 0
        assert len(errors) == 1, "no further reports once the outcome landed"
    finally:
        worker.on_stop()
        HOOKS.clear()


def test_finalize_error_still_pending_at_stop_gets_a_last_attempt(
    runtime: Runtime,
    engine: Engine,
    fault: Callable[[int], _Fault],
    live_process: int,
) -> None:
    """Stopping a worker with a pending finalize tries it once more before the process
    deregisters, so a blip that cleared during shutdown does not cost a duplicate run."""
    _SINK.clear()
    errors: list[BaseException] = []
    HOOKS.register_error(errors.append)
    job_id = finalize_retry_limited.enqueue(5)
    assert job_id is not None

    budget = len(results._FINALIZE_BACKOFF_S) + 1
    injected = fault(budget)
    worker = Worker(runtime, threads=1, poll_interval=0.01, process_id=live_process)
    worker.on_start()
    try:
        worker.poll()
        assert _claims_owned_by(engine, job_id, live_process) == 1
        assert len(errors) == 1
    finally:
        worker.on_stop()
        HOOKS.clear()

    assert injected.calls == budget + 1
    assert _SINK == [5]
    assert _claims_owned_by(engine, job_id, live_process) == 0
    assert _finished_at(engine, job_id) is not None
