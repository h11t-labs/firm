"""Counting semaphores for concurrency control.

A semaphore row's ``value`` is the *remaining* capacity for a ``key``. Acquiring decrements it;
releasing increments it (capped at the limit). When capacity is exhausted a job is *blocked*
instead of made ready; on release we promote the next blocked job for that key to ready.

``expires_at`` is a failsafe: if a holder dies without releasing, the dispatcher's maintenance
pass reclaims expired semaphores and expired blocked entries.

These operations must run inside a serialized transaction (``BEGIN IMMEDIATE`` on SQLite); the
callers ensure that. SQLite has no row-level locking, so that write-lock is what keeps the
operations correct there.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Connection, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from .._core.clock import now_utc
from .._core.dialects import Dialect
from . import schema

_sem = schema.semaphores
_blocked = schema.blocked_executions
_ready = schema.ready_executions


def _decrement(conn: Connection, key: str, expires: datetime, now: datetime) -> bool:
    result = conn.execute(
        update(_sem)
        .where(_sem.c.key == key, _sem.c.value > 0)
        .values(value=_sem.c.value - 1, expires_at=expires, updated_at=now)
    )
    return bool(result.rowcount)


def acquire(conn: Connection, key: str, limit: int, duration_s: float) -> bool:
    """Take one unit of capacity for ``key``; return ``True`` if acquired, ``False`` if full.

    A ``False`` means the caller inserts a blocked row in this same transaction. The
    exhausted path therefore re-checks under ``SELECT ... FOR UPDATE`` (a no-op lock on
    SQLite, whose immediate transaction already serializes writers): the failed decrement
    locks nothing on Postgres/MySQL, so without the lock a concurrent ``release_and_promote``
    could run entirely between the failed decrement and the blocked-row commit — seeing no
    blocked row yet and leaving the slot free while the job waits for the maintenance pass
    (10 minutes by default). Holding the row lock forces that release to land either before
    the re-check (we take the freed slot) or after our blocked row is visible (it promotes).
    """
    now = now_utc()
    expires = now + timedelta(seconds=duration_s)

    if _decrement(conn, key, expires, now):
        return True

    row = conn.execute(
        select(_sem.c.id, _sem.c.value).where(_sem.c.key == key).with_for_update()
    ).first()
    if row is not None:
        if row.value > 0:  # a release slipped in since the failed decrement
            return _decrement(conn, key, expires, now)
        return False  # genuinely exhausted; the row stays locked until we commit

    # No row yet -> create it already holding one unit (value = limit - 1).
    try:
        with conn.begin_nested():
            conn.execute(insert(_sem).values(key=key, value=limit - 1, expires_at=expires))
        return True
    except IntegrityError:
        # Lost an insert race; fall back to decrementing the row the winner created.
        return _decrement(conn, key, expires, now)


def release(conn: Connection, key: str, limit: int, duration_s: float) -> bool:
    """Return one unit of capacity for ``key`` (capped at ``limit``)."""
    now = now_utc()
    expires = now + timedelta(seconds=duration_s)
    result = conn.execute(
        update(_sem)
        .where(_sem.c.key == key, _sem.c.value < limit)
        .values(value=_sem.c.value + 1, expires_at=expires, updated_at=now)
    )
    return bool(result.rowcount)


def promote_one(
    conn: Connection, dialect: Dialect, key: str, limit: int, duration_s: float
) -> bool:
    """If capacity is available, move the next blocked job for ``key`` to ready.

    The blocked row is selected ``FOR UPDATE SKIP LOCKED`` (on Postgres/MySQL) so two concurrent
    releases of the same key can't promote the same job twice.
    """
    stmt = (
        select(_blocked.c.id, _blocked.c.job_id, _blocked.c.queue_name, _blocked.c.priority)
        .where(_blocked.c.concurrency_key == key)
        .order_by(_blocked.c.priority, _blocked.c.job_id)
        .limit(1)
    )
    row = conn.execute(dialect.with_skip_locked(stmt)).first()
    if row is None:
        return False
    if not acquire(conn, key, limit, duration_s):
        return False
    conn.execute(
        insert(_ready).values(job_id=row.job_id, queue_name=row.queue_name, priority=row.priority)
    )
    conn.execute(delete(_blocked).where(_blocked.c.id == row.id))
    return True


def release_and_promote(
    conn: Connection, dialect: Dialect, key: str, limit: int, duration_s: float
) -> None:
    """Release one unit and hand the freed slot to the next blocked job, if any."""
    release(conn, key, limit, duration_s)
    promote_one(conn, dialect, key, limit, duration_s)


def forfeit_slot(conn: Connection, dialect: Dialect, key: str) -> None:
    """Hand a held slot for ``key`` to the next blocked job, or return it to capacity.

    Used when a slot-holding job is discarded outside a worker (e.g. from the dashboard),
    where the job class's ConcurrencySpec (limit/duration) may not be importable. Promoting
    *without* re-acquiring transfers the slot directly, so the limit isn't needed; the bare
    increment fallback is safe because the caller guarantees the discarded job held a slot.
    """
    stmt = (
        select(_blocked.c.id, _blocked.c.job_id, _blocked.c.queue_name, _blocked.c.priority)
        .where(_blocked.c.concurrency_key == key)
        .order_by(_blocked.c.priority, _blocked.c.job_id)
        .limit(1)
    )
    row = conn.execute(dialect.with_skip_locked(stmt)).first()
    if row is not None:
        conn.execute(
            insert(_ready).values(
                job_id=row.job_id, queue_name=row.queue_name, priority=row.priority
            )
        )
        conn.execute(delete(_blocked).where(_blocked.c.id == row.id))
    else:
        conn.execute(
            update(_sem)
            .where(_sem.c.key == key)
            .values(value=_sem.c.value + 1, updated_at=now_utc())
        )
