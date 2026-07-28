"""Flask integration — a ``FirmQueue`` extension + a ``flask firm-queue worker`` command.

    from flask import Flask
    from firm.queue.contrib.flask import FirmQueue

    app = Flask(__name__)
    app.config["FIRM_QUEUE_DATABASE_URL"] = "postgresql://localhost/app"
    FirmQueue(app)

    @app.post("/welcome/<int:user_id>")
    def welcome(user_id):
        send_welcome.enqueue(user_id)   # a normal @bq.job
        return "", 202

Run workers in a separate process with ``flask firm-queue worker`` (the production shape), or pass
``embed_workers=True`` to run them inside the web process (dev / single-process only — every web
worker would otherwise start its own supervisor).

The extension configures **firm-queue only**, so everything it claims on the app is named for the
queue rather than for the suite: ``FirmQueue``, ``app.extensions["firm_queue"]``, and the
``flask firm-queue`` command group. The unqualified ``Firm`` / ``"firm"`` / ``flask firm``
spellings shipped in 1.0.0 and still work; they are removed in 2.0.

Needs the ``[flask]`` extra.
"""

from __future__ import annotations

import os
import time
import warnings
from typing import Any

from firm._core.config import Runtime
from firm.queue import configure
from firm.queue.config import current_runtime

# Longest-lived first: an explicit database_url= beats app config, which beats the environment.
# Within each of those, the queue's own key beats the suite-wide one — the same per-module
# override / shared fallback shape `firm-ui` uses for its --queue-url and FIRM_DATABASE_URL.
_CONFIG_KEYS = ("FIRM_QUEUE_DATABASE_URL", "FIRM_DATABASE_URL")


def _require_flask() -> None:
    try:
        import flask  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the 'flask' extra
        raise ImportError(
            'The firm-queue Flask integration requires "flask". Install the flask extra: '
            'pip install "firm-queue[flask]"'
        ) from exc


class FirmQueue:
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
        url = self.database_url or _resolve_url(app)
        if not url:
            raise RuntimeError(
                "FirmQueue needs database_url=, app.config['FIRM_QUEUE_DATABASE_URL'] "
                "(or ['FIRM_DATABASE_URL']), or the same names in the environment."
            )
        self.runtime = configure(database_url=url)
        app.extensions["firm_queue"] = self
        app.extensions["firm"] = self  # deprecated alias, removed in 2.0
        app.cli.add_command(self._cli_group("firm-queue"))
        app.cli.add_command(self._cli_group("firm", renamed_to="firm-queue"))
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

    def _cli_group(self, name: str, *, renamed_to: str | None = None) -> Any:
        import click

        help_text = "Run firm-queue workers."
        if renamed_to is not None:
            help_text = f"Deprecated alias for `flask {renamed_to}` (removed in 2.0)."

        @click.group(name, help=help_text)
        def group() -> None:
            if renamed_to is not None:
                click.echo(
                    f"warning: `flask {name}` is now `flask {renamed_to}`; "
                    "the old name is removed in 2.0.",
                    err=True,
                )

        @group.command("worker", help="Run a worker + dispatcher until interrupted.")
        @click.option("--queues", default="*", help="Comma-separated queue patterns.")
        @click.option("--threads", default=3, type=int)
        def worker(queues: str, threads: int) -> None:
            supervisor = _build_supervisor(tuple(queues.split(",")), threads)
            supervisor.start()
            click.echo("firm-queue worker running (Ctrl-C to stop)")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                supervisor.stop()

        return group


def _resolve_url(app: Any) -> str | None:
    for key in _CONFIG_KEYS:
        if url := app.config.get(key):
            return str(url)
    for key in _CONFIG_KEYS:
        if url := os.environ.get(key):
            return url
    return None


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


def __getattr__(name: str) -> Any:
    # `Firm` is the 1.0.0 name. Hand back the very same class rather than a subclass, so an
    # isinstance() check in a half-migrated codebase keeps holding across both spellings.
    if name == "Firm":
        warnings.warn(
            "firm.queue.contrib.flask.Firm is now FirmQueue; the old name is removed in 2.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return FirmQueue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FirmQueue"]
