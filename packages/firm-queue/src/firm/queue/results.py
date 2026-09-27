"""Executing a claimed job and recording the outcome.

* success -> delete the claim, stamp ``finished_at``, release the concurrency semaphore;
* failure -> delete the claim, then either re-schedule for a retry (if attempts remain) or
  write a ``failed_executions`` row; release the semaphore either way.

Releasing the semaphore also promotes the next blocked job for that key to ready.

Recording the outcome is the one write that must not be lost once the body has run: the claim
row stays owned by this live process until it lands, and no recovery sweep touches a live
process's claims. A transient database error there (a failover at COMMIT, a lock-wait or
deadlock abort, SQLite ``database is locked``) is therefore retried in-thread with a short
backoff, and past that budget handed back to the caller as :class:`FinalizeError` — the worker
keeps re-trying it before claiming new work.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, TypeVar

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import DBAPIError, OperationalError

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

_T = TypeVar("_T")

# Backoff between in-thread attempts to record an outcome. Kept short on purpose: a pool thread
# sleeping here holds up ``Worker.poll`` (which waits on every future of its batch) and eats
# into the 5s join budget of ``stop()``; longer outages are the worker's pending list's job.
_FINALIZE_BACKOFF_S: tuple[float, ...] = (0.1, 0.5, 2.0)


class FinalizeError(RuntimeError):
    """A job's body has run, but recording its outcome kept failing with transient database
    errors past the in-thread retry budget.

    The claim row is still owned by this process — correctly so: the job must not run anywhere
    else — so the caller has to keep re-trying until the outcome lands.
    :class:`~firm.queue.worker.Worker` does that at the top of every poll and reports each
    exhausted budget through the ``thread_error`` hook. ``finalize`` is idempotent: if an earlier
    commit did land but its acknowledgement was lost, ``_delete_own_claim`` finds nothing and it
    exits early.
    """

    def __init__(self, job_id: int, cause: DBAPIError, finalize: Callable[[], object]) -> None:
        super().__init__(
            f"could not record the outcome of job {job_id}; its claim is still held by this "
            f"process and will be retried: {cause.orig!r}"
        )
        self.job_id = job_id
        self.finalize = finalize

    def retry(self) -> None:
        """Re-run the finalize with a fresh backoff budget; raises :class:`FinalizeError` again
        (a new instance, same ``finalize``) if the database is still unavailable."""
        _run_finalize(self.job_id, self.finalize)


def _is_transient(exc: DBAPIError) -> bool:
    """Connection drops (``connection_invalidated``), failovers, lock-wait timeouts, deadlocks and
    serialization aborts all surface as ``OperationalError`` in psycopg, pymysql and sqlite3.
    ``IntegrityError``/``ProgrammingError`` are bugs and must not be retried."""
    return exc.connection_invalidated or isinstance(exc, OperationalError)


def _run_finalize(job_id: int, finalize: Callable[[], _T]) -> _T:
    """Run ``finalize`` (a single, idempotent outcome transaction) with a bounded retry on
    transient errors; hand it to the caller as :class:`FinalizeError` once the budget is spent."""
    for delay in (*_FINALIZE_BACKOFF_S, None):
        try:
            return finalize()
        except DBAPIError as exc:
            if not _is_transient(exc):
                raise
            if delay is None:
                raise FinalizeError(job_id, exc, finalize) from exc
            time.sleep(delay)
    raise AssertionError("unreachable")


def execute_claimed(runtime: Runtime, job_id: int, process_id: int | None = None) -> bool:
    """Run a claimed job; record success/failure (with retry). Return ``True`` on success.

    Raises :class:`FinalizeError` when the outcome could not be recorded because of transient
    database errors that outlasted the in-thread retry budget (the claim is then still ours).

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
        ).first()

    if row is None:
        # The job row vanished between claim and execution (a concurrent discard/clear).
        # Drop our claim and move on instead of raising NoResultFound into the worker.
        _run_finalize(job_id, partial(_drop_own_claim, runtime, job_id, process_id))
        return False

    concurrency_key = row.concurrency_key
    try:
        job = REGISTRY.lookup(row.class_name)
    except UnknownJob as exc:
        _run_finalize(
            job_id,
            partial(
                _finalize_failure,
                runtime,
                job_id,
                exc,
                row.attempts,
                RetryPolicy(),
                concurrency_key,
                None,
                process_id,
            ),
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
        _run_finalize(
            job_id,
            partial(
                _finalize_failure,
                runtime,
                job_id,
                exc,
                row.attempts,
                job.retry_policy,
                concurrency_key,
                job.concurrency,
                process_id,
            ),
        )
        return False
    else:
        return _run_finalize(
            job_id,
            partial(
                _finalize_success, runtime, job_id, concurrency_key, job.concurrency, process_id
            ),
        )


def _release(
    conn, dialect: Dialect, concurrency_key: str | None, spec: ConcurrencySpec | None
) -> None:
    if not concurrency_key:
        return
    if spec is not None:
        semaphore.release_and_promote(conn, dialect, concurrency_key, spec.limit, spec.duration)
    else:
        # The class is unknown (spec unimportable) but the claim held a slot: hand it to the
        # next blocked job instead of stranding the key until semaphore expiry (~3 min).
        semaphore.forfeit_slot(conn, dialect, concurrency_key)


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


def _drop_own_claim(runtime: Runtime, job_id: int, process_id: int | None) -> None:
    with immediate_transaction(runtime.engine) as conn:
        _delete_own_claim(conn, job_id, process_id)


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
