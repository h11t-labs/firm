"""Thread-mode supervisor end-to-end: enqueue -> dispatch -> worker -> finished."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.exc import OperationalError

import firm.queue as bq
from firm._core import process as process_registry
from firm._core.config import Runtime
from firm._core.process import HeartbeatPoller, ProcessExitError, ProcessInfo
from firm.queue import schema
from firm.queue import supervisor as sup_mod
from firm.queue.hooks import HOOKS
from firm.queue.supervisor import (
    DispatcherConfig,
    ForkSupervisor,
    SchedulerConfig,
    SupervisorConfig,
    ThreadSupervisor,
    WorkerConfig,
)
from firm.queue.worker import Worker

_SINK: list[int] = []


@bq.job()
def e2e_job(x: int) -> None:
    _SINK.append(x)


def _finished_jobs(engine: Engine) -> int:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(func.count())
                .select_from(schema.jobs)
                .where(schema.jobs.c.finished_at.is_not(None))
            ).scalar()
            or 0
        )


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


_CLAIM_GATE = threading.Event()
_CLAIM_STARTED = threading.Event()


@bq.job()
def gated_job() -> None:
    _CLAIM_STARTED.set()
    _CLAIM_GATE.wait(10)


def test_thread_supervisor_claims_carry_its_process_id(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """Claims must reference the supervisor's heartbeated process row — a NULL process_id
    claim is invisible to recover_orphaned_claims forever (the Q-F1 regression)."""
    _CLAIM_GATE.clear()
    _CLAIM_STARTED.clear()
    gated_job.enqueue()
    config = SupervisorConfig(workers=[WorkerConfig(poll_interval=0.02)], dispatchers=[])

    try:
        with ThreadSupervisor(runtime, config) as supervisor:
            assert _CLAIM_STARTED.wait(10)
            with engine.connect() as conn:
                claimed_process_id = conn.execute(
                    select(schema.claimed_executions.c.process_id)
                ).scalar_one()
            assert claimed_process_id == supervisor.process_id
            _CLAIM_GATE.set()
            assert _wait_until(lambda: _finished_jobs(engine) == 1)
    finally:
        _CLAIM_GATE.set()

    assert count(schema.claimed_executions) == 0


def test_thread_supervisor_runs_immediate_and_scheduled(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    _SINK.clear()
    config = SupervisorConfig(
        workers=[WorkerConfig(poll_interval=0.02)],
        dispatchers=[DispatcherConfig(poll_interval=0.05)],
    )

    e2e_job.enqueue(1)
    e2e_job.enqueue_in(timedelta(seconds=0.1), 2)

    with ThreadSupervisor(runtime, config):
        assert _wait_until(lambda: _finished_jobs(engine) == 2)

    assert sorted(_SINK) == [1, 2]
    assert count(schema.claimed_executions) == 0
    assert count(schema.scheduled_executions) == 0
    assert count(schema.processes) == 0


def test_thread_supervisor_recovers_predecessor_stale_claims(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """Q-R1: thread mode never pruned stale-heartbeat processes, so a hard-killed
    predecessor's claim was shielded from the absent-row sweep and stranded forever. The
    supervisor now reaps at startup (and periodically via ReaperLoop) in thread mode too."""
    from datetime import timedelta

    from sqlalchemy import update

    from firm._core import process as process_registry
    from firm._core.clock import now_utc
    from firm._core.process import ProcessInfo
    from firm.queue.claim import claim_ready

    _SINK.clear()
    e2e_job.enqueue(42)
    dead_pid = process_registry.register(
        engine, ProcessInfo(kind="Supervisor", name="crashed-predecessor", pid=1)
    )
    assert len(claim_ready(engine, runtime.dialect, ["*"], 5, dead_pid)) == 1
    with engine.begin() as conn:
        conn.execute(
            update(schema.processes)
            .where(schema.processes.c.id == dead_pid)
            .values(last_heartbeat_at=now_utc() - timedelta(seconds=600))
        )

    config = SupervisorConfig(workers=[WorkerConfig(poll_interval=0.02)], dispatchers=[])
    with ThreadSupervisor(runtime, config):
        assert _wait_until(lambda: _finished_jobs(engine) == 1)

    assert _SINK == [42]
    assert count(schema.claimed_executions) == 0
    assert count(schema.processes) == 0


def _prune(engine: Engine, process_id: int | None) -> int:
    """Delete a process row as a reaper would, next to a live peer's row (whose id it returns).
    The peer registers first, so SQLite cannot hand the pruned rowid straight back out and the
    evicted supervisor's fresh registration is told apart from the pruned one."""
    peer = process_registry.register(engine, ProcessInfo(kind="Worker", name="peer", pid=0))
    with engine.begin() as conn:
        conn.execute(delete(schema.processes).where(schema.processes.c.id == process_id))
    return peer


