"""Supervisor — run the worker/dispatcher/scheduler processes and keep them alive.

Two modes:

* :class:`ThreadSupervisor` runs every component as a thread in one process. Simple, portable,
  great for development, tests, and embedding.
* :class:`ForkSupervisor` forks a child process per component (the production default). The
  supervisor reaps and restarts dead children, prunes processes with stale heartbeats and
  recovers their in-flight jobs, and shuts down on signals: TERM/INT drain gracefully within
  ``shutdown_timeout``; QUIT exits immediately.

Forking happens **before** any threads are started, and each child calls ``runtime.reset()``
first so it never reuses a SQLite handle inherited from the parent.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from types import FrameType

from .._core import process as process_registry
from .._core.config import Runtime
from .._core.poller import InterruptiblePoller
from .._core.process import HeartbeatPoller, ProcessInfo
from .dispatcher import DispatcherLoop, MaintenanceLoop
from .hooks import HOOKS
from .recovery import recover_orphaned_claims
from .scheduler import RecurringTask, Scheduler, SchedulerLoop
from .worker import Worker


@dataclass
class WorkerConfig:
    queues: tuple[str, ...] = ("*",)
    threads: int = 3
    poll_interval: float = 0.1


@dataclass
class DispatcherConfig:
    batch_size: int = 500
    poll_interval: float = 1.0
    maintenance_interval: float = 600.0


@dataclass
class SchedulerConfig:
    poll_interval: float = 5.0


ChildConfig = WorkerConfig | DispatcherConfig | SchedulerConfig


@dataclass
class SupervisorConfig:
    workers: list[WorkerConfig] = field(default_factory=lambda: [WorkerConfig()])
    dispatchers: list[DispatcherConfig] = field(default_factory=lambda: [DispatcherConfig()])
    recurring: list[RecurringTask] = field(default_factory=list)
    alive_threshold: float = 300.0
    shutdown_timeout: float = 5.0
    heartbeat_interval: float = 60.0

    def __post_init__(self) -> None:
        if not self.workers and not self.dispatchers and not self.recurring:
            raise ValueError(
                "SupervisorConfig has nothing to run: configure at least one worker, "
                "dispatcher, or recurring task."
            )

    def child_configs(self) -> list[ChildConfig]:
        children: list[ChildConfig] = [*self.workers, *self.dispatchers]
        if self.recurring:
            children.append(SchedulerConfig())
        return children


def _kind_of(config: ChildConfig) -> str:
    if isinstance(config, WorkerConfig):
        return "worker"
    if isinstance(config, DispatcherConfig):
        return "dispatcher"
    return "scheduler"


def _build_loops(
    runtime: Runtime,
    config: ChildConfig,
    recurring: list[RecurringTask],
    process_id: int | None,
) -> list[InterruptiblePoller]:
    if isinstance(config, WorkerConfig):
        return [
            Worker(
                runtime,
                queues=config.queues,
                threads=config.threads,
                poll_interval=config.poll_interval,
                process_id=process_id,
            )
        ]
    if isinstance(config, DispatcherConfig):
        return [
            DispatcherLoop(
                runtime, batch_size=config.batch_size, poll_interval=config.poll_interval
            ),
            MaintenanceLoop(
                runtime, interval=config.maintenance_interval, batch_size=config.batch_size
            ),
        ]
    return [SchedulerLoop(Scheduler(runtime, recurring), poll_interval=config.poll_interval)]


class ThreadSupervisor:
    """Run all components as threads in the current process."""

    def __init__(self, runtime: Runtime, config: SupervisorConfig | None = None) -> None:
        self.runtime = runtime
        self.config = config or SupervisorConfig()
        self._loops: list[InterruptiblePoller] = []
        self.process_id: int | None = None

    def start(self) -> None:
        recover_orphaned_claims(self.runtime)
        self.process_id = process_registry.register(
            self.runtime.engine,
            ProcessInfo(
                kind="Supervisor",
                name=process_registry.generate_name("supervisor"),
                pid=os.getpid(),
            ),
        )
        HOOKS.fire("supervisor_start")
        for child in self.config.child_configs():
            # Claims carry the supervisor's own (heartbeated) process row: if this process
            # dies, the row goes stale, gets pruned, and the claims are recovered. A NULL
            # process_id would make them invisible to recover_orphaned_claims forever.
            loops = _build_loops(self.runtime, child, self.config.recurring, self.process_id)
            for loop in loops:
                loop.start()
                self._loops.append(loop)
            HOOKS.fire(f"{_kind_of(child)}_start")
        heartbeat = HeartbeatPoller(
            self.runtime.engine, self.process_id, self.config.heartbeat_interval
        )
        heartbeat.start()
        self._loops.append(heartbeat)

    def stop(self) -> None:
        for loop in reversed(self._loops):
            loop.stop(timeout=self.config.shutdown_timeout)
        self._loops.clear()
        if self.process_id is not None:
            process_registry.deregister(self.runtime.engine, self.process_id)
            self.process_id = None
        HOOKS.fire("supervisor_stop")

    def __enter__(self) -> ThreadSupervisor:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _run_child(
    runtime: Runtime, supervisor_config: SupervisorConfig, child: ChildConfig, supervisor_id: int
) -> int:
    """Body of a forked child: register, run loops + heartbeat, drain on SIGTERM."""
    # Drop (not close) the connections inherited from the parent: they are the parent's live
    # sockets, and closing them here would terminate server sessions the parent still holds.
    runtime.reset(close=False)
    stop = threading.Event()

    def _graceful(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    def _immediate(_signum: int, _frame: FrameType | None) -> None:
        os._exit(1)

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGQUIT, _immediate)

    kind = _kind_of(child)
    process_id = process_registry.register(
        runtime.engine,
        ProcessInfo(
            kind=kind.capitalize(),
            name=process_registry.generate_name(kind),
            pid=os.getpid(),
            supervisor_id=supervisor_id,
        ),
    )
    loops = _build_loops(runtime, child, supervisor_config.recurring, process_id)
    heartbeat = HeartbeatPoller(runtime.engine, process_id, supervisor_config.heartbeat_interval)
    for loop in loops:
        loop.start()
    heartbeat.start()
    HOOKS.fire(f"{kind}_start")

    stop.wait()

    HOOKS.fire(f"{kind}_stop")
    for loop in reversed(loops):
        loop.stop(timeout=supervisor_config.shutdown_timeout)
    heartbeat.stop()
    process_registry.deregister(runtime.engine, process_id)
    HOOKS.fire(f"{kind}_exit")
    return 0


class ForkSupervisor:
    """Fork a child process per component; reap, restart, and recover."""

    def __init__(self, runtime: Runtime, config: SupervisorConfig | None = None) -> None:
        self.runtime = runtime
        self.config = config or SupervisorConfig()
        self.process_id: int | None = None
        self._children: dict[int, ChildConfig] = {}
        self._stop = threading.Event()
        self._immediate = False
        self._heartbeat: HeartbeatPoller | None = None

    def start(self) -> None:
        """Fork children and supervise until a shutdown signal; blocks the caller."""
        recover_orphaned_claims(self.runtime)
        self.process_id = process_registry.register(
            self.runtime.engine,
            ProcessInfo(
                kind="Supervisor",
                name=process_registry.generate_name("supervisor"),
                pid=os.getpid(),
            ),
        )
        self._install_signals()
        HOOKS.fire("supervisor_start")
        for child in self.config.child_configs():
            self._spawn(child)
        # Heartbeat our own row (children heartbeat theirs): without it, a supervisor
        # outliving alive_threshold would prune its *own* registration in _supervise and
        # appear dead in the registry for the rest of its life. Started after forking so
        # no child inherits the heartbeat thread.
        self._heartbeat = HeartbeatPoller(
            self.runtime.engine, self.process_id, self.config.heartbeat_interval
        )
        self._heartbeat.start()
        self._supervise()
        self._shutdown()

    def _spawn(self, child: ChildConfig) -> None:
        assert self.process_id is not None
        pid = os.fork()
        if pid == 0:  # child
            code = 1
            try:
                code = _run_child(self.runtime, self.config, child, self.process_id)
            finally:
                os._exit(code)
        self._children[pid] = child

    def _supervise(self) -> None:
        last_prune = time.monotonic()
        while not self._stop.is_set():
            self._reap_and_restart()
            now = time.monotonic()
            if now - last_prune >= self.config.heartbeat_interval:
                dead = process_registry.prune_dead(self.runtime.engine, self.config.alive_threshold)
                if dead:
                    recover_orphaned_claims(self.runtime, dead)
                last_prune = now
            self._stop.wait(0.2)

    def _reap_and_restart(self) -> None:
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            child = self._children.pop(pid, None)
            if child is not None and not self._stop.is_set():
                self._spawn(child)  # replace an unexpectedly-dead child

    def _install_signals(self) -> None:
        signal.signal(signal.SIGTERM, self._on_terminate)
        signal.signal(signal.SIGINT, self._on_terminate)
        signal.signal(signal.SIGQUIT, self._on_quit)

    def _on_terminate(self, _signum: int, _frame: FrameType | None) -> None:
        self._immediate = False
        self._stop.set()

    def _on_quit(self, _signum: int, _frame: FrameType | None) -> None:
        self._immediate = True
        self._stop.set()

    def _shutdown(self) -> None:
        HOOKS.fire("supervisor_stop")
        if self._heartbeat is not None:
            self._heartbeat.stop()
            self._heartbeat = None
        sig = signal.SIGKILL if self._immediate else signal.SIGTERM
        for pid in list(self._children):
            _signal_pid(pid, sig)

        if not self._immediate:
            deadline = time.monotonic() + self.config.shutdown_timeout
            while self._children and time.monotonic() < deadline:
                self._reap_nohang()
                time.sleep(0.05)
            for pid in list(self._children):
                _signal_pid(pid, signal.SIGKILL)
        self._reap_nohang()

        # Children escalated to SIGKILL (or dead without cleanup) never deregistered; their
        # leftover rows would hide their in-flight claims from the absent-row sweep below
        # for up to alive_threshold (~6 minutes of job limbo after every hard shutdown).
        # The parent knows exactly whom it spawned — clean up and recover explicitly.
        if self.process_id is not None:
            killed = process_registry.deregister_children(self.runtime.engine, self.process_id)
            if killed:
                recover_orphaned_claims(self.runtime, killed)

        recover_orphaned_claims(self.runtime)
        if self.process_id is not None:
            process_registry.deregister(self.runtime.engine, self.process_id)
            self.process_id = None

    def _reap_nohang(self) -> None:
        while self._children:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            self._children.pop(pid, None)


def _signal_pid(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, sig)
