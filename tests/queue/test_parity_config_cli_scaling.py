"""Parity tests ported from rails/solid_queue covering configuration, the CLI mode
selection, and worker/dispatcher scaling behaviour.

Upstream sources (solid_queue/test):
  * test/unit/configuration_test.rb
  * test/unit/dispatcher_test.rb
  * test/unit/worker_test.rb
  * test/integration/cli_test.rb (mode selection)

solid_queue drives everything through a single ``SolidQueue::Configuration`` object that
yields ``processes`` (workers/dispatchers/schedulers). firm instead exposes plain
dataclasses (``SupervisorConfig`` / ``WorkerConfig`` / ``DispatcherConfig``) plus a click
group in ``firm.queue.cli``. Each test below adapts the upstream intent to those shapes,
asserting on the resulting config objects or on the supervisor class the CLI selects.

Some upstream knobs have no firm equivalent yet (config validation, a maintenance on/off
toggle, a mode env var). Those are marked ``xfail(strict=False)`` so the gap is visible
without going red.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from click.testing import CliRunner
from sqlalchemy import Engine

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import cli, schema
from firm.queue.dispatcher import dispatch_once
from firm.queue.supervisor import (
    DispatcherConfig,
    SchedulerConfig,
    SupervisorConfig,
    WorkerConfig,
)
from firm.queue.worker import Worker

# ---------------------------------------------------------------------------
# configuration_test.rb
# ---------------------------------------------------------------------------


def test_default_configuration_processes_all_queues_and_dispatches() -> None:
    """Port of configuration_test.rb::"default configuration to process all queues and
    dispatch": with no explicit queues a default config yields a worker that processes
    every queue ("*") plus a dispatcher (and no scheduler)."""
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
    """Port of configuration_test.rb::"validate configuration": a config with no workers,
    dispatchers, or recurring tasks has nothing to run and is rejected at construction."""
    with pytest.raises(ValueError):
        SupervisorConfig(workers=[], dispatchers=[])


def test_multiple_workers_with_the_same_configuration_are_independent() -> None:
    """Port of configuration_test.rb::"mulitple workers with the same configuration":
    building N workers from one template yields N independent WorkerConfig objects."""
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

    worker_children = [c for c in config.child_configs() if isinstance(c, WorkerConfig)]
    assert len(worker_children) == 3


def test_no_scheduler_without_static_recurring_tasks() -> None:
    """Port of configuration_test.rb::"no recurring scheduler is set up when there are no
    static recurring tasks": with recurring=[] no SchedulerConfig child is created."""
    config = SupervisorConfig()
    assert config.recurring == []
    assert not any(isinstance(c, SchedulerConfig) for c in config.child_configs())


# ---------------------------------------------------------------------------
# cli_test.rb -- supervisor mode selection
# ---------------------------------------------------------------------------


class _FakeSupervisor:
    """Stand-in for the real supervisors so ``start`` records the chosen mode without
    forking or entering its blocking run loop."""

    selected: str | None = None

    def __init__(self, runtime: object, config: object) -> None:
        self.runtime = runtime
        self.config = config

    def start(self) -> None:
        type(self)._record()

    def stop(self) -> None:  # used only by the thread branch
        pass

    @classmethod
    def _record(cls) -> None:  # pragma: no cover - overridden per subclass
        raise NotImplementedError


class _FakeForkSupervisor(_FakeSupervisor):
    @classmethod
    def _record(cls) -> None:
        _FakeSupervisor.selected = "fork"


class _FakeThreadSupervisor(_FakeSupervisor):
    @classmethod
    def _record(cls) -> None:
        _FakeSupervisor.selected = "thread"


@pytest.fixture
def patched_supervisors(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSupervisor]:
    """Replace the supervisor classes the ``start`` command instantiates, and stub out the
    real DB ``configure`` so no engine is created. Returns the recorder class."""
    _FakeSupervisor.selected = None
    monkeypatch.setattr(cli, "ForkSupervisor", _FakeForkSupervisor)
    monkeypatch.setattr(cli, "ThreadSupervisor", _FakeThreadSupervisor)
    # _configure() only needs to return *something* truthy; the fakes never touch it.
    monkeypatch.setattr(cli, "configure", lambda database_url: object())

    # The thread branch enters ``while True: time.sleep(1)`` after start(); make the first
    # sleep raise KeyboardInterrupt so the CLI's own ``except`` calls stop() and returns 0.
    def _interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", _interrupt)
    return _FakeSupervisor


def test_mode_defaults_to_fork(patched_supervisors: type[_FakeSupervisor]) -> None:
    """Port of cli_test.rb::"mode defaults to fork when there is no env var or option":
    with neither an override option nor an env var, ``firm-queue start`` runs the fork
    supervisor."""
    result = CliRunner().invoke(cli.main, ["start", "--database-url", "sqlite://"])
    assert result.exit_code == 0, result.output
    assert patched_supervisors.selected == "fork"


