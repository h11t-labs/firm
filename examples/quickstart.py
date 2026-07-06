"""All four firm modules in one runnable script — queue + cache + channel + audit on SQLite.

    uv run python examples/quickstart.py

Real apps create the schema with Alembic migrations; here we create the queue schema directly
(the cache, channel, and audit create theirs automatically), and we drain the queue once instead
of running a long-lived worker.
"""

from __future__ import annotations

import time

import firm.queue as bq
from firm.audit import AuditLog
from firm.cache import Cache
from firm.channel import Channel
from firm.queue import current_runtime
from firm.queue import schema as queue_schema
from firm.queue.worker import run_ready

DB = "sqlite:///firm-quickstart.db"


@bq.job()
def greet(name: str) -> None:
    print(f"  [job] hello, {name}")


def main() -> None:
    # --- queue ---
    bq.configure(database_url=DB)
    queue_schema.create_all(current_runtime().engine)  # demo only; use Alembic in production
    greet.enqueue("Ada")
    greet.enqueue("Grace")
    processed = run_ready(current_runtime(), limit=10)  # drain once; no worker process needed
    print(f"queue: processed {processed} job(s)")

    # --- cache ---
    with Cache(database_url=DB) as cache:
        cache.set("user:1", {"name": "Ada", "admin": True})
        print("cache get   :", cache.get("user:1"))
        print("cache fetch :", cache.fetch("pi", lambda: 3.14159))  # computes + stores on miss
        cache.increment("hits")
        cache.increment("hits", 4)
        print("cache count :", cache.get("hits"))

    # --- channel ---
    received: list[bytes] = []
    with Channel(database_url=DB, polling_interval=0.02) as channel:
        channel.subscribe("room", received.append)  # only sees messages broadcast from now on
        channel.broadcast("room", b"ping")
        channel.broadcast("room", "hello")  # str is UTF-8 encoded
        time.sleep(0.2)  # let the background listener poll
    print("channel recv:", received)

    # --- audit ---
    with AuditLog(database_url=DB) as audit:
        audit.record("user.login", actor=("User", 1), context={"ip": "127.0.0.1"})
        audit.record(
            "invoice.paid", subject=("Invoice", 42), actor=("User", 1), data={"amount": 4200}
        )
        print("audit log   :", [e["action"] for e in audit.history(limit=10)])


if __name__ == "__main__":
    main()
