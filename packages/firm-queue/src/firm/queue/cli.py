"""Command-line entry point: ``firm-queue start|work|dispatch|maintenance``.

Jobs live in the user's own modules, so commands take ``--import`` to load the modules that
register them, and a database URL via ``--database-url`` or ``FIRM_QUEUE_DATABASE_URL``.
"""

from __future__ import annotations

import importlib
import os
import time

from .._core import process as process_registry
from .._core.cli import db_option, require_click, require_url
from .._core.config import Runtime
from .._core.process import HeartbeatPoller, ProcessInfo
from . import __version__
from .config import configure
from .dispatcher import dispatch_once, run_maintenance
from .hooks import HOOKS
from .supervisor import (
    DispatcherConfig,
    ForkSupervisor,
    SupervisorConfig,
    ThreadSupervisor,
    WorkerConfig,
)
from .worker import Worker, run_ready

click = require_click("queue")

# Matches SupervisorConfig.heartbeat_interval; the standalone commands have no supervisor
# config to read it from.
_HEARTBEAT_INTERVAL = 60.0


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
@click.option("--queues", default="*", help="Comma-separated queue patterns.")
@click.option("--threads", default=3, type=int)
@click.option("--mode", type=click.Choice(["fork", "thread"]), default="fork")
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
@click.option("--queues", default="*")
@click.option("--threads", default=3, type=int)
def work(database_url: str | None, imports: tuple[str, ...], queues: str, threads: int) -> None:
    runtime = _configure(database_url, imports)
    process_id = _register_worker_process(runtime, "worker")
    worker = Worker(
        runtime, queues=tuple(queues.split(",")), threads=threads, process_id=process_id
    )
    heartbeat = HeartbeatPoller(
        runtime.engine, process_id, _HEARTBEAT_INTERVAL, on_error=HOOKS.fire_error
    )
    worker.start()
    heartbeat.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        heartbeat.stop()
        process_registry.deregister(runtime.engine, process_id)


@main.command(help="Drain ready jobs once and exit (no polling).")
@_db_option
@_import_option
@click.option("--queues", default="*")
@click.option("--limit", default=100, type=int)
def drain(database_url: str | None, imports: tuple[str, ...], queues: str, limit: int) -> None:
    runtime = _configure(database_url, imports)
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