def test_mode_option_selects_thread(patched_supervisors: type[_FakeSupervisor]) -> None:
    """Port of cli_test.rb mode-override variant: ``--mode thread`` selects the thread
    supervisor (the real CLI flag is ``--mode`` with choices fork/thread)."""
    result = CliRunner().invoke(
        cli.main, ["start", "--database-url", "sqlite://", "--mode", "thread"]
    )
    assert result.exit_code == 0, result.output
    assert patched_supervisors.selected == "thread"


def test_mode_option_selects_fork_explicitly(
    patched_supervisors: type[_FakeSupervisor],
) -> None:
    """The explicit ``--mode fork`` form also selects the fork supervisor."""
    result = CliRunner().invoke(
        cli.main, ["start", "--database-url", "sqlite://", "--mode", "fork"]
    )
    assert result.exit_code == 0, result.output
    assert patched_supervisors.selected == "fork"


@pytest.mark.xfail(
    strict=False,
    reason="firm's `start` command has no env var for mode (only --mode); solid_queue "
    "reads SOLID_QUEUE_MODE / a configurable env var, so this override does not exist.",
)
def test_mode_env_var_override(
    patched_supervisors: type[_FakeSupervisor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port of cli_test.rb env-var variant: setting the mode env var should make the CLI
    pick the thread supervisor even without ``--mode``. firm exposes no such env var, so
    this xfails (the env var is ignored and the default fork supervisor runs)."""
    monkeypatch.setenv("FIRM_QUEUE_MODE", "thread")
    result = CliRunner().invoke(cli.main, ["start", "--database-url", "sqlite://"])
    assert result.exit_code == 0, result.output
    assert patched_supervisors.selected == "thread"


# ---------------------------------------------------------------------------
# dispatcher_test.rb
# ---------------------------------------------------------------------------


@bq.job()
def _parity_dispatch_job() -> None:
    pass


def _add_scheduled(engine: Engine, scheduled_at, queue: str = "default", priority: int = 0) -> int:
    from sqlalchemy import insert

    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(
                queue_name=queue, class_name="J", priority=priority, scheduled_at=scheduled_at
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(schema.scheduled_executions).values(
                job_id=job_id, queue_name=queue, priority=priority, scheduled_at=scheduled_at
            )
        )
    return job_id


def test_two_dispatch_passes_do_not_double_promote(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """Port of dispatcher_test.rb::"run more than one instance of the dispatcher":
    two dispatch passes over the same due scheduled jobs promote each job to ready exactly
    once (the second pass finds nothing left to promote)."""
    from datetime import timedelta

    from firm._core.clock import now_utc

    for _ in range(4):
        _add_scheduled(engine, now_utc() - timedelta(minutes=1))

    first = dispatch_once(runtime)
    second = dispatch_once(runtime)

    assert first == 4
    assert second == 0
    assert count(schema.ready_executions) == 4
    assert count(schema.scheduled_executions) == 0


@pytest.mark.xfail(
    strict=False,
    reason="firm's SupervisorConfig/DispatcherConfig have no flag to disable concurrency "
    "maintenance: the supervisor always builds a MaintenanceLoop alongside every "
    "DispatcherLoop (gap vs solid_queue Dispatcher's optional concurrency maintenance).",
)
def test_concurrency_maintenance_is_optional() -> None:
    """Port of dispatcher_test.rb::"concurrency maintenance is optional": maintenance can
    be turned off via config. firm has no such toggle, so we assert one exists; the missing
    attribute makes this xfail and documents the gap."""
    config = DispatcherConfig()
    assert getattr(config, "concurrency_maintenance", True) is False or hasattr(
        config, "concurrency_maintenance"
    ), "DispatcherConfig should expose a concurrency-maintenance toggle"
    # Force the gap explicit: there is currently no such attribute.
    assert hasattr(config, "concurrency_maintenance")


# ---------------------------------------------------------------------------
# worker_test.rb
# ---------------------------------------------------------------------------


_PROCESSED: list[int] = []


@bq.job()
def _parity_pool_job(value: int) -> None:
    _PROCESSED.append(value)


def test_worker_processes_more_jobs_than_pool_size(
    runtime: Runtime, engine: Engine, count: Callable[..., int]
) -> None:
    """Port of worker_test.rb::"claim and process more enqueued jobs than the pool size
    allows to process at once": enqueue more jobs than the worker's thread/pool size and run
    the worker; all jobs are eventually processed."""
    _PROCESSED.clear()
    pool_size = 2
    total = pool_size * 3  # comfortably more than one pool-full

    for i in range(total):
        _parity_pool_job.enqueue(i)

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
