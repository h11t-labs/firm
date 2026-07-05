"""Enqueuing — insert the job row and route its execution.

Routing rules:

* future ``scheduled_at`` -> ``scheduled_executions`` (the dispatcher promotes it, and applies
  concurrency control at that point);
* immediate, no concurrency -> ``ready_executions``;
* immediate, concurrency-limited -> acquire the semaphore (in a serialized transaction); on
  success -> ``ready_executions``, otherwise ``blocked_executions`` (or nothing, when
  ``on_conflict="discard"``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, insert

from .._core.clock import now_utc
from .._core.config import Runtime
from .._core.database import immediate_transaction
from . import schema, semaphore
from .config import current_runtime
from .serialization import serialize

if TYPE_CHECKING:
    from .job import Job

_jobs = schema.jobs
_ready = schema.ready_executions
_scheduled = schema.scheduled_executions
_blocked = schema.blocked_executions


def _insert_job(
    conn: Connection, job: Job, args_blob: str, scheduled_at: datetime, concurrency_key: str | None
) -> int:
    inserted = conn.execute(
        insert(_jobs).values(
            queue_name=job.queue_name,
            class_name=job.class_name,
            arguments=args_blob,
            priority=job.priority,
            scheduled_at=scheduled_at,
            concurrency_key=concurrency_key,
        )
    )
    primary_key = inserted.inserted_primary_key
    assert primary_key is not None
    return primary_key[0]


def _insert_ready(conn: Connection, job_id: int, job: Job) -> None:
    conn.execute(
        insert(_ready).values(job_id=job_id, queue_name=job.queue_name, priority=job.priority)
    )


def enqueue(
    job: Job,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    scheduled_at: datetime | None = None,
    runtime: Runtime | None = None,
) -> int | None:
    """Persist ``job`` and return its ``job_id`` (``None`` if discarded on conflict)."""
    rt = runtime or current_runtime()
    args_blob = serialize(args, kwargs)
    now = now_utc()
    effective_scheduled = scheduled_at or now
    is_future = scheduled_at is not None and scheduled_at > now
    spec = job.concurrency

    if is_future:
        key = spec.key_for(args, kwargs) if spec is not None else None
        with rt.engine.begin() as conn:
            job_id = _insert_job(conn, job, args_blob, effective_scheduled, key)
            conn.execute(
                insert(_scheduled).values(
                    job_id=job_id,
                    queue_name=job.queue_name,
                    priority=job.priority,
                    scheduled_at=scheduled_at,
                )
            )
        return job_id

    if spec is None:
        with rt.engine.begin() as conn:
            job_id = _insert_job(conn, job, args_blob, effective_scheduled, None)
            _insert_ready(conn, job_id, job)
        return job_id

    key = spec.key_for(args, kwargs)
    with immediate_transaction(rt.engine) as conn:
        acquired = semaphore.acquire(conn, key, spec.limit, spec.duration)
        if not acquired and spec.on_conflict == "discard":
            return None
        job_id = _insert_job(conn, job, args_blob, effective_scheduled, key)
        if acquired:
            _insert_ready(conn, job_id, job)
        else:
            conn.execute(
                insert(_blocked).values(
                    job_id=job_id,
                    queue_name=job.queue_name,
                    priority=job.priority,
                    concurrency_key=key,
                    expires_at=now + timedelta(seconds=spec.duration),
                )
            )
    return job_id
