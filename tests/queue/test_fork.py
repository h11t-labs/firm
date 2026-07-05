"""Fork-mode specs: a real forked worker processes jobs; the fork supervisor drains on TERM.

Skipped where ``os.fork`` is unavailable; SQLite-only (the fork model is backend-independent).
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable

import pytest
from sqlalchemy import Engine, func, select

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.supervisor import (
    DispatcherConfig,
    ForkSupervisor,
    SupervisorConfig,
    WorkerConfig,
)
from firm.queue.worker import Worker

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")


@pytest.fixture(autouse=True)
def _sqlite_only(is_sqlite: bool) -> None:
    if not is_sqlite:
        pytest.skip("fork-mode tests are SQLite-only")


@bq.job()
def fork_job(x: int) -> None:
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


def test_forked_worker_processes_job(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    fork_job.enqueue(1)
    pid = os.fork()
    if pid == 0:  # child
        try:
            runtime.reset()
            worker = Worker(runtime, poll_interval=0.02)
            worker.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _finished(runtime.engine) < 1:
                time.sleep(0.02)
            worker.stop()
        finally:
            os._exit(0)

    os.waitpid(pid, 0)
    assert _finished(engine) == 1
    assert count(schema.claimed_executions) == 0


def test_fork_supervisor_drains_then_terminates(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    fork_job.enqueue(1)
    fork_job.enqueue(2)
    pid = os.fork()
    if pid == 0:
        try:
            runtime.reset()
            ForkSupervisor(
                runtime,
                SupervisorConfig(
                    workers=[WorkerConfig(poll_interval=0.02)],
                    dispatchers=[DispatcherConfig(poll_interval=0.05)],
                    shutdown_timeout=2.0,
                ),
            ).start()
        finally:
            os._exit(0)

    drained = _wait_until(lambda: _finished(engine) == 2, timeout=15.0)
    os.kill(pid, signal.SIGTERM)
    _, status = os.waitpid(pid, 0)

    assert drained
    assert os.WIFEXITED(status) or os.WIFSIGNALED(status)
    assert count(schema.claimed_executions) == 0