def _evictable_config() -> SupervisorConfig:
    return SupervisorConfig(
        workers=[WorkerConfig(poll_interval=0.02)], dispatchers=[], heartbeat_interval=0.05
    )


def test_evicted_thread_supervisor_restarts_under_a_new_registration(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """A live supervisor whose row is pruned (heartbeats stalled past alive_threshold, so a
    reaper re-readied its claims) must not keep claiming under the dead id: every other
    process's absent-row sweep would re-ready those claims mid-run. It stops its components
    and restarts them under a fresh row — the thread-mode counterpart of fork mode's child
    restart — leaving the pruned id alone."""
    errors: list[BaseException] = []
    HOOKS.register_error(errors.append)
    _CLAIM_GATE.clear()
    _CLAIM_STARTED.clear()
    try:
        with ThreadSupervisor(runtime, _evictable_config()) as supervisor:
            pruned = supervisor.process_id
            old_loops = list(supervisor._loops)
            peer = _prune(engine, pruned)
            assert _wait_until(lambda: supervisor.process_id not in (None, pruned, peer))
            assert any(isinstance(error, ProcessExitError) for error in errors)
            assert all(loop.stopping for loop in old_loops)

            gated_job.enqueue()
            assert _CLAIM_STARTED.wait(10)
            with engine.connect() as conn:
                claimed_by = conn.execute(
                    select(schema.claimed_executions.c.process_id)
                ).scalar_one()
                registered = set(conn.execute(select(schema.processes.c.id)).scalars())
            assert claimed_by == supervisor.process_id
            assert registered == {peer, supervisor.process_id}
            _CLAIM_GATE.set()
            assert _wait_until(lambda: _finished_jobs(engine) == 1)
    finally:
        _CLAIM_GATE.set()
        HOOKS.clear()

    assert count(schema.claimed_executions) == 0
    assert count(schema.processes) == 1  # the peer's row


def test_evicted_thread_supervisor_retries_until_it_can_register(
    runtime: Runtime, engine: Engine, count: Callable[..., int], monkeypatch
) -> None:
    """If the database is still unavailable when an evicted supervisor tries to come back, it
    reports the error and retries after a heartbeat interval: it never stays down silently."""
    errors: list[BaseException] = []
    HOOKS.register_error(errors.append)
    register = process_registry.register
    failures = [RuntimeError("database unavailable")]

    def flaky_register(engine_: Engine, info: ProcessInfo) -> int:
        if info.kind == "Supervisor" and failures:
            raise failures.pop()
        return register(engine_, info)

    try:
        with ThreadSupervisor(runtime, _evictable_config()) as supervisor:
            pruned = supervisor.process_id
            monkeypatch.setattr(process_registry, "register", flaky_register)
            peer = _prune(engine, pruned)
            assert _wait_until(lambda: supervisor.process_id not in (None, pruned, peer))
            assert any(str(error) == "database unavailable" for error in errors)
    finally:
        HOOKS.clear()

    assert count(schema.processes) == 1  # the peer's row


def test_error_hook_may_stop_an_evicted_supervisor(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """The eviction is reported from the restart thread, not the heartbeat's, so an error hook
    that reacts by stopping the supervisor works: everything stops and nothing restarts."""
    supervisor = ThreadSupervisor(runtime, _evictable_config())
    stopped = threading.Event()

    def stop_on_eviction(error: BaseException) -> None:
        if isinstance(error, ProcessExitError):
            supervisor.stop()
            stopped.set()

    HOOKS.register_error(stop_on_eviction)
    try:
        supervisor.start()
        loops = list(supervisor._loops)
        _prune(engine, supervisor.process_id)
        assert stopped.wait(10)
        time.sleep(0.2)  # room for a wrong restart to show up
        assert supervisor.process_id is None
        assert supervisor._loops == []
        assert all(loop.stopping for loop in loops)
    finally:
        HOOKS.clear()
        supervisor.stop()

    assert count(schema.processes) == 1  # the peer's row


def test_eviction_noticed_during_stop_does_not_revive_the_supervisor(
    runtime: Runtime, engine: Engine, count: Callable[..., int], monkeypatch
) -> None:
    """The first heartbeat through after an outage can find the row pruned while stop() is
    still tearing down: the restart that eviction asks for must not run once stop() began."""
    supervisor = ThreadSupervisor(runtime, _evictable_config())
    supervisor.start()
    heartbeat = next(loop for loop in supervisor._loops if isinstance(loop, HeartbeatPoller))
    reaper = supervisor._loops[-1]  # the first loop stop() tears down
    stop_reaper = reaper.stop

    def pruned_while_stopping(timeout: float | None = 5.0) -> None:
        _prune(engine, supervisor.process_id)
        assert _wait_until(lambda: heartbeat.stopping)  # the heartbeat noticed the eviction
        stop_reaper(timeout)

    monkeypatch.setattr(reaper, "stop", pruned_while_stopping)
    supervisor.stop()
    time.sleep(0.3)  # room for a wrong restart to show up

    assert supervisor.process_id is None
    assert supervisor._loops == []
    assert count(schema.processes) == 1  # the peer's row


def test_start_hook_that_stops_the_supervisor_leaves_nothing_running(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    supervisor = ThreadSupervisor(runtime, _evictable_config())
    HOOKS.register("worker_start", supervisor.stop)
    try:
        supervisor.start()
        time.sleep(0.2)  # room for a heartbeat or reaper that should not exist
        assert supervisor.process_id is None
        assert supervisor._loops == []
        assert count(schema.processes) == 0
    finally:
        HOOKS.clear()
        supervisor.stop()


def test_stale_restart_requests_do_nothing(runtime: Runtime, count: Callable[..., int]) -> None:
    """A restart requested for an earlier set of loops (an eviction noticed as stop() began, a
    retry still pending, a second request for loops already replaced) must neither revive a
    stopped supervisor nor tear down the loops running now."""
    config = SupervisorConfig(workers=[WorkerConfig(poll_interval=0.02)], dispatchers=[])
    supervisor = ThreadSupervisor(runtime, config)
    supervisor.start()
    stale = supervisor._generation
    supervisor.stop()

    supervisor._restart(stale)
    assert supervisor.process_id is None
    assert count(schema.processes) == 0

    supervisor.start()
    try:
        running, loops = supervisor.process_id, list(supervisor._loops)
        supervisor._restart(stale)
        assert (supervisor.process_id, supervisor._loops) == (running, loops)

        current = supervisor._generation
        supervisor._restart(current)  # goes through, replacing the loops
        running, loops = supervisor.process_id, list(supervisor._loops)
        supervisor._restart(current)  # a second request for the replaced loops
        assert (supervisor.process_id, supervisor._loops) == (running, loops)
    finally:
        supervisor.stop()


def test_fork_shutdown_recovers_sigkilled_children_claims(
    runtime: Runtime, engine: Engine, add_ready, count: Callable[..., int]
) -> None:
    """A child escalated to SIGKILL never deregisters; its leftover process row used to hide
    its claims from the final recovery sweep (~6 min of job limbo). The parent must clean up
    the rows of the children it killed and recover their claims explicitly (Q-F4)."""
    from firm._core import process as process_registry
    from firm._core.process import ProcessInfo
    from firm.queue.claim import claim_ready
    from firm.queue.supervisor import ForkSupervisor

    supervisor = ForkSupervisor(runtime)
    supervisor.process_id = process_registry.register(
        engine, ProcessInfo(kind="Supervisor", name="sup-under-test", pid=1)
    )
    child_id = process_registry.register(
        engine,
        ProcessInfo(kind="Worker", name="killed-child", pid=2, supervisor_id=supervisor.process_id),
    )
    job_id = add_ready()
    assert claim_ready(engine, runtime.dialect, ["*"], 5, child_id) == [job_id]

    supervisor._shutdown()  # no live children: simulates "children were SIGKILLed and reaped"

    assert count(schema.claimed_executions) == 0
    assert count(schema.ready_executions) == 1
    assert count(schema.processes) == 0


def test_fork_supervisor_heartbeats_its_own_row(runtime: Runtime, monkeypatch) -> None:
    """Without a heartbeat of its own, a fork supervisor older than alive_threshold pruned its
    *own* registration during _supervise and looked dead for the rest of its life (Q-F3)."""
    from firm.queue.supervisor import ForkSupervisor

    created: list[object] = []

    class _RecordingHeartbeat:
        def __init__(self, engine: Engine, process_id: int, interval: float, on_error=None) -> None:
            self.process_id = process_id
            self.started = False
            self.stopped = False
            created.append(self)

        def start(self) -> None:
            self.started = True

        def stop(self, timeout: float | None = None) -> None:
            self.stopped = True

    monkeypatch.setattr("firm.queue.supervisor.HeartbeatPoller", _RecordingHeartbeat)
    monkeypatch.setattr(ForkSupervisor, "_spawn", lambda self, child: None)
    monkeypatch.setattr(ForkSupervisor, "_supervise", lambda self: None)
    monkeypatch.setattr(ForkSupervisor, "_install_signals", lambda self: None)

    ForkSupervisor(runtime).start()

    assert len(created) == 1
    heartbeat = created[0]
    assert heartbeat.started and heartbeat.stopped
    assert isinstance(heartbeat.process_id, int)


_FAKE_PIDS = (900001, 900002)


class _StubHeartbeat:
    """Stands in for the fork supervisor's own HeartbeatPoller: no thread, no database."""

    def __init__(self, engine: Engine, process_id: int, interval: float, on_error=None) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self, timeout: float | None = None) -> None:
        pass


@pytest.fixture
def fork_stubs(monkeypatch) -> list[tuple[int, int]]:
    """Run ForkSupervisor's supervise/shutdown control flow in-process and deterministically: no
    fork (children are fake pids), no signal handlers, no heartbeat thread. Returns the
    ``(pid, signal)`` pairs the supervisor sends to its children."""
    signalled: list[tuple[int, int]] = []
    pids = iter(_FAKE_PIDS)

    def fake_spawn(self: ForkSupervisor, child: sup_mod.ChildConfig) -> None:
        self._children[next(pids)] = child

    monkeypatch.setattr(sup_mod, "HeartbeatPoller", _StubHeartbeat)
    monkeypatch.setattr(sup_mod, "_signal_pid", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(ForkSupervisor, "_spawn", fake_spawn)
    monkeypatch.setattr(ForkSupervisor, "_install_signals", lambda self: None)
    return signalled


def _terminated(signalled: list[tuple[int, int]]) -> list[int]:
    """The pids asked to drain (fake pids are never reaped, so each is later SIGKILLed too)."""
    return sorted(pid for pid, sig in signalled if sig == signal.SIGTERM)


def _fork_config() -> SupervisorConfig:
    return SupervisorConfig(
        workers=[WorkerConfig(), WorkerConfig()],
        dispatchers=[],
        heartbeat_interval=0.0,  # the periodic reap runs on every supervise iteration
        shutdown_timeout=0.05,  # fake pids are never reaped: don't wait 5 s for them
    )


def test_fork_supervisor_survives_a_transient_reap_error(
    runtime: Runtime, count: Callable[..., int], fork_stubs: list[tuple[int, int]], monkeypatch
) -> None:
    """A database hiccup during the periodic reap (a failover, a lock) used to escape start():
    the supervisor died with a traceback, its children ran on unsupervised and were never
    restarted, and nothing pruned stale rows for that deployment any more. The error is reported
    through ``on_thread_error`` — as thread mode's ReaperLoop already does — and supervision goes
    on; a later normal shutdown still signals the children and deregisters the supervisor."""
    boom = OperationalError("DELETE FROM firm_queue_processes", None, Exception("db is locked"))
    supervisor = ForkSupervisor(runtime, _fork_config())
    reaps = 0

    def flaky_reap(rt: Runtime, threshold: float) -> int:
        nonlocal reaps
        reaps += 1
        if reaps == 1:
            return 0  # the startup reap
        if reaps == 2:
            raise boom  # the first periodic tick fails
        supervisor._stop.set()  # a later tick succeeds; then ask for a normal shutdown
        return 0

    monkeypatch.setattr(sup_mod, "reap_dead_processes", flaky_reap)
    errors: list[BaseException] = []
    HOOKS.register_error(errors.append)
    try:
        supervisor.start()
    finally:
        HOOKS.clear()

    assert boom in errors
    assert reaps >= 3, "supervision stopped after the transient error"
    assert _terminated(fork_stubs) == sorted(_FAKE_PIDS)
    assert count(schema.processes) == 0


def test_fork_supervisor_shuts_down_when_supervise_raises(
    runtime: Runtime, count: Callable[..., int], fork_stubs: list[tuple[int, int]], monkeypatch
) -> None:
    """Whatever escapes the supervise loop (say ``os.fork`` failing while replacing a dead
    child), the children are signalled and the registry cleaned up before it propagates."""
    monkeypatch.setattr(sup_mod, "reap_dead_processes", lambda rt, threshold: 0)

    def exploding_supervise(self: ForkSupervisor) -> None:
        raise RuntimeError("unexpected failure in the supervise loop")

    monkeypatch.setattr(ForkSupervisor, "_supervise", exploding_supervise)
    supervisor = ForkSupervisor(runtime, _fork_config())
    with pytest.raises(RuntimeError, match="unexpected failure"):
        supervisor.start()

    assert _terminated(fork_stubs) == sorted(_FAKE_PIDS)
    assert count(schema.processes) == 0


def test_fork_shutdown_reports_a_database_error_instead_of_raising(
    runtime: Runtime, engine: Engine, fork_stubs: list[tuple[int, int]], monkeypatch
) -> None:
    """By the time _shutdown reaches the registry, the children are already signalled and reaped;
    a database that is still down there is a partial failure to report, not a reason to raise
    (which would only bury whatever ended supervision in the first place)."""
    supervisor = ForkSupervisor(runtime, _fork_config())
    supervisor.process_id = process_registry.register(
        engine, ProcessInfo(kind="Supervisor", name="sup-under-test", pid=1)
    )
    boom = OperationalError("DELETE FROM firm_queue_processes", None, Exception("connection lost"))

    def failing_deregister(engine_: Engine, process_id: int) -> None:
        raise boom

    monkeypatch.setattr(process_registry, "deregister", failing_deregister)
    errors: list[BaseException] = []
    HOOKS.register_error(errors.append)
    try:
        supervisor._shutdown()
    finally:
        HOOKS.clear()

    assert errors == [boom]


def test_scheduler_config_is_reachable_through_supervisor_config() -> None:
    """QL-5: SupervisorConfig always built SchedulerConfig() with defaults, making its
    poll_interval unreachable; it is now a configurable field."""
    from firm.queue.scheduler import RecurringTask
    from firm.queue.supervisor import SchedulerConfig

    config = SupervisorConfig(
        recurring=[RecurringTask(key="t", schedule="* * * * *", job=gated_job)],
        scheduler=SchedulerConfig(poll_interval=9.0),
    )
    assert config.child_configs()[-1].poll_interval == 9.0


def test_dispatcher_builds_a_maintenance_loop_by_default(runtime: Runtime) -> None:
    from firm.queue.dispatcher import DispatcherLoop, MaintenanceLoop
    from firm.queue.supervisor import _build_loops

    loops = _build_loops(runtime, DispatcherConfig(), [], None)
    assert any(isinstance(loop, DispatcherLoop) for loop in loops)
    assert any(isinstance(loop, MaintenanceLoop) for loop in loops)


def test_concurrency_maintenance_can_be_disabled(runtime: Runtime) -> None:
    """Upstream: dispatcher_test.rb::"concurrency maintenance is optional". With the toggle off
    the dispatcher runs alone — no MaintenanceLoop is built."""
    from firm.queue.dispatcher import DispatcherLoop, MaintenanceLoop
    from firm.queue.supervisor import _build_loops

    loops = _build_loops(runtime, DispatcherConfig(concurrency_maintenance=False), [], None)
    assert any(isinstance(loop, DispatcherLoop) for loop in loops)
    assert not any(isinstance(loop, MaintenanceLoop) for loop in loops)


def test_default_configuration_processes_all_queues_and_dispatches() -> None:
    # upstream: configuration_test.rb "default configuration to process all queues and dispatch".
    # With no explicit queues a default config yields a worker over every queue ("*") plus a
    # dispatcher (and no scheduler).
    config = SupervisorConfig()

    assert len(config.workers) == 1
    assert config.workers[0].queues == ("*",)
    assert len(config.dispatchers) == 1
    assert isinstance(config.dispatchers[0], DispatcherConfig)

    children = config.child_configs()
    assert any(isinstance(c, WorkerConfig) and c.queues == ("*",) for c in children)
    assert any(isinstance(c, DispatcherConfig) for c in children)
    assert not any(isinstance(c, SchedulerConfig) for c in children)


def test_invalid_configuration_is_rejected() -> None:
    # upstream: configuration_test.rb "validate configuration". A config with no workers,
    # dispatchers, or recurring tasks has nothing to run and is rejected at construction.
    with pytest.raises(ValueError):
        SupervisorConfig(workers=[], dispatchers=[])


def test_multiple_workers_with_the_same_configuration_are_independent() -> None:
    # upstream: configuration_test.rb "mulitple workers with the same configuration". Building N
    # workers from one template yields N independent WorkerConfig objects.
    template = WorkerConfig(queues=("default",), threads=5)
    workers = [WorkerConfig(queues=template.queues, threads=template.threads) for _ in range(3)]
    config = SupervisorConfig(workers=workers)

    assert len(config.workers) == 3
    for worker in config.workers:
        assert worker.queues == ("default",)
        assert worker.threads == 5

    # Independent instances: mutating one must not affect the others.
    config.workers[0].threads = 1
    assert [w.threads for w in config.workers] == [1, 5, 5]
    assert len([c for c in config.child_configs() if isinstance(c, WorkerConfig)]) == 3


def test_no_scheduler_without_static_recurring_tasks() -> None:
    # upstream: configuration_test.rb "no recurring scheduler is set up when there are no static
    # recurring tasks". With recurring=[] no SchedulerConfig child is created.
    config = SupervisorConfig()
    assert config.recurring == []
    assert not any(isinstance(c, SchedulerConfig) for c in config.child_configs())


_PROCESSED: list[int] = []


@bq.job()
def _pool_job(value: int) -> None:
    _PROCESSED.append(value)


def test_worker_processes_more_jobs_than_pool_size(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    # upstream: worker_test.rb "claim and process more enqueued jobs than the pool size allows to
    # process at once". Enqueue more jobs than the worker's pool size; all are eventually run.
    _PROCESSED.clear()
    pool_size = 2
    total = pool_size * 3  # comfortably more than one pool-full

    for i in range(total):
        _pool_job.enqueue(i)

    worker = Worker(runtime, threads=pool_size, poll_interval=0.01)
    worker.start()
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and len(_PROCESSED) < total:
            time.sleep(0.02)
    finally:
        worker.stop()

    assert sorted(_PROCESSED) == list(range(total))
    assert count(schema.ready_executions) == 0
    assert count(schema.claimed_executions) == 0
