"""A minimal Flask app backed by firm-queue + firm-cache.

    uv run flask --app examples.flask_app run

Run workers in a separate process with `flask --app examples.flask_app firm worker`
(or pass `embed_workers=True` to Firm(...) to run one inside the dev server).
"""

from __future__ import annotations

from flask import Flask

import firm.queue as bq
from firm._core.database import create_engine_for
from firm.cache import Cache
from firm.contrib.flask import Firm
from firm.queue import schema as queue_schema

DB = "sqlite:///firm-flask.db"

# Demo convenience: create the queue schema up front (use Alembic migrations in production).
_engine = create_engine_for(DB)
queue_schema.create_all(_engine)
_engine.dispose()

cache = Cache(database_url=DB)


@bq.job()
def send_welcome(user_id: int) -> None:
    print(f"  [job] welcome user {user_id}")


app = Flask(__name__)
app.config["FIRM_DATABASE_URL"] = DB
Firm(app)  # configures the queue + registers `flask firm worker`


@app.post("/welcome/<int:user_id>")
def welcome(user_id: int) -> tuple[dict[str, bool], int]:
    send_welcome.enqueue(user_id)
    return {"queued": True}, 202


@app.get("/report/<int:report_id>")
def report(report_id: int) -> dict[str, object]:
    return cache.fetch(f"report:{report_id}", lambda: {"id": report_id, "rows": 123})
