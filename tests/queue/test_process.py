"""Process registry + crash-recovery specs."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import Engine, insert, select, update

from firm._core import process as pr
from firm._core.clock import now_utc
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.recovery import recover_orphaned_claims


def test_register_heartbeat_deregister(engine: Engine, count: Callable[..., int]) -> None:
    pid = pr.register(engine, pr.ProcessInfo(kind="Worker", name="w1", pid=1234))
    assert count(schema.processes) == 1
    pr.heartbeat(engine, pid)
    with engine.connect() as conn:
        heartbeat_at = conn.execute(
            select(schema.processes.c.last_heartbeat_at).where(schema.processes.c.id == pid)
        ).scalar()
    assert heartbeat_at is not None
    pr.deregister(engine, pid)
    assert count(schema.processes) == 0


def test_heartbeat_raises_when_process_row_missing(
    engine: Engine, count: Callable[..., int]
) -> None:
    """Upstream: worker_test.rb::"terminate on heartbeat when unregistered". A process pruned
    from the registry while still alive learns of it on its next heartbeat — the zero-row update
    raises ProcessExitError instead of silently no-op'ing, so the worker can self-terminate."""
    pid = pr.register(engine, pr.ProcessInfo(kind="Worker", name="w-unreg", pid=7))
    pr.deregister(engine, pid)
    assert count(schema.processes) == 0

    with pytest.raises(pr.ProcessExitError):
        pr.heartbeat(engine, pid)


def test_heartbeat_poller_self_terminates_on_eviction(engine: Engine) -> None:
    """The HeartbeatPoller stops itself and fires ``on_evicted`` once its row is gone, so the
    owning worker/child shuts down rather than running on after being declared dead."""
    pid = pr.register(engine, pr.ProcessInfo(kind="Worker", name="w-evict", pid=8))
    pr.deregister(engine, pid)  # row gone: the first heartbeat will see zero rows

    evicted = threading.Event()
    poller = pr.HeartbeatPoller(engine, pid, 0.01, on_evicted=evicted.set)
    poller.start()
    try:
        assert evicted.wait(2.0), "on_evicted should fire when the row is missing"
        assert poller.stopping, "poller should stop itself on eviction"
    finally:
        poller.stop()


def test_prune_dead_returns_stale_processes(engine: Engine, count: Callable[..., int]) -> None:
    pid = pr.register(engine, pr.ProcessInfo(kind="Worker", name="w1", pid=1))
    with engine.begin() as conn:
        conn.execute(
            update(schema.processes).values(last_heartbeat_at=now_utc() - timedelta(seconds=120))
        )
    assert pr.prune_dead(engine, alive_threshold_s=60) == [pid]
    assert count(schema.processes) == 0


def test_recover_orphaned_claims_of_missing_process(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(queue_name="default", class_name="J")
        ).inserted_primary_key[0]
        conn.execute(insert(schema.claimed_executions).values(job_id=job_id, process_id=9999))
    assert recover_orphaned_claims(runtime) == 1
    assert count(schema.claimed_executions) == 0
    assert count(schema.ready_executions) == 1


def test_recover_specific_process_ids(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    pid = pr.register(engine, pr.ProcessInfo(kind="Worker", name="w", pid=1))
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(queue_name="default", class_name="J")
        ).inserted_primary_key[0]
        conn.execute(insert(schema.claimed_executions).values(job_id=job_id, process_id=pid))
    assert recover_orphaned_claims(runtime, [pid]) == 1
    assert count(schema.ready_executions) == 1
