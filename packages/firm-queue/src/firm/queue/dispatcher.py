"""Dispatcher — promote due scheduled jobs to ready, and run concurrency maintenance.

* :func:`dispatch_once` moves ``scheduled_executions`` whose time has come into
  ``ready_executions`` (or ``blocked_executions`` when a concurrency limit is hit), in priority
  order, capped at ``batch_size``.
* :func:`run_maintenance` is the failsafe: it deletes expired semaphores and force-promotes
  blocked jobs whose ``expires_at`` has passed, so a crashed holder can't wedge a key forever.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, insert, select

from .._core.clock import now_utc
from .._core.config import Runtime
from .._core.poller import InterruptiblePoller
from . import schema, semaphore
from .concurrency import DEFAULT_DURATION, ConcurrencySpec
from .hooks import HOOKS
from .registry import REGISTRY, UnknownJob

_jobs = schema.jobs
_scheduled = schema.scheduled_executions
_ready = schema.ready_executions
_blocked = schema.blocked_executions
_sem = schema.semaphores

DEFAULT_BATCH_SIZE = 500


def _spec_for(class_name: str) -> ConcurrencySpec | None:
    try:
        return REGISTRY.lookup(class_name).concurrency
    except UnknownJob:
        return None


def dispatch_once(runtime: Runtime, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Promote up to ``batch_size`` due scheduled jobs; return how many were dispatched."""
    engine, dialect = runtime.engine, runtime.dialect
    now = now_utc()
    with dialect.begin_claim_tx(engine) as conn:
        stmt = (
            select(
                _scheduled.c.id,
                _scheduled.c.job_id,
                _scheduled.c.queue_name,
                _scheduled.c.priority,
                _jobs.c.class_name,
                _jobs.c.concurrency_key,
            )
            .select_from(_scheduled.join(_jobs, _scheduled.c.job_id == _jobs.c.id))
            .where(_scheduled.c.scheduled_at <= now)
            .order_by(_scheduled.c.scheduled_at, _scheduled.c.priority, _scheduled.c.job_id)
            .limit(batch_size)
        )
        rows = conn.execute(dialect.with_skip_locked(stmt)).all()
        if not rows:
            return 0

        for row in rows:
            spec = _spec_for(row.class_name) if row.concurrency_key else None
            if row.concurrency_key and spec is not None:
                if semaphore.acquire(conn, row.concurrency_key, spec.limit, spec.duration):
                    _to_ready(conn, row)
                elif spec.on_conflict == "discard":
                    # Key is full and the job opted out of queuing: drop it (mirroring enqueue's
                    # discard) instead of blocking it for the failsafe to later promote.
                    conn.execute(delete(_scheduled).where(_scheduled.c.id == row.id))
                    conn.execute(delete(_jobs).where(_jobs.c.id == row.job_id))
                    continue
                else:
                    _to_blocked(conn, row, spec.duration)
            else:
                _to_ready(conn, row)
            conn.execute(delete(_scheduled).where(_scheduled.c.id == row.id))
        return len(rows)


def _to_ready(conn, row) -> None:
    conn.execute(
        insert(_ready).values(job_id=row.job_id, queue_name=row.queue_name, priority=row.priority)
    )


def _to_blocked(conn, row, duration_s: float) -> None:
    conn.execute(
        insert(_blocked).values(
            job_id=row.job_id,
            queue_name=row.queue_name,
            priority=row.priority,
            concurrency_key=row.concurrency_key,
            expires_at=now_utc() + timedelta(seconds=duration_s),
        )
    )


def run_maintenance(runtime: Runtime, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Expire stuck semaphores and promote blocked jobs whose key now has capacity.

    Promoting is not limited to *expired* blocked jobs: any blocked key with a free slot gets one
    job promoted, so a job never lingers in ``blocked`` while capacity is available (the window
    that opens if a release happens to interleave with a dispatch parking a job). Returns the
    number of jobs promoted to ready.
    """
    now = now_utc()
    promoted = 0
    dialect = runtime.dialect
    with dialect.begin_claim_tx(runtime.engine) as conn:
        # Failsafe: drop semaphores whose holder died without releasing.
        conn.execute(delete(_sem).where(_sem.c.expires_at < now))

        # Failsafe promised by semaphore.py: blocked rows whose own expires_at passed are
        # released to ready outright — whatever held their slot has long expired, and a
        # crashed holder must not wedge the key until a lucky release comes along.
        expired = conn.execute(
            dialect.with_skip_locked(
                select(_blocked.c.id, _blocked.c.job_id, _blocked.c.queue_name, _blocked.c.priority)
                .where(_blocked.c.expires_at < now)
                .limit(batch_size)
            )
        ).all()
        for row in expired:
            conn.execute(
                insert(_ready).values(
                    job_id=row.job_id, queue_name=row.queue_name, priority=row.priority
                )
            )
        if expired:
            conn.execute(delete(_blocked).where(_blocked.c.id.in_([r.id for r in expired])))
            promoted += len(expired)

        # One representative class_name per blocked key (all jobs sharing a key share a spec).
        keyed = conn.execute(
            select(_blocked.c.concurrency_key, func.min(_jobs.c.class_name))
            .select_from(_blocked.join(_jobs, _blocked.c.job_id == _jobs.c.id))
            .group_by(_blocked.c.concurrency_key)
            .limit(batch_size)
        ).all()

        for key, class_name in keyed:
            spec = _spec_for(class_name)
            limit = spec.limit if spec is not None else 1
            duration = spec.duration if spec is not None else DEFAULT_DURATION
            if semaphore.promote_one(conn, dialect, key, limit, duration):
                promoted += 1
    return promoted


class DispatcherLoop(InterruptiblePoller):
    """Background loop that promotes due scheduled jobs to ready."""

    def __init__(
        self, runtime: Runtime, batch_size: int = DEFAULT_BATCH_SIZE, poll_interval: float = 1.0
    ) -> None:
        super().__init__(poll_interval, name="dispatcher", on_error=HOOKS.fire_error)
        self.runtime = runtime
        self.batch_size = batch_size

    def poll(self) -> int:
        return dispatch_once(self.runtime, self.batch_size)


class MaintenanceLoop(InterruptiblePoller):
    """Background loop that expires semaphores and promotes expired blocked jobs."""

    def __init__(
        self, runtime: Runtime, interval: float = 600.0, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        super().__init__(interval, name="maintenance", on_error=HOOKS.fire_error)
        self.runtime = runtime
        self.batch_size = batch_size

    def poll(self) -> int:
        return run_maintenance(self.runtime, self.batch_size)
