"""Process registration + heartbeats.

Each running worker/dispatcher/scheduler/supervisor registers a row and refreshes
``last_heartbeat_at`` on a timer. The supervisor prunes rows whose heartbeat has gone stale,
which is how dead processes are detected so their in-flight work can be recovered.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Engine, delete, insert, select, update

from .clock import now_utc
from .poller import InterruptiblePoller
from .schema import processes as _processes


@dataclass
class ProcessInfo:
    kind: str
    name: str
    pid: int
    hostname: str | None = None
    supervisor_id: int | None = None
    metadata: str | None = None


def current_hostname() -> str:
    return socket.gethostname()


def register(engine: Engine, info: ProcessInfo) -> int:
    with engine.begin() as conn:
        inserted = conn.execute(
            insert(_processes).values(
                kind=info.kind,
                name=info.name,
                pid=info.pid,
                hostname=info.hostname or current_hostname(),
                supervisor_id=info.supervisor_id,
                metadata=info.metadata,
                last_heartbeat_at=now_utc(),
            )
        )
        primary_key = inserted.inserted_primary_key
        assert primary_key is not None
        return primary_key[0]


def heartbeat(engine: Engine, process_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(_processes)
            .where(_processes.c.id == process_id)
            .values(last_heartbeat_at=now_utc())
        )


def deregister(engine: Engine, process_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(delete(_processes).where(_processes.c.id == process_id))


def deregister_children(engine: Engine, supervisor_id: int) -> list[int]:
    """Delete every process row registered under ``supervisor_id``; return their ids.

    Used by the supervisor's shutdown: a child escalated to SIGKILL never deregisters
    itself, and its leftover row would hide its claims from the absent-row recovery sweep.
    """
    with engine.begin() as conn:
        children = [
            row[0]
            for row in conn.execute(
                select(_processes.c.id).where(_processes.c.supervisor_id == supervisor_id)
            )
        ]
        if children:
            conn.execute(delete(_processes).where(_processes.c.id.in_(children)))
        return children


def prune_dead(engine: Engine, alive_threshold_s: float) -> list[int]:
    """Delete processes whose heartbeat is older than the threshold; return their ids."""
    cutoff = now_utc() - timedelta(seconds=alive_threshold_s)
    with engine.begin() as conn:
        dead = [
            row[0]
            for row in conn.execute(
                select(_processes.c.id).where(_processes.c.last_heartbeat_at < cutoff)
            )
        ]
        if dead:
            conn.execute(delete(_processes).where(_processes.c.id.in_(dead)))
        return dead


def generate_name(kind: str) -> str:
    return f"{kind.lower()}-{current_hostname()}-{os.getpid()}"


class HeartbeatPoller(InterruptiblePoller):
    """Refreshes a process's ``last_heartbeat_at`` on a timer."""

    def __init__(
        self,
        engine: Engine,
        process_id: int,
        interval: float,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        super().__init__(interval, name="heartbeat", on_error=on_error)
        self.engine = engine
        self.process_id = process_id

    def poll(self) -> int:
        heartbeat(self.engine, self.process_id)
        return 0
