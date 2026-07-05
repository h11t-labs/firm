"""A worker-side pipeline using all three modules: a job reads/writes the cache and announces
completion on a channel.

    uv run python examples/combined_worker.py
"""

from __future__ import annotations

import json
import time

import firm.queue as bq
from firm._core.config import current_runtime
from firm.cache import Cache
from firm.channel import Channel
from firm.queue import schema as queue_schema
from firm.queue.worker import run_ready

DB = "sqlite:///firm-combined.db"

bq.configure(database_url=DB)
cache = Cache(database_url=DB)
channel = Channel(database_url=DB, polling_interval=0.02)


@bq.job()
def build_report(report_id: int) -> None:
    # read inputs through the cache, do the work, store the result, announce it on a channel
    inputs = cache.fetch(f"report:inputs:{report_id}", lambda: {"rows": 100})
    result = {"id": report_id, "rows": inputs["rows"], "status": "done"}
    cache.set(f"report:result:{report_id}", result)
    channel.broadcast("reports", json.dumps({"report_id": report_id, "status": "done"}))


def main() -> None:
    queue_schema.create_all(current_runtime().engine)  # demo only; use Alembic in production

    announcements: list[bytes] = []
    channel.subscribe("reports", announcements.append)

    build_report.enqueue(42)
    run_ready(current_runtime(), limit=10)  # run the job (drains the queue once)
    time.sleep(0.2)  # let the channel listener deliver the announcement

    print("cached result:", cache.get("report:result:42"))
    print("announced    :", announcements)

    cache.close()
    channel.close()


if __name__ == "__main__":
    main()
