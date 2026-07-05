"""Claiming ready jobs — the heart of the worker loop.

Within one short transaction (``BEGIN IMMEDIATE`` on SQLite) we select the highest-priority
ready rows, move them into ``claimed_executions``, and delete them from ``ready_executions``.
Because the transaction holds the write lock (SQLite) or uses ``FOR UPDATE SKIP LOCKED``
(PostgreSQL/MySQL), two workers never claim the same job.

Queue selection: ``"*"`` polls every non-paused queue in global priority
order; otherwise exact names and ``"prefix*"`` patterns are resolved (in the given order) and
polled queue-by-queue. Paused queues are always skipped.
"""

from __future__ import annotations

from sqlalchemy import Connection, Engine, Select, delete, distinct, insert, select

from .._core.clock import now_utc
from .._core.dialects import Dialect
from . import schema

WILDCARD = "*"

_ready = schema.ready_executions
_claimed = schema.claimed_executions
_pauses = schema.pauses


def _paused_queues(conn: Connection) -> set[str]:
    return {row[0] for row in conn.execute(select(_pauses.c.queue_name))}


def _available_queues(conn: Connection) -> list[str]:
    stmt = select(distinct(_ready.c.queue_name)).order_by(_ready.c.queue_name)
    return [row[0] for row in conn.execute(stmt)]


def resolve_queues(conn: Connection, patterns: list[str]) -> list[str] | None:
    """Concrete queues to poll, in order — or ``None`` meaning "all queues" (wildcard)."""
    if any(p == WILDCARD for p in patterns):
        return None
    available = _available_queues(conn)
    paused = _paused_queues(conn)
    selected: list[str] = []
    for pattern in patterns:
        if pattern.endswith(WILDCARD):
            prefix = pattern[:-1]
            matches = [q for q in available if q.startswith(prefix)]
        else:
            matches = [q for q in available if q == pattern]
        for q in matches:
            if q not in paused and q not in selected:
                selected.append(q)
    return selected


def _claim_rows(
    conn: Connection, dialect: Dialect, stmt: Select, process_id: int | None
) -> list[int]:
    rows = conn.execute(dialect.with_skip_locked(stmt)).all()
    if not rows:
        return []
    ids = [row.id for row in rows]
    job_ids = [row.job_id for row in rows]
    created = now_utc()
    conn.execute(
        insert(_claimed),
        [{"job_id": jid, "process_id": process_id, "created_at": created} for jid in job_ids],
    )
    conn.execute(delete(_ready).where(_ready.c.id.in_(ids)))
    return job_ids


def claim_ready(
    engine: Engine,
    dialect: Dialect,
    queues: list[str],
    limit: int,
    process_id: int | None,
) -> list[int]:
    """Claim up to ``limit`` ready jobs for ``process_id``; return the claimed ``job_id``s."""
    if limit <= 0:
        return []
    with dialect.begin_claim_tx(engine) as conn:
        target = resolve_queues(conn, queues)
        if target is None:
            paused = _paused_queues(conn)
            stmt = select(_ready.c.id, _ready.c.job_id)
            if paused:
                stmt = stmt.where(_ready.c.queue_name.notin_(paused))
            stmt = stmt.order_by(_ready.c.priority, _ready.c.job_id).limit(limit)
            return _claim_rows(conn, dialect, stmt, process_id)

        claimed: list[int] = []
        remaining = limit
        for queue_name in target:
            if remaining <= 0:
                break
            stmt = (
                select(_ready.c.id, _ready.c.job_id)
                .where(_ready.c.queue_name == queue_name)
                .order_by(_ready.c.priority, _ready.c.job_id)
                .limit(remaining)
            )
            got = _claim_rows(conn, dialect, stmt, process_id)
            claimed.extend(got)
            remaining -= len(got)
        return claimed
