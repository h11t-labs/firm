"""Command-line entry point: ``firm-queue start|work|drain|dispatch|maintenance``.

Jobs live in the user's own modules, so commands take ``--import`` to load the modules that
register them, and a database URL via ``--database-url`` or ``FIRM_QUEUE_DATABASE_URL``.
"""

from __future__ import annotations

import importlib
import os
import threading
import time

from .._core import process as process_registry
from .._core.cli import db_option, require_click, require_url
from .._core.config import Runtime
from .._core.process import HeartbeatPoller, ProcessInfo
from . import __version__
from .config import configure
from .dispatcher import dispatch_once, run_maintenance
from .hooks import HOOKS
from .recovery import ReaperLoop, reap_dead_processes, recover_orphaned_claims
from .supervisor import (
    DispatcherConfig,
    ForkSupervisor,
    SupervisorConfig,
    ThreadSupervisor,
    WorkerConfig,
)
from .worker import Worker, run_ready

click = require_click("queue")

# Match SupervisorConfig.heartbeat_interval/alive_threshold; the standalone commands have no
# supervisor config to read them from.
_HEARTBEAT_INTERVAL = 60.0
_ALIVE_THRESHOLD = 300.0


def _recover_at_startup(runtime: Runtime) -> None:
    """Prune stale-heartbeat processes and recover orphaned claims before starting work.

    Without a supervisor around, nothing else reaps: a predecessor that was hard-killed left a
    stale process row that shields its claims from the absent-row sweep forever."""
    reap_dead_processes(runtime, _ALIVE_THRESHOLD)
    recover_orphaned_claims(runtime)


def _register_worker_process(runtime: Runtime, kind_name: str) -> int:
    """Register a process row for a standalone command, so its claims carry a ``process_id``
    and a crash leaves a stale row that ``prune_dead`` + recovery can find — a NULL
    ``process_id`` claim would be stranded forever."""
    return process_registry.register(
        runtime.engine,
        ProcessInfo(
            kind="Worker",
            name=process_registry.generate_name(kind_name),
            pid=os.getpid(),
        ),
    )


_db_option = db_option("FIRM_QUEUE_DATABASE_URL")
_import_option = click.option(
    "--import",
    "imports",
    multiple=True,
    help="Module(s) to import so their @job definitions register (repeatable).",
)


def _configure(database_url: str | None, imports: tuple[str, ...]) -> Runtime:
    url = require_url(database_url, "FIRM_QUEUE_DATABASE_URL")
    for module in imports:
        importlib.import_module(module)
    return configure(database_url=url)


@click.group(help="firm-queue — database-backed background jobs.")
@click.version_option(__version__, prog_name="firm-queue")
def main() -> None:
    pass


@main.command(help="Run the full stack (workers + dispatcher).")
@_db_option
@_import_option
@click.option("--queues", default="*", show_default=True, help="Comma-separated queue patterns.")
@click.option("--threads", default=3, type=int, show_default=True)
@click.option(
    "--mode",
    type=click.Choice(["fork", "thread"]),
    default="fork",
    show_default=True,
    envvar="FIRM_QUEUE_MODE",
    help="Supervisor mode; falls back to the FIRM_QUEUE_MODE env var, then 'fork'.",
)
def start(
    database_url: str | None, imports: tuple[str, ...], queues: str, threads: int, mode: str
) -> None:
    runtime = _configure(database_url, imports)
    config = SupervisorConfig(
        workers=[WorkerConfig(queues=tuple(queues.split(",")), threads=threads)],
        dispatchers=[DispatcherConfig()],
    )
    if mode == "fork":
        ForkSupervisor(runtime, config).start()
    else:
        supervisor = ThreadSupervisor(runtime, config)
        supervisor.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            supervisor.stop()


@main.command(help="Run a single worker until interrupted.")
@_db_option
@_import_option
@click.option("--queues", default="*", show_default=True)
@click.option("--threads", default=3, type=int, show_default=True)
def work(database_url: str | None, imports: tuple[str, ...], queues: str, threads: int) -> None:
    runtime = _configure(database_url, imports)
    _recover_at_startup(runtime)
    process_id = _register_worker_process(runtime, "worker")
    worker = Worker(
        runtime, queues=tuple(queues.split(",")), threads=threads, process_id=process_id
    )
    # If our process row is pruned while we're still alive, self-terminate instead of running on.
    evicted = threading.Event()
    heartbeat = HeartbeatPoller(
        runtime.engine,
        process_id,
        _HEARTBEAT_INTERVAL,
        on_error=HOOKS.fire_error,
        on_evicted=evicted.set,
    )
    # No supervisor around to reap for us: recover hard-killed peers' claims ourselves.
    reaper = ReaperLoop(runtime, _HEARTBEAT_INTERVAL, _ALIVE_THRESHOLD, on_error=HOOKS.fire_error)
    worker.start()
    heartbeat.start()
    reaper.start()
    try:
        while not evicted.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        heartbeat.stop()
        reaper.stop()
        process_registry.deregister(runtime.engine, process_id)


@main.command(help="Drain ready jobs once and exit (no polling).")
@_db_option
@_import_option
@click.option("--queues", default="*", show_default=True)
@click.option("--limit", default=100, type=int, show_default=True)
def drain(database_url: str | None, imports: tuple[str, ...], queues: str, limit: int) -> None:
    runtime = _configure(database_url, imports)
    _recover_at_startup(runtime)  # re-readied orphans are drained like any other ready job
    process_id = _register_worker_process(runtime, "drain")
    try:
        processed = run_ready(
            runtime, queues=tuple(queues.split(",")), limit=limit, process_id=process_id
        )
    finally:
        process_registry.deregister(runtime.engine, process_id)
    click.echo(f"processed {processed} job(s)")


@main.command(help="Promote due scheduled jobs once and exit.")
@_db_option
@_import_option
def dispatch(database_url: str | None, imports: tuple[str, ...]) -> None:
    runtime = _configure(database_url, imports)
    click.echo(f"dispatched {dispatch_once(runtime)} job(s)")


@main.command(help="Run concurrency maintenance once and exit.")
@_db_option
@_import_option
def maintenance(database_url: str | None, imports: tuple[str, ...]) -> None:
    runtime = _configure(database_url, imports)
    click.echo(f"promoted {run_maintenance(runtime)} blocked job(s)")


if __name__ == "__main__":  # pragma: no cover
    main()
