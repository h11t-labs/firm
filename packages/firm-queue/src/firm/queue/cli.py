"""Command-line entry point: ``firm-queue start|work|dispatch|maintenance``.

Jobs live in the user's own modules, so commands take ``--import`` to load the modules that
register them, and a database URL via ``--database-url`` or ``FIRM_QUEUE_DATABASE_URL``.
"""

from __future__ import annotations

import importlib
import os
import time

try:
    import click
except ImportError as exc:  # pragma: no cover - exercised only without the 'queue' extra
    raise ImportError(
        'The firm-queue CLI requires "click". Install the queue extra: pip install "firm[queue]"'
    ) from exc

from .._core.config import Runtime
from . import __version__
from .config import configure
from .dispatcher import dispatch_once, run_maintenance
from .supervisor import (
    DispatcherConfig,
    ForkSupervisor,
    SupervisorConfig,
    ThreadSupervisor,
    WorkerConfig,
)
from .worker import Worker, run_ready

_db_option = click.option(
    "--database-url",
    default=None,
    help="SQLAlchemy URL (or set FIRM_QUEUE_DATABASE_URL).",
)
_import_option = click.option(
    "--import",
    "imports",
    multiple=True,
    help="Module(s) to import so their @job definitions register (repeatable).",
)


def _configure(database_url: str | None, imports: tuple[str, ...]) -> Runtime:
    url = database_url or os.environ.get("FIRM_QUEUE_DATABASE_URL")
    if not url:
        raise click.UsageError(
            "No database URL: pass --database-url or set FIRM_QUEUE_DATABASE_URL."
        )
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
    worker = Worker(runtime, queues=tuple(queues.split(",")), threads=threads)
    worker.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        worker.stop()


@main.command(help="Drain ready jobs once and exit (no polling).")
@_db_option
@_import_option
@click.option("--queues", default="*")
@click.option("--limit", default=100, type=int)
def drain(database_url: str | None, imports: tuple[str, ...], queues: str, limit: int) -> None:
    runtime = _configure(database_url, imports)
    processed = run_ready(runtime, queues=tuple(queues.split(",")), limit=limit)
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
