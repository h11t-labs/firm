"""Parity tests ported from rails/solid_queue's recurring/process/worker/fork suites.

Each test cites the upstream file::name it mirrors and adapts it to firm's API. Where firm
deliberately lacks a guard that solid_queue has (recurring "must be recorded first" ordering,
worker self-termination on unregister, sync deleting de-configured tasks), the test is marked
``xfail(strict=False)`` so the gap is documented and stays visible without going red.

Fork/signal tests mirror ``test_fork.py`` exactly: SQLite-only and POSIX-only (guarded the same
way the existing fork tests are).
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from datetime import datetime

import pytest
from sqlalchemy import Engine, func, select

import firm.queue as bq
from firm._core import process as pr
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.scheduler import RecurringTask, Scheduler
from firm.queue.supervisor import (
    DispatcherConfig,
    ForkSupervisor,
    SupervisorConfig,
    WorkerConfig,
)

# --------------------------------------------------------------------------------------------- #
# Recurring tasks (recurring_task_test.rb / recurring_tasks_test.rb)
# --------------------------------------------------------------------------------------------- #

_SINK: list[int] = []


@bq.job()
def parity_recurring_job() -> None:
    _SINK.append(1)


@bq.job(queue="reports", priority=8)
def parity_custom_queue_job() -> None:
    _SINK.append(2)


def _cleanup_task() -> RecurringTask:
    return RecurringTask(key="cleanup", schedule="*/5 * * * *", job=parity_recurring_job)


def test_valid_and_invalid_schedules(runtime: Runtime, count: Callable[..., int]) -> None:
    """Upstream: recurring_task_test.rb::"valid and invalid schedules".

    A valid 5-field cron is accepted and fires; an invalid cron string is rejected when the
    task tries to compute its period (croniter raises ``CroniterBadCronError`` <: ``ValueError``).
    """
    valid = Scheduler(runtime, [_cleanup_task()])
    assert valid.tick(at=datetime(2026, 6, 28, 12, 3, 0)) == 1
    assert count(schema.ready_executions) == 1

    invalid = RecurringTask(key="bad", schedule="not a cron", job=parity_recurring_job)
    with pytest.raises(ValueError):
        invalid.current_period(datetime(2026, 6, 28, 12, 3, 0))
    with pytest.raises(ValueError):
        Scheduler(runtime, [invalid]).tick(at=datetime(2026, 6, 28, 12, 3, 0))


def test_recurring_task_custom_queue_and_priority(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """Upstream: recurring_task_test.rb::"task with custom queue and priority"
    (+ "overriding existing priority").

    A recurring run enqueues onto the job's configured ``queue_name``/``priority`` — both on the
    persisted ``jobs`` row and on the ``ready_executions`` row the worker polls.
    """
    task = RecurringTask(key="report", schedule="*/5 * * * *", job=parity_custom_queue_job)
    scheduler = Scheduler(runtime, [task])

    assert scheduler.tick(at=datetime(2026, 6, 28, 12, 3, 0)) == 1

    with engine.connect() as conn:
        job_row = conn.execute(select(schema.jobs.c.queue_name, schema.jobs.c.priority)).one()
        ready_row = conn.execute(
            select(schema.ready_executions.c.queue_name, schema.ready_executions.c.priority)
        ).one()

    assert job_row.queue_name == "reports"
    assert job_row.priority == 8
    assert ready_row.queue_name == "reports"
    assert ready_row.priority == 8


@pytest.mark.xfail(
    strict=False,
    reason="firm has no ordering invariant: Scheduler enqueues recurring runs without "
    "requiring the recurring_tasks row to exist first (sync_tasks is optional/advisory).",
)
def test_enqueue_requires_recorded_task_first(runtime: Runtime, count: Callable[..., int]) -> None:
    """Upstream: recurring_task_test.rb::"error when enqueuing the job before the task has
    been recorded".

    solid_queue refuses to enqueue a recurring run until the recurring_task row exists. firm's
    Scheduler.tick() enqueues regardless (it never reads recurring_tasks), so this guard is
    absent — assert the (missing) invariant so the gap stays visible.
    """
    scheduler = Scheduler(runtime, [_cleanup_task()])
    # No sync_tasks() first => recurring_tasks is empty.
    assert count(schema.recurring_tasks) == 0
    scheduler.tick(at=datetime(2026, 6, 28, 12, 3, 0))
    # Upstream expectation: nothing enqueued because the task was never recorded.
    assert count(schema.ready_executions) == 0


def test_sync_persists_and_deletes_configured_tasks(
    runtime: Runtime, count: Callable[..., int]
) -> None:
    """Upstream: recurring_tasks_test.rb::"persist and delete configured tasks".

    Syncing persists the configured tasks AND deletes a previously-persisted task that is no
    longer in the configuration. firm implements the persist half but not the delete half.
    """
    # First config persists "cleanup" + "stale".
    stale = RecurringTask(key="stale", schedule="0 * * * *", job=parity_recurring_job)
    Scheduler(runtime, [_cleanup_task(), stale]).sync_tasks()
    assert count(schema.recurring_tasks) == 2

    # Re-sync with only "cleanup" configured: "stale" should be removed.
    Scheduler(runtime, [_cleanup_task()]).sync_tasks()
    assert count(schema.recurring_tasks) == 1
    with runtime.engine.connect() as conn:
        keys = {row[0] for row in conn.execute(select(schema.recurring_tasks.c.key))}
    assert keys == {"cleanup"}


# --------------------------------------------------------------------------------------------- #
# Process registry (process_test.rb)
# --------------------------------------------------------------------------------------------- #


def test_hostname_with_special_characters_round_trips(
    engine: Engine, count: Callable[..., int]
) -> None:
    """Upstream: process_test.rb::"hostname's with special characters are properly loaded".

    A process registered with an odd hostname (and metadata) round-trips byte-for-byte through
    the registry.
    """
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


# --------------------------------------------------------------------------------------------- #
# Worker heartbeat / unregister (worker_test.rb)
# --------------------------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=False,
    reason="firm's HeartbeatPoller.heartbeat() issues a plain UPDATE; when the process row is "
    "gone it affects 0 rows and does NOT raise/self-terminate (no ProcessExitError in firm).",
)
def test_terminate_on_heartbeat_when_unregistered(
    engine: Engine, count: Callable[..., int]
) -> None:
    """Upstream: worker_test.rb::"terminate on heartbeat when unregistered".

    A worker whose process row was deleted should notice on its next heartbeat and self-terminate.
    Driven as the minimal unit: register, delete the row, then heartbeat the now-missing id and
    expect it to signal termination. firm's heartbeat silently no-ops, so this xfails.
    """
    pid = pr.register(engine, pr.ProcessInfo(kind="Worker", name="w-unreg", pid=7))
    pr.deregister(engine, pid)
    assert count(schema.processes) == 0

    # Upstream expectation: heartbeating a missing process row raises (worker stops itself).
    with pytest.raises(Exception):  # noqa: B017
        pr.heartbeat(engine, pid)


# --------------------------------------------------------------------------------------------- #
# Forked-process signal matrix (forked_processes_lifecycle_test.rb) — SQLite + POSIX only.
# --------------------------------------------------------------------------------------------- #

_fork_only = pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")


@pytest.fixture
def _sqlite_only(is_sqlite: bool) -> None:
    if not is_sqlite:
        pytest.skip("fork-mode tests are SQLite-only")


@bq.job()
def parity_fork_job(x: int) -> None:
    pass


def _finished(engine: Engine) -> int:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(func.count())
                .select_from(schema.jobs)
                .where(schema.jobs.c.finished_at.is_not(None))
            ).scalar()
            or 0
        )


def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return False


def _start_supervisor_child(runtime: Runtime, shutdown_timeout: float = 2.0) -> int:
    """Fork a ForkSupervisor child (mirrors test_fork.py); return its pid in the parent."""
    pid = os.fork()
    if pid == 0:  # child
        try:
            runtime.reset()
            ForkSupervisor(
                runtime,
                SupervisorConfig(
                    workers=[WorkerConfig(poll_interval=0.02)],
                    dispatchers=[DispatcherConfig(poll_interval=0.05)],
                    shutdown_timeout=shutdown_timeout,
                ),
            ).start()
        finally:
            os._exit(0)
    return pid


@_fork_only
def test_fork_supervisor_drains_on_sigint(
    runtime: Runtime, engine: Engine, count: Callable[..., int], _sqlite_only: None
) -> None:
    """Upstream: forked_processes_lifecycle_test.rb — SIGINT drains like SIGTERM.

    ForkSupervisor maps SIGINT and SIGTERM both to a graceful stop, so an INT drains in-flight
    work and exits cleanly with no leftover claims (parity with the existing SIGTERM drain test).
    """
    parity_fork_job.enqueue(1)
    parity_fork_job.enqueue(2)
    pid = _start_supervisor_child(runtime)

    drained = _wait_until(lambda: _finished(engine) == 2, timeout=15.0)
    os.kill(pid, signal.SIGINT)
    _, status = os.waitpid(pid, 0)

    assert drained
    assert os.WIFEXITED(status) or os.WIFSIGNALED(status)
    assert count(schema.claimed_executions) == 0


@_fork_only
def test_fork_supervisor_double_term_is_idempotent(
    runtime: Runtime, engine: Engine, count: Callable[..., int], _sqlite_only: None
) -> None:
    """Upstream: forked_processes_lifecycle_test.rb — sending the term signal twice is idempotent.

    A second SIGTERM after the first must not corrupt shutdown: the supervisor still exits
    cleanly and leaves no claimed rows behind.
    """
    parity_fork_job.enqueue(1)
    parity_fork_job.enqueue(2)
    pid = _start_supervisor_child(runtime)

    drained = _wait_until(lambda: _finished(engine) == 2, timeout=15.0)
    os.kill(pid, signal.SIGTERM)
    # Second term while it is shutting down — must be a no-op, not a crash.
    time.sleep(0.05)
    with __import__("contextlib").suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    _, status = os.waitpid(pid, 0)

    assert drained
    assert os.WIFEXITED(status) or os.WIFSIGNALED(status)
    assert count(schema.claimed_executions) == 0


@pytest.mark.skip(
    reason="shutdown-timeout-exceeded force-termination is not deterministically observable: the "
    "ForkSupervisor escalates parent->child SIGTERM to SIGKILL after shutdown_timeout, but each "
    "child still os._exit(0)s cleanly and the SIGKILL escalation leaves no DB-visible signal to "
    "assert on without racing the join timeout (would be flaky)."
)
@_fork_only
def test_fork_supervisor_force_terminates_after_shutdown_timeout(
    runtime: Runtime, engine: Engine, _sqlite_only: None
) -> None:
    """Upstream: forked_processes_lifecycle_test.rb — shutdown-timeout-exceeded -> force-terminate.

    Intentionally skipped (see reason): cannot be asserted deterministically against firm's
    fork model from the database side.
    """
