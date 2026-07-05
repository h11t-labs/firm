"""Finished-job retention, manual retry of failed jobs, and manual discard.

These functions are a supported operational surface: the dashboard (firm-ui) and the CLI call
them directly. Changing their signatures is a breaking change.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, insert, select, update

from .._core.clock import now_utc
from .._core.config import Runtime
from . import schema, semaphore

_jobs = schema.jobs
_failed = schema.failed_executions
_ready = schema.ready_executions
_claimed = schema.claimed_executions

DEFAULT_BATCH_SIZE = 500


def clear_finished(
    runtime: Runtime,
    older_than: timedelta | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Delete finished jobs (optionally only those finished before ``older_than`` ago)."""
    with runtime.engine.begin() as conn:
        stmt = select(_jobs.c.id).where(_jobs.c.finished_at.is_not(None))
        if older_than is not None:
            stmt = stmt.where(_jobs.c.finished_at < now_utc() - older_than)
        ids = [row[0] for row in conn.execute(stmt.limit(batch_size))]
        if not ids:
            return 0
        conn.execute(delete(_jobs).where(_jobs.c.id.in_(ids)))
        return len(ids)


def retry_failed(runtime: Runtime, job_id: int) -> bool:
    """Move a failed job back to ready, resetting its attempt counter."""
    with runtime.engine.begin() as conn:
        row = (
            conn.execute(
                select(_jobs.c.queue_name, _jobs.c.priority)
                .select_from(_failed.join(_jobs, _failed.c.job_id == _jobs.c.id))
                .where(_failed.c.job_id == job_id)
            )
        ).first()
        if row is None:
            return False
        conn.execute(delete(_failed).where(_failed.c.job_id == job_id))
        conn.execute(update(_jobs).where(_jobs.c.id == job_id).values(attempts=0, finished_at=None))
        conn.execute(
            insert(_ready).values(job_id=job_id, queue_name=row.queue_name, priority=row.priority)
        )
        return True


def discard_job(runtime: Runtime, job_id: int) -> bool:
    """Delete a job outright (FK cascade removes its execution rows); return whether it did.

    Refuses (returns ``False``) when the job is currently claimed — a running job can't be
    meaningfully discarded. If the job holds a concurrency slot (a ready execution with a
    ``concurrency_key``), the slot is handed to the next blocked job so a discard never
    strands blocked work until the semaphore-expiry failsafe.
    """
    with runtime.dialect.begin_claim_tx(runtime.engine) as conn:
        # Take the ready row first rather than doing a non-locking claimed-check: an
        # in-flight claim transaction holds this row FOR UPDATE, so the DELETE serializes
        # against it — rowcount 1 proves no worker can be (or become) running this job. The
        # old SELECT-on-claimed could return "not claimed" and still lose to a racing claim,
        # letting a discard that reported True execute anyway.
        ready_deleted = conn.execute(delete(_ready).where(_ready.c.job_id == job_id)).rowcount
        if not ready_deleted and (
            conn.execute(select(_claimed.c.id).where(_claimed.c.job_id == job_id)).first()
            is not None
        ):
            return False
        row = conn.execute(select(_jobs.c.concurrency_key).where(_jobs.c.id == job_id)).first()
        if row is None:
            return False
        holds_slot = row.concurrency_key is not None and bool(ready_deleted)
        conn.execute(delete(_jobs).where(_jobs.c.id == job_id))
        if holds_slot:
            semaphore.forfeit_slot(conn, runtime.dialect, row.concurrency_key)
        return True


def retry_all_failed(runtime: Runtime) -> int:
    """Retry every failed job; return how many were re-enqueued."""
    with runtime.engine.connect() as conn:
        ids = [row[0] for row in conn.execute(select(_failed.c.job_id))]
    return sum(1 for job_id in ids if retry_failed(runtime, job_id))
