"""A minimal FastAPI app backed by firm-queue + firm-cache.

    uv run uvicorn examples.fastapi_app:app --reload

`embed_workers=True` runs a worker inside the dev server; in production run workers separately
(`firm-queue start`) and set `embed_workers=False`.
"""

from __future__ import annotations

from fastapi import FastAPI

import firm.queue as bq
from firm._core.database import create_engine_for
from firm.cache import Cache
from firm.contrib.fastapi import lifespan
from firm.queue import schema as queue_schema

DB = "sqlite:///firm-fastapi.db"

# Demo convenience: create the queue schema up front (use Alembic migrations in production).
_engine = create_engine_for(DB)
queue_schema.create_all(_engine)
_engine.dispose()

cache = Cache(database_url=DB)


@bq.job()
def send_welcome(user_id: int) -> None:
    print(f"  [job] welcome user {user_id}")


app = FastAPI(lifespan=lifespan(database_url=DB, embed_workers=True))


@app.post("/welcome/{user_id}")
def welcome(user_id: int) -> dict[str, bool]:
    send_welcome.enqueue(user_id)  # returns immediately; the worker runs it
    return {"queued": True}


@app.get("/report/{report_id}")
def report(report_id: int) -> dict[str, object]:
    # read-through cache: compute on a miss, serve from cache on a hit
    return cache.fetch(f"report:{report_id}", lambda: {"id": report_id, "rows": 123})
