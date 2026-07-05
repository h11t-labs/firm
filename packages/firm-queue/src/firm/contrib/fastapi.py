"""FastAPI integration — a lifespan that configures firm-queue with your app.

    from fastapi import FastAPI
    from firm.contrib.fastapi import lifespan

    app = FastAPI(lifespan=lifespan(database_url="postgresql://localhost/app"))

    @app.post("/welcome/{user_id}")
    def welcome(user_id: int):
        send_welcome.enqueue(user_id)   # a normal @bq.job

Pass ``embed_workers=True`` to also run a worker+dispatcher in the app process (handy for dev or a
single-process deploy); in production run workers separately with ``firm-queue start``.

The helper itself only needs the standard library + firm; the ``[fastapi]`` extra just
installs FastAPI for your app.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

from firm.queue import configure
from firm.queue.config import current_runtime


def lifespan(
    *,
    database_url: str | None = None,
    embed_workers: bool = False,
    queues: tuple[str, ...] = ("*",),
    threads: int = 3,
) -> Callable[[Any], contextlib.AbstractAsyncContextManager[None]]:
    """Build a FastAPI ``lifespan`` that configures firm on startup (and optionally runs a
    worker+dispatcher), tearing the workers down on shutdown."""

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        url = database_url or os.environ.get("FIRM_QUEUE_DATABASE_URL")
        if not url:
            raise RuntimeError(
                "firm FastAPI lifespan needs database_url= or FIRM_QUEUE_DATABASE_URL."
            )
        configure(database_url=url)
        # Build (don't start) before the try, so a partial-start failure inside still hits the
        # finally and gets stop()'d — never leaking daemon threads or a stale process row.
        supervisor = _make_supervisor(queues, threads) if embed_workers else None
        try:
            if supervisor is not None:
                supervisor.start()
            yield
        finally:
            if supervisor is not None:
                supervisor.stop()

    return _lifespan


def _make_supervisor(queues: tuple[str, ...], threads: int) -> Any:
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
