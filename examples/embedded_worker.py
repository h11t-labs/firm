"""Embedded worker: run the queue *inside* your app process with ThreadSupervisor.

Every role (worker, dispatcher) runs as a background thread in this one process — no separate
worker deployment. Good for a single service at low-to-moderate job volume. For the scalable
alternative — a separate ``firm-queue start`` worker deployment — see ``examples/deploy/``.

    uv run python examples/embedded_worker.py
"""

from __future__ import annotations

import threading

import firm.queue as bq
from firm.queue import current_runtime
from firm.queue import schema as queue_schema
from firm.queue.supervisor import (
    DispatcherConfig,
    SupervisorConfig,
    ThreadSupervisor,
    WorkerConfig,
)

DB = "sqlite:///firm-embedded.db"

bq.configure(database_url=DB)

_done = threading.Event()


@bq.job()
def greet(name: str) -> None:
    print(f"[worker thread] hi {name}")
    _done.set()


def main() -> None:
    queue_schema.create_all(current_runtime().engine)  # demo only; use Alembic in production

    config = SupervisorConfig(
        workers=[WorkerConfig(queues=("*",), threads=2, poll_interval=0.05)],
        dispatchers=[DispatcherConfig()],
    )

    # The supervisor starts every role as a background thread and stops them on block exit.
    with ThreadSupervisor(current_runtime(), config):
        print("[main thread] supervisor running; enqueuing work")
        greet.enqueue("Ada")
        # ... your web app keeps serving requests here; jobs drain in the background.
        if not _done.wait(timeout=5.0):
            print("[main thread] timed out waiting for the job")

    print("[main thread] supervisor stopped, workers drained")


if __name__ == "__main__":
    main()
