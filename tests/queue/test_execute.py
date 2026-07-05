"""End-to-end execute specs: enqueue -> claim -> run -> finish/fail."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine, insert, select

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.worker import run_ready

_SINK: list[int] = []


@bq.job()
def record_job(value: int) -> None:
    _SINK.append(value)


@bq.job()
def boom_job() -> None:
    raise ValueError("boom")


def test_execute_success_finishes_job(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    _SINK.clear()
    record_job.enqueue(42)

    assert run_ready(runtime) == 1
    assert _SINK == [42]
    assert count(schema.claimed_executions) == 0
    assert count(schema.failed_executions) == 0

    with engine.connect() as conn:
        finished_at = conn.execute(select(schema.jobs.c.finished_at)).scalar()
    assert finished_at is not None


def test_execute_failure_records_failed_execution(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    boom_job.enqueue()

    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 1
    assert count(schema.claimed_executions) == 0

    with engine.connect() as conn:
        error = conn.execute(select(schema.failed_executions.c.error)).scalar()
    assert error is not None
    assert "ValueError" in error
    assert "boom" in error


def test_unregistered_class_records_failed(runtime: Runtime, count: Callable[..., int]) -> None:
    with runtime.engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(queue_name="default", class_name="nope.NotReal")
        ).inserted_primary_key[0]
        conn.execute(
            insert(schema.ready_executions).values(job_id=job_id, queue_name="default", priority=0)
        )

    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 1


@bq.job()
def exit_job() -> None:
    raise SystemExit(3)


def test_base_exception_from_job_is_finalized_as_failure(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """SystemExit/KeyboardInterrupt from a job body must not strand the claim: the poll
    thread would die while the heartbeat keeps the process row fresh, so the claim would be
    neither finalized nor recovered (the Q-F2 zombie-worker regression)."""
    exit_job.enqueue()

    assert run_ready(runtime) == 1
    assert count(schema.failed_executions) == 1
    assert count(schema.claimed_executions) == 0

    with engine.connect() as conn:
        error = conn.execute(select(schema.failed_executions.c.error)).scalar()
    assert error is not None
    assert "SystemExit" in error


def test_worker_keeps_processing_after_base_exception_job(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    import time

    from firm.queue.worker import Worker

    _SINK.clear()
    exit_job.enqueue()

    worker = Worker(runtime, poll_interval=0.02)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and count(schema.failed_executions) < 1:
            time.sleep(0.02)
        assert count(schema.failed_executions) == 1

        record_job.enqueue(7)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _SINK != [7]:
            time.sleep(0.02)
        assert _SINK == [7]
    finally:
        worker.stop()


def test_zombie_worker_does_not_finalize_reclaimed_job(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """After prune -> recover -> reclaim, a zombie worker (stale heartbeat, still alive)
    finishing its stale copy must not delete the new owner's claim row: if it did and the new
    worker then died mid-run, no recovery pass would ever find the job again (Q-F8)."""
    from firm._core import process as process_registry
    from firm._core.process import ProcessInfo
    from firm.queue.claim import claim_ready
    from firm.queue.recovery import recover_orphaned_claims
    from firm.queue.results import execute_claimed

    _SINK.clear()
    record_job.enqueue(1)
    zombie = process_registry.register(engine, ProcessInfo(kind="Worker", name="zombie", pid=1))
    owner = process_registry.register(engine, ProcessInfo(kind="Worker", name="fresh", pid=2))

    [job_id] = claim_ready(engine, runtime.dialect, ["*"], 5, zombie)
    # The zombie's heartbeat goes stale: it is pruned and its claim recovered + reclaimed.
    process_registry.deregister(engine, zombie)
    assert recover_orphaned_claims(runtime) == 1
    assert claim_ready(engine, runtime.dialect, ["*"], 5, owner) == [job_id]

    # The zombie finishes its stale copy: the new owner's claim must survive untouched.
    assert execute_claimed(runtime, job_id, process_id=zombie) is False
    assert count(schema.claimed_executions) == 1
    with engine.connect() as conn:
        assert conn.execute(select(schema.jobs.c.finished_at)).scalar() is None

    # The real owner finalizes normally.
    assert execute_claimed(runtime, job_id, process_id=owner) is True
    assert count(schema.claimed_executions) == 0
    with engine.connect() as conn:
        assert conn.execute(select(schema.jobs.c.finished_at)).scalar() is not None


def test_vanished_job_row_drops_claim_without_raising(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """If the job row disappears between claim and execution, execute_claimed must clean up
    its claim and return False rather than raising NoResultFound into the worker thread."""
    from sqlalchemy import delete as sa_delete

    from firm.queue.claim import claim_ready
    from firm.queue.results import execute_claimed

    record_job.enqueue(9)
    [job_id] = claim_ready(engine, runtime.dialect, ["*"], 5, None)
    with engine.begin() as conn:
        conn.execute(sa_delete(schema.jobs).where(schema.jobs.c.id == job_id))

    assert execute_claimed(runtime, job_id) is False
    assert count(schema.claimed_executions) == 0


def test_unknown_job_with_concurrency_key_forfeits_its_slot(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """QL-6: an UnknownJob whose claim held a semaphore slot used to strand the key until
    semaphore expiry (~minutes); the slot is now handed to the next blocked job."""
    from sqlalchemy import insert as sa_insert

    from firm._core.clock import now_utc
    from firm._core.database import immediate_transaction
    from firm.queue import semaphore
    from firm.queue.claim import claim_ready
    from firm.queue.results import execute_claimed

    key = "ghost-key"
    with immediate_transaction(engine) as conn:
        assert semaphore.acquire(conn, key, limit=1, duration_s=300) is True

    with engine.begin() as conn:
        unknown_id = conn.execute(
            sa_insert(schema.jobs).values(
                queue_name="default", class_name="nope.Ghost", priority=0, concurrency_key=key
            )
        ).inserted_primary_key[0]
        conn.execute(
            sa_insert(schema.ready_executions).values(
                job_id=unknown_id, queue_name="default", priority=0
            )
        )
        blocked_id = conn.execute(
            sa_insert(schema.jobs).values(
                queue_name="default", class_name="nope.Waiting", priority=0, concurrency_key=key
            )
        ).inserted_primary_key[0]
        conn.execute(
            sa_insert(schema.blocked_executions).values(
                job_id=blocked_id,
                queue_name="default",
                priority=0,
                concurrency_key=key,
                expires_at=now_utc(),
            )
        )

    assert claim_ready(engine, runtime.dialect, ["*"], 5, None) == [unknown_id]
    assert execute_claimed(runtime, unknown_id) is False  # UnknownJob -> failed

    # The slot went to the blocked sibling instead of stranding the key.
    assert count(schema.blocked_executions) == 0
    with engine.connect() as conn:
        ready_job = conn.execute(select(schema.ready_executions.c.job_id)).scalar_one()
    assert ready_job == blocked_id


def test_worker_surfaces_every_infra_failure_in_a_batch(runtime: Runtime, monkeypatch) -> None:
    """QL-4: the sequential future.result() loop aborted on the first infrastructure
    exception, dropping the siblings' errors unretrieved."""
    import time

    from firm.queue import worker as worker_module
    from firm.queue.hooks import HOOKS
    from firm.queue.worker import Worker

    record_job.enqueue(1)
    record_job.enqueue(2)

    def _broken_execute(runtime_, job_id, process_id=None):
        raise RuntimeError(f"infra-{job_id}")

    monkeypatch.setattr(worker_module, "execute_claimed", _broken_execute)
    errors: list[BaseException] = []
    HOOKS.register_error(errors.append)
    worker = Worker(runtime, threads=2, poll_interval=0.02)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(errors) < 2:
            time.sleep(0.02)
    finally:
        worker.stop()
        HOOKS.clear()

    infra = sorted(str(e) for e in errors if str(e).startswith("infra-"))
    assert len(infra) == 2, f"expected both failures surfaced, got {errors!r}"
