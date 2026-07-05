"""Queue management API specs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import Engine, update

import firm.queue as bq
from firm._core.clock import now_utc
from firm._core.config import Runtime
from firm.queue import queues, schema


@bq.job(queue="reports")
def report_job() -> None:
    pass


def test_pause_resume(runtime: Runtime) -> None:
    assert queues.is_paused(runtime, "reports") is False
    queues.pause(runtime, "reports")
    queues.pause(runtime, "reports")  # idempotent
    assert queues.is_paused(runtime, "reports") is True
    queues.resume(runtime, "reports")
    assert queues.is_paused(runtime, "reports") is False


def test_size_and_all_queues(runtime: Runtime) -> None:
    report_job.enqueue()
    report_job.enqueue()
    assert queues.size(runtime, "reports") == 2
    assert "reports" in queues.all_queues(runtime)


def test_clear_discards_ready_jobs(runtime: Runtime, count: Callable[..., int]) -> None:
    report_job.enqueue()
    report_job.enqueue()
    assert queues.clear(runtime, "reports") == 2
    assert queues.size(runtime, "reports") == 0
    assert count(schema.jobs) == 0


def test_latency_reflects_oldest_job(runtime: Runtime, engine: Engine) -> None:
    report_job.enqueue()
    with engine.begin() as conn:
        conn.execute(
            update(schema.ready_executions).values(created_at=now_utc() - timedelta(seconds=60))
        )
    assert queues.latency(runtime, "reports") >= 59
    assert queues.latency(runtime, "empty") == 0.0


def test_clear_skips_job_being_claimed_concurrently(runtime, engine, add_ready, count) -> None:
    """Q-F7 companion: clear() used to select ready job ids in a plain transaction and then
    cascade-delete the jobs — racing a claim could delete a running job's claim row. The
    skip-locked delete-ready-first version must leave the in-flight job alone."""
    import threading
    import time as _time

    from sqlalchemy import delete as sa_delete
    from sqlalchemy import insert as sa_insert
    from sqlalchemy import select as sa_select

    from firm._core.clock import now_utc
    from firm.queue import schema

    being_claimed = add_ready()
    add_ready()  # a second, genuinely-ready job: the one clear() should discard
    cleared: dict[str, int] = {}
    done = threading.Event()

    def _clearer() -> None:
        cleared["count"] = queues.clear(runtime, "default")
        done.set()

    clearer = threading.Thread(target=_clearer)
    with runtime.dialect.begin_claim_tx(engine) as conn:
        picked = conn.execute(
            runtime.dialect.with_skip_locked(
                sa_select(schema.ready_executions.c.id).where(
                    schema.ready_executions.c.job_id == being_claimed
                )
            )
        ).one()
        conn.execute(
            sa_insert(schema.claimed_executions).values(job_id=being_claimed, created_at=now_utc())
        )
        conn.execute(
            sa_delete(schema.ready_executions).where(schema.ready_executions.c.id == picked.id)
        )
        clearer.start()
        # On Postgres/MySQL the clear proceeds concurrently and skips the locked row; on
        # SQLite it waits for the write lock. Either way the claimed job must survive.
        _time.sleep(0.2)
    clearer.join(10)

    assert done.is_set()
    assert cleared["count"] == 1  # only the genuinely-ready job was discarded
    assert count(schema.claimed_executions) == 1
    with engine.connect() as conn:
        from sqlalchemy import select as sa_select2

        remaining = {row[0] for row in conn.execute(sa_select2(schema.jobs.c.id))}
    assert remaining == {being_claimed}
