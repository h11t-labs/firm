"""``manage.py firm_worker`` — run firm's supervisor with Django loaded.

    python manage.py firm_worker --mode fork --threads 5 --queues default,billing

Job bodies touch the Django ORM, so the worker process needs ``django.setup()`` to have run.
A management command gets that — and firm's configuration, from ``AppConfig.ready()`` — for
free, which is why this is less trouble than pointing the stock ``firm-queue start`` CLI at a
Django project.

The flags are ``firm-queue start``'s, so an invocation carries over between the two. The two it
does *not* repeat are the ones Django settings already own: there is no ``--database-url``
(``FIRM_QUEUE["DATABASE_ALIAS"]`` decides), and ``--import`` is rarely needed because
``<app>/jobs.py`` is autodiscovered at startup.

``--mode fork`` is the production default: real multi-core parallelism, and crashed children
are restarted. ``--mode thread`` runs everything in this process — development, and Windows.
"""

from __future__ import annotations

import os
import time
from importlib import import_module
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connections

from firm.queue.supervisor import (
    DispatcherConfig,
    ForkSupervisor,
    SupervisorConfig,
    ThreadSupervisor,
    WorkerConfig,
)

from ...conf import build_runtime, get_settings


class Command(BaseCommand):
    help = "Run firm-queue's workers and dispatcher (the Django twin of `firm-queue start`)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--queues",
            default=None,
            help="Comma-separated queue patterns [default: FIRM_QUEUE['QUEUES'], then '*'].",
        )
        parser.add_argument(
            "--threads",
            type=int,
            default=None,
            help="Threads per worker [default: FIRM_QUEUE['THREADS'], then 3].",
        )
        parser.add_argument(
            "--mode",
            choices=["fork", "thread"],
            default=None,
            help=(
                "Supervisor mode; falls back to FIRM_QUEUE['MODE'], then the FIRM_QUEUE_MODE "
                "env var, then 'fork'."
            ),
        )
        parser.add_argument(
            "--import",
            dest="imports",
            action="append",
            default=None,
            metavar="MODULE",
            help="Extra module to import so its @job definitions register (repeatable).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        conf = get_settings()
        queues = options["queues"] or conf["QUEUES"] or "*"
        threads = options["threads"] or conf["THREADS"] or 3
        mode = options["mode"] or conf["MODE"] or os.environ.get("FIRM_QUEUE_MODE") or "fork"
        for module in options["imports"] or ():
            import_module(module)

        # ready() already configured firm; this hands back that same runtime.
        runtime = build_runtime(conf)
        config = SupervisorConfig(
            workers=[WorkerConfig(queues=tuple(queues.split(",")), threads=threads)],
            dispatchers=[DispatcherConfig()],
        )
        self.stdout.write(f"firm worker: queues={queues} threads={threads} mode={mode}")

        if mode == "fork":
            # firm drops the connections its own children inherit, but knows nothing about
            # Django's. Close them here so parent and children never share an ORM socket.
            connections.close_all()
            ForkSupervisor(runtime, config).start()  # blocks until SIGTERM/SIGINT
            return

        supervisor = ThreadSupervisor(runtime, config)
        supervisor.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            # In the finally, not just the except: a failure in the wait loop must still
            # deregister the process row and stop the threads.
            supervisor.stop()
