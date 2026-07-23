"""Thread-mode supervisor end-to-end: enqueue -> dispatch -> worker -> finished."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import Engine, func, select

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.supervisor import (
    DispatcherConfig,
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
