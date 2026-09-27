"""Process registry + crash-recovery specs."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import Engine, event, insert, select, update
from sqlalchemy.exc import OperationalError

from firm._core import process as pr
from firm._core.clock import now_utc
from firm._core.config import Runtime
from firm._core.database import create_engine_for
from firm.queue import schema
from firm.queue.recovery import ReaperLoop, reap_dead_processes, recover_orphaned_claims


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


def test_hostname_with_special_characters_round_trips(
    engine: Engine, count: Callable[..., int]
) -> None:
    # upstream: process_test.rb "hostname's with special characters are properly loaded". A
    # process registered with an odd hostname and metadata round-trips byte-for-byte.
    odd_hostname = "hosté-ü.local:8080 (replica #1)"
    odd_metadata = '{"hostname":"hosté","tags":["a/b","c d"]}'
    pid = pr.register(
        engine,
        pr.ProcessInfo(
            kind="Worker",
            name="worker-special",
            pid=4242,
            hostname=odd_hostname,
            metadata=odd_metadata,
        ),
    )
    assert count(schema.processes) == 1
    with engine.connect() as conn:
        row = conn.execute(
            select(
                schema.processes.c.hostname,
                schema.processes.c.metadata,
                schema.processes.c.name,
                schema.processes.c.pid,
            ).where(schema.processes.c.id == pid)
        ).one()
    assert row.hostname == odd_hostname
    assert row.metadata == odd_metadata
    assert row.name == "worker-special"
    assert row.pid == 4242


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


@pytest.mark.parametrize("sweep", ["prune_dead", "deregister_children"])
def test_registry_sweeps_survive_a_commit_between_their_select_and_delete(
    db_url: str, engine: Engine, is_sqlite: bool, sweep: str
) -> None:
    """``prune_dead`` and ``deregister_children`` SELECT their victims, then DELETE them. Under a
    deferred ``BEGIN`` on SQLite, a commit by another connection between the two statements
    upgrades the read snapshot and the DELETE fails at once with SQLITE_BUSY_SNAPSHOT — which
    ``busy_timeout`` never waits on. Taking the write lock up front makes the peer wait instead."""
    if not is_sqlite:
        pytest.skip("SQLite snapshot-upgrade semantics only")
    supervisor_id = pr.register(engine, pr.ProcessInfo(kind="Supervisor", name="sup", pid=1))
    victim = pr.register(
        engine, pr.ProcessInfo(kind="Worker", name="stale", pid=2, supervisor_id=supervisor_id)
    )
    with engine.begin() as conn:
        conn.execute(
            update(schema.processes)
            .where(schema.processes.c.id == victim)
            .values(last_heartbeat_at=now_utc() - timedelta(seconds=1000))
        )
    peer_engine = create_engine_for(db_url, busy_timeout_ms=50)
    interleaved: list[str] = []

    def peer_commits_before_the_delete(
        conn, cursor, statement: str, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("DELETE"):
            return
        interleaved.append(statement)
        # A peer registers itself while the sweep sits between its SELECT and its DELETE. Held
        # off by the sweep's write lock, the peer times out (suppressed); without that lock it
        # commits and the sweep's DELETE is the one that fails.
        with contextlib.suppress(OperationalError), peer_engine.begin() as peer:
            peer.execute(
                insert(schema.processes).values(
                    kind="Worker", name="peer", pid=3, hostname="h", last_heartbeat_at=now_utc()
                )
            )

    event.listen(engine, "before_cursor_execute", peer_commits_before_the_delete)
    try:
        if sweep == "prune_dead":
            deleted = pr.prune_dead(engine, alive_threshold_s=300)
        else:
            deleted = pr.deregister_children(engine, supervisor_id)
    finally:
        event.remove(engine, "before_cursor_execute", peer_commits_before_the_delete)
        peer_engine.dispose()

    assert interleaved, "the peer never got between SELECT and DELETE; the test is inert"
    assert deleted == [victim]
    with engine.connect() as conn:
        remaining = set(conn.execute(select(schema.processes.c.id)).scalars())
    assert victim not in remaining
    assert supervisor_id in remaining


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


def _claim_under_stale_process(engine: Engine) -> int:
    """Simulate a hard-killed worker: a claim whose process row survives with a stale heartbeat."""
    pid = pr.register(engine, pr.ProcessInfo(kind="Worker", name="crashed", pid=1))
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(queue_name="default", class_name="J")
        ).inserted_primary_key[0]
        conn.execute(insert(schema.claimed_executions).values(job_id=job_id, process_id=pid))
        conn.execute(
            update(schema.processes)
            .where(schema.processes.c.id == pid)
            .values(last_heartbeat_at=now_utc() - timedelta(seconds=600))
        )
    return pid


def test_reap_dead_processes_recovers_stale_heartbeat_claims(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """Q-R1: a SIGKILLed process leaves a stale-heartbeat row that shields its claims from the
    absent-row sweep; reap_dead_processes prunes the row and re-readies the claims."""
    _claim_under_stale_process(engine)

    # The absent-row sweep alone cannot see it: the stale row still exists.
    assert recover_orphaned_claims(runtime) == 0
    assert count(schema.ready_executions) == 0

    assert reap_dead_processes(runtime, alive_threshold_s=300) == 1
    assert count(schema.processes) == 0
    assert count(schema.claimed_executions) == 0
    assert count(schema.ready_executions) == 1


def test_reap_dead_processes_spares_fresh_heartbeats(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    pid = pr.register(engine, pr.ProcessInfo(kind="Worker", name="alive", pid=1))
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(queue_name="default", class_name="J")
        ).inserted_primary_key[0]
        conn.execute(insert(schema.claimed_executions).values(job_id=job_id, process_id=pid))

    assert reap_dead_processes(runtime, alive_threshold_s=300) == 0
    assert count(schema.processes) == 1
    assert count(schema.claimed_executions) == 1


def test_reaper_loop_recovers_while_running(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """The poller form used by thread mode and `firm-queue work` reaps periodically."""
    _claim_under_stale_process(engine)

    loop = ReaperLoop(runtime, interval=0.01, alive_threshold_s=300)
    loop.start()
    try:
        deadline = now_utc() + timedelta(seconds=5)
        while count(schema.ready_executions) == 0 and now_utc() < deadline:
            threading.Event().wait(0.02)
    finally:
        loop.stop()

    assert count(schema.claimed_executions) == 0
    assert count(schema.ready_executions) == 1
