"""Flask integration — a ``Firm`` extension + a ``flask firm worker`` command.

    from flask import Flask
    from firm.contrib.flask import Firm

    app = Flask(__name__)
    app.config["FIRM_DATABASE_URL"] = "postgresql://localhost/app"
    Firm(app)

    @app.post("/welcome/<int:user_id>")
    def welcome(user_id):
        send_welcome.enqueue(user_id)   # a normal @bq.job
        return "", 202

Run workers in a separate process with ``flask firm worker`` (the production shape), or pass
``embed_workers=True`` to run them inside the web process (dev / single-process only — every web
worker would otherwise start its own supervisor).

Needs the ``[flask]`` extra.
"""

from __future__ import annotations

import os
import time
from typing import Any

from firm._core.config import Runtime
from firm.queue import configure
from firm.queue.config import current_runtime


def _require_flask() -> None:
    try:
        import flask  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the 'flask' extra
        raise ImportError(
            'The firm Flask integration requires "flask". Install the flask extra: '
            'pip install "firm[flask]"'
        ) from exc


class Firm:
    """Flask extension: configures firm-queue for the app and registers its CLI."""

    def __init__(
        self,
        app: Any | None = None,
        *,
        database_url: str | None = None,
        embed_workers: bool = False,
        queues: tuple[str, ...] = ("*",),
        threads: int = 3,
    ) -> None:
        self.database_url = database_url
        self.embed_workers = embed_workers
        self.queues = tuple(queues)
        self.threads = threads
        self.runtime: Runtime | None = None
        self._supervisor: Any = None
        self._atexit_registered = False
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Any) -> None:
        _require_flask()
        url = (
            self.database_url
            or app.config.get("FIRM_DATABASE_URL")
            or os.environ.get("FIRM_QUEUE_DATABASE_URL")
        )
        if not url:
            raise RuntimeError(
                "Firm needs database_url=, app.config['FIRM_DATABASE_URL'], "
                "or FIRM_QUEUE_DATABASE_URL."
            )
        self.runtime = configure(database_url=url)
        app.extensions["firm"] = self
        app.cli.add_command(self._cli_group())
        if self.embed_workers:
            self.stop()  # idempotent: drop any supervisor left by an earlier init_app
            self._start_supervisor()
            if not self._atexit_registered:
                import atexit

                atexit.register(self.stop)
                self._atexit_registered = True

    def stop(self) -> None:
        if self._supervisor is not None:
            self._supervisor.stop()
            self._supervisor = None

    def _start_supervisor(self) -> None:
        self._supervisor = _build_supervisor(self.queues, self.threads)
        self._supervisor.start()

    def _cli_group(self) -> Any:
        import click

        @click.group("firm", help="Run firm-queue workers.")
        def group() -> None:
            pass

        @group.command("worker", help="Run a worker + dispatcher until interrupted.")
        @click.option("--queues", default="*", help="Comma-separated queue patterns.")
        @click.option("--threads", default=3, type=int)
        def worker(queues: str, threads: int) -> None:
            supervisor = _build_supervisor(tuple(queues.split(",")), threads)
            supervisor.start()
            click.echo("firm worker running (Ctrl-C to stop)")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                supervisor.stop()

        return group


def _build_supervisor(queues: tuple[str, ...], threads: int) -> Any:
    from firm.queue.supervisor import (
        DispatcherConfig,
        SupervisorConfig,
        ThreadSupervisor,
        WorkerConfig,
    )

    return ThreadSupervisor(
        current_runtime(),
        SupervisorConfig(
            workers=[WorkerConfig(queues=queues, threads=threads)],
            dispatchers=[DispatcherConfig()],
        ),
    )
