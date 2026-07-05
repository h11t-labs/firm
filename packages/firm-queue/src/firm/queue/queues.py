"""Queue management API: pause/resume/size/clear/latency."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, insert, select

from .._core.clock import now_utc
from .._core.config import Runtime
from . import schema

_ready = schema.ready_executions
_pauses = schema.pauses
_jobs = schema.jobs


def all_queues(runtime: Runtime) -> list[str]:
    """Distinct queue names that currently have ready jobs."""
    with runtime.engine.connect() as conn:
        rows = conn.execute(select(_ready.c.queue_name).distinct().order_by(_ready.c.queue_name))
        return [row[0] for row in rows]


def size(runtime: Runtime, queue: str) -> int:
    """Number of ready jobs in ``queue``."""
    with runtime.engine.connect() as conn:
        return (
            conn.execute(
                select(func.count()).select_from(_ready).where(_ready.c.queue_name == queue)
            ).scalar()
            or 0
        )


def pause(runtime: Runtime, queue: str) -> None:
    with runtime.engine.begin() as conn:
        if conn.execute(select(_pauses.c.id).where(_pauses.c.queue_name == queue)).first() is None:
            conn.execute(insert(_pauses).values(queue_name=queue))


def resume(runtime: Runtime, queue: str) -> None:
    with runtime.engine.begin() as conn:
        conn.execute(delete(_pauses).where(_pauses.c.queue_name == queue))


def is_paused(runtime: Runtime, queue: str) -> bool:
    with runtime.engine.connect() as conn:
        return (
            conn.execute(select(_pauses.c.id).where(_pauses.c.queue_name == queue)).first()
            is not None
        )


def clear(runtime: Runtime, queue: str) -> int:
    """Discard all ready jobs in ``queue`` (deletes the jobs; cascades to executions).

    The ready rows are taken ``FOR UPDATE SKIP LOCKED`` inside a claim transaction and
    deleted before the jobs: a row a worker is claiming right now is skipped (that job runs;
    it is no longer "ready"), and a row we lock can't be claimed — so a clear can never
    cascade-delete the claim of a job mid-run.
    """
    dialect = runtime.dialect
    with dialect.begin_claim_tx(runtime.engine) as conn:
        stmt = dialect.with_skip_locked(
            select(_ready.c.id, _ready.c.job_id).where(_ready.c.queue_name == queue)
        )
        rows = conn.execute(stmt).all()
        if not rows:
            return 0
        conn.execute(delete(_ready).where(_ready.c.id.in_([row.id for row in rows])))
        conn.execute(delete(_jobs).where(_jobs.c.id.in_([row.job_id for row in rows])))
        return len(rows)


def latency(runtime: Runtime, queue: str, now: datetime | None = None) -> float:
    """Seconds since the oldest ready job in ``queue`` was enqueued (0 if empty)."""
    moment = now or now_utc()
    with runtime.engine.connect() as conn:
        oldest = conn.execute(
            select(func.min(_ready.c.created_at)).where(_ready.c.queue_name == queue)
        ).scalar()
    if oldest is None:
        return 0.0
    return max(0.0, (moment - oldest).total_seconds())
