"""Specs for the FastAPI lifespan integration."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import firm.queue as bq
from firm.contrib.fastapi import lifespan
from firm.queue import schema
from firm.queue.config import current_runtime, set_runtime


@bq.job()
def _fjob(x: int) -> None:
    pass


def test_lifespan_configures_runtime_and_enqueues(queue_url) -> None:
    app = FastAPI(lifespan=lifespan(database_url=queue_url))

    @app.post("/go")
    def go() -> dict[str, bool]:
        _fjob.enqueue(1)
        return {"ok": True}

    try:
        with TestClient(app) as client:  # entering runs the lifespan startup
            assert client.post("/go").status_code == 200
            with current_runtime().engine.connect() as conn:
                count = conn.execute(
                    select(func.count()).select_from(schema.ready_executions)
                ).scalar()
            assert count == 1
    finally:
        set_runtime(None)


def test_lifespan_requires_a_url() -> None:
    app = FastAPI(lifespan=lifespan())  # no url, no env var
    with pytest.raises(RuntimeError), TestClient(app):
        pass


def test_lifespan_embed_workers_start_and_stop(queue_url) -> None:
    app = FastAPI(lifespan=lifespan(database_url=queue_url, embed_workers=True))
    try:
        with TestClient(app):  # startup starts the supervisor; exit stops it — neither errors
            pass
    finally:
        set_runtime(None)
