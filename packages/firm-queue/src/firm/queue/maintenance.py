"""Finished-job retention, manual retry of failed jobs, and manual discard.

These functions are a supported operational surface: the dashboard (firm-ui) and the CLI call
them directly. Changing their signatures is a breaking change.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, insert, select, update

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
    dialect = runtime.dialect
    with dialect.begin_claim_tx(runtime.engine) as conn:
        # Lock the jobs row up front (FOR UPDATE on Postgres/MySQL; a no-op on SQLite, whose
        # begin_claim_tx already serializes writers via BEGIN IMMEDIATE). A concurrent
        # dispatch_once promoting this same scheduled job holds this row FOR UPDATE via its
        # scheduled⋈jobs join, so the lock forces the two to serialize: either the promotion
        # commits first — then our ready-delete below sees its fresh ready row and we forfeit
        # the slot it acquired — or the dispatcher SKIP-LOCKs this row and never acquires a
        # slot at all. Without the lock, our ready-delete could run before the promotion's
        # ready-insert (rowcount 0 -> holds_slot False), then delete(_jobs) would cascade the
        # freshly promoted ready row while forfeit_slot never runs, leaking the slot.
        row = conn.execute(
            dialect.with_row_lock(select(_jobs.c.concurrency_key).where(_jobs.c.id == job_id))
        ).first()
        if row is None:
            return False
        # Take the ready row rather than doing a non-locking claimed-check: once we hold it, no
        # worker can claim it, so deleting it proves no worker can be (or become) running this
        # job. Take it without waiting, though: a claim in progress already holds it and next
        # needs a key-share lock on the jobs row we hold (the claimed_executions foreign key), so
        # waiting would deadlock. A ready row we can't lock is being claimed right now — the job
        # is about to run, so refuse, as for a claimed one. Keep the plain existence check below
        # the first non-locking read in this transaction: MySQL's REPEATABLE READ snapshot starts
        # at the first such read, and an earlier one could hide a ready row committed since.
        ready = select(_ready.c.id).where(_ready.c.job_id == job_id)
        ready_ids = [ready_row.id for ready_row in conn.execute(dialect.with_skip_locked(ready))]
        if not ready_ids and conn.execute(ready).first() is not None:
            return False
        ready_deleted = (
            conn.execute(delete(_ready).where(_ready.c.id.in_(ready_ids))).rowcount
            if ready_ids
            else 0
        )
        if not ready_deleted and (
            conn.execute(select(_claimed.c.id).where(_claimed.c.job_id == job_id)).first()
            is not None
        ):
            return False
        holds_slot = row.concurrency_key is not None and bool(ready_deleted)
        conn.execute(delete(_jobs).where(_jobs.c.id == job_id))
        if holds_slot:
            semaphore.forfeit_slot(conn, dialect, row.concurrency_key)
        return True


def retry_all_failed(runtime: Runtime, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Retry every failed job, each at most once; return how many were re-enqueued.

    Processes in chunks of ``batch_size`` per transaction rather than one transaction per job,
    so a dashboard "Retry all" over a large backlog doesn't fan out into thousands of serial
    commits. Each chunk mirrors :func:`retry_failed` exactly — delete the failed rows, reset the
    jobs' ``attempts``/``finished_at``, and insert the ready rows — inside one transaction. The
    chunks walk ``job_id`` upward to the highest one failed at the start, so a job that fails
    again while this runs is not retried a second time by the same call.
    """
    with runtime.engine.connect() as conn:
        last = conn.execute(select(func.max(_failed.c.job_id))).scalar()
    if last is None:
        return 0
    total = 0
    after = 0
    while True:
        with runtime.engine.begin() as conn:
            rows = conn.execute(
                select(_failed.c.job_id, _jobs.c.queue_name, _jobs.c.priority)
                .select_from(_failed.join(_jobs, _failed.c.job_id == _jobs.c.id))
                .where(_failed.c.job_id > after, _failed.c.job_id <= last)
                .order_by(_failed.c.job_id)
                .limit(batch_size)
            ).all()
            if not rows:
                return total
            ids = [row.job_id for row in rows]
            conn.execute(delete(_failed).where(_failed.c.job_id.in_(ids)))
            conn.execute(
                update(_jobs).where(_jobs.c.id.in_(ids)).values(attempts=0, finished_at=None)
            )
            conn.execute(
                insert(_ready),
                [
                    {"job_id": row.job_id, "queue_name": row.queue_name, "priority": row.priority}
                    for row in rows
                ],
            )
            total += len(rows)
            after = ids[-1]
