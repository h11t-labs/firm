"""Crash recovery: rescue jobs whose worker died mid-flight.

A ``claimed_executions`` row whose ``process_id`` points at a process that is gone (pruned for a
stale heartbeat, or never cleaned up) is *orphaned*. We move it back to ``ready_executions`` so
another worker finishes it.

Orphaned claims are re-enqueued for at-least-once delivery rather than being recorded as failed.
Jobs must therefore be idempotent — the standard guidance for any at-least-once retry.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sqlalchemy import delete, insert, select

from .._core import process as process_registry
from .._core.config import Runtime
from .._core.poller import InterruptiblePoller
from . import schema

_claimed = schema.claimed_executions
_jobs = schema.jobs
_ready = schema.ready_executions
_processes = schema.processes


def recover_orphaned_claims(runtime: Runtime, process_ids: Sequence[int] | None = None) -> int:
    """Re-ready claims of dead processes; return how many were recovered.

    With ``process_ids`` given, recover claims of exactly those processes; otherwise recover any
    claim whose ``process_id`` is set but absent from the ``processes`` table.
    """
    dialect = runtime.dialect
    with dialect.begin_claim_tx(runtime.engine) as conn:
        stmt = select(
            _claimed.c.id, _claimed.c.job_id, _jobs.c.queue_name, _jobs.c.priority
        ).select_from(_claimed.join(_jobs, _claimed.c.job_id == _jobs.c.id))
        if process_ids is not None:
            if not process_ids:
                return 0
            stmt = stmt.where(_claimed.c.process_id.in_(list(process_ids)))
        else:
            # process_id set, but no matching live process row (NOT EXISTS is NULL-safe on all DBs).
            live = select(_processes.c.id).where(_processes.c.id == _claimed.c.process_id)
            stmt = stmt.where(_claimed.c.process_id.is_not(None), ~live.exists())

        rows = conn.execute(dialect.with_skip_locked(stmt)).all()
        for row in rows:
            conn.execute(
                insert(_ready).values(
                    job_id=row.job_id, queue_name=row.queue_name, priority=row.priority
                )
            )
            conn.execute(delete(_claimed).where(_claimed.c.id == row.id))
        return len(rows)


def reap_dead_processes(runtime: Runtime, alive_threshold_s: float) -> int:
    """Prune processes with stale heartbeats and recover their claims; return the recovered count.

    The absent-row sweep in :func:`recover_orphaned_claims` only sees claims whose process row is
    *gone* — a hard-killed process (SIGKILL, OOM) leaves its row behind with a stale heartbeat,
    which shields its claims from that sweep. Something must therefore prune stale rows in every
    deployment shape, not just under :class:`~firm.queue.supervisor.ForkSupervisor`.
    """
    dead = process_registry.prune_dead(runtime.engine, alive_threshold_s)
    if not dead:
        return 0
    return recover_orphaned_claims(runtime, dead)


class ReaperLoop(InterruptiblePoller):
    """Run :func:`reap_dead_processes` on a timer.

    ForkSupervisor reaps inline in its supervise loop; ThreadSupervisor (thread mode and the
    embedded contrib adapters) and the standalone ``firm-queue work`` command run this poller
    instead, so a hard-killed peer's in-flight jobs are recovered there too.
    """

    def __init__(
        self,
        runtime: Runtime,
        interval: float,
        alive_threshold_s: float,
        *,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        super().__init__(interval, name="reaper", on_error=on_error)
        self.runtime = runtime
        self.alive_threshold_s = alive_threshold_s

    def poll(self) -> int:
        return reap_dead_processes(self.runtime, self.alive_threshold_s)
