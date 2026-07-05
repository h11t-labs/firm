"""Executing a claimed job and recording the outcome.

* success -> delete the claim, stamp ``finished_at``, release the concurrency semaphore;
* failure -> delete the claim, then either re-schedule for a retry (if attempts remain) or
  write a ``failed_executions`` row; release the semaphore either way.

Releasing the semaphore also promotes the next blocked job for that key to ready.
"""

from __future__ import annotations

import traceback
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, insert, select, update

from .._core.clock import now_utc
from .._core.config import Runtime
from .._core.database import immediate_transaction
from .._core.dialects import Dialect
from . import schema, semaphore
from .config import QueueSettings
from .job import RetryPolicy
from .registry import REGISTRY, UnknownJob
from .serialization import deserialize

if TYPE_CHECKING:
    from .concurrency import ConcurrencySpec

_jobs = schema.jobs
_claimed = schema.claimed_executions
_failed = schema.failed_executions
_scheduled = schema.scheduled_executions


def execute_claimed(runtime: Runtime, job_id: int, process_id: int | None = None) -> bool:
    """Run a claimed job; record success/failure (with retry). Return ``True`` on success.

    ``process_id`` scopes the finalize to the claim this worker owns. After a
    prune -> recover -> reclaim cycle, a zombie worker (stale heartbeat, still alive)
    finishing its stale copy must not delete the *new* owner's claim row — if it did and the
    new worker then died mid-run, no recovery pass would ever find the job again.
    """
    with runtime.engine.connect() as conn:
        row = conn.execute(
            select(
                _jobs.c.class_name,
                _jobs.c.arguments,
                _jobs.c.attempts,
                _jobs.c.concurrency_key,
            ).where(_jobs.c.id == job_id)
        ).one()

    concurrency_key = row.concurrency_key
    try:
        job = REGISTRY.lookup(row.class_name)
    except UnknownJob as exc:
        _finalize_failure(
            runtime, job_id, exc, row.attempts, RetryPolicy(), concurrency_key, None, process_id
        )
        return False

    args, kwargs = deserialize(row.arguments)
    try:
        job.perform(*args, **kwargs)
    except BaseException as exc:
        # BaseException on purpose: a job body raising SystemExit/KeyboardInterrupt must
        # still be finalized as a failure. Letting it escape would kill the worker's poll
        # thread while its heartbeat keeps the process row fresh — the claim would then be
        # neither finalized nor ever recovered (the process still looks alive), and the
        # worker would silently stop processing inside an apparently healthy process.
        _finalize_failure(
            runtime,
            job_id,
            exc,
            row.attempts,
            job.retry_policy,
            concurrency_key,
            job.concurrency,
            process_id,
        )
        return False
    else:
        return _finalize_success(runtime, job_id, concurrency_key, job.concurrency, process_id)


def _release(
    conn, dialect: Dialect, concurrency_key: str | None, spec: ConcurrencySpec | None
) -> None:
    if concurrency_key and spec is not None:
        semaphore.release_and_promote(conn, dialect, concurrency_key, spec.limit, spec.duration)


def _preserve_finished(runtime: Runtime) -> bool:
    # A runtime built outside configure() (e.g. the dashboard's) carries plain Settings;
    # default to preserving in that case.
    settings = runtime.settings
    return settings.preserve_finished_jobs if isinstance(settings, QueueSettings) else True


def _delete_own_claim(conn, job_id: int, process_id: int | None) -> bool:
    """Delete the claim row *this* worker owns; ``False`` means the claim was pruned and
    reclaimed by another process while we ran — the new owner finalizes, not us."""
    owner = (
        _claimed.c.process_id.is_(None)
        if process_id is None
        else _claimed.c.process_id == process_id
    )
    stmt = delete(_claimed).where(_claimed.c.job_id == job_id, owner)
    return bool(conn.execute(stmt).rowcount)


def _finalize_success(
    runtime: Runtime,
    job_id: int,
    concurrency_key: str | None,
    spec: ConcurrencySpec | None,
    process_id: int | None,
) -> bool:
    with immediate_transaction(runtime.engine) as conn:
        if not _delete_own_claim(conn, job_id, process_id):
            return False
        if _preserve_finished(runtime):
            conn.execute(update(_jobs).where(_jobs.c.id == job_id).values(finished_at=now_utc()))
        else:
            conn.execute(delete(_jobs).where(_jobs.c.id == job_id))
        _release(conn, runtime.dialect, concurrency_key, spec)
    return True


def _finalize_failure(
    runtime: Runtime,
    job_id: int,
    exc: BaseException,
    attempts: int,
    retry_policy: RetryPolicy,
    concurrency_key: str | None,
    spec: ConcurrencySpec | None,
    process_id: int | None,
) -> None:
    next_attempt = attempts + 1
    delay = retry_policy.retry_delay(next_attempt)
    with immediate_transaction(runtime.engine) as conn:
        if not _delete_own_claim(conn, job_id, process_id):
            return
        if delay is not None:
            jrow = conn.execute(
                select(_jobs.c.queue_name, _jobs.c.priority).where(_jobs.c.id == job_id)
            ).one()
            conn.execute(update(_jobs).where(_jobs.c.id == job_id).values(attempts=next_attempt))
            conn.execute(
                insert(_scheduled).values(
                    job_id=job_id,
                    queue_name=jrow.queue_name,
                    priority=jrow.priority,
                    scheduled_at=now_utc() + timedelta(seconds=delay),
                )
            )
        else:
            error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            # A job_id can already have a failed row (e.g. a duplicate run after at-least-once
            # recovery); update it in place rather than violating the unique index.
            updated = conn.execute(
                update(_failed).where(_failed.c.job_id == job_id).values(error=error)
            ).rowcount
            if not updated:
                conn.execute(insert(_failed).values(job_id=job_id, error=error))
        _release(conn, runtime.dialect, concurrency_key, spec)
