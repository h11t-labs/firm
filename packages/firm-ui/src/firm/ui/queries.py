"""Read-only queries over the firm-queue schema (SQLAlchemy only — no heavy deps).

Everything here is a plain ``SELECT`` returning dicts, so it's trivially testable without a running
server and keeps the rendering layer free of ORM objects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from firm.queue import schema

_jobs = schema.jobs
_ready = schema.ready_executions
_claimed = schema.claimed_executions
_scheduled = schema.scheduled_executions
_blocked = schema.blocked_executions
_failed = schema.failed_executions
_processes = schema.processes
_recurring = schema.recurring_tasks
_pauses = schema.pauses

_EXEC_TABLES = {
    "ready": _ready,
    "scheduled": _scheduled,
    "blocked": _blocked,
    "claimed": _claimed,
    "failed": _failed,
}

# The state tabs, in display order. "finished" is derived from jobs.finished_at, not an exec table.
STATES = ["ready", "scheduled", "blocked", "claimed", "failed", "finished"]

_STATE_TS = {
    "ready": _ready.c.created_at,
    "claimed": _claimed.c.created_at,
    "blocked": _blocked.c.created_at,
    "failed": _failed.c.created_at,
    "scheduled": _scheduled.c.scheduled_at,
}

DEFAULT_ALIVE_THRESHOLD = 300.0


def _count(conn: Connection, table: Any) -> int:
    return conn.execute(select(func.count()).select_from(table)).scalar() or 0


def state_counts(conn: Connection, queue: str | None = None) -> dict[str, int]:
    """Per-state job counts, optionally scoped to one queue. Only claimed/failed executions lack
    their own ``queue_name`` column, so a queue filter joins back to ``jobs`` for every state —
    the unfiltered (common) path stays a plain per-table count with no join."""
    counts: dict[str, int] = {}
    for state, table in _EXEC_TABLES.items():
        if queue is None:
            stmt = select(func.count()).select_from(table)
        else:
            stmt = (
                select(func.count())
                .select_from(table.join(_jobs, table.c.job_id == _jobs.c.id))
                .where(_jobs.c.queue_name == queue)
            )
        counts[state] = conn.execute(stmt).scalar() or 0

    finished_stmt = select(func.count()).select_from(_jobs).where(_jobs.c.finished_at.is_not(None))
    total_stmt = select(func.count()).select_from(_jobs)
    if queue is not None:
        finished_stmt = finished_stmt.where(_jobs.c.queue_name == queue)
        total_stmt = total_stmt.where(_jobs.c.queue_name == queue)
    counts["finished"] = conn.execute(finished_stmt).scalar() or 0
    counts["total"] = conn.execute(total_stmt).scalar() or 0
    return counts


def queue_rows(conn: Connection, now: datetime) -> list[dict[str, Any]]:
    """One row per queue that has ready work or is paused. A single grouped scan of
    ``firm_queue_ready_executions`` yields every queue's ready count and oldest-ready timestamp;
    paused queues with zero ready rows are merged back in (size 0). This runs on the overview,
    which auto-refreshes, so it stays one query regardless of how many queues exist — not the two
    per queue the per-name loop used to issue."""
    grouped = conn.execute(
        select(
            _ready.c.queue_name,
            func.count().label("size"),
            func.min(_ready.c.created_at).label("oldest"),
        ).group_by(_ready.c.queue_name)
    ).all()
    stats = {r.queue_name: (r.size, r.oldest) for r in grouped}
    paused_names = {r[0] for r in conn.execute(select(_pauses.c.queue_name))}
    rows: list[dict[str, Any]] = []
    for name in sorted(stats.keys() | paused_names):
        size, oldest = stats.get(name, (0, None))
        latency = 0.0 if oldest is None else max(0.0, (now - oldest).total_seconds())
        rows.append(
            {"name": name, "size": size, "latency": latency, "paused": name in paused_names}
        )
    return rows


def jobs_by_state(
    conn: Connection, state: str, limit: int = 50, offset: int = 0, queue: str | None = None
) -> list[dict[str, Any]]:
    cols = [_jobs.c.id, _jobs.c.queue_name, _jobs.c.class_name, _jobs.c.priority, _jobs.c.attempts]
    if state == "finished":
        stmt = (
            select(*cols, _jobs.c.finished_at.label("ts"))
            .where(_jobs.c.finished_at.is_not(None))
            .order_by(_jobs.c.finished_at.desc())
        )
    else:
        table = _EXEC_TABLES[state]
        ts = _STATE_TS[state]
        order = ts.asc() if state == "scheduled" else _jobs.c.id.desc()
        stmt = (
            select(*cols, ts.label("ts"))
            .select_from(table.join(_jobs, table.c.job_id == _jobs.c.id))
            .order_by(order)
        )
    if queue is not None:
        stmt = stmt.where(_jobs.c.queue_name == queue)
    rows = conn.execute(stmt.limit(limit).offset(offset)).all()
    return [
        {
            "id": r.id,
            "queue_name": r.queue_name,
            "class_name": r.class_name,
            "priority": r.priority,
            "attempts": r.attempts,
            "ts": r.ts,
        }
        for r in rows
    ]


def job_detail(conn: Connection, job_id: int) -> dict[str, Any] | None:
    job = conn.execute(select(_jobs).where(_jobs.c.id == job_id)).first()
    if job is None:
        return None
    state = "finished" if job.finished_at is not None else "unknown"
    error: str | None = None
    process_id: int | None = None
    for name, table in _EXEC_TABLES.items():
        row = conn.execute(select(table).where(table.c.job_id == job_id)).first()
        if row is not None:
            state = name
            if name == "failed":
                error = row.error
            if name == "claimed":
                process_id = row.process_id
            break
    return {
        "id": job.id,
        "queue_name": job.queue_name,
        "class_name": job.class_name,
        "arguments": job.arguments,
        "priority": job.priority,
        "attempts": job.attempts,
        "scheduled_at": job.scheduled_at,
        "finished_at": job.finished_at,
        "concurrency_key": job.concurrency_key,
        "created_at": job.created_at,
        "state": state,
        "error": error,
        "process_id": process_id,
    }


def processes(
    conn: Connection, now: datetime, alive_threshold: float = DEFAULT_ALIVE_THRESHOLD
) -> list[dict[str, Any]]:
    rows = conn.execute(select(_processes).order_by(_processes.c.last_heartbeat_at.desc())).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        age = (now - r.last_heartbeat_at).total_seconds()
        out.append(
            {
                "id": r.id,
                "kind": r.kind,
                "name": r.name,
                "pid": r.pid,
                "hostname": r.hostname,
                "last_heartbeat_at": r.last_heartbeat_at,
                "age": age,
                "alive": age <= alive_threshold,
            }
        )
    return out


def recurring(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(select(_recurring).order_by(_recurring.c.key)).all()
    return [
        {
            "key": r.key,
            "schedule": r.schedule,
            "class_name": r.class_name,
            "queue_name": r.queue_name,
            "command": r.command,
        }
        for r in rows
    ]
